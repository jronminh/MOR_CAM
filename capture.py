"""
capture.py - ket noi camera GigE (Hikrobot MV-CE200-10GM), khoa cung 14 node
anh huong tuyen tinh, ap config theo site (exposure/gain/mang), chup mot
khung theo lenh, luu anh giu nguyen bit depth + metadata. Toan bo tham so
qua CLI argparse - khong con file config ngoai.

Su dung:
    python capture.py [--ip IP | --serial SERIAL] [--exposure-us US]
                       [--gain-db DB] [--outdir DIR] ...
    python capture.py --help    # xem toan bo flag

Toan bo ten node GenICam dung o day da xac minh tren camera that qua
camera_info.py (xem node_map_full.json, reference/camera_report.md,
reference/hikrobot_phanloai_tuyentinh.md, reference/capture_bindings_and_issues.md).
Khong doan/hard-code khi chua tra cuu.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import struct
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
# SECTION 1 - CONFIG BAT BIEN (LINEARITY LOCKED)
#
# 14 node duoi day khong bao gio doi, khong co CLI flag. Sai mot gia tri la
# hong vat ly am tham: R^2 van cao, MOR do duoc lech he thong ma khong co
# dau hieu loi ro rang. Nhom theo dung muc Appendix A trong
# reference/hikrobot_phanloai_tuyentinh.md de doi chieu nguoc lai tai lieu
# de dang khi tai lieu cap nhat.
# ---------------------------------------------------------------------------
LINEARITY_LOCKED: dict[str, Any] = {
    "image_format": {  # A.2 Image Format Control
        "pixel_format": "Mono12",
        "binning_h": "BinningHorizontal1",
        "binning_v": "BinningVertical1",
        "test_pattern": "Off",
    },
    "acquisition": {  # A.3 Acquisition Control
        "exposure_auto": "Off",
        "exposure_mode": "Timed",
        "hdr_enable": False,
    },
    "analog": {  # A.4 Analog Control (Gain khong o day - la config theo site, xem SECTION 2)
        "gain_auto": "Off",
        "digital_shift_enable": False,
        "black_level_enable": True,
        "black_level": 200,  # khoa dung gia tri de dark frame hien co con hop le; doi so phai chup lai dark frame
        "gamma_enable": False,
        "sharpness_enable": False,
    },
    "lut": {  # A.7 LUT Control
        "lut_enable": False,
    },
}

NOISE_REDUCTION_CANDIDATES = [
    "NoiseReductionEnable", "DigitalNoiseReductionMode", "NoiseReduction", "TZDenoiseOpen",
]


def _enforce_noise_reduction(node_map) -> dict:
    """Cac node noise reduction tren camera nay (firmware V3.1.1) deu co
    access NI/NA - khong the set va khong the doc duoc trang thai. Day
    KHONG duoc coi la loi (khong the "khong tat duoc" mot thu khong the
    bat len duoc): ISP khong co pipeline noise reduction hoat dong. Neu
    mot camera/firmware khac co node nay o RW, code se tu dong tat va
    xac minh nhu cac node linearity khac. Khong thuoc mot muc A.x nao
    trong tai lieu phan loai - day la phong ngua rieng cua capture.py."""
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


def _enforce_image_format(node_map, cfg: dict) -> dict:  # A.2
    return {
        "pixel_format": gc.set_enum_and_verify(
            node_map, ["PixelFormat"], cfg["pixel_format"], "Pixel format"),
        "binning_h": gc.set_enum_and_verify(
            node_map, ["BinningHorizontal"], cfg["binning_h"], "Binning ngang"),
        "binning_v": gc.set_enum_and_verify(
            node_map, ["BinningVertical"], cfg["binning_v"], "Binning doc"),
        "test_pattern": gc.set_enum_and_verify(
            node_map, ["TestPattern"], cfg["test_pattern"], "Test pattern"),
    }


def _enforce_acquisition(node_map, cfg: dict) -> dict:  # A.3
    return {
        "exposure_auto": gc.set_enum_and_verify(
            node_map, ["ExposureAuto"], cfg["exposure_auto"], "Auto exposure"),
        "exposure_mode": gc.set_enum_and_verify(
            node_map, ["ExposureMode"], cfg["exposure_mode"], "Exposure mode"),
        "hdr_enable": gc.set_bool_and_verify(
            node_map, ["HDREnable"], cfg["hdr_enable"], "HDR"),
    }


def _enforce_analog(node_map, cfg: dict) -> dict:  # A.4
    results = {
        "gain_auto": gc.set_enum_and_verify(node_map, ["GainAuto"], cfg["gain_auto"], "Auto gain"),
        "digital_shift_enable": gc.set_bool_and_verify(
            node_map, ["DigitalShiftEnable"], cfg["digital_shift_enable"], "Digital shift"),
        "black_level_enable": gc.set_bool_and_verify(
            node_map, ["BlackLevelEnable"], cfg["black_level_enable"], "Black level enable"),
        "black_level": gc.set_int_and_verify(node_map, ["BlackLevel"], cfg["black_level"], "Black level"),
        "gamma_enable": gc.set_bool_and_verify(node_map, ["GammaEnable"], cfg["gamma_enable"], "Gamma"),
        "sharpness_enable": gc.set_bool_and_verify(
            node_map, ["SharpnessEnable"], cfg["sharpness_enable"], "Sharpness"),
    }
    log.warning(
        "Black level = %s (Enable=%s): pedestal cong them vao moi pixel, khong triet tieu qua ty le "
        "Weber. Khoa dung gia tri nay de dark frame hien co con hop le - doi gia tri phai chup lai dark frame.",
        results["black_level"]["value"], results["black_level_enable"]["value"])
    return results


def _enforce_lut(node_map, cfg: dict) -> dict:  # A.7
    return {"lut_enable": gc.set_bool_and_verify(node_map, ["LUTEnable"], cfg["lut_enable"], "LUT")}


def enforce_linearity_locked(node_map) -> dict:
    """Khoa + xac minh 14 node LINEARITY_LOCKED. Loi bat ky node nao raise
    gc.ParameterError - goi noi dung (cmd_capture) phai dung lai, khong chup."""
    return {
        "image_format": _enforce_image_format(node_map, LINEARITY_LOCKED["image_format"]),
        "acquisition": _enforce_acquisition(node_map, LINEARITY_LOCKED["acquisition"]),
        "analog": _enforce_analog(node_map, LINEARITY_LOCKED["analog"]),
        "lut": _enforce_lut(node_map, LINEARITY_LOCKED["lut"]),
        "noise_reduction": _enforce_noise_reduction(node_map),
    }


# ---------------------------------------------------------------------------
# SECTION 2 - CONFIG THEO SITE
#
# Dat mot lan luc lap dat, co dinh sau do. Khong anh huong tuyen tinh (mang
# thuoc A.16 Transport Layer Control trong tai lieu phan loai - noi ro
# khong anh huong tuyen tinh) nhung van "co dinh sau lap" nen o day chu
# khong phai logic dieu khien.
# ---------------------------------------------------------------------------
def _apply_optional_int(node_map, candidates, desired, label) -> dict:
    if desired is None:
        name, value = gc.read_value(node_map, candidates, label)
        return {"node": name, "value": value, "set_by_user": False}
    result = gc.set_int_and_verify(node_map, candidates, int(desired), label)
    result["set_by_user"] = True
    return result


def _apply_optional_bool(node_map, candidates, desired, label) -> dict:
    if desired is None:
        name, value = gc.read_value(node_map, candidates, label)
        return {"node": name, "value": value, "set_by_user": False}
    result = gc.set_bool_and_verify(node_map, candidates, bool(desired), label)
    result["set_by_user"] = True
    return result


def _apply_optional_ip(node_map, candidates, desired_dotted, label) -> dict:
    """Node GEV luu IP dang Integer 32-bit; desired_dotted la chuoi
    'a.b.c.d' hoac None (khong dung, chi doc + ghi nhan)."""
    if desired_dotted is None:
        name, value = gc.read_value(node_map, candidates, label)
        return {"node": name, "value": gc.ip_int_to_dotted(value), "set_by_user": False}
    try:
        desired_int = struct.unpack("!I", socket.inet_aton(desired_dotted))[0]
    except OSError as e:
        raise gc.ParameterError(f"{label}: '{desired_dotted}' khong phai dia chi IPv4 hop le ({e})") from e
    result = gc.set_int_and_verify(node_map, candidates, desired_int, label)
    result["value"] = gc.ip_int_to_dotted(result["value"])
    result["set_by_user"] = True
    return result


def _apply_site_network(node_map, net: dict) -> dict:
    results = {
        "packet_size": _apply_optional_int(
            node_map, ["GevSCPSPacketSize", "DeviceStreamChannelPacketSize"], net["packet_size"], "Packet size"),
        "scpd": _apply_optional_int(node_map, ["GevSCPD"], net["scpd"], "SCPD (inter-packet delay)"),
        "persistent_ip": _apply_optional_ip(
            node_map, ["GevPersistentIPAddress"], net["persistent_ip"], "Persistent IP"),
        "persistent_subnet": _apply_optional_ip(
            node_map, ["GevPersistentSubnetMask"], net["persistent_subnet"], "Persistent subnet mask"),
        "persistent_gateway": _apply_optional_ip(
            node_map, ["GevPersistentDefaultGateway"], net["persistent_gateway"], "Persistent gateway"),
        "dhcp": _apply_optional_bool(node_map, ["GevCurrentIPConfigurationDHCP"], net["dhcp"], "DHCP"),
        "persistent_ip_mode": _apply_optional_bool(
            node_map, ["GevCurrentIPConfigurationPersistentIP"], net["persistent_ip_mode"], "Persistent IP mode"),
        "do_not_fragment": _apply_optional_bool(
            node_map, ["GevSCPSDoNotFragment"], net["do_not_fragment"], "SCPS Do Not Fragment"),
        "heartbeat_timeout_ms": _apply_optional_int(
            node_map, ["GevHeartbeatTimeout"], net["heartbeat_timeout_ms"], "Heartbeat timeout"),
    }
    # GevPersistent*/GevCurrentIPConfiguration* chi co hieu luc sau khi camera
    # khoi dong lai (GigE Vision spec) - khong lam rot ket noi phien hien tai.
    reboot_fields = ("persistent_ip", "persistent_subnet", "persistent_gateway", "dhcp", "persistent_ip_mode")
    if any(results[f]["set_by_user"] for f in reboot_fields):
        log.warning(
            "Da doi cau hinh IP persistent/DHCP - chi co hieu luc sau khi camera khoi dong lai.")
    return results


def apply_site_config(node_map, config: dict) -> dict:
    site = config["site"]
    return {
        "exposure_us": gc.set_float_and_verify(
            node_map, ["ExposureTime", "ExposureTimeAbs"], float(site["exposure_us"]), "Exposure time"),
        "gain_db": gc.set_float_and_verify(node_map, ["Gain"], float(site["gain_db"]), "Gain"),
        "network": _apply_site_network(node_map, site["network"]),
    }


# ---------------------------------------------------------------------------
# Config: gia tri mac dinh an toan, dung lam schema noi bo + fallback khi
# mot flag CLI khong duoc chi dinh.
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "camera": {"serial": None, "ip": None},
    "gentl": {"cti": None},
    "site": {
        "exposure_us": 5000.0,
        "gain_db": 0.0,
        "network": {
            "packet_size": None,
            "scpd": None,
            "persistent_ip": None,
            "persistent_subnet": None,
            "persistent_gateway": None,
            "dhcp": None,
            "persistent_ip_mode": None,
            "do_not_fragment": None,
            "heartbeat_timeout_ms": None,
        },
    },
    "output": {
        "dir": "./captures",
        "image_format": "tiff16",
        "also_save_npy": False,
        "write_metadata_json": True,
    },
    "logging": {"dir": DEFAULT_LOG_DIR},
}


def config_from_args(args: argparse.Namespace) -> dict:
    """Dung dict config (cung hinh dang DEFAULTS) truc tiep tu cac flag CLI
    da parse - thay cho viec doc config.yaml."""
    return {
        "camera": {"serial": args.serial, "ip": args.ip},
        "gentl": {"cti": args.cti},
        "site": {
            "exposure_us": args.exposure_us,
            "gain_db": args.gain_db,
            "network": {
                "packet_size": args.packet_size,
                "scpd": args.scpd,
                "persistent_ip": args.persistent_ip,
                "persistent_subnet": args.persistent_subnet,
                "persistent_gateway": args.persistent_gateway,
                "dhcp": args.dhcp,
                "persistent_ip_mode": args.persistent_ip_mode,
                "do_not_fragment": args.do_not_fragment,
                "heartbeat_timeout_ms": args.heartbeat_timeout_ms,
            },
        },
        "output": {
            "dir": args.outdir,
            "image_format": args.image_format,
            "also_save_npy": args.save_npy,
            "write_metadata_json": args.metadata,
        },
        "logging": {"dir": args.log_dir},
    }


def _warn_unwired_fields(config: dict) -> None:
    """image_format hien dien trong CLI/schema nhung chua co code nao ap
    dung len file that su. Canh bao ro de nguoi dung khong tuong nham la
    da co hieu luc."""
    if config["output"]["image_format"] != DEFAULTS["output"]["image_format"]:
        log.warning(
            "--image-format=%s: CHUA duoc wired-up, anh van luon ghi .tiff (xem save_image()).",
            config["output"]["image_format"])


def cti_path_for_platform(config: dict) -> str:
    explicit = config["gentl"].get("cti")
    path, source = gc.find_cti(explicit)
    if explicit and path == explicit:
        source = "--cti"
    return path


# ---------------------------------------------------------------------------
# SECTION 3 - LOGIC DIEU KHIEN: chup, luu, doc thiet bi.
# Khong dinh nghia hang so linearity nao o day - chi goi SECTION 1/2.
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


def build_metadata(device_info: dict, locked_results: dict, site_results: dict) -> dict:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "device_serial": device_info["serial"],
        "device_firmware": device_info["firmware"],
        "linearity_locked": locked_results,  # A.2/A.3/A.4/A.7 + noise_reduction, xem LINEARITY_LOCKED
        "site": site_results,  # exposure_us, gain_db, network (A.16)
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

        log.info("--- Khoa + xac minh 14 node linearity ---")
        try:
            locked_results = enforce_linearity_locked(node_map)
        except gc.ParameterError as e:
            log.error("KHONG the khoa cung tham so tuyen tinh: %s", e)
            log.error("DUNG. Khong chup anh (anh voi ISP con bat la anh vo dung cho du an nay).")
            return 2

        log.info("--- Ap dung config theo site ---")
        try:
            site_results = apply_site_config(node_map, config)
        except gc.ParameterError as e:
            log.error("Khong ap dung duoc config site: %s", e)
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
            meta = build_metadata(device_info, locked_results, site_results)
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
    cam.add_argument("--ip", default=DEFAULTS["camera"]["ip"],
                      help="IP camera. Bo trong de dung serial hoac thiet bi dau tien do duoc.")
    cam.add_argument("--serial", default=DEFAULTS["camera"]["serial"], help="Serial camera.")
    cam.add_argument("--cti", default=DEFAULTS["gentl"]["cti"], help="Duong dan .cti tuong minh. Bo trong de tu do tim.")

    site = parser.add_argument_group("Site (dat mot lan luc lap dat, co dinh sau do)")
    site.add_argument("--exposure-us", type=float, default=DEFAULTS["site"]["exposure_us"])
    site.add_argument("--gain-db", type=float, default=DEFAULTS["site"]["gain_db"])
    site.add_argument("--packet-size", type=int, default=None,
                       help="GevSCPSPacketSize. Bo trong = giu nguyen gia tri hien tai tren camera.")
    site.add_argument("--scpd", type=int, default=None,
                       help="GevSCPD (inter-packet delay). Bo trong = giu nguyen.")
    site.add_argument("--persistent-ip", default=None,
                       help="GevPersistentIPAddress, dang 'a.b.c.d'. Chi co hieu luc sau khi camera khoi dong lai.")
    site.add_argument("--persistent-subnet", default=None, help="GevPersistentSubnetMask, dang 'a.b.c.d'.")
    site.add_argument("--persistent-gateway", default=None, help="GevPersistentDefaultGateway, dang 'a.b.c.d'.")
    site.add_argument("--dhcp", dest="dhcp", action="store_true", default=None,
                       help="Bat GevCurrentIPConfigurationDHCP. Bo trong = giu nguyen.")
    site.add_argument("--no-dhcp", dest="dhcp", action="store_false")
    site.add_argument("--persistent-ip-mode", dest="persistent_ip_mode", action="store_true", default=None,
                       help="Bat GevCurrentIPConfigurationPersistentIP. Bo trong = giu nguyen.")
    site.add_argument("--no-persistent-ip-mode", dest="persistent_ip_mode", action="store_false")
    site.add_argument("--do-not-fragment", dest="do_not_fragment", action="store_true", default=None,
                       help="Bat GevSCPSDoNotFragment. Bo trong = giu nguyen.")
    site.add_argument("--no-do-not-fragment", dest="do_not_fragment", action="store_false")
    site.add_argument("--heartbeat-timeout-ms", type=int, default=None,
                       help="GevHeartbeatTimeout(ms). Bo trong = giu nguyen.")

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
