"""
capture.py - chuong trinh chinh: ket noi camera GigE (Hikrobot MV-CE200-10GM),
ep + xac minh cac thong so linearity-critical, chup mot khung theo lenh, luu
anh giu nguyen bit depth + metadata. Toan bo tham so duoc dieu khien qua CLI
argparse - khong con file config ngoai.

Su dung:
    python capture.py [--ip IP | --serial SERIAL] [--pixel-format Mono8|Mono10|Mono12]
                       [--exposure-us US] [--gain-db DB] [--outdir DIR] ...
    python capture.py --help    # xem toan bo flag

Toan bo ten node GenICam dung o day da xac minh tren camera that qua
camera_info.py (xem node_map_full.json, reference/camera_report.md,
reference/capture_bindings_and_issues.md). Khong doan/hard-code khi chua tra cuu.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

import gev_camera as gc

# Console: muc INFO, dinh dang gon - giu nguyen hanh vi cu (truoc day dung
# logging.basicConfig, cac module khac nhu gui.py/focus.py dang phu thuoc
# vao viec root logger co san handler nay khi import capture.py).
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logging.getLogger().addHandler(_console_handler)
logging.getLogger().setLevel(logging.DEBUG)

log = logging.getLogger("capture")

DEFAULT_LOG_DIR = "./logs"


def add_file_logging(log_dir: str, command: str) -> Path:
    """Them file log chi tiet (muc DEBUG, co timestamp, ten logger) ben canh
    console. Console chi in tu INFO tro len (khong doi), nhung file log ghi
    ca cac dong DEBUG - vi du so lan retry cua fetch_buffer_retrying() khi
    gap loi UnicodeDecodeError cua MvProducerGEV.cti (xem
    reference/capture_bindings_and_issues.md muc 4) - de xem lai lich su chay va debug
    sau nay ma khong can bat lai --verbose."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    file_path = log_path / f"{command}_{ts}.log"

    handler = logging.FileHandler(file_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logging.getLogger().addHandler(handler)

    return file_path


# ---------------------------------------------------------------------------
# Config: gia tri mac dinh an toan, dung lam schema noi bo + fallback khi
# mot flag CLI khong duoc chi dinh.
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "camera": {"serial": None, "ip": None},
    "gentl": {"cti": None},
    "acquisition": {
        "pixel_format": "Mono8",   # an toan nhat, khong can giai nen, luon duoc ho tro
        "exposure_us": 10000.0,
        "gain_db": 0.0,
    },
    "enforce_linear": {
        "gamma_enable": False,
        "auto_exposure": "Off",
        "auto_gain": "Off",
        "lut_enable": False,
        "exposure_mode": "Timed",
    },
    "black_level": {"mode": "keep_and_record", "value": None},
    "roi": {"width": None, "height": None, "x_offset": 0, "y_offset": 0},
    "output": {
        "dir": "./captures",
        "image_format": "tiff16",
        "also_save_npy": False,
        "write_metadata_json": True,
    },
    "network": {"packet_size": None},
    "logging": {"dir": DEFAULT_LOG_DIR},
}


def config_from_args(args: argparse.Namespace) -> dict:
    """Dung dict config (cung hinh dang DEFAULTS) truc tiep tu cac flag CLI
    da parse - thay cho viec doc config.yaml."""
    return {
        "camera": {"serial": args.serial, "ip": args.ip},
        "gentl": {"cti": args.cti},
        "acquisition": {
            "pixel_format": args.pixel_format,
            "exposure_us": args.exposure_us,
            "gain_db": args.gain_db,
        },
        "enforce_linear": {
            "gamma_enable": args.gamma_enable,
            "auto_exposure": args.auto_exposure,
            "auto_gain": args.auto_gain,
            "lut_enable": args.lut_enable,
            "exposure_mode": args.exposure_mode,
        },
        "black_level": {"mode": args.black_level_mode, "value": args.black_level_value},
        "roi": {
            "width": args.roi_width,
            "height": args.roi_height,
            "x_offset": args.roi_x_offset,
            "y_offset": args.roi_y_offset,
        },
        "output": {
            "dir": args.outdir,
            "image_format": args.image_format,
            "also_save_npy": args.save_npy,
            "write_metadata_json": args.metadata,
        },
        "network": {"packet_size": args.packet_size},
        "logging": {"dir": args.log_dir},
    }


