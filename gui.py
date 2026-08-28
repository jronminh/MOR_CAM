"""
gui.py - GUI Tkinter don gian: theo doi trang thai camera, chinh toan bo
thong so co the chinh duoc ma capture.py/gev_camera.py ho tro, an chup,
preview anh vua chup.

Chi la lop giao dien mong. Toan bo logic (enforce_linear, apply_adjustable,
single_capture, save_image, build_metadata) lay truc tiep tu capture.py/
gev_camera.py - da test tren camera that, KHONG viet lai o day.

Chay: python gui.py [--config config.yaml] [--log-dir DIR]

Ghi file log giong het capture.py (dung chung capture.add_file_logging(),
xem README.md muc "File log") - vao logs/gui_<timestamp>.log, muc DEBUG.
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

        acq = self.config["acquisition"]
        lin = self.config["enforce_linear"]
        bl = self.config["black_level"]
        roi = self.config["roi"]
        out = self.config["output"]

        self.pixel_format_var = tk.StringVar(value=acq["pixel_format"])
        self.exposure_var = tk.StringVar(value=str(acq["exposure_us"]))
        self.gain_var = tk.StringVar(value=str(acq["gain_db"]))
        self.binning_h_var = tk.StringVar(value=str(acq["binning_h"]))
        self.binning_v_var = tk.StringVar(value=str(acq["binning_v"]))

        self.gamma_enable_var = tk.BooleanVar(value=bool(lin["gamma_enable"]))
        self.auto_exposure_var = tk.StringVar(value=lin["auto_exposure"])
        self.auto_gain_var = tk.StringVar(value=lin["auto_gain"])
        self.lut_enable_var = tk.BooleanVar(value=bool(lin["lut_enable"]))
        self.exposure_mode_var = tk.StringVar(value=lin["exposure_mode"])

        self.black_level_mode_var = tk.StringVar(value=bl["mode"])

        self.roi_width_var = tk.StringVar(value="" if roi.get("width") is None else str(roi["width"]))
        self.roi_height_var = tk.StringVar(value="" if roi.get("height") is None else str(roi["height"]))
        self.roi_x_offset_var = tk.StringVar(value=str(roi.get("x_offset") or 0))
        self.roi_y_offset_var = tk.StringVar(value=str(roi.get("y_offset") or 0))

        self.outdir_var = tk.StringVar(value=out["dir"])
        self.also_save_npy_var = tk.BooleanVar(value=bool(out["also_save_npy"]))
        self.write_metadata_json_var = tk.BooleanVar(value=bool(out["write_metadata_json"]))

        notebook = ttk.Notebook(top)
        notebook.grid(row=3, column=0, sticky="ew")

        def row(parent, label, widget_factory, r):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2, padx=(4, 4))
            w = widget_factory()
            w.grid(row=r, column=1, sticky="w", pady=2)
            return w

        # -- Tab: Acquisition ------------------------------------------------
        tab_acq = ttk.Frame(notebook, padding=8)
        notebook.add(tab_acq, text="Acquisition")
        r = 0
        row(tab_acq, "Pixel format:", lambda: ttk.Combobox(
            tab_acq, textvariable=self.pixel_format_var,
            values=["Mono8", "Mono10", "Mono12"], width=15, state="readonly"), r)
        r += 1
        row(tab_acq, "Exposure (us):", lambda: ttk.Entry(tab_acq, textvariable=self.exposure_var, width=17), r)
        r += 1
        row(tab_acq, "Gain (dB):", lambda: ttk.Entry(tab_acq, textvariable=self.gain_var, width=17), r)
        r += 1
        ttk.Label(tab_acq, text="Binning H / V:").grid(row=r, column=0, sticky="w", pady=2, padx=(4, 4))
        bf = ttk.Frame(tab_acq)
        bf.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Combobox(bf, textvariable=self.binning_h_var, values=["1", "2", "4"],
                     width=6, state="readonly").pack(side="left")
        ttk.Combobox(bf, textvariable=self.binning_v_var, values=["1", "2", "4"],
                     width=6, state="readonly").pack(side="left", padx=(4, 0))

        # -- Tab: Linearity (ISP) --------------------------------------------
        tab_lin = ttk.Frame(notebook, padding=8)
        notebook.add(tab_lin, text="Linearity / ISP")
        r = 0
        ttk.Checkbutton(tab_lin, text="Gamma enable", variable=self.gamma_enable_var).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1
        ttk.Checkbutton(tab_lin, text="LUT enable", variable=self.lut_enable_var).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1
        row(tab_lin, "Auto exposure:", lambda: ttk.Combobox(
            tab_lin, textvariable=self.auto_exposure_var,
            values=["Off", "Once", "Continuous"], width=15, state="readonly"), r)
        r += 1
        row(tab_lin, "Auto gain:", lambda: ttk.Combobox(
            tab_lin, textvariable=self.auto_gain_var,
            values=["Off", "Once", "Continuous"], width=15, state="readonly"), r)
        r += 1
        row(tab_lin, "Exposure mode:", lambda: ttk.Combobox(
            tab_lin, textvariable=self.exposure_mode_var,
            values=["Timed"], width=15, state="readonly"), r)

        # -- Tab: Black level & ROI ------------------------------------------
        tab_roi = ttk.Frame(notebook, padding=8)
        notebook.add(tab_roi, text="Black level / ROI")
        r = 0
        row(tab_roi, "Black level:", lambda: ttk.Combobox(
            tab_roi, textvariable=self.black_level_mode_var,
            values=["keep_and_record", "set_zero"], width=15, state="readonly"), r)
        r += 1
        row(tab_roi, "ROI width (trong = full):", lambda: ttk.Entry(
            tab_roi, textvariable=self.roi_width_var, width=17), r)
        r += 1
        row(tab_roi, "ROI height (trong = full):", lambda: ttk.Entry(
            tab_roi, textvariable=self.roi_height_var, width=17), r)
        r += 1
        row(tab_roi, "ROI offset X:", lambda: ttk.Entry(
            tab_roi, textvariable=self.roi_x_offset_var, width=17), r)
        r += 1
        row(tab_roi, "ROI offset Y:", lambda: ttk.Entry(
            tab_roi, textvariable=self.roi_y_offset_var, width=17), r)

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
        ttk.Button(btn_frame, text="Lam moi thong so tu camera",
                   command=self.on_refresh_click).pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(top, text="Log", padding=4)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        top.rowconfigure(5, weight=1)
        self.log_text = tk.Text(log_frame, height=16, width=48, state="disabled")
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
            self.msg_queue.put(("connected", self.device_info))
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
            _, pf = gc.read_value(self.node_map, ["PixelFormat"], "PixelFormat")
            _, exp = gc.read_value(self.node_map, ["ExposureTime"], "ExposureTime")
            _, gain = gc.read_value(self.node_map, ["Gain"], "Gain")
            _, bh = gc.read_value(self.node_map, ["BinningHorizontal"], "BinningHorizontal")
            _, bv = gc.read_value(self.node_map, ["BinningVertical"], "BinningVertical")
            _, gamma = gc.read_value(self.node_map, ["GammaEnable"], "GammaEnable")
            _, auto_exp = gc.read_value(self.node_map, ["ExposureAuto"], "ExposureAuto")
            _, auto_gain = gc.read_value(self.node_map, ["GainAuto"], "GainAuto")
            _, lut = gc.read_value(self.node_map, ["LUTEnable"], "LUTEnable")
            _, exp_mode = gc.read_value(self.node_map, ["ExposureMode"], "ExposureMode")
            _, width = gc.read_value(self.node_map, ["Width"], "Width")
            _, height = gc.read_value(self.node_map, ["Height"], "Height")
            _, x_off = gc.read_value(self.node_map, ["OffsetX"], "OffsetX")
            _, y_off = gc.read_value(self.node_map, ["OffsetY"], "OffsetY")

            if pf:
                self.pixel_format_var.set(pf)
            if exp is not None:
                self.exposure_var.set(str(exp))
            if gain is not None:
                self.gain_var.set(str(gain))
            if bh:
                self.binning_h_var.set(bh.replace("BinningHorizontal", ""))
            if bv:
                self.binning_v_var.set(bv.replace("BinningVertical", ""))
            if gamma is not None:
                self.gamma_enable_var.set(bool(gamma))
            if auto_exp:
                self.auto_exposure_var.set(auto_exp)
            if auto_gain:
                self.auto_gain_var.set(auto_gain)
            if lut is not None:
                self.lut_enable_var.set(bool(lut))
            if exp_mode:
                self.exposure_mode_var.set(exp_mode)
            if width is not None:
                self.roi_width_var.set(str(width))
            if height is not None:
                self.roi_height_var.set(str(height))
            if x_off is not None:
                self.roi_x_offset_var.set(str(x_off))
            if y_off is not None:
                self.roi_y_offset_var.set(str(y_off))
            self.status_var.set("Da lam moi thong so tu camera.")
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
            binning_h = int(self.binning_h_var.get())
            binning_v = int(self.binning_v_var.get())
            roi_width = self._parse_optional_int(self.roi_width_var.get())
            roi_height = self._parse_optional_int(self.roi_height_var.get())
            roi_x_offset = int(self.roi_x_offset_var.get() or 0)
            roi_y_offset = int(self.roi_y_offset_var.get() or 0)
        except ValueError:
            raise ValueError("Exposure/Gain/Binning/ROI phai la so hop le.")

        cfg["acquisition"]["pixel_format"] = self.pixel_format_var.get()
        cfg["acquisition"]["exposure_us"] = exposure_us
        cfg["acquisition"]["gain_db"] = gain_db
        cfg["acquisition"]["binning_h"] = binning_h
        cfg["acquisition"]["binning_v"] = binning_v

        cfg["enforce_linear"]["gamma_enable"] = self.gamma_enable_var.get()
        cfg["enforce_linear"]["auto_exposure"] = self.auto_exposure_var.get()
        cfg["enforce_linear"]["auto_gain"] = self.auto_gain_var.get()
        cfg["enforce_linear"]["lut_enable"] = self.lut_enable_var.get()
        cfg["enforce_linear"]["exposure_mode"] = self.exposure_mode_var.get()

        cfg["black_level"]["mode"] = self.black_level_mode_var.get()

        cfg["roi"]["width"] = roi_width
        cfg["roi"]["height"] = roi_height
        cfg["roi"]["x_offset"] = roi_x_offset
        cfg["roi"]["y_offset"] = roi_y_offset

        cfg["output"]["dir"] = self.outdir_var.get() or "./captures"
        cfg["output"]["also_save_npy"] = self.also_save_npy_var.get()
        cfg["output"]["write_metadata_json"] = self.write_metadata_json_var.get()
        return cfg

    @staticmethod
    def _parse_optional_int(text: str) -> int | None:
        text = text.strip()
        return int(text) if text else None

    def _capture_worker(self, cfg: dict) -> None:
        try:
            linear_results = capture.enforce_linear(self.node_map, cfg)
            adjustable_results = capture.apply_adjustable(self.node_map, cfg)
            arr, capture_info = capture.single_capture(self.ia, self.node_map, cfg)

            out_dir = Path(cfg["output"]["dir"])
            base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            save_info = capture.save_image(arr, capture_info, out_dir, base_name, cfg)

            if cfg["output"]["write_metadata_json"]:
                meta = capture.build_metadata(
                    self.device_info, linear_results, adjustable_results, capture_info, save_info)
                with open(out_dir / f"{base_name}.json", "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2, default=str)

            dtype = capture.UNPACKED_DTYPE[capture_info["data_format"]]
            image = arr.view(dtype).reshape(capture_info["height"], capture_info["width"])
            preview_u8 = focus_mod.to_u8_preview(image, downscale=8)

            self.msg_queue.put(("captured", save_info, preview_u8))
        except gc.ParameterError as e:
            self.msg_queue.put(("capture_error", f"Khong the ep/ap dung thong so: {e}", cfg))
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
                    info = item[1]
                    self.connecting = False
                    self.reconnect_btn.config(state="normal")
                    self.status_var.set("Da ket noi camera.")
                    self.device_var.set(
                        f"Model={info.get('model')}  Serial={info.get('serial')}  FW={info.get('firmware')}")
                    self.on_refresh_click()
                elif kind == "connect_error":
                    self.connecting = False
                    self.reconnect_btn.config(state="normal")
                    self.status_var.set("Loi ket noi camera. Sua IP/Serial roi bam 'Ket noi lai' neu can.")
                    messagebox.showerror("Loi ket noi", item[1])
                elif kind == "captured":
                    save_info, preview_u8 = item[1], item[2]
                    self.status_var.set("Chup xong.")
                    self.last_capture_var.set(
                        f"{save_info['image_path']}  ({save_info['dtype']}, {save_info['shape']}, "
                        f"min={save_info['min']} max={save_info['max']})")
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
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-dir", default=None,
                         help="Ghi de logging.dir trong config. Truyen '' de tat file log.")
    args = parser.parse_args()

    config = capture.load_config(args.config)

    # Dung chung ham/backend voi capture.py (khong phan biet log tu dau ra -
    # cung mot he thong logging Python, cung dinh dang, cung DEFAULT_LOG_DIR).
    log_dir = args.log_dir if args.log_dir is not None else config["logging"]["dir"]
    if config["logging"]["enable"] and log_dir:
        try:
            log_file = capture.add_file_logging(log_dir, "gui")
            log.info("Ghi log chi tiet (muc DEBUG) vao: %s", log_file)
        except OSError as e:
            log.warning("Khong tao duoc file log (%s), tiep tuc chi voi console.", e)

    root = tk.Tk()
    root.geometry("1200x760")
    CameraGUI(root, config)
    root.mainloop()


if __name__ == "__main__":
    main()
