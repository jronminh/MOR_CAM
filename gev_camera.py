"""
Shared module for connecting to a GigE camera (Hikrobot MV-CE200-10GM) via harvesters.

Collected here because camera_info.py, capture.py and focus.py all need:
  - locating MvProducerGEV.cti,
  - working around 2 confirmed UnicodeDecodeError bugs in MvProducerGEV.cti
    (build V3.1.1 200717) when used with harvesters 1.4.3 / genicam 1.5.1,
  - GenICam node read/write helpers with read-back verification.

Everything here was tested directly on real hardware during development
(see reference/capture_bindings_and_issues.md and reference/camera_report.md). Node names are
never guessed: every set-and-verify function reads back from the real node
map and raises a clear error if the value doesn't match expectations.
"""
from __future__ import annotations

import logging
import os
import platform
import socket
import time
from typing import Iterable, Optional

log = logging.getLogger("gev_camera")

IFACE_NAMES = {
    0: "Value", 1: "Base", 2: "Integer", 3: "Boolean", 4: "Command",
    5: "Float", 6: "String", 7: "Register", 8: "Category",
    9: "Enumeration", 10: "EnumEntry", 11: "Port",
}
ACCESS_NAMES = {0: "NI", 1: "NA", 2: "WO", 3: "RO", 4: "RW"}
VIS_NAMES = {0: "Beginner", 1: "Expert", 2: "Guru", 3: "Invisible"}


class CameraConnectionError(RuntimeError):
    """Could not enumerate or open the camera."""


class ParameterError(RuntimeError):
    """A node set or read-back failed (node doesn't exist, wrong type,
    read-back doesn't match the set value, or access mode disallows it)."""


# ---------------------------------------------------------------------------
# Locating MvProducerGEV.cti
# ---------------------------------------------------------------------------
def find_cti(explicit_path: Optional[str] = None) -> tuple[str, str]:
    """Returns (path, source). Does not hard-code a single path: tries
    candidates from environment variables set by the MVS SDK and the default
    install locations, only accepting a file that actually exists on disk."""
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path, "explicitly specified"
        raise FileNotFoundError(f".cti path was specified but does not exist: {explicit_path}")

    candidates: list[tuple[str, str]] = []
    for env_name in ("GENICAM_GENTL64_PATH", "GENICAM_GENTL32_PATH"):
        env_dir = os.environ.get(env_name)
        if env_dir:
            candidates.append((os.path.join(env_dir, "MvProducerGEV.cti"), f"environment variable {env_name}"))

    if platform.system() == "Windows":
        for base in (r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
                     r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win32_i86",
                     r"C:\Program Files\Common Files\MVS\Runtime\Win64_x64"):
            candidates.append((os.path.join(base, "MvProducerGEV.cti"), "default MVS SDK install path (Windows)"))
    else:
        for base in ("/opt/MVS/lib/64", "/opt/MVS/lib/32"):
            candidates.append((os.path.join(base, "MvProducerGEV.cti"), "default MVS SDK install path (Linux)"))

    for path, source in candidates:
        if os.path.isfile(path):
            return path, source

    tried = "\n".join(f"  - {p}  ({s})" for p, s in candidates)
    raise FileNotFoundError(
        "MvProducerGEV.cti not found. Tried:\n" + tried +
        "\nCommon cause: Hikrobot MVS SDK is not installed, or installed at a different path. "
        "Locate the real file in the MVS SDK and pass it via config/--cti."
    )


# ---------------------------------------------------------------------------
# Working around 2 UnicodeDecodeError bugs in MvProducerGEV.cti (confirmed on
# real hardware, see reference/capture_bindings_and_issues.md item 4):
#   (1) reading the node map of the *local TL device* (before reaching the
#       camera's actual node map) returns a URL that isn't valid UTF-8.
#   (2) ImageAcquirer registers module events (System/Interface/Device) when
#       opening a streaming-capable connection (needed by capture.py/focus.py,
#       not needed by camera_info.py since it only reads the node map).
# A third, unrelated error appears when fetching the first image buffer after
# ia.start(): see fetch_buffer_retrying() below - this one is flaky (fails
# 0-5 times then succeeds), cannot be worked around with a patch, and must be
# retried at the fetch call site instead.
# ---------------------------------------------------------------------------
_patched = False