def _warn_unwired_fields(config: dict) -> None:
    """3 field nay hien dien trong CLI/schema nhung chua co code nao ap dung
    len camera/file that su. Canh bao ro de nguoi dung khong tuong nham la
    da co hieu luc."""
    if config["output"]["image_format"] != DEFAULTS["output"]["image_format"]:
        log.warning(
            "--image-format=%s: CHUA duoc wired-up, anh van luon ghi .tiff (xem save_image()).",
            config["output"]["image_format"])
    if config["network"]["packet_size"] is not None:
        log.warning(
            "--packet-size=%s: CHUA duoc wired-up, khong co code nao set GevSCPSPacketSize len camera.",
            config["network"]["packet_size"])
    if config["black_level"]["value"] is not None and config["black_level"]["mode"] != "set_zero":
        log.warning(
            "--black-level-value=%s: CHUA duoc wired-up khi black-level-mode=%s "
            "(chi mode=set_zero moi ghi gia tri, va luon ghi 0 chu khong doc field nay).",
            config["black_level"]["value"], config["black_level"]["mode"])


def cti_path_for_platform(config: dict) -> str:
    explicit = config["gentl"].get("cti")
    path, source = gc.find_cti(explicit)
    if explicit and path == explicit:
        source = "--cti"
    return path


# ---------------------------------------------------------------------------
# Section 5.3: ep + xac minh cac thong so linearity-critical
# ---------------------------------------------------------------------------
NOISE_REDUCTION_CANDIDATES = [
    "NoiseReductionEnable", "DigitalNoiseReductionMode", "NoiseReduction", "TZDenoiseOpen",
]


def _enforce_noise_reduction(node_map) -> dict:
    """Cac node noise reduction tren camera nay (firmware V3.1.1) deu co
    access NI/NA - khong the set va khong the doc duoc trang thai. Day
    KHONG duoc coi la loi (khong the "khong tat duoc" mot thu khong the
    bat len duoc): ISP khong co pipeline noise reduction hoat dong. Neu
    mot camera/firmware khac co node nay o RW, code se tu dong tat va
    xac minh nhu cac thong so linearity-critical khac."""
    name, node = gc.find_first_node(node_map, NOISE_REDUCTION_CANDIDATES)
    if node is None:
        log.warning("Noise reduction: khong tim thay node nao trong %s", NOISE_REDUCTION_CANDIDATES)
        return {"node": None, "access": None, "note": "khong tim thay node nao"}

    access = gc.access_mode_of(node)
    if access in ("NI", "NA"):
        log.info(
            "Noise reduction (%s): access=%s - khong duoc firmware nay ho tro/kich hoat, "
            "khong phai loi (khong co gi de tat).", name, access)
        return {"node": name, "access": access, "note": "khong kha dung tren firmware nay (NI/NA)"}

    iface = int(node.node.principal_interface_type)
    try:
        if iface == 3:  # Boolean
            result = gc.set_bool_and_verify(node_map, [name], False, "Noise reduction")
        elif iface == 9:  # Enumeration
            result = gc.set_enum_and_verify(node_map, [name], "Off", "Noise reduction")
        else:  # Integer/Float threshold-style: dat ve 0
            result = gc.set_int_and_verify(node_map, [name], 0, "Noise reduction")
        return result
    except gc.ParameterError as e:
        raise gc.ParameterError(f"Noise reduction ({name}): {e}") from e


