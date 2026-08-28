"""
gui.py - GUI Tkinter: ket noi camera, hien thi 14 tham so linearity da khoa
(chi doc), chinh tham so theo site (exposure/gain/mang), chup anh, preview.

Chi la lop giao dien mong quanh capture.py/gev_camera.py - logic thuc su
(enforce_linearity_locked, apply_site_config, single_capture, save_image,
build_metadata) khong viet lai o day.

Chay: python gui.py [--ip IP] [--serial SERIAL] [--log-dir DIR]
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np

import capture
import focus as focus_mod
import gev_camera as gc

log = logging.getLogger("gui")

TRI_BOOL_VALUES = ["(khong doi)", "True", "False"]


def _read_site_snapshot(node_map) -> dict:
    """Doc toan bo config theo site (exposure/gain/mang) khong ghi gi len
    camera - dung capture._apply_site_network voi toan None de tan dung
    lai nhanh doc-chi cua no thay vi doc tung node rieng o day."""
    _, exposure = gc.read_value(node_map, ["ExposureTime", "ExposureTimeAbs"], "ExposureTime")
    _, gain = gc.read_value(node_map, ["Gain"], "Gain")
    network = capture._apply_site_network(node_map, capture.DEFAULTS["site"]["network"])
    return {"exposure_us": {"value": exposure}, "gain_db": {"value": gain}, **network}


def _populate_result_tree(tree: ttk.Treeview, grouped: dict) -> None:
    """grouped: {ten_nhom: {ten_field: {"value": ...}}} - dung cho panel
    'Tham so khoa cung' (nhom theo A.2/A.3/A.4/A.7 + noise_reduction)."""
    tree.delete(*tree.get_children())
    for group_name, fields in grouped.items():
        if isinstance(fields, dict) and "value" in fields:
            tree.insert("", "end", text=group_name, values=(fields["value"],))
            continue
        parent = tree.insert("", "end", text=group_name, values=("",))
        for field_name, result in fields.items():
            value = result.get("value") if isinstance(result, dict) else result
            tree.insert(parent, "end", text=field_name, values=(value,))


def _populate_flat_tree(tree: ttk.Treeview, fields: dict) -> None:
    tree.delete(*tree.get_children())
    for field_name, result in fields.items():
        value = result.get("value") if isinstance(result, dict) else result
        tree.insert("", "end", text=field_name, values=(value,))


class QueueLogHandler(logging.Handler):
    """Log tu worker thread khong duoc dung truc tiep de sua widget Tk (khong
    thread-safe). Handler nay chi bo message vao Queue; main thread doc va
    hien thi trong _poll_queue()."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


