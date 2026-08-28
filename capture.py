"""
capture.py - connects to a GigE camera (Hikrobot MV-CE200-10GM), locks the 14
nodes that affect linearity, applies site config (exposure/gain/network),
captures a single frame on command, and saves the image with bit depth and
metadata preserved. All parameters come from CLI argparse - no config file.

Usage:
    python capture.py [--ip IP | --serial SERIAL] [--exposure-us US]
                       [--gain-db DB] [--outdir DIR] ...
    python capture.py --help    # see all flags

GenICam node names here were verified on real hardware (see
reference/camera_report.md). Do not guess or hard-code without verifying.
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

# INFO level on console; focus.py relies on this handler being present when it imports capture.py.
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logging.getLogger().addHandler(_console_handler)
logging.getLogger().setLevel(logging.DEBUG)

log = logging.getLogger("capture")

DEFAULT_LOG_DIR = "./logs"


def add_file_logging(log_dir: str, command: str) -> Path:
    """Adds a timestamped DEBUG-level file log alongside the console (console stays INFO-only)."""
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
# SECTION 1 - LINEARITY LOCKED CONFIG (INVARIANT)
# The 14 nodes below never change and have no CLI flag: a wrong value causes
# silent physical corruption (R^2 stays high but the MOR reading carries a
# systematic offset). Grouped to match Appendix A in
# reference/hikrobot_phanloai_tuyentinh.md.
# ---------------------------------------------------------------------------
LINEARITY_LOCKED: dict[str, Any] = {
    "image_format": {"pixel_format": "Mono12", "binning_h": "BinningHorizontal1",  # A.2
                      "binning_v": "BinningVertical1", "test_pattern": "Off"},
    "acquisition": {"exposure_auto": "Off", "exposure_mode": "Timed", "hdr_enable": False},  # A.3
    "analog": {  # A.4 (Gain is not here - it's site config, see SECTION 2)
        "gain_auto": "Off",
        "digital_shift_enable": False,
        "black_level_enable": True,
        "black_level": 200,  # locked to keep the existing dark frame valid; changing it requires a new dark frame
        "gamma_enable": False,
        "sharpness_enable": False,
    },
    "lut": {"lut_enable": False},  # A.7
}

NOISE_REDUCTION_CANDIDATES = [
    "NoiseReductionEnable", "DigitalNoiseReductionMode", "NoiseReduction", "TZDenoiseOpen",
]


def _enforce_noise_reduction(node_map) -> dict:
    """Firmware V3.1.1 locks these nodes at access NI/NA (unreadable/unsettable) -
    not an error, the ISP simply has no active noise reduction pipeline. A firmware
    with an RW node here would be turned off and verified like the linearity nodes."""
    name, node = gc.find_first_node(node_map, NOISE_REDUCTION_CANDIDATES)
    if node is None:
        log.warning("Noise reduction: no candidate node found among %s", NOISE_REDUCTION_CANDIDATES)
        return {"node": None, "access": None, "note": "no candidate node found"}

    access = gc.access_mode_of(node)
    if access in ("NI", "NA"):
        log.info(
            "Noise reduction (%s): access=%s - not supported/enabled by this firmware, "
            "not an error (nothing to turn off).", name, access)
        return {"node": name, "access": access, "note": "unavailable on this firmware (NI/NA)"}

    iface = int(node.node.principal_interface_type)
    try:
        if iface == 3:  # Boolean
            result = gc.set_bool_and_verify(node_map, [name], False, "Noise reduction")
        elif iface == 9:  # Enumeration
            result = gc.set_enum_and_verify(node_map, [name], "Off", "Noise reduction")
        else:  # Integer/Float threshold-style: set to 0
            result = gc.set_int_and_verify(node_map, [name], 0, "Noise reduction")
        return result
    except gc.ParameterError as e:
        raise gc.ParameterError(f"Noise reduction ({name}): {e}") from e


def enforce_linearity_locked(node_map) -> dict:
    """Locks and verifies the 14 LINEARITY_LOCKED nodes. Any node failure raises
    gc.ParameterError - the caller (cmd_capture) must abort and not capture."""
    results = {}

    # A.2 Image Format Control
    fmt = LINEARITY_LOCKED["image_format"]
    results["image_format"] = {
        "pixel_format": gc.set_enum_and_verify(node_map, ["PixelFormat"], fmt["pixel_format"], "Pixel format"),
        "binning_h": gc.set_enum_and_verify(node_map, ["BinningHorizontal"], fmt["binning_h"], "Binning horizontal"),
        "binning_v": gc.set_enum_and_verify(node_map, ["BinningVertical"], fmt["binning_v"], "Binning vertical"),
        "test_pattern": gc.set_enum_and_verify(node_map, ["TestPattern"], fmt["test_pattern"], "Test pattern"),
    }

    # A.3 Acquisition Control
    acq = LINEARITY_LOCKED["acquisition"]
    results["acquisition"] = {
        "exposure_auto": gc.set_enum_and_verify(node_map, ["ExposureAuto"], acq["exposure_auto"], "Auto exposure"),
        "exposure_mode": gc.set_enum_and_verify(node_map, ["ExposureMode"], acq["exposure_mode"], "Exposure mode"),
        "hdr_enable": gc.set_bool_and_verify(node_map, ["HDREnable"], acq["hdr_enable"], "HDR"),
    }

    # A.4 Analog Control
    ana = LINEARITY_LOCKED["analog"]
    results["analog"] = {
        "gain_auto": gc.set_enum_and_verify(node_map, ["GainAuto"], ana["gain_auto"], "Auto gain"),
        "digital_shift_enable": gc.set_bool_and_verify(
            node_map, ["DigitalShiftEnable"], ana["digital_shift_enable"], "Digital shift"),
        "black_level_enable": gc.set_bool_and_verify(
            node_map, ["BlackLevelEnable"], ana["black_level_enable"], "Black level enable"),
        "black_level": gc.set_int_and_verify(node_map, ["BlackLevel"], ana["black_level"], "Black level"),
        "gamma_enable": gc.set_bool_and_verify(node_map, ["GammaEnable"], ana["gamma_enable"], "Gamma"),
        "sharpness_enable": gc.set_bool_and_verify(
            node_map, ["SharpnessEnable"], ana["sharpness_enable"], "Sharpness"),
    }
    log.warning(
        "Black level = %s (Enable=%s): a pedestal added to every pixel, not proportional through the Weber "
        "ratio. Locked at this value to keep the existing dark frame valid - changing it requires a new dark frame.",
        results["analog"]["black_level"]["value"], results["analog"]["black_level_enable"]["value"])

    # A.7 LUT Control
    results["lut"] = {
        "lut_enable": gc.set_bool_and_verify(node_map, ["LUTEnable"], LINEARITY_LOCKED["lut"]["lut_enable"], "LUT"),
    }

    results["noise_reduction"] = _enforce_noise_reduction(node_map)
    return results


# ---------------------------------------------------------------------------
# SECTION 2 - SITE CONFIG
# Set once at install time, fixed afterward. Network (A.16) doesn't affect
# linearity but still belongs to the "fixed after install" group, hence here.
# ---------------------------------------------------------------------------
def _apply_optional(node_map, candidates, desired, label, setter) -> dict:
    if desired is None:
        name, value = gc.read_value(node_map, candidates, label)
        return {"node": name, "value": value, "set_by_user": False}
    result = setter(node_map, candidates, desired, label)
    result["set_by_user"] = True
    return result


def _apply_optional_int(node_map, candidates, desired, label) -> dict:
    return _apply_optional(node_map, candidates, desired, label,
                            lambda nm, c, d, l: gc.set_int_and_verify(nm, c, int(d), l))


def _apply_optional_bool(node_map, candidates, desired, label) -> dict:
    return _apply_optional(node_map, candidates, desired, label,
                            lambda nm, c, d, l: gc.set_bool_and_verify(nm, c, bool(d), l))


def _apply_optional_ip(node_map, candidates, desired_dotted, label) -> dict:
    """GEV nodes store IP as a 32-bit Integer; desired_dotted is 'a.b.c.d' or None."""
    if desired_dotted is None:
        name, value = gc.read_value(node_map, candidates, label)
        return {"node": name, "value": gc.ip_int_to_dotted(value), "set_by_user": False}
    try:
        desired_int = struct.unpack("!I", socket.inet_aton(desired_dotted))[0]
    except OSError as e:
        raise gc.ParameterError(f"{label}: '{desired_dotted}' is not a valid IPv4 address ({e})") from e
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
    # GevPersistent*/GevCurrentIPConfiguration* only take effect after the camera reboots.
    reboot_fields = ("persistent_ip", "persistent_subnet", "persistent_gateway", "dhcp", "persistent_ip_mode")
    if any(results[f]["set_by_user"] for f in reboot_fields):
        log.warning(
            "Changed persistent IP/DHCP config - only takes effect after the camera reboots.")
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
# Config: safe default values, used as the internal schema + fallback when a
# CLI flag isn't specified.
# ---------------------------------------------------------------------------
_NETWORK_FIELDS = ("packet_size", "scpd", "persistent_ip", "persistent_subnet", "persistent_gateway",
                    "dhcp", "persistent_ip_mode", "do_not_fragment", "heartbeat_timeout_ms")

DEFAULTS: dict[str, Any] = {
    "camera": {"serial": None, "ip": None},
    "gentl": {"cti": None},
    "site": {"exposure_us": 5000.0, "gain_db": 0.0, "network": dict.fromkeys(_NETWORK_FIELDS)},
    "output": {"dir": "./captures", "image_format": "tiff16", "also_save_npy": False, "write_metadata_json": True},
    "logging": {"dir": DEFAULT_LOG_DIR},
}


def config_from_args(args: argparse.Namespace) -> dict:
    """Builds a config dict (same shape as DEFAULTS) directly from parsed CLI
    flags - replaces reading a config.yaml."""
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
    """image_format exists in the CLI/schema but no code applies it to the
    actual file yet. Warn clearly so the user doesn't assume it's in effect."""
    if config["output"]["image_format"] != DEFAULTS["output"]["image_format"]:
        log.warning(
            "--image-format=%s: NOT wired up yet, images are always written as .tiff (see save_image()).",
            config["output"]["image_format"])


def cti_path_for_platform(config: dict) -> str:
    explicit = config["gentl"].get("cti")
    path, source = gc.find_cti(explicit)
    if explicit and path == explicit:
        source = "--cti"
    return path


# ---------------------------------------------------------------------------
# SECTION 3 - CONTROL LOGIC: capture, save, read device info.
# No linearity constants are defined here - only calls into SECTION 1/2.
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
        log.debug("could not read num_underrun: %s", e)

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
            "note": ("harvesters/GenTL does not expose an exact packet loss ratio via this API; "
                     "buffer_complete=False or num_underrun>0 indicates data was lost."),
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
            f"pixel_format={data_format} is a packed format. Decoding packed 10/12-bit is NOT "
            "implemented in this PoC (risk of silently misreading the bit layout). Use the unpacked "
            "variant (Mono10/Mono12) - confirmed supported on this camera (see reference/camera_report.md)."
        )
    if data_format not in UNPACKED_DTYPE:
        raise NotImplementedError(f"pixel_format={data_format} is not yet supported for saving in this PoC.")

    dtype = UNPACKED_DTYPE[data_format]
    image = arr.view(dtype).reshape(height, width)

    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / f"{base_name}.tiff"
    ok = cv2.imwrite(str(image_path), image)
    if not ok:
        raise IOError(f"cv2.imwrite failed: {image_path}")

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
        "linearity_locked": locked_results,  # A.2/A.3/A.4/A.7 + noise_reduction, see LINEARITY_LOCKED
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
    log.info("Using .cti: %s", cti_path)

    h = ia = None
    try:
        h, ia = gc.connect_control(cti_path, config["camera"].get("ip"), config["camera"].get("serial"))
        node_map = ia.remote_device.node_map

        device_info = read_device_info(node_map)
        log.info("Connected: model=%s serial=%s firmware=%s",
                 device_info["model"], device_info["serial"], device_info["firmware"])

        log.info("--- Locking + verifying 14 linearity nodes ---")
        try:
            locked_results = enforce_linearity_locked(node_map)
        except gc.ParameterError as e:
            log.error("FAILED to lock linearity parameters: %s", e)
            log.error("STOPPING. No image captured (an image with the ISP still active is useless for this project).")
            return 2

        log.info("--- Applying site config ---")
        try:
            site_results = apply_site_config(node_map, config)
        except gc.ParameterError as e:
            log.error("Failed to apply site config: %s", e)
            return 2

        log.info("--- Capturing one frame ---")
        arr, capture_info = single_capture(ia, node_map, config)
        if not capture_info["is_complete"]:
            log.warning("Buffer incomplete (is_complete=False) - image may be corrupt/missing data.")

        out_dir = Path(config["output"]["dir"])
        base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        save_info = save_image(arr, capture_info, out_dir, base_name, config)
        log.info("Saved image: %s (%s, %s, min=%d max=%d)",
                 save_info["image_path"], save_info["dtype"], save_info["shape"],
                 save_info["min"], save_info["max"])

        if config["output"]["write_metadata_json"]:
            meta = build_metadata(device_info, locked_results, site_results)
            meta_path = out_dir / f"{base_name}.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
            log.info("Saved metadata: %s", meta_path)

        return 0
    finally:
        if h is not None:
            gc.disconnect_control(h, ia)
            log.info("Closed camera connection and released Harvester.")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    cam = parser.add_argument_group("Camera")
    cam.add_argument("--ip", default=DEFAULTS["camera"]["ip"],
                      help="Camera IP. Leave empty to use serial or the first device found.")
    cam.add_argument("--serial", default=DEFAULTS["camera"]["serial"], help="Camera serial number.")
    cam.add_argument("--cti", default=DEFAULTS["gentl"]["cti"], help="Explicit .cti path. Leave empty to auto-discover.")

    site = parser.add_argument_group("Site (set once at install time, fixed afterward)")
    site.add_argument("--exposure-us", type=float, default=DEFAULTS["site"]["exposure_us"])
    site.add_argument("--gain-db", type=float, default=DEFAULTS["site"]["gain_db"])
    site.add_argument("--packet-size", type=int, default=None,
                       help="GevSCPSPacketSize. Leave empty to keep the camera's current value.")
    site.add_argument("--scpd", type=int, default=None,
                       help="GevSCPD (inter-packet delay). Leave empty to keep the current value.")
    site.add_argument("--persistent-ip", default=None,
                       help="GevPersistentIPAddress, as 'a.b.c.d'. Only takes effect after the camera reboots.")
    site.add_argument("--persistent-subnet", default=None, help="GevPersistentSubnetMask, as 'a.b.c.d'.")
    site.add_argument("--persistent-gateway", default=None, help="GevPersistentDefaultGateway, as 'a.b.c.d'.")
    site.add_argument("--dhcp", dest="dhcp", action="store_true", default=None,
                       help="Enable GevCurrentIPConfigurationDHCP. Leave empty to keep the current value.")
    site.add_argument("--no-dhcp", dest="dhcp", action="store_false")
    site.add_argument("--persistent-ip-mode", dest="persistent_ip_mode", action="store_true", default=None,
                       help="Enable GevCurrentIPConfigurationPersistentIP. Leave empty to keep the current value.")
    site.add_argument("--no-persistent-ip-mode", dest="persistent_ip_mode", action="store_false")
    site.add_argument("--do-not-fragment", dest="do_not_fragment", action="store_true", default=None,
                       help="Enable GevSCPSDoNotFragment. Leave empty to keep the current value.")
    site.add_argument("--no-do-not-fragment", dest="do_not_fragment", action="store_false")
    site.add_argument("--heartbeat-timeout-ms", type=int, default=None,
                       help="GevHeartbeatTimeout(ms). Leave empty to keep the current value.")

    out = parser.add_argument_group("Output")
    out.add_argument("--outdir", default=DEFAULTS["output"]["dir"])
    out.add_argument("--image-format", choices=["tiff16", "npy"], default=DEFAULTS["output"]["image_format"],
                      help="NOT wired up yet, images are always written as .tiff (see the warning at runtime).")
    out.add_argument("--save-npy", dest="save_npy", action="store_true",
                      default=DEFAULTS["output"]["also_save_npy"])
    out.add_argument("--no-save-npy", dest="save_npy", action="store_false")
    out.add_argument("--metadata", dest="metadata", action="store_true",
                      default=DEFAULTS["output"]["write_metadata_json"])
    out.add_argument("--no-metadata", dest="metadata", action="store_false")

    log_grp = parser.add_argument_group("Logging")
    log_grp.add_argument("--log-dir", default=DEFAULTS["logging"]["dir"],
                          help="Directory for the detailed log file. Pass '' to disable file logging.")

    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    config = config_from_args(args)
    _warn_unwired_fields(config)

    if args.log_dir:
        try:
            log_file = add_file_logging(args.log_dir, "capture")
            log.info("Writing detailed (DEBUG) log to: %s", log_file)
        except OSError as e:
            log.warning("Could not create log file (%s), continuing with console only.", e)

    def _on_sigint(signum, frame):
        log.warning("Received Ctrl-C, cleaning up...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        return cmd_capture(args, config)
    except KeyboardInterrupt:
        log.warning("Stopped by user (Ctrl-C).")
        return 130
    except (gc.CameraConnectionError, gc.ParameterError) as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