def enforce_linear(node_map, config: dict) -> dict:
    cfg = config["enforce_linear"]
    results: dict[str, Any] = {}

    results["gamma_enable"] = gc.set_bool_and_verify(
        node_map, ["GammaEnable"], bool(cfg["gamma_enable"]), "Gamma")
    results["auto_exposure"] = gc.set_enum_and_verify(
        node_map, ["ExposureAuto"], str(cfg["auto_exposure"]), "Auto exposure")
    results["auto_gain"] = gc.set_enum_and_verify(
        node_map, ["GainAuto"], str(cfg["auto_gain"]), "Auto gain")
    results["lut_enable"] = gc.set_bool_and_verify(
        node_map, ["LUTEnable"], bool(cfg["lut_enable"]), "LUT")
    results["exposure_mode"] = gc.set_enum_and_verify(
        node_map, ["ExposureMode"], str(cfg["exposure_mode"]), "Exposure mode")

    results["noise_reduction"] = _enforce_noise_reduction(node_map)

    log.info("AWB: bo qua - cam bien mono, khong ap dung.")
    results["awb"] = {"note": "bo qua - cam bien mono, AWB khong ap dung"}

    return results


# ---------------------------------------------------------------------------
# Section 5.4: thong so chinh duoc
# ---------------------------------------------------------------------------
def apply_adjustable(node_map, config: dict) -> dict:
    acq = config["acquisition"]
    results: dict[str, Any] = {}

    results["pixel_format"] = gc.set_enum_and_verify(
        node_map, ["PixelFormat"], str(acq["pixel_format"]), "Pixel format")

    # ExposureAuto phai da Off (enforce_linear chay truoc ham nay).
    results["exposure_us"] = gc.set_float_and_verify(
        node_map, ["ExposureTime", "ExposureTimeAbs"], float(acq["exposure_us"]), "Exposure time")

    # GainAuto phai da Off.
    results["gain_db"] = gc.set_float_and_verify(
        node_map, ["Gain"], float(acq["gain_db"]), "Gain")

    # Binning khong dung: luon ep ve 1x1 (pixel goc, khong gop pixel) va xac
    # minh, khong cho chinh qua CLI - xem reference/capture_bindings_and_issues.md muc 3.
    gc.set_enum_and_verify(node_map, ["BinningHorizontal"], "BinningHorizontal1", "Binning ngang")
    gc.set_enum_and_verify(node_map, ["BinningVertical"], "BinningVertical1", "Binning doc")

    results["black_level"] = _apply_black_level(node_map, config["black_level"])

    results["roi"] = _apply_roi(node_map, config["roi"])

    return results


def _apply_black_level(node_map, cfg: dict) -> dict:
    mode = cfg.get("mode", "keep_and_record")
    if mode == "set_zero":
        value_result = gc.set_int_and_verify(node_map, ["BlackLevel"], 0, "Black level")
    elif mode == "keep_and_record":
        name, node = gc.find_first_node(node_map, ["BlackLevel"])
        if node is None:
            raise gc.ParameterError("Black level: khong tim thay node BlackLevel")
        value_result = {"node": name, "value": node.value, "access": gc.access_mode_of(node)}
        log.info("Black level (%s) = %s [giu nguyen, mode=keep_and_record]", name, node.value)
    else:
        raise gc.ParameterError(f"black_level.mode khong hop le: {mode!r} (chi keep_and_record | set_zero)")

    enable_name, enable_val = gc.read_value(node_map, ["BlackLevelEnable"], "Black level enable")
    log.warning(
        "Black level = %s (BlackLevelEnable=%s): day la pedestal CONG THEM vao moi pixel, KHONG "
        "triet tieu trong tuong phan Weber. Da ghi vao metadata; buoc calibration sau phai tru "
        "dark frame. Xem reference/capture_bindings_and_issues.md muc 2.",
        value_result["value"], enable_val)

    return {
        "mode": mode,
        "value": value_result["value"],
        "enable_node": enable_name,
        "enable": enable_val,
    }


def _apply_roi(node_map, cfg: dict) -> dict:
    width_max = node_map.WidthMax.value
    height_max = node_map.HeightMax.value
    width = cfg.get("width") or width_max
    height = cfg.get("height") or height_max
    x_offset = cfg.get("x_offset", 0) or 0
    y_offset = cfg.get("y_offset", 0) or 0

    w_result = gc.set_int_and_verify(node_map, ["Width"], width, "ROI width")
    h_result = gc.set_int_and_verify(node_map, ["Height"], height, "ROI height")
    if node_map.has_node("OffsetX"):
        gc.set_int_and_verify(node_map, ["OffsetX"], x_offset, "ROI offset X")
    if node_map.has_node("OffsetY"):
        gc.set_int_and_verify(node_map, ["OffsetY"], y_offset, "ROI offset Y")

    return {"width": w_result["value"], "height": h_result["value"],
            "x_offset": x_offset, "y_offset": y_offset}


