"""
Module dung chung cho ket noi camera GigE (Hikrobot MV-CE200-10GM) qua harvesters.

Gom lai o day vi camera_info.py, capture.py va focus.py deu can:
  - dinh vi MvProducerGEV.cti,
  - vong qua 2 loi UnicodeDecodeError da xac minh trong MvProducerGEV.cti
    (ban V3.1.1 200717) khi dung voi harvesters 1.4.3 / genicam 1.5.1,
  - cac ham doc/ghi node GenICam co xac minh read-back.

Tat ca da duoc thu truc tiep tren camera that trong qua trinh phat trien
(xem capture_bindings_and_issues.md va camera_report.md). Khong doan ten
node: moi ham set-and-verify deu doc lai tu node map that va bao loi ro
neu khong dung nhu ky vong.
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
    """Khong do/mo duoc camera."""


class ParameterError(RuntimeError):
    """Set hoac read-back mot node that bai (node khong ton tai, sai kieu,
    read-back khong khop gia tri da set, hoac access mode khong cho phep)."""


# ---------------------------------------------------------------------------
# Dinh vi MvProducerGEV.cti
# ---------------------------------------------------------------------------
def find_cti(explicit_path: Optional[str] = None) -> tuple[str, str]:
    """Tra ve (duong_dan, nguon). Khong hard-code mot duong dan duy nhat:
    thu cac ung vien tu bien moi truong do MVS SDK thiet lap va cac vi tri
    cai dat mac dinh, chi nhan file thuc su ton tai tren dia."""
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path, "chi dinh truc tiep"
        raise FileNotFoundError(f"duong dan .cti da chi dinh nhung khong ton tai: {explicit_path}")

    candidates: list[tuple[str, str]] = []
    for env_name in ("GENICAM_GENTL64_PATH", "GENICAM_GENTL32_PATH"):
        env_dir = os.environ.get(env_name)
        if env_dir:
            candidates.append((os.path.join(env_dir, "MvProducerGEV.cti"), f"bien moi truong {env_name}"))

    if platform.system() == "Windows":
        for base in (r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
                     r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win32_i86",
                     r"C:\Program Files\Common Files\MVS\Runtime\Win64_x64"):
            candidates.append((os.path.join(base, "MvProducerGEV.cti"), "duong dan cai dat MVS SDK mac dinh (Windows)"))
    else:
        for base in ("/opt/MVS/lib/64", "/opt/MVS/lib/32"):
            candidates.append((os.path.join(base, "MvProducerGEV.cti"), "duong dan cai dat MVS SDK mac dinh (Linux)"))

    for path, source in candidates:
        if os.path.isfile(path):
            return path, source

    tried = "\n".join(f"  - {p}  ({s})" for p, s in candidates)
    raise FileNotFoundError(
        "Khong tim thay MvProducerGEV.cti. Da thu:\n" + tried +
        "\nNguyen nhan thuong gap: chua cai Hikrobot MVS SDK, hoac cai o duong dan khac. "
        "Dinh vi file that trong MVS SDK roi truyen qua config/--cti."
    )


# ---------------------------------------------------------------------------
# Vong qua 2 loi UnicodeDecodeError trong MvProducerGEV.cti (xac minh tren
# camera that, xem capture_bindings_and_issues.md muc 4):
#   (1) doc node map cua *local TL device* (truoc khi cham toi node map that
#       cua camera) tra ve URL khong phai UTF-8 hop le.
#   (2) ImageAcquirer dang ky module event (System/Interface/Device) khi mo
#       ket noi co streaming (can cho capture.py/focus.py, khong can cho
#       camera_info.py vi chi doc node map).
# Mot loi thu 3, khac ban chat, xuat hien khi fetch buffer anh dau tien sau
# ia.start(): xem fetch_buffer_retrying() ben duoi - day la loi khong on
# dinh (fail 0-5 lan roi thanh cong), khong the vong qua bang patch, phai
# retry o tang goi ham fetch.
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
                "non-UTF8 URL info tu GenTL port (Hikrobot MvProducerGEV quirk), bo qua: %s", e)
            return False, None

    Module._retrieve_file_path = _patched_retrieve_file_path

    def _wrap_register_event(cls):
        orig = cls.register_event

        def patched(self, event_id, *a, **kw):
            try:
                return orig(self, event_id, *a, **kw)
            except UnicodeDecodeError as e:
                harvesters_logger.warning(
                    "non-UTF8 du lieu event tu GenTL module %s (Hikrobot MvProducerGEV quirk), "
                    "coi nhu module nay khong ho tro event: %s", cls.__name__, e)
                raise gentl.NotImplementedException(
                    0, "event registration not usable (non-UTF8 data from producer)")

        cls.register_event = patched

    for cls in (gentl.System, gentl.Interface, gentl.Device):
        _wrap_register_event(cls)

    _patched = True


def fetch_buffer_retrying(ia, timeout_ms: int = 15000, max_attempts: int = 200):
    """ia.fetch() qua MvProducerGEV.cti fail voi UnicodeDecodeError mot so
    lan dau (du lieu event rac, khong lien quan noi dung anh - xac minh
    bang thuc nghiem: cung mot khung anh, kich thuoc payload khac nhau,
    error luon o vi tri byte ~80-95, khong phu thuoc kich thuoc anh), roi
    thanh cong binh thuong. So lan fail quan sat duoc: 0-5 lan voi ROI nho,
    toi ~19 lan voi full-res sau khi da doi nhieu node khac (co ve co lien
    quan so luong event con don trong hang doi truoc do) - moi lan retry
    gan nhu tuc thi nen cho phep nhieu lan thu, gioi han tong thoi gian
    bang timeout_ms thay vi so lan."""
    last_err: Optional[Exception] = None
    deadline = time.monotonic() + (timeout_ms / 1000.0) * 3  # bien do cho retry, khong phai timeout cua 1 lan fetch
    attempt = 0
    while attempt < max_attempts and time.monotonic() < deadline:
        attempt += 1
        try:
            return ia.fetch(timeout=timeout_ms / 1000.0)
        except UnicodeDecodeError as e:
            last_err = e
            log.debug("fetch buffer: lan %d fail (non-UTF8 event data), thu lai", attempt)
            continue
    raise CameraConnectionError(
        f"Khong fetch duoc buffer sau {attempt} lan (loi UnicodeDecodeError lap lai): {last_err}"
    )


# ---------------------------------------------------------------------------
# Ket noi
# ---------------------------------------------------------------------------
def _select_device(devices, ip: Optional[str], serial: Optional[str]):
    if serial:
        chosen = next((d for d in devices if d.serial_number == serial), None)
        if chosen is None:
            raise CameraConnectionError(f"Khong tim thay thiet bi voi serial={serial}")
        return chosen
    if ip:
        for d in devices:
            if getattr(d, "ip_address", None) == ip:
                return d
        raise CameraConnectionError(f"Khong tim thay thiet bi voi ip={ip}")
    if len(devices) == 0:
        raise CameraConnectionError("Khong co thiet bi nao trong danh sach")
    return devices[0]


def _no_device_message(cti_path: str) -> str:
    return (
        "Khong do duoc thiet bi GigE nao. Nguyen nhan thuong gap:\n"
        f"  - Sai duong dan .cti (dang dung: {cti_path})\n"
        "  - Host va camera trung dia chi IP (vd ca hai cung 192.168.100.253)\n"
        "  - Camera chua duoc cap nguon PoE / 12VDC\n"
        "  - Host va camera khac subnet (camera thuong o 192.168.100.253/24)\n"
        "  - Firewall chan GigE Vision discovery (UDP broadcast)"
    )


def connect_readonly(cti_path: str, ip: Optional[str] = None, serial: Optional[str] = None):
    """Mo camera o DEVICE_ACCESS_READONLY, khong di qua ImageAcquirer (khong
    can streaming). Dung cho camera_info.py. Tra ve (harvester, device_proxy,
    remote_device)."""
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
    log.info("Thiet bi da chon: %s", chosen)

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
        log.warning("loi khi disconnect node map: %s", e)
    try:
        device_proxy.module.close()
    except Exception as e:
        log.warning("loi khi dong device: %s", e)
    try:
        h.reset()
    except Exception as e:
        log.warning("loi khi reset Harvester: %s", e)


def connect_control(cti_path: str, ip: Optional[str] = None, serial: Optional[str] = None):
    """Mo camera qua ImageAcquirer (quyen exclusive) - dung cho capture.py
    va focus.py, can set node va chup/stream anh. Tra ve (harvester, ia)."""
    apply_unicode_workarounds()
    from harvesters.core import Harvester

    h = Harvester()
    h.add_file(cti_path)
    h.update()
    if len(h.device_info_list) == 0:
        h.reset()
        raise CameraConnectionError(_no_device_message(cti_path))

    chosen = _select_device(h.device_info_list, ip, serial)
    log.info("Thiet bi da chon: %s", chosen)

    idx = h.device_info_list.index(chosen)
    ia = h.create(idx)
    return h, ia


def disconnect_control(h, ia) -> None:
    try:
        ia.destroy()
    except Exception as e:
        log.warning("loi khi destroy ImageAcquirer: %s", e)
    try:
        h.reset()
    except Exception as e:
        log.warning("loi khi reset Harvester: %s", e)


# ---------------------------------------------------------------------------
# Tien ich mang / phien ban
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
            s.connect((dest_ip, 3956))  # cong GVCP chuan; UDP connect() khong gui goi tin
            return s.getsockname()[0]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Doc/ghi node co xac minh - khong hard-code: luon tim node that qua
# has_node()/get_node() truoc khi thao tac, va luon doc lai sau khi set.
# ---------------------------------------------------------------------------
def find_first_node(node_map, candidates: Iterable[str]):
    """Tra ve (ten_node, node) cho ung vien dau tien ton tai trong node map,
    hoac (None, None) neu khong ung vien nao ton tai."""
    for name in candidates:
        if node_map.has_node(name):
            return name, node_map.get_node(name)
    return None, None


def access_mode_of(node) -> str:
    return ACCESS_NAMES.get(int(node.get_access_mode()), "?")


def set_bool_and_verify(node_map, candidates: Iterable[str], desired: bool, label: str) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: khong tim thay node nao trong {list(candidates)}")
    access = access_mode_of(node)
    if access == "RW":
        node.value = desired
    elif access not in ("RO",):
        raise ParameterError(f"{label} ({name}): access={access}, khong the set va khong doc duoc de xac nhan")
    readback = node.value
    if readback != desired:
        raise ParameterError(f"{label} ({name}): read-back={readback}, ky vong={desired}")
    log.info("%s (%s) = %s [access=%s]", label, name, readback, access)
    return {"node": name, "value": readback, "access": access}


def set_enum_and_verify(node_map, candidates: Iterable[str], desired: str, label: str) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: khong tim thay node nao trong {list(candidates)}")
    access = access_mode_of(node)
    available = [e.symbolic for e in node.entries if access_mode_of(e) in ("RO", "RW")]
    if desired not in available:
        raise ParameterError(f"{label} ({name}): gia tri '{desired}' khong co trong enum kha dung {available}")
    if access == "RW":
        node.value = desired
    elif access != "RO":
        raise ParameterError(f"{label} ({name}): access={access}, khong the set")
    readback = node.value
    if readback != desired:
        raise ParameterError(f"{label} ({name}): read-back={readback}, ky vong={desired}")
    log.info("%s (%s) = %s [access=%s]", label, name, readback, access)
    return {"node": name, "value": readback, "access": access}


def set_float_and_verify(node_map, candidates: Iterable[str], desired: float, label: str,
                          rel_tol: float = 1e-3) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: khong tim thay node nao trong {list(candidates)}")
    access = access_mode_of(node)
    if access != "RW":
        raise ParameterError(f"{label} ({name}): access={access}, can RW de set")
    lo, hi = node.min, node.max
    clamped = min(max(desired, lo), hi)
    if clamped != desired:
        log.warning("%s (%s): gia tri %.4f ngoai dai [%.4f, %.4f], ghim ve %.4f", label, name, desired, lo, hi, clamped)
    node.value = clamped
    readback = node.value
    if abs(readback - clamped) > max(rel_tol * abs(clamped), 1e-6):
        raise ParameterError(f"{label} ({name}): read-back={readback}, ky vong~={clamped}")
    log.info("%s (%s) = %s [access=%s, dai=[%s,%s]]", label, name, readback, access, lo, hi)
    return {"node": name, "value": readback, "access": access, "min": lo, "max": hi}


def set_int_and_verify(node_map, candidates: Iterable[str], desired: int, label: str) -> dict:
    name, node = find_first_node(node_map, candidates)
    if node is None:
        raise ParameterError(f"{label}: khong tim thay node nao trong {list(candidates)}")
    access = access_mode_of(node)
    if access != "RW":
        raise ParameterError(f"{label} ({name}): access={access}, can RW de set")
    lo, hi = node.min, node.max
    clamped = min(max(desired, lo), hi)
    node.value = clamped
    readback = node.value
    if readback != clamped:
        raise ParameterError(f"{label} ({name}): read-back={readback}, ky vong={clamped}")
    log.info("%s (%s) = %s [access=%s, dai=[%s,%s]]", label, name, readback, access, lo, hi)
    return {"node": name, "value": readback, "access": access, "min": lo, "max": hi}


def read_value(node_map, candidates: Iterable[str], label: str):
    name, node = find_first_node(node_map, candidates)
    if node is None:
        log.warning("%s: khong tim thay node nao trong %s", label, list(candidates))
        return None, None
    access = access_mode_of(node)
    if access not in ("RO", "RW"):
        log.info("%s (%s): access=%s, khong doc duoc gia tri", label, name, access)
        return name, None
    return name, node.value