def apply_unicode_workarounds() -> None:
    global _patched
    if _patched:
        return

    from harvesters.core import Module, _logger as harvesters_logger
    import genicam.gentl as gentl

    _orig_retrieve_file_path = Module._retrieve_file_path

    @staticmethod
    def _patched_retrieve_file_path(*, port=None, url=None, file_path_to_load=None,
                                     xml_dir_to_store=None, file_dict=None):
        try:
            return _orig_retrieve_file_path(
                port=port, url=url, file_path_to_load=file_path_to_load,
                xml_dir_to_store=xml_dir_to_store, file_dict=file_dict)
        except UnicodeDecodeError as e:
            harvesters_logger.warning(
                "non-UTF8 URL info from GenTL port (Hikrobot MvProducerGEV quirk), skipping: %s", e)
            return False, None

    Module._retrieve_file_path = _patched_retrieve_file_path

    def _wrap_register_event(cls):
        orig = cls.register_event

        def patched(self, event_id, *a, **kw):
            try:
                return orig(self, event_id, *a, **kw)
            except UnicodeDecodeError as e:
                harvesters_logger.warning(
                    "non-UTF8 event data from GenTL module %s (Hikrobot MvProducerGEV quirk), "
                    "treating this module as not supporting events: %s", cls.__name__, e)
                raise gentl.NotImplementedException(
                    0, "event registration not usable (non-UTF8 data from producer)")

        cls.register_event = patched

    for cls in (gentl.System, gentl.Interface, gentl.Device):
        _wrap_register_event(cls)

    _patched = True