# ---------------------------------------------------------------------------
# Section 5.5: chup don
# ---------------------------------------------------------------------------
def single_capture(ia, node_map, config: dict) -> tuple[np.ndarray, dict]:
    gc.set_enum_and_verify(node_map, ["AcquisitionMode"], "SingleFrame", "Acquisition mode")

    ia.start()
    try:
        buffer = gc.fetch_buffer_retrying(ia, timeout_ms=15000)
        try:
            component = buffer.payload.components[0]
            width, height = component.width, component.height
            data_format = component.data_format
            arr = component.data.copy()
            is_complete = buffer.is_complete()
            timestamp_ns = buffer.timestamp_ns
            timestamp_ticks = buffer.timestamp
            timestamp_freq = buffer.timestamp_frequency
            frame_id = buffer.frame_id
        finally:
            buffer.queue()
    finally:
        ia.stop()

    num_underrun = None
    try:
        num_underrun = ia._data_streams[0].module.num_underrun
    except Exception as e:
        log.debug("khong doc duoc num_underrun: %s", e)

    info = {
        "width": width,
        "height": height,
        "data_format": data_format,
        "is_complete": is_complete,
        "frame_id": frame_id,
        "timestamp_camera_ticks": timestamp_ticks,
        "timestamp_camera_ns": timestamp_ns,
        "timestamp_camera_tick_frequency_hz": timestamp_freq,
        "packet_loss": {
            "buffer_complete": is_complete,
            "num_underrun": num_underrun,
            "note": ("harvesters/GenTL khong cho ty le mat goi chinh xac qua API nay; "
                     "buffer_complete=False hoac num_underrun>0 la dau hieu co mat du lieu."),
        },
    }
    return arr, info


# ---------------------------------------------------------------------------
# Section 5.7: luu anh + metadata
# ---------------------------------------------------------------------------
UNPACKED_DTYPE = {"Mono8": np.uint8, "Mono10": np.uint16, "Mono12": np.uint16}
PACKED_FORMATS = {"Mono10Packed", "Mono12Packed", "Mono10p", "Mono12p"}


def save_image(arr: np.ndarray, info: dict, out_dir: Path, base_name: str, config: dict) -> dict:
    import cv2

    data_format = info["data_format"]
    width, height = info["width"], info["height"]

    if data_format in PACKED_FORMATS:
        raise NotImplementedError(
            f"pixel_format={data_format} la dinh dang packed. Giai nen packed 10/12-bit CHUA duoc "
            "cai dat trong PoC nay (rui ro giai sai bit-layout im lang). Dung ban unpacked "
            "(Mono10/Mono12) - da xac nhan camera nay ho tro (xem reference/camera_report.md)."
        )
    if data_format not in UNPACKED_DTYPE:
        raise NotImplementedError(f"pixel_format={data_format} chua duoc ho tro de luu trong PoC nay.")

    dtype = UNPACKED_DTYPE[data_format]
    image = arr.view(dtype).reshape(height, width)

    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / f"{base_name}.tiff"
    ok = cv2.imwrite(str(image_path), image)
    if not ok:
        raise IOError(f"cv2.imwrite that bai: {image_path}")

    npy_path = None
    if config["output"].get("also_save_npy"):
        npy_path = out_dir / f"{base_name}.npy"
        np.save(npy_path, image)

    return {
        "image_path": str(image_path),
        "npy_path": str(npy_path) if npy_path else None,
        "dtype": str(image.dtype),
        "shape": list(image.shape),
        "min": int(image.min()),
        "max": int(image.max()),
    }


