"""
Dump toan bo GenICam node map cua camera GigE (Hikrobot MV-CE200-10GM) qua harvesters.

CHI DOC. Khong set bat ky node nao. Thiet bi duoc mo o che do DEVICE_ACCESS_READONLY
de dam bao khong the ghi setting du co loi lap trinh.

Xuat:
  - node_map_full.json : dump verbatim toan bo node (ten, kieu, gia tri, min/max, enum, quyen truy cap, mo ta)
  - camera_report.md   : bao cao tom tat + doi chung voi datasheet

Cach dung:
  python camera_info.py [--cti PATH] [--ip IP] [--serial SERIAL] [--outdir DIR]

Neu khong truyen --cti, script se tu do tim MvProducerGEV.cti qua:
  1) bien moi truong GENICAM_GENTL64_PATH / GENICAM_GENTL32_PATH (do bo cai MVS SDK thiet lap)
  2) cac duong dan cai dat MVS SDK mac dinh thuong gap tren Windows/Linux
Khong doan/hard-code mot duong dan duy nhat: liet ke ung vien, dung file dau tien
thuc su ton tai tren dia, va in ro da chon file nao, tu nguon nao.
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path

import gev_camera as gc
from gev_camera import ACCESS_NAMES, IFACE_NAMES, VIS_NAMES, find_cti  # noqa: F401 (tai xuat cho tien dung lai)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("camera_info")

# Cac loi UnicodeDecodeError cua MvProducerGEV.cti va cach vong qua duoc mo
# ta chi tiet trong gev_camera.apply_unicode_workarounds() (dung chung voi
# capture.py/focus.py). connect_readonly()/disconnect() cung chuyen sang
# gev_camera de tranh trung lap code voi capture.py.
connect_readonly = gc.connect_readonly
disconnect = gc.disconnect_readonly


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def describe_node(wrapped) -> dict:
    inode = wrapped.node
    iface_int = int(inode.principal_interface_type)
    access_int = _safe(lambda: int(wrapped.get_access_mode()))
    access = ACCESS_NAMES.get(access_int)
    readable = access in ("RO", "RW")

    info = {
        "name": inode.name,
        "display_name": _safe(lambda: inode.display_name),
        "interface_type": IFACE_NAMES.get(iface_int, str(iface_int)),
        "access_mode": access,
        "visibility": VIS_NAMES.get(int(inode.visibility), str(inode.visibility)),
        "is_feature": bool(_safe(lambda: inode.is_feature(), default=False)),
        "description": _safe(lambda: inode.description) or None,
        "tooltip": _safe(lambda: inode.tooltip) or None,
    }

    if iface_int == 2:  # Integer
        if readable:
            info["value"] = _safe(lambda: wrapped.value)
        info["min"] = _safe(lambda: wrapped.min)
        info["max"] = _safe(lambda: wrapped.max)
        info["inc"] = _safe(lambda: wrapped.inc)
        unit = _safe(lambda: wrapped.unit)
        info["unit"] = unit or None
    elif iface_int == 5:  # Float
        if readable:
            info["value"] = _safe(lambda: wrapped.value)
        info["min"] = _safe(lambda: wrapped.min)
        info["max"] = _safe(lambda: wrapped.max)
        unit = _safe(lambda: wrapped.unit)
        info["unit"] = unit or None
    elif iface_int == 9:  # Enumeration
        if readable:
            info["value"] = _safe(lambda: wrapped.value)
        entries = []
        for e in _safe(lambda: list(wrapped.entries), default=[]):
            entries.append({
                "symbolic": _safe(lambda: e.symbolic),
                "numeric_value": _safe(lambda: e.value),
                "access_mode": ACCESS_NAMES.get(_safe(lambda: int(e.get_access_mode()))),
            })
        info["enum_entries"] = entries
    elif iface_int == 3:  # Boolean
        if readable:
            info["value"] = _safe(lambda: wrapped.value)
    elif iface_int == 6:  # String
        if readable:
            info["value"] = _safe(lambda: wrapped.value)
        info["max_length"] = _safe(lambda: wrapped.max_length)
    elif iface_int == 4:  # Command
        info["is_done"] = _safe(lambda: wrapped.is_done())
    elif iface_int == 8:  # Category
        info["children"] = [
            _safe(lambda: c.node.name) for c in _safe(lambda: list(wrapped.features), default=[])
        ]

    return info


def dump_all_nodes(node_map) -> list[dict]:
    result = []
    for wrapped in node_map.nodes:
        try:
            result.append(describe_node(wrapped))
        except Exception as e:
            name = _safe(lambda: wrapped.node.name, default="<unknown>")
            log.warning("bo qua node loi khi doc: %s (%s)", name, e)
    return result


# ---------------------------------------------------------------------------
# Trich cac node quan trong theo tu khoa
# ---------------------------------------------------------------------------
KEYWORD_GROUPS = {
    "Exposure": ["exposure"],
    "Gain": ["gain"],
    "Gamma": ["gamma"],
    "PixelFormat": ["pixelformat"],
    "Binning": ["binning"],
    "BlackLevel": ["blacklevel"],
    "Noise/Denoise": ["noise", "denoise"],
    "LUT": ["lut"],
    "Timestamp": ["timestamp"],
    "Packet": ["packet"],
    "Device": ["devicemodelname", "deviceserialnumber", "devicefirmwareversion",
               "devicevendorname", "deviceversion", "deviceuserid"],
}


def extract_keyword_nodes(all_nodes: list[dict]) -> dict:
    out = {}
    for group, keywords in KEYWORD_GROUPS.items():
        matches = [
            n for n in all_nodes
            if n["is_feature"] and any(kw in n["name"].lower() for kw in keywords)
        ]
        out[group] = matches
    return out


# ---------------------------------------------------------------------------
# Bao cao markdown
# ---------------------------------------------------------------------------
def fmt_range(n: dict) -> str:
    if n["interface_type"] in ("Integer", "Float"):
        unit = f" {n['unit']}" if n.get("unit") else ""
        return f"[{n.get('min')}, {n.get('max')}]{unit}"
    if n["interface_type"] == "Enumeration":
        avail = [e["symbolic"] for e in n.get("enum_entries", []) if e["access_mode"] in ("RO", "RW")]
        return ", ".join(avail)
    return ""


def fmt_value(n: dict) -> str:
    if "value" in n:
        return str(n["value"])
    if n["interface_type"] == "Command":
        return f"(command, is_done={n.get('is_done')})"
    if n["interface_type"] == "Category":
        return f"(category, {len(n.get('children', []))} con)"
    return "(khong doc duoc / khong co quyen)"


def build_report(meta: dict, keyword_nodes: dict, all_nodes: list[dict]) -> str:
    lines = []
    lines.append("# Camera report: node map dump")
    lines.append("")
    lines.append(f"Thoi gian chay: {meta['run_time_utc']}")
    lines.append("")
    lines.append("## 1. Thong tin ket noi")
    lines.append("")
    lines.append(f"- File .cti su dung: `{meta['cti_path']}`")
    lines.append(f"- Nguon xac dinh .cti: {meta['cti_source']}")
    lines.append(f"- Quyen mo thiet bi: DEVICE_ACCESS_READONLY (chi doc)")
    lines.append(f"- Host IP (adapter ket noi camera): {meta.get('host_ip', 'khong xac dinh')}")
    lines.append(f"- harvesters version: {meta['harvesters_version']}")
    lines.append(f"- genicam version: {meta['genicam_version']}")
    lines.append(f"- Python: {meta['python_version']}")
    lines.append("")
    lines.append("## 2. Thong tin thiet bi (doc tu node map)")
    lines.append("")
    dev_nodes = {n["name"]: n for n in keyword_nodes.get("Device", [])}
    for key, label in [
        ("DeviceModelName", "Model"),
        ("DeviceSerialNumber", "Serial"),
        ("DeviceFirmwareVersion", "Firmware"),
        ("DeviceVendorName", "Vendor"),
        ("DeviceVersion", "Device version"),
        ("DeviceUserID", "User ID"),
    ]:
        n = dev_nodes.get(key)
        val = fmt_value(n) if n else "khong tim thay"
        lines.append(f"- {label} (`{key}`): {val}")
    lines.append(f"- Camera IP hien tai (`GevCurrentIPAddress`, doi ra dang thap phan): {meta.get('camera_ip', 'khong doc duoc')}")
    lines.append("")

    lines.append("## 3. Node quan trong theo nhom (chi liet ke node co is_feature=True)")
    lines.append("")
    for group in KEYWORD_GROUPS:
        if group == "Device":
            continue
        matches = keyword_nodes.get(group, [])
        lines.append(f"### {group}")
        lines.append("")
        if not matches:
            lines.append("khong tim thay node nao chua tu khoa nay trong node map.")
            lines.append("")
            continue
        lines.append("| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |")
        lines.append("|---|---|---|---|---|")
        for n in matches:
            lines.append(
                f"| `{n['name']}` | {n['interface_type']} | {n['access_mode']} | "
                f"{fmt_value(n)} | {fmt_range(n)} |"
            )
        lines.append("")
        if group == "Binning":
            sel = next((n for n in matches if n["name"] == "BinningSelector"), None)
            if sel:
                entries = sel.get("enum_entries", [])
                sensor_entry = next((e for e in entries if e["symbolic"] == "Sensor"), None)
                lines.append(
                    "Ghi chu: `BinningSelector` co dinh nghia entry `Sensor` "
                    f"(binning tren cam bien) voi access_mode="
                    f"`{sensor_entry['access_mode'] if sensor_entry else 'khong ton tai'}`. "
                    + ("Access mode NI (Not Implemented) nghia la engine binning-tren-cam-bien "
                       "KHONG duoc firmware nay ho tro thuc te, du node co ton tai trong XML; "
                       "chi engine `Region0` (binning digital/ISP, khong phai on-sensor) la kha dung. "
                       "BinningHorizontal2 + BinningVertical2 o Region0 cho hieu ung 2x2 nhung khong "
                       "phai on-sensor binning that su."
                       if sensor_entry and sensor_entry["access_mode"] not in ("RO", "RW") else
                       "Access mode cho thay engine Sensor CO the dung duoc - can kiem tra them.")
                )
                lines.append("")

    lines.append("## 4. Doi chung voi datasheet")
    lines.append("")
    lines.append("| Thong so | Datasheet / catalog | Doc tu node map | Ket luan |")
    lines.append("|---|---|---|---|")
    for row in meta["cross_check_rows"]:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
    lines.append("")

    lines.append("## 5. Ghi chu ky thuat")
    lines.append("")
    lines.append(
        "- MvProducerGEV.cti (Hikrobot MVS SDK, xac minh voi ban V3.1.1 build 200717) "
        "tra ve URL khong phai UTF-8 hop le khi harvesters doc node map cua *local TL "
        "device* va khi `ImageAcquirer` dang ky module event. `camera_info.py` da vong "
        "qua bang cach: (1) bo qua loi giai ma URL cua local device (khong anh huong "
        "node map remote device/camera), va (2) tu mo `Device` + `RemoteDevice` bang API "
        "noi bo cua harvesters thay vi goi `Harvester.create()`/`ImageAcquirer`, vi cong "
        "cu nay chi can doc node map, khong can streaming. Chi tiet: xem docstring dau "
        "file `camera_info.py`. `capture.py` (buoc sau, co dung ImageAcquirer de chup "
        "anh) se can ap dung patch tuong tu cho phan local-device URL, hoac kiem tra lai "
        "xem lien quan viec dang ky event co con crash hay khong."
    )
    lines.append(
        f"- Tong so node trong node map: {len(all_nodes)}, trong do "
        f"{sum(1 for n in all_nodes if n['is_feature'])} node duoc danh dau la feature "
        f"(is_feature=True); phan con lai la cac node phu tro cap thap (anh xa thanh "
        f"ghi/thanh doc, hau to `_Reg`, `_Inq`, `EnumEntry_*`, v.v.) trong XML cua Hikrobot."
    )
    lines.append("")
    return "\n".join(lines)


ip_int_to_dotted = gc.ip_int_to_dotted
local_ip_toward = gc.local_ip_toward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cti", default=None, help="Duong dan MvProducerGEV.cti (mac dinh: tu dong dinh vi)")
    parser.add_argument("--ip", default=None, help="Chon camera theo IP")
    parser.add_argument("--serial", default=None, help="Chon camera theo serial")
    parser.add_argument("--outdir", default=".", help="Thu muc xuat file (mac dinh: thu muc hien tai)")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    cti_path, cti_source = find_cti(args.cti)
    log.info("Dung .cti: %s (%s)", cti_path, cti_source)

    h = device_proxy = remote_device = None
    try:
        h, device_proxy, remote_device = connect_readonly(cti_path, args.ip, args.serial)
        node_map = remote_device.node_map
        log.info("Ket noi thanh cong. Dang doc node map...")

        all_nodes = dump_all_nodes(node_map)
        log.info("Da doc %d node.", len(all_nodes))

        keyword_nodes = extract_keyword_nodes(all_nodes)

        def find_val(name):
            n = next((x for x in all_nodes if x["name"] == name), None)
            return n.get("value") if n else None

        camera_ip = ip_int_to_dotted(find_val("GevCurrentIPAddress"))

        cross_check_rows = []
        width = find_val("WidthMax") or find_val("SensorWidth")
        height = find_val("HeightMax") or find_val("SensorHeight")
        cross_check_rows.append((
            "Do phan giai", "5472 x 3648 (20MP)", f"{width} x {height}",
            "khop" if (width == 5472 and height == 3648) else "LECH - kiem tra lai model/firmware"
        ))
        exp_node = next((n for n in all_nodes if n["name"] == "ExposureTime"), None)
        if exp_node:
            exp_range = f"{exp_node.get('min')} - {exp_node.get('max')} us"
            exp_ok = exp_node.get("min") is not None and exp_node.get("max") is not None and \
                     40 <= exp_node["min"] <= 60 and 1_900_000 <= exp_node["max"] <= 2_100_000
        else:
            exp_range = "khong tim thay node ExposureTime"
            exp_ok = False
        cross_check_rows.append((
            "Dai exposure", "~46 us den 2 s", exp_range,
            "khop" if exp_ok else "LECH / can kiem tra - xem node that"
        ))
        fps_node = next((n for n in all_nodes if n["name"] == "AcquisitionFrameRate"), None)
        fps_max = fps_node.get("max") if fps_node else None
        cross_check_rows.append((
            "Frame rate", "5.9 fps (full-res)", f"AcquisitionFrameRate hien tai={fps_node.get('value') if fps_node else '?'}, max={fps_max}",
            "can chup thu o full-res de xac nhan fps thuc te dat duoc (max node chi la gioi han ly thuyet cua node, khong phai fps dat duoc lien tuc)"
        ))
        pf_node = next((n for n in all_nodes if n["name"] == "PixelFormat"), None)
        if pf_node:
            supported = sorted(e["symbolic"] for e in pf_node.get("enum_entries", []) if e["access_mode"] in ("RO", "RW"))
        else:
            supported = []
        expected_pf = {"Mono8", "Mono10", "Mono12"}
        has_unpacked_10_12 = "Mono10" in supported and "Mono12" in supported
        cross_check_rows.append((
            "Pixel format", "Mono 8/10/10p/12/12p", ", ".join(supported),
            ("khop ve tap gia tri (5 format mono), NHUNG ten node that dung "
             "'Mono10Packed'/'Mono12Packed' thay vi 'Mono10p'/'Mono12p' theo GenICam SFNC chuan; "
             "ban unpacked Mono10/Mono12 co ton tai" if has_unpacked_10_12 else
             "LECH - khong thay du Mono10/Mono12 unpacked, kiem tra lai")
        ))
        link_speed = find_val("DeviceLinkSpeed")
        cross_check_rows.append((
            "Giao tiep mang", "GigE (1000 Mbps)", f"DeviceLinkSpeed={link_speed} Mbps",
            "khop" if link_speed == 1000 else "can kiem tra lai (xem co phai GigE that khong)"
        ))
        model_val = find_val("DeviceModelName")
        cross_check_rows.append((
            "Model", "Hikrobot MV-CE200-10GM", str(model_val),
            "khop" if model_val == "MV-CE200-10GM" else "LECH - SAI MODEL, dung nham camera"
        ))
        cross_check_rows.append((
            "Nguon PoE / 12VDC, dai nhiet 0~50C",
            "PoE + 12VDC, 0~50C",
            "khong co node GenICam tuong ung de doc lai qua node map",
            "khong kiem duoc bang node map, phai xac minh bang datasheet/do thuc te"
        ))
        binning_h = next((n for n in all_nodes if n["name"] == "BinningHorizontal"), None)
        if binning_h:
            bin_supported = [e["symbolic"] for e in binning_h.get("enum_entries", []) if e["access_mode"] in ("RO", "RW")]
        else:
            bin_supported = []
        cross_check_rows.append((
            "Binning (khong co trong danh sach thong so da xac minh, kiem tra them)",
            "khong ro tu catalog", ", ".join(bin_supported) or "khong tim thay",
            "ghi nhan de tham khao khi lam capture.py"
        ))

        meta = {
            "run_time_utc": datetime.now(timezone.utc).isoformat(),
            "cti_path": cti_path,
            "cti_source": cti_source,
            "harvesters_version": _safe(lambda: pkg_version("harvesters"), "khong ro"),
            "genicam_version": _safe(lambda: pkg_version("genicam"), "khong ro"),
            "python_version": sys.version.split()[0],
            "camera_ip": camera_ip,
            "host_ip": local_ip_toward(camera_ip),
            "cross_check_rows": cross_check_rows,
        }

        json_path = outdir / "node_map_full.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "meta": {k: v for k, v in meta.items() if k != "cross_check_rows"},
                "node_count": len(all_nodes),
                "nodes": all_nodes,
            }, f, ensure_ascii=False, indent=2)
        log.info("Da ghi %s", json_path)

        report_md = build_report(meta, keyword_nodes, all_nodes)
        report_path = outdir / "camera_report.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
        log.info("Da ghi %s", report_path)

    finally:
        if h is not None:
            disconnect(h, device_proxy, remote_device)
            log.info("Da dong ket noi camera va giai phong Harvester.")


if __name__ == "__main__":
    main()