def fetch_buffer_retrying(ia, timeout_ms: int = 15000, max_attempts: int = 200):
    """ia.fetch() through MvProducerGEV.cti fails with UnicodeDecodeError for
    the first few attempts (garbage event data, unrelated to image content -
    confirmed experimentally: same frame, different payload sizes, error
    always at byte offset ~80-95, independent of image size), then succeeds
    normally. Observed failure counts: 0-5 with a small ROI, up to ~19 at
    full resolution after changing several other nodes (appears related to
    stale events left in the queue beforehand) - each retry is near-instant
    so many attempts are allowed, bounding total time by timeout_ms rather
    than attempt count."""
    last_err: Optional[Exception] = None
    deadline = time.monotonic() + (timeout_ms / 1000.0) * 3  # retry budget, not a single fetch's timeout
    attempt = 0
    while attempt < max_attempts and time.monotonic() < deadline:
        attempt += 1
        try:
            return ia.fetch(timeout=timeout_ms / 1000.0)
        except UnicodeDecodeError as e:
            last_err = e
            log.debug("fetch buffer: attempt %d failed (non-UTF8 event data), retrying", attempt)
            continue
    raise CameraConnectionError(
        f"Could not fetch a buffer after {attempt} attempts (repeated UnicodeDecodeError): {last_err}"
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def _select_device(devices, ip: Optional[str], serial: Optional[str]):
    if serial:
        chosen = next((d for d in devices if d.serial_number == serial), None)
        if chosen is None:
            raise CameraConnectionError(f"No device found with serial={serial}")
        return chosen
    if ip:
        for d in devices:
            if getattr(d, "ip_address", None) == ip:
                return d
        raise CameraConnectionError(f"No device found with ip={ip}")
    if len(devices) == 0:
        raise CameraConnectionError("No devices in the list")
    return devices[0]


def _no_device_message(cti_path: str) -> str:
    return (
        "No GigE device found. Common causes:\n"
        f"  - Wrong .cti path (currently using: {cti_path})\n"
        "  - Host and camera share the same IP address (e.g. both at 192.168.100.253)\n"
        "  - Camera not powered (PoE / 12VDC)\n"
        "  - Host and camera are on different subnets (camera is usually at 192.168.100.253/24)\n"
        "  - Firewall blocking GigE Vision discovery (UDP broadcast)"
    )


def connect_readonly(cti_path: str, ip: Optional[str] = None, serial: Optional[str] = None):
    """Opens the camera at DEVICE_ACCESS_READONLY, without going through
    ImageAcquirer (no streaming needed). Used by camera_info.py. Returns
    (harvester, device_proxy, remote_device)."""
    apply_unicode_workarounds()
    from harvesters.core import Harvester, Device, RemoteDevice
    from genicam.gentl import DEVICE_ACCESS_FLAGS_LIST

    h = Harvester()
    h.add_file(cti_path)
    h.update()
    if len(h.device_info_list) == 0:
        h.reset()
        raise CameraConnectionError(_no_device_message(cti_path))

    chosen = _select_device(h.device_info_list, ip, serial)
    log.info("Selected device: %s", chosen)

    raw_device = chosen.create_device()
    device_proxy = Device(module=raw_device, parent=chosen.parent)
    device_proxy.open(DEVICE_ACCESS_FLAGS_LIST.DEVICE_ACCESS_READONLY)
    device_proxy_opened = Device(module=device_proxy.module, parent=device_proxy.parent)
    remote_device = RemoteDevice(module=device_proxy_opened.module, parent=device_proxy_opened)

    return h, device_proxy_opened, remote_device


def disconnect_readonly(h, device_proxy, remote_device) -> None:
    try:
        if remote_device.node_map:
            remote_device.node_map.disconnect()
    except Exception as e:
        log.warning("error disconnecting node map: %s", e)
    try:
        device_proxy.module.close()
    except Exception as e:
        log.warning("error closing device: %s", e)
    try:
        h.reset()
    except Exception as e:
        log.warning("error resetting Harvester: %s", e)


def connect_control(cti_path: str, ip: Optional[str] = None, serial: Optional[str] = None):
    """Opens the camera through ImageAcquirer (exclusive access) - used by
    capture.py and focus.py, which need to set nodes and capture/stream
    images. Returns (harvester, ia)."""
    apply_unicode_workarounds()
    from harvesters.core import Harvester

    h = Harvester()
    h.add_file(cti_path)
    h.update()
    if len(h.device_info_list) == 0:
        h.reset()
        raise CameraConnectionError(_no_device_message(cti_path))

    chosen = _select_device(h.device_info_list, ip, serial)
    log.info("Selected device: %s", chosen)

    idx = h.device_info_list.index(chosen)
    ia = h.create(idx)
    return h, ia


def disconnect_control(h, ia) -> None:
    try:
        ia.destroy()
    except Exception as e:
        log.warning("error destroying ImageAcquirer: %s", e)
    try:
        h.reset()
    except Exception as e:
        log.warning("error resetting Harvester: %s", e)


# ---------------------------------------------------------------------------
# Network / version utilities
# ---------------------------------------------------------------------------
def ip_int_to_dotted(value) -> Optional[str]:
    if value is None:
        return None
    try:
        v = int(value)
        return ".".join(str((v >> shift) & 0xFF) for shift in (24, 16, 8, 0))
    except Exception:
        return None


def local_ip_toward(dest_ip: Optional[str]) -> Optional[str]:
    if not dest_ip:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((dest_ip, 3956))  # standard GVCP port; UDP connect() doesn't send a packet
            return s.getsockname()[0]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Verified node read/write - never hard-coded: always look up the real node
# via has_node()/get_node() before acting on it, and always read back after set.
# ---------------------------------------------------------------------------
def find_first_node(node_map, candidates: Iterable[str]):
    """Returns (node_name, node) for the first candidate that exists in the
    node map, or (None, None) if no candidate exists."""
    for name in candidates:
        if node_map.has_node(name):
            return name, node_map.get_node(name)
    return None, None


def access_mode_of(node) -> str:
    return ACCESS_NAMES.get(int(node.get_access_mode()), "?")


def set_bool_and_verify(node_map, candidates: Iterable[str], desired: bool, label: str) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: no candidate node found in {list(candidates)}")
    access = access_mode_of(node)
    if access == "RW":
        node.value = desired
    elif access not in ("RO",):
        raise ParameterError(f"{label} ({name}): access={access}, cannot set and cannot read back to verify")
    readback = node.value
    if readback != desired:
        raise ParameterError(f"{label} ({name}): read-back={readback}, expected={desired}")
    log.info("%s (%s) = %s [access=%s]", label, name, readback, access)
    return {"node": name, "value": readback, "access": access}


def set_enum_and_verify(node_map, candidates: Iterable[str], desired: str, label: str) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: no candidate node found in {list(candidates)}")
    access = access_mode_of(node)
    available = [e.symbolic for e in node.entries if access_mode_of(e) in ("RO", "RW")]
    if desired not in available:
        raise ParameterError(f"{label} ({name}): value '{desired}' is not among the available enum entries {available}")
    if access == "RW":
        node.value = desired
    elif access != "RO":
        raise ParameterError(f"{label} ({name}): access={access}, cannot set")
    readback = node.value
    if readback != desired:
        raise ParameterError(f"{label} ({name}): read-back={readback}, expected={desired}")
    log.info("%s (%s) = %s [access=%s]", label, name, readback, access)
    return {"node": name, "value": readback, "access": access}


def set_float_and_verify(node_map, candidates: Iterable[str], desired: float, label: str,
                          rel_tol: float = 1e-3) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: no candidate node found in {list(candidates)}")
    access = access_mode_of(node)
    if access != "RW":
        raise ParameterError(f"{label} ({name}): access={access}, RW required to set")
    lo, hi = node.min, node.max
    clamped = min(max(desired, lo), hi)
    if clamped != desired:
        log.warning("%s (%s): value %.4f out of range [%.4f, %.4f], clamped to %.4f", label, name, desired, lo, hi, clamped)
    node.value = clamped
    readback = node.value
    if abs(readback - clamped) > max(rel_tol * abs(clamped), 1e-6):
        raise ParameterError(f"{label} ({name}): read-back={readback}, expected~={clamped}")
    log.info("%s (%s) = %s [access=%s, range=[%s,%s]]", label, name, readback, access, lo, hi)
    return {"node": name, "value": readback, "access": access, "min": lo, "max": hi}


def set_int_and_verify(node_map, candidates: Iterable[str], desired: int, label: str) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: no candidate node found in {list(candidates)}")
    access = access_mode_of(node)
    if access != "RW":
        raise ParameterError(f"{label} ({name}): access={access}, RW required to set")
    lo, hi = node.min, node.max
    clamped = min(max(desired, lo), hi)
    node.value = clamped
    readback = node.value
    if readback != clamped:
        raise ParameterError(f"{label} ({name}): read-back={readback}, expected={clamped}")
    log.info("%s (%s) = %s [access=%s, range=[%s,%s]]", label, name, readback, access, lo, hi)
    return {"node": name, "value": readback, "access": access, "min": lo, "max": hi}


def read_value(node_map, candidates: Iterable[str], label: str):
    name, node = find_first_node(node_map, candidates)
    if node is None:
        log.warning("%s: no candidate node found in %s", label, list(candidates))
        return None, None
    access = access_mode_of(node)
    if access not in ("RO", "RW"):
        log.info("%s (%s): access=%s, cannot read the value", label, name, access)
        return name, None
    return name, node.value