def build_metadata(device_info: dict, linear_results: dict, adjustable_results: dict) -> dict:
    black_level = adjustable_results["black_level"]
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "device_serial": device_info["serial"],
        "device_firmware": device_info["firmware"],
        "pixel_format": adjustable_results["pixel_format"]["value"],
        "exposure_us": adjustable_results["exposure_us"]["value"],
        "gain_db": adjustable_results["gain_db"]["value"],
        "black_level_mode": black_level["mode"],
        "black_level": black_level["value"],
        "black_level_enable": black_level["enable"],
        "gamma_enable": linear_results["gamma_enable"]["value"],
        "auto_exposure": linear_results["auto_exposure"]["value"],
        "auto_gain": linear_results["auto_gain"]["value"],
        "lut_enable": linear_results["lut_enable"]["value"],
        "exposure_mode": linear_results["exposure_mode"]["value"],
    }


def read_device_info(node_map) -> dict:
    out = {}
    for key, cand in [
        ("model", ["DeviceModelName"]),
        ("serial", ["DeviceSerialNumber"]),
        ("firmware", ["DeviceFirmwareVersion"]),
        ("vendor", ["DeviceVendorName"]),
    ]:
        _, val = gc.read_value(node_map, cand, key)
        out[key] = val
    return out


# ---------------------------------------------------------------------------
# Chup mot khung
# ---------------------------------------------------------------------------
def cmd_capture(args: argparse.Namespace, config: dict) -> int:
    cti_path = cti_path_for_platform(config)
    log.info("Dung .cti: %s", cti_path)

    h = ia = None
    try:
        h, ia = gc.connect_control(cti_path, config["camera"].get("ip"), config["camera"].get("serial"))
        node_map = ia.remote_device.node_map

        device_info = read_device_info(node_map)
        log.info("Da ket noi: model=%s serial=%s firmware=%s",
                 device_info["model"], device_info["serial"], device_info["firmware"])

        log.info("--- Ep va xac minh thong so linearity-critical ---")
        try:
            linear_results = enforce_linear(node_map, config)
        except gc.ParameterError as e:
            log.error("KHONG the ep thong so linearity-critical ve trang thai an toan: %s", e)
            log.error("DUNG. Khong chup anh (anh voi ISP con bat la anh vo dung cho du an nay).")
            return 2

        log.info("--- Ap dung thong so chinh duoc ---")
        try:
            adjustable_results = apply_adjustable(node_map, config)
        except gc.ParameterError as e:
            log.error("Khong ap dung duoc thong so: %s", e)
            return 2

        log.info("--- Chup mot khung ---")
        arr, capture_info = single_capture(ia, node_map, config)
        if not capture_info["is_complete"]:
            log.warning("Buffer khong day du (is_complete=False) - anh co the loi/thieu du lieu.")

        out_dir = Path(config["output"]["dir"])
        base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        save_info = save_image(arr, capture_info, out_dir, base_name, config)
        log.info("Da luu anh: %s (%s, %s, min=%d max=%d)",
                 save_info["image_path"], save_info["dtype"], save_info["shape"],
                 save_info["min"], save_info["max"])

        if config["output"]["write_metadata_json"]:
            meta = build_metadata(device_info, linear_results, adjustable_results)
            meta_path = out_dir / f"{base_name}.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
            log.info("Da luu metadata: %s", meta_path)

        return 0
    finally:
        if h is not None:
            gc.disconnect_control(h, ia)
            log.info("Da dong ket noi camera va giai phong Harvester.")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    cam = parser.add_argument_group("Camera")
    cam.add_argument("--ip", default=DEFAULTS["camera"]["ip"], help="IP camera. Bo trong de dung serial hoac thiet bi dau tien do duoc.")
    cam.add_argument("--serial", default=DEFAULTS["camera"]["serial"], help="Serial camera.")
    cam.add_argument("--cti", default=DEFAULTS["gentl"]["cti"], help="Duong dan .cti tuong minh. Bo trong de tu do tim.")

    acq = parser.add_argument_group("Acquisition")
    acq.add_argument("--pixel-format", choices=["Mono8", "Mono10", "Mono12"],
                      default=DEFAULTS["acquisition"]["pixel_format"])
    acq.add_argument("--exposure-us", type=float, default=DEFAULTS["acquisition"]["exposure_us"])
    acq.add_argument("--gain-db", type=float, default=DEFAULTS["acquisition"]["gain_db"])

    lin = parser.add_argument_group("Linearity-critical (enforce_linear)")
    lin.add_argument("--gamma-enable", dest="gamma_enable", action="store_true",
                      default=DEFAULTS["enforce_linear"]["gamma_enable"])
    lin.add_argument("--no-gamma-enable", dest="gamma_enable", action="store_false")
    lin.add_argument("--auto-exposure", choices=["Off", "Once", "Continuous"],
                      default=DEFAULTS["enforce_linear"]["auto_exposure"])
    lin.add_argument("--auto-gain", choices=["Off", "Once", "Continuous"],
                      default=DEFAULTS["enforce_linear"]["auto_gain"])
    lin.add_argument("--lut-enable", dest="lut_enable", action="store_true",
                      default=DEFAULTS["enforce_linear"]["lut_enable"])
    lin.add_argument("--no-lut-enable", dest="lut_enable", action="store_false")
    lin.add_argument("--exposure-mode", choices=["Timed"], default=DEFAULTS["enforce_linear"]["exposure_mode"])

    bl = parser.add_argument_group("Black level")
    bl.add_argument("--black-level-mode", choices=["keep_and_record", "set_zero"],
                     default=DEFAULTS["black_level"]["mode"])
    bl.add_argument("--black-level-value", type=int, default=DEFAULTS["black_level"]["value"],
                     help="CHUA wired-up (xem canh bao khi chay).")

    roi = parser.add_argument_group("ROI")
    roi.add_argument("--roi-width", type=int, default=DEFAULTS["roi"]["width"], help="Bo trong = full sensor.")
    roi.add_argument("--roi-height", type=int, default=DEFAULTS["roi"]["height"], help="Bo trong = full sensor.")
    roi.add_argument("--roi-x-offset", type=int, default=DEFAULTS["roi"]["x_offset"])
    roi.add_argument("--roi-y-offset", type=int, default=DEFAULTS["roi"]["y_offset"])

    out = parser.add_argument_group("Output")
    out.add_argument("--outdir", default=DEFAULTS["output"]["dir"])
    out.add_argument("--image-format", choices=["tiff16", "npy"], default=DEFAULTS["output"]["image_format"],
                      help="CHUA wired-up, anh luon ghi .tiff (xem canh bao khi chay).")
    out.add_argument("--save-npy", dest="save_npy", action="store_true",
                      default=DEFAULTS["output"]["also_save_npy"])
    out.add_argument("--no-save-npy", dest="save_npy", action="store_false")
    out.add_argument("--metadata", dest="metadata", action="store_true",
                      default=DEFAULTS["output"]["write_metadata_json"])
    out.add_argument("--no-metadata", dest="metadata", action="store_false")

    net = parser.add_argument_group("Network")
    net.add_argument("--packet-size", type=int, default=DEFAULTS["network"]["packet_size"],
                      help="CHUA wired-up (xem canh bao khi chay).")

    log_grp = parser.add_argument_group("Logging")
    log_grp.add_argument("--log-dir", default=DEFAULTS["logging"]["dir"],
                          help="Thu muc ghi file log chi tiet. Truyen '' de tat file log.")

    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    config = config_from_args(args)
    _warn_unwired_fields(config)

    if args.log_dir:
        try:
            log_file = add_file_logging(args.log_dir, "capture")
            log.info("Ghi log chi tiet (muc DEBUG) vao: %s", log_file)
        except OSError as e:
            log.warning("Khong tao duoc file log (%s), tiep tuc chi voi console.", e)

    def _on_sigint(signum, frame):
        log.warning("Nhan Ctrl-C, dang don dep...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        return cmd_capture(args, config)
    except KeyboardInterrupt:
        log.warning("Da dung boi nguoi dung (Ctrl-C).")
        return 130
    except (gc.CameraConnectionError, gc.ParameterError) as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
