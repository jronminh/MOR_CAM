"""
capture.py - chuong trinh chinh: ket noi camera GigE (Hikrobot MV-CE200-10GM),
ep + xac minh cac thong so linearity-critical, chup mot khung theo lenh, luu
anh giu nguyen bit depth + metadata. Co subcommand "focus" cho canh net truc
tiep. Chay duoc tren Windows va Linux, chi khac duong dan .cti trong config.

Su dung:
    python capture.py capture [--config config.yaml] [--outdir DIR]
    python capture.py focus   [--config config.yaml] [--mode auto|gui|headless_score]

Toan bo ten node GenICam dung o day da xac minh tren camera that qua
camera_info.py (xem node_map_full.json, reference/camera_report.md,
reference/capture_bindings_and_issues.md). Khong doan/hard-code khi chua tra cuu.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

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
# Config: gia tri mac dinh an toan. Truong nao trong file YAML khong co se
# dung mac dinh o day, va duoc log ro (yeu cau 5.2).
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "camera": {"serial": None, "ip": None},
    "gentl": {"cti_windows": None, "cti_linux": None},
    "acquisition": {
        "pixel_format": "Mono8",   # an toan nhat, khong can giai nen, luon duoc ho tro
        "exposure_us": 10000.0,
        "gain_db": 0.0,
        "binning_h": 1,
        "binning_v": 1,
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
    "preview": {
        "enable": True,
        "mode": "auto",
        "fps_limit": 5,
        "downscale": 4,
        "preview_image_path": "./preview_latest.png",
    },
    "network": {"packet_size": None},
    "logging": {"enable": True, "dir": DEFAULT_LOG_DIR},
}


def _deep_merge_with_defaults(defaults: dict, user: dict, path: str = "") -> dict:
    out = {}
    for key, default_val in defaults.items():
        full_key = f"{path}.{key}" if path else key
        if key not in user or user[key] is None:
            if isinstance(default_val, dict):
                out[key] = _deep_merge_with_defaults(default_val, {}, full_key)
            else:
                log.info("config: '%s' khong duoc chi dinh, dung mac dinh: %r", full_key, default_val)
                out[key] = default_val
        elif isinstance(default_val, dict) and isinstance(user[key], dict):
            out[key] = _deep_merge_with_defaults(default_val, user[key], full_key)
        else:
            out[key] = user[key]
    # giu lai cac key nguoi dung co ma default khong dinh nghia (vd cac ghi chu rieng)
    for key, val in user.items():
        if key not in out:
            out[key] = val
    return out


class _ConfigYamlLoader(yaml.SafeLoader):
    """YAML 1.1 (PyYAML SafeLoader mac dinh) doc 'Off'/'On'/'Yes'/'No' nhu
    boolean, nhung config.yaml dung 'Off'/'On' lam GIA TRI ENUM GenICam
    (vd ExposureAuto: Off). Bo cac tu khoa on/off/yes/no khoi bo nhan dien
    boolean ngam dinh, chi giu true/false that su la boolean - de "Off"/
    "On" doc dung la chuoi, khong bi ep thanh False/True."""


_ConfigYamlLoader.yaml_implicit_resolvers = {
    first_char: [r for r in resolvers if r[0] != "tag:yaml.org,2002:bool"]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_ConfigYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.load(f, Loader=_ConfigYamlLoader) or {}
    return _deep_merge_with_defaults(DEFAULTS, user_cfg)


def cti_path_for_platform(config: dict) -> str:
    import platform
    key = "cti_windows" if platform.system() == "Windows" else "cti_linux"
    explicit = config["gentl"].get(key)
    path, source = gc.find_cti(explicit)
    if explicit and path == explicit:
        source = f"config.gentl.{key}"
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

    results["binning_h"] = _apply_binning(node_map, "BinningHorizontal", int(acq["binning_h"]), "ngang")
    results["binning_v"] = _apply_binning(node_map, "BinningVertical", int(acq["binning_v"]), "doc")

    results["black_level"] = _apply_black_level(node_map, config["black_level"])

    results["roi"] = _apply_roi(node_map, config["roi"])

    return results


def _apply_binning(node_map, node_name: str, multiplier: int, label_vn: str) -> dict:
    if multiplier not in (1, 2, 4):
        raise gc.ParameterError(f"Binning {label_vn}: gia tri {multiplier} khong hop le (chi 1/2/4)")
    enum_value = f"{node_name}{multiplier}"
    result = gc.set_enum_and_verify(node_map, [node_name], enum_value, f"Binning {label_vn}")
    if multiplier != 1:
        log.warning(
            "Binning %s = %s: day la binning DIGITAL (Region0), KHONG phai on-sensor - "
            "BinningSelector.Sensor co access NI tren camera nay. Khong cai thien SNR nhu "
            "on-sensor binning; chi giam dung luong luu tru. Xem reference/capture_bindings_and_issues.md muc 3.",
            label_vn, enum_value)
    return result


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


def build_metadata(device_info: dict, linear_results: dict, adjustable_results: dict,
                    capture_info: dict, save_info: dict) -> dict:
    return {
        "timestamp_host_utc": datetime.now(timezone.utc).isoformat(),
        "timestamp_camera": {
            "ticks": capture_info["timestamp_camera_ticks"],
            "ns": capture_info["timestamp_camera_ns"],
            "tick_frequency_hz": capture_info["timestamp_camera_tick_frequency_hz"],
        },
        "device": device_info,
        "pixel_format": adjustable_results["pixel_format"]["value"],
        "exposure_us": adjustable_results["exposure_us"]["value"],
        "gain_db": adjustable_results["gain_db"]["value"],
        "binning_h": adjustable_results["binning_h"]["value"],
        "binning_v": adjustable_results["binning_v"]["value"],
        "binning_is_on_sensor": False,
        "roi": adjustable_results["roi"],
        "black_level": adjustable_results["black_level"],
        "linearity_readback": {
            k: v for k, v in linear_results.items()
        },
        "frame": {
            "width": capture_info["width"],
            "height": capture_info["height"],
            "frame_id": capture_info["frame_id"],
            "is_complete": capture_info["is_complete"],
        },
        "packet_loss": capture_info["packet_loss"],
        "image": save_info,
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
# Subcommand: capture
# ---------------------------------------------------------------------------
def cmd_capture(args: argparse.Namespace, config: dict) -> int:
    if args.outdir:
        config["output"]["dir"] = args.outdir

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
            meta = build_metadata(device_info, linear_results, adjustable_results, capture_info, save_info)
            meta_path = out_dir / f"{base_name}.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
            log.info("Da luu metadata: %s", meta_path)

        return 0
    finally:
        if h is not None:
            gc.disconnect_control(h, ia)
            log.info("Da dong ket noi camera va giai phong Harvester.")


# ---------------------------------------------------------------------------
# Subcommand: focus (uy quyen cho focus.py)
# ---------------------------------------------------------------------------
def cmd_focus(args: argparse.Namespace, config: dict) -> int:
    import focus as focus_mod

    if args.mode:
        config["preview"]["mode"] = args.mode

    cti_path = cti_path_for_platform(config)
    log.info("Dung .cti: %s", cti_path)

    h = ia = None
    try:
        h, ia = gc.connect_control(cti_path, config["camera"].get("ip"), config["camera"].get("serial"))
        node_map = ia.remote_device.node_map

        log.info("--- Ep va xac minh thong so linearity-critical (che do focus cung ap dung) ---")
        try:
            enforce_linear(node_map, config)
        except gc.ParameterError as e:
            log.error("KHONG the ep thong so linearity-critical: %s. DUNG.", e)
            return 2

        gc.set_enum_and_verify(node_map, ["PixelFormat"], str(config["acquisition"]["pixel_format"]), "Pixel format")
        _apply_roi(node_map, config["roi"])

        focus_mod.run_focus(ia, node_map, config)
        return 0
    finally:
        if h is not None:
            gc.disconnect_control(h, ia)
            log.info("Da dong ket noi camera va giai phong Harvester.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_capture = sub.add_parser("capture", help="Chup mot khung va luu anh + metadata")
    p_capture.add_argument("--config", default="config.yaml")
    p_capture.add_argument("--outdir", default=None, help="Ghi de output.dir trong config")
    p_capture.add_argument("--log-dir", default=None,
                            help="Ghi de logging.dir trong config. Truyen '' de tat file log.")
    p_capture.set_defaults(func=cmd_capture)

    p_focus = sub.add_parser("focus", help="Canh net truc tiep (streaming lien tuc, tai gioi han)")
    p_focus.add_argument("--config", default="config.yaml")
    p_focus.add_argument("--mode", choices=["auto", "gui", "headless_score"], default=None)
    p_focus.add_argument("--log-dir", default=None,
                          help="Ghi de logging.dir trong config. Truyen '' de tat file log.")
    p_focus.set_defaults(func=cmd_focus)

    args = parser.parse_args()

    config = load_config(args.config)

    log_dir = args.log_dir if args.log_dir is not None else config["logging"]["dir"]
    if config["logging"]["enable"] and log_dir:
        try:
            log_file = add_file_logging(log_dir, args.command)
            log.info("Ghi log chi tiet (muc DEBUG) vao: %s", log_file)
        except OSError as e:
            log.warning("Khong tao duoc file log (%s), tiep tuc chi voi console.", e)

    def _on_sigint(signum, frame):
        log.warning("Nhan Ctrl-C, dang don dep...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        log.warning("Da dung boi nguoi dung (Ctrl-C).")
        return 130
    except (gc.CameraConnectionError, gc.ParameterError) as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