class CameraGUI:
    def __init__(self, root: tk.Tk, config: dict):
        self.root = root
        self.root.title("MOR CAM - Hikrobot MV-CE200-10GM")
        self.config = config

        self.msg_queue: queue.Queue = queue.Queue()
        self.h = None
        self.ia = None
        self.node_map = None
        self.device_info: dict = {}
        self.capture_lock = threading.Lock()
        self.connecting = False
        self._preview_photo = None  # giu reference, khong de bi garbage-collect
        self._last_capture_cfg: dict | None = None  # cho nut "Thu lai" sau loi chup

        self._build_widgets()
        logging.getLogger().addHandler(QueueLogHandler(self.msg_queue))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_queue)
        self._start_connect()

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=3)
        self.root.columnconfigure(1, weight=2)
        self.root.rowconfigure(0, weight=1)

        preview_frame = ttk.Frame(self.root, padding=8)
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview_label = ttk.Label(preview_frame, relief="sunken", anchor="center",
                                        text="Chua co anh", width=60)
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        self.last_capture_var = tk.StringVar(value="")
        ttk.Label(preview_frame, textvariable=self.last_capture_var, wraplength=480).grid(
            row=1, column=0, sticky="w", pady=(4, 0))

        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=1, sticky="nsew")
        top.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Dang ket noi...")
        ttk.Label(top, textvariable=self.status_var, font=("", 10, "bold")).grid(
            row=0, column=0, sticky="w")

        self.device_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.device_var).grid(row=1, column=0, sticky="w", pady=(0, 4))

        cam = self.config["camera"]
        self.ip_var = tk.StringVar(value=cam.get("ip") or "")
        self.serial_var = tk.StringVar(value=cam.get("serial") or "")

        conn = ttk.Frame(top)
        conn.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(conn, text="IP:").pack(side="left")
        ttk.Entry(conn, textvariable=self.ip_var, width=16).pack(side="left", padx=(2, 8))
        ttk.Label(conn, text="Serial:").pack(side="left")
        ttk.Entry(conn, textvariable=self.serial_var, width=16).pack(side="left", padx=(2, 8))
        self.reconnect_btn = ttk.Button(conn, text="Ket noi lai", command=self.on_reconnect_click)
        self.reconnect_btn.pack(side="left")

        site = self.config["site"]
        out = self.config["output"]

        self.exposure_var = tk.StringVar(value=str(site["exposure_us"]))
        self.gain_var = tk.StringVar(value=str(site["gain_db"]))

        self.packet_size_var = tk.StringVar(value="")
        self.scpd_var = tk.StringVar(value="")
        self.heartbeat_timeout_var = tk.StringVar(value="")
        self.persistent_ip_var = tk.StringVar(value="")
        self.persistent_subnet_var = tk.StringVar(value="")
        self.persistent_gateway_var = tk.StringVar(value="")
        self.dhcp_var = tk.StringVar(value=TRI_BOOL_VALUES[0])
        self.persistent_ip_mode_var = tk.StringVar(value=TRI_BOOL_VALUES[0])
        self.do_not_fragment_var = tk.StringVar(value=TRI_BOOL_VALUES[0])

        self.outdir_var = tk.StringVar(value=out["dir"])
        self.also_save_npy_var = tk.BooleanVar(value=bool(out["also_save_npy"]))
        self.write_metadata_json_var = tk.BooleanVar(value=bool(out["write_metadata_json"]))

        notebook = ttk.Notebook(top)
        notebook.grid(row=3, column=0, sticky="nsew")
        top.rowconfigure(3, weight=1)

        def row(parent, label, widget_factory, r):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(4, 4))
            w = widget_factory()
            w.grid(row=r, column=1, sticky="w", pady=2)
            return w

        # -- Tab: Tham so khoa cung (chi doc) ---------------------------
        tab_locked = ttk.Frame(notebook, padding=8)
        notebook.add(tab_locked, text="Tham so khoa cung")
        ttk.Label(tab_locked, text="14 node linearity - khoa cung, khong sua duoc o day.").pack(
            anchor="w", pady=(0, 4))
        self.locked_tree = ttk.Treeview(tab_locked, columns=("value",), show="tree headings", height=16)
        self.locked_tree.heading("#0", text="Tham so")
        self.locked_tree.heading("value", text="Gia tri")
        self.locked_tree.column("#0", width=220)
        self.locked_tree.column("value", width=160)
        self.locked_tree.pack(fill="both", expand=True)

        # -- Tab: Site ----------------------------------------------------
        tab_site = ttk.Frame(notebook, padding=8)
        notebook.add(tab_site, text="Site")

        site_edit = ttk.LabelFrame(tab_site, text="Ap dung moi lan chup", padding=6)
        site_edit.pack(fill="x", pady=(0, 8))
        row(site_edit, "Exposure (us):", lambda: ttk.Entry(site_edit, textvariable=self.exposure_var, width=17), 0)
        row(site_edit, "Gain (dB):", lambda: ttk.Entry(site_edit, textvariable=self.gain_var, width=17), 1)

        net_edit = ttk.LabelFrame(tab_site, text="Mang - de trong = khong doi", padding=6)
        net_edit.pack(fill="x", pady=(0, 8))
        row(net_edit, "Packet size:", lambda: ttk.Entry(net_edit, textvariable=self.packet_size_var, width=17), 0)
        row(net_edit, "SCPD:", lambda: ttk.Entry(net_edit, textvariable=self.scpd_var, width=17), 1)
        row(net_edit, "Heartbeat timeout (ms):",
            lambda: ttk.Entry(net_edit, textvariable=self.heartbeat_timeout_var, width=17), 2)
        row(net_edit, "Persistent IP:",
            lambda: ttk.Entry(net_edit, textvariable=self.persistent_ip_var, width=17), 3)
        row(net_edit, "Persistent subnet:",
            lambda: ttk.Entry(net_edit, textvariable=self.persistent_subnet_var, width=17), 4)
        row(net_edit, "Persistent gateway:",
            lambda: ttk.Entry(net_edit, textvariable=self.persistent_gateway_var, width=17), 5)
        row(net_edit, "DHCP:", lambda: ttk.Combobox(
            net_edit, textvariable=self.dhcp_var, values=TRI_BOOL_VALUES, width=15, state="readonly"), 6)
        row(net_edit, "Persistent IP mode:", lambda: ttk.Combobox(
            net_edit, textvariable=self.persistent_ip_mode_var, values=TRI_BOOL_VALUES, width=15, state="readonly"), 7)
        row(net_edit, "Do not fragment:", lambda: ttk.Combobox(
            net_edit, textvariable=self.do_not_fragment_var, values=TRI_BOOL_VALUES, width=15, state="readonly"), 8)

        site_current = ttk.LabelFrame(tab_site, text="Gia tri hien tai tren camera", padding=6)
        site_current.pack(fill="both", expand=True)
        self.site_current_tree = ttk.Treeview(site_current, columns=("value",), show="tree headings", height=9)
        self.site_current_tree.heading("#0", text="Tham so")
        self.site_current_tree.heading("value", text="Gia tri")
        self.site_current_tree.column("#0", width=220)
        self.site_current_tree.column("value", width=160)
        self.site_current_tree.pack(fill="both", expand=True)

        # -- Tab: Output -------------------------------------------------
        tab_out = ttk.Frame(notebook, padding=8)
        notebook.add(tab_out, text="Output")
        r = 0
        row(tab_out, "Thu muc luu:", lambda: ttk.Entry(tab_out, textvariable=self.outdir_var, width=32), r)
        r += 1
        ttk.Checkbutton(tab_out, text="Luu them .npy", variable=self.also_save_npy_var).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1
        ttk.Checkbutton(tab_out, text="Ghi metadata .json", variable=self.write_metadata_json_var).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=2)

        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=4, column=0, sticky="ew", pady=8)
        self.capture_btn = ttk.Button(btn_frame, text="Chup anh", command=self.on_capture_click)
        self.capture_btn.pack(side="left")
        self.retry_btn = ttk.Button(btn_frame, text="Thu lai chup", command=self.on_retry_click,
                                     state="disabled")
        self.retry_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btn_frame, text="Lam moi / khoa lai tu camera",
                   command=self.on_refresh_click).pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(top, text="Log", padding=4)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        top.rowconfigure(5, weight=1)
        self.log_text = tk.Text(log_frame, height=12, width=48, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------ connect
    def _start_connect(self) -> None:
        self.connecting = True
        self.reconnect_btn.config(state="disabled")
        self.status_var.set("Dang ket noi...")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        try:
            if self.h is not None:
                gc.disconnect_control(self.h, self.ia)
                self.h = self.ia = self.node_map = None
            cti_path = capture.cti_path_for_platform(self.config)
            ip = self.ip_var.get().strip() or None
            serial = self.serial_var.get().strip() or None
            self.h, self.ia = gc.connect_control(cti_path, ip, serial)
            self.node_map = self.ia.remote_device.node_map
            self.device_info = capture.read_device_info(self.node_map)

            locked_results = None
            try:
                locked_results = capture.enforce_linearity_locked(self.node_map)
            except gc.ParameterError as e:
                log.error("Khong khoa duoc tham so tuyen tinh khi ket noi: %s", e)

            site_snapshot = _read_site_snapshot(self.node_map)
            self.msg_queue.put(("connected", self.device_info, locked_results, site_snapshot))
        except Exception as e:
            self.msg_queue.put(("connect_error", str(e)))

    def on_reconnect_click(self) -> None:
        if self.connecting or not self.capture_lock.acquire(blocking=False):
            return  # dang ket noi hoac dang chup do, bo qua
        self.capture_lock.release()
        self._start_connect()

    def on_refresh_click(self) -> None:
        if self.node_map is None:
            messagebox.showwarning("Chua ket noi", "Camera chua ket noi xong.")
            return
        try:
            locked_results = capture.enforce_linearity_locked(self.node_map)
            _populate_result_tree(self.locked_tree, locked_results)
            site_snapshot = _read_site_snapshot(self.node_map)
            _populate_flat_tree(self.site_current_tree, site_snapshot)
            self.status_var.set("Da lam moi / khoa lai tham so tu camera.")
        except gc.ParameterError as e:
            messagebox.showerror("Khong khoa duoc tham so", str(e))
        except Exception as e:
            messagebox.showerror("Loi", str(e))

    # ------------------------------------------------------------ capture
    def on_capture_click(self) -> None:
        if self.ia is None or self.node_map is None:
            messagebox.showwarning("Chua ket noi", "Camera chua ket noi xong.")
            return
        if not self.capture_lock.acquire(blocking=False):
            return  # dang chup do, bo qua click them

        try:
            cfg = self._build_config_from_fields()
        except ValueError as e:
            self.capture_lock.release()
            messagebox.showerror("Thong so khong hop le", str(e))
            return

        self._run_capture(cfg)

    def on_retry_click(self) -> None:
        """Chay lai lenh chup voi dung thong so cua lan chup bi loi gan nhat,
        khong can nguoi dung dien lai form."""
        if self._last_capture_cfg is None:
            return
        if self.ia is None or self.node_map is None:
            messagebox.showwarning("Chua ket noi", "Camera chua ket noi xong.")
            return
        if not self.capture_lock.acquire(blocking=False):
            return
        self._run_capture(self._last_capture_cfg)

    def _run_capture(self, cfg: dict) -> None:
        self.capture_btn.config(state="disabled")
        self.retry_btn.config(state="disabled")
        self.status_var.set("Dang chup...")
        threading.Thread(target=self._capture_worker, args=(cfg,), daemon=True).start()

    def _build_config_from_fields(self) -> dict:
        cfg = copy.deepcopy(self.config)
        try:
            exposure_us = float(self.exposure_var.get())
            gain_db = float(self.gain_var.get())
            packet_size = self._parse_optional_int(self.packet_size_var.get())
            scpd = self._parse_optional_int(self.scpd_var.get())
            heartbeat_timeout_ms = self._parse_optional_int(self.heartbeat_timeout_var.get())
        except ValueError:
            raise ValueError("Exposure/Gain/Packet size/SCPD/Heartbeat phai la so hop le.")

        cfg["site"]["exposure_us"] = exposure_us
        cfg["site"]["gain_db"] = gain_db
        cfg["site"]["network"] = {
            "packet_size": packet_size,
            "scpd": scpd,
            "persistent_ip": self.persistent_ip_var.get().strip() or None,
            "persistent_subnet": self.persistent_subnet_var.get().strip() or None,
            "persistent_gateway": self.persistent_gateway_var.get().strip() or None,
            "dhcp": self._parse_tri_bool(self.dhcp_var.get()),
            "persistent_ip_mode": self._parse_tri_bool(self.persistent_ip_mode_var.get()),
            "do_not_fragment": self._parse_tri_bool(self.do_not_fragment_var.get()),
            "heartbeat_timeout_ms": heartbeat_timeout_ms,
        }

        cfg["output"]["dir"] = self.outdir_var.get() or "./captures"
        cfg["output"]["also_save_npy"] = self.also_save_npy_var.get()
        cfg["output"]["write_metadata_json"] = self.write_metadata_json_var.get()
        return cfg

    @staticmethod
    def _parse_optional_int(text: str) -> int | None:
        text = text.strip()
        return int(text) if text else None

    @staticmethod
    def _parse_tri_bool(text: str) -> bool | None:
        return {"True": True, "False": False}.get(text)

    def _capture_worker(self, cfg: dict) -> None:
        try:
            locked_results = capture.enforce_linearity_locked(self.node_map)
            self.msg_queue.put(("locked_results", locked_results))
            site_results = capture.apply_site_config(self.node_map, cfg)
            arr, capture_info = capture.single_capture(self.ia, self.node_map, cfg)

            out_dir = Path(cfg["output"]["dir"])
            base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            save_info = capture.save_image(arr, capture_info, out_dir, base_name, cfg)

            if cfg["output"]["write_metadata_json"]:
                meta = capture.build_metadata(self.device_info, locked_results, site_results)
                with open(out_dir / f"{base_name}.json", "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2, default=str)

            dtype = capture.UNPACKED_DTYPE[capture_info["data_format"]]
            image = arr.view(dtype).reshape(capture_info["height"], capture_info["width"])
            preview_u8 = focus_mod.to_u8_preview(image, downscale=8)

            self.msg_queue.put(("captured", save_info, preview_u8, site_results))
        except gc.ParameterError as e:
            self.msg_queue.put(("capture_error", f"Khong the khoa/ap dung tham so: {e}", cfg))
        except Exception as e:
            self.msg_queue.put(("capture_error", str(e), cfg))
        finally:
            self.capture_lock.release()

    # ------------------------------------------------- main-thread polling
    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "connected":
                    _, device_info, locked_results, site_snapshot = item
                    self.connecting = False
                    self.reconnect_btn.config(state="normal")
                    self.device_var.set(
                        f"Model={device_info.get('model')}  Serial={device_info.get('serial')}  "
                        f"FW={device_info.get('firmware')}")
                    if locked_results is not None:
                        _populate_result_tree(self.locked_tree, locked_results)
                        self.status_var.set("Da ket noi camera, tham so tuyen tinh da khoa.")
                    else:
                        self.status_var.set("Da ket noi nhung KHONG khoa duoc tham so tuyen tinh - xem log.")
                    _populate_flat_tree(self.site_current_tree, site_snapshot)
                    if site_snapshot["exposure_us"]["value"] is not None:
                        self.exposure_var.set(str(site_snapshot["exposure_us"]["value"]))
                    if site_snapshot["gain_db"]["value"] is not None:
                        self.gain_var.set(str(site_snapshot["gain_db"]["value"]))
                elif kind == "connect_error":
                    self.connecting = False
                    self.reconnect_btn.config(state="normal")
                    self.status_var.set("Loi ket noi camera. Sua IP/Serial roi bam 'Ket noi lai' neu can.")
                    messagebox.showerror("Loi ket noi", item[1])
                elif kind == "locked_results":
                    _populate_result_tree(self.locked_tree, item[1])
                elif kind == "captured":
                    save_info, preview_u8, site_results = item[1], item[2], item[3]
                    self.status_var.set("Chup xong.")
                    self.last_capture_var.set(
                        f"{save_info['image_path']}  ({save_info['dtype']}, {save_info['shape']}, "
                        f"min={save_info['min']} max={save_info['max']})")
                    _populate_flat_tree(self.site_current_tree, site_results)
                    self._show_preview(preview_u8)
                    self.capture_btn.config(state="normal")
                    self.retry_btn.config(state="disabled")
                    self._last_capture_cfg = None
                elif kind == "capture_error":
                    err, cfg = item[1], item[2]
                    self.status_var.set("Loi khi chup. Co the bam 'Thu lai chup'.")
                    self.capture_btn.config(state="normal")
                    self._last_capture_cfg = cfg
                    self.retry_btn.config(state="normal")
                    messagebox.showerror("Loi chup anh", err)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _append_log(self, text: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _show_preview(self, image_u8: np.ndarray) -> None:
        max_w = 640
        h, w = image_u8.shape[:2]
        if w > max_w:
            scale = max_w / w
            image_u8 = cv2.resize(image_u8, (max_w, int(h * scale)))
        ok, buf = cv2.imencode(".png", image_u8)
        if not ok:
            return
        self._preview_photo = tk.PhotoImage(data=buf.tobytes())
        self.preview_label.config(image=self._preview_photo, text="")

    # -------------------------------------------------------------- close
    def on_close(self) -> None:
        if self.h is not None:
            try:
                gc.disconnect_control(self.h, self.ia)
            except Exception as e:
                log.warning("loi khi dong ket noi: %s", e)
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default=None, help="IP camera. Co the doi lai trong cua so.")
    parser.add_argument("--serial", default=None, help="Serial camera. Co the doi lai trong cua so.")
    parser.add_argument("--log-dir", default=capture.DEFAULT_LOG_DIR,
                         help="Thu muc ghi file log chi tiet. Truyen '' de tat file log.")
    args = parser.parse_args()

    config = copy.deepcopy(capture.DEFAULTS)
    config["camera"]["ip"] = args.ip
    config["camera"]["serial"] = args.serial

    if args.log_dir:
        try:
            log_file = capture.add_file_logging(args.log_dir, "gui")
            log.info("Ghi log chi tiet (muc DEBUG) vao: %s", log_file)
        except OSError as e:
            log.warning("Khong tao duoc file log (%s), tiep tuc chi voi console.", e)

    root = tk.Tk()
    root.geometry("1200x760")
    CameraGUI(root, config)
    root.mainloop()


if __name__ == "__main__":
    main()
