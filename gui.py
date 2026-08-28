"""
gui.py - GUI Tkinter don gian: theo doi trang thai camera, chinh cac thong
so co ban, an chup, preview anh vua chup.

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
        self._preview_photo = None  # giu reference, khong de bi garbage-collect

        self._build_widgets()
        logging.getLogger().addHandler(QueueLogHandler(self.msg_queue))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_queue)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Dang ket noi...")
        ttk.Label(top, textvariable=self.status_var, font=("", 10, "bold")).grid(
            row=0, column=0, sticky="w")

        self.device_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.device_var).grid(row=1, column=0, sticky="w", pady=(0, 8))

        acq = self.config["acquisition"]
        self.pixel_format_var = tk.StringVar(value=acq["pixel_format"])
        self.exposure_var = tk.StringVar(value=str(acq["exposure_us"]))
        self.gain_var = tk.StringVar(value=str(acq["gain_db"]))
        self.binning_h_var = tk.StringVar(value=str(acq["binning_h"]))
        self.binning_v_var = tk.StringVar(value=str(acq["binning_v"]))
        self.black_level_mode_var = tk.StringVar(value=self.config["black_level"]["mode"])
        self.outdir_var = tk.StringVar(value=self.config["output"]["dir"])

        params = ttk.LabelFrame(top, text="Thong so", padding=8)
        params.grid(row=2, column=0, sticky="ew")

        def row(label, widget_factory, r):
            ttk.Label(params, text=label).grid(row=r, column=0, sticky="w", pady=2)
            w = widget_factory()
            w.grid(row=r, column=1, sticky="w", pady=2)
            return w

        r = 0
        row("Pixel format:", lambda: ttk.Combobox(
            params, textvariable=self.pixel_format_var,
            values=["Mono8", "Mono10", "Mono12"], width=15, state="readonly"), r)
        r += 1
        row("Exposure (us):", lambda: ttk.Entry(params, textvariable=self.exposure_var, width=17), r)
        r += 1
        row("Gain (dB):", lambda: ttk.Entry(params, textvariable=self.gain_var, width=17), r)
        r += 1

        ttk.Label(params, text="Binning H / V:").grid(row=r, column=0, sticky="w", pady=2)
        bf = ttk.Frame(params)
        bf.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Combobox(bf, textvariable=self.binning_h_var, values=["1", "2", "4"],
                     width=6, state="readonly").pack(side="left")
        ttk.Combobox(bf, textvariable=self.binning_v_var, values=["1", "2", "4"],
                     width=6, state="readonly").pack(side="left", padx=(4, 0))
        r += 1

        row("Black level:", lambda: ttk.Combobox(
            params, textvariable=self.black_level_mode_var,
            values=["keep_and_record", "set_zero"], width=15, state="readonly"), r)
        r += 1
        row("Thu muc luu:", lambda: ttk.Entry(params, textvariable=self.outdir_var, width=32), r)

        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=3, column=0, sticky="ew", pady=8)
        self.capture_btn = ttk.Button(btn_frame, text="Chup anh", command=self.on_capture_click)
        self.capture_btn.pack(side="left")
        ttk.Button(btn_frame, text="Lam moi thong so tu camera",
                   command=self.on_refresh_click).pack(side="left", padx=(8, 0))

        self.preview_label = ttk.Label(top, relief="sunken", anchor="center", text="Chua co anh")
        self.preview_label.grid(row=4, column=0, sticky="nsew", pady=(4, 4))
        top.rowconfigure(4, weight=1)

        self.last_capture_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.last_capture_var, wraplength=560).grid(
            row=5, column=0, sticky="w")

        log_frame = ttk.LabelFrame(top, text="Log", padding=4)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(8, 0))
        top.rowconfigure(6, weight=1)
        self.log_text = tk.Text(log_frame, height=8, width=72, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------ connect
    def _connect_worker(self) -> None:
        try:
            cti_path = capture.cti_path_for_platform(self.config)
            self.h, self.ia = gc.connect_control(
                cti_path, self.config["camera"].get("ip"), self.config["camera"].get("serial"))
            self.node_map = self.ia.remote_device.node_map
            self.device_info = capture.read_device_info(self.node_map)
            self.msg_queue.put(("connected", self.device_info))
        except Exception as e:
            self.msg_queue.put(("connect_error", str(e)))

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

        self.capture_btn.config(state="disabled")
        self.status_var.set("Dang chup...")
        threading.Thread(target=self._capture_worker, args=(cfg,), daemon=True).start()

    def _build_config_from_fields(self) -> dict:
        cfg = copy.deepcopy(self.config)
        try:
            exposure_us = float(self.exposure_var.get())
            gain_db = float(self.gain_var.get())
            binning_h = int(self.binning_h_var.get())
            binning_v = int(self.binning_v_var.get())
        except ValueError:
            raise ValueError("Exposure/Gain/Binning phai la so hop le.")
        cfg["acquisition"]["pixel_format"] = self.pixel_format_var.get()
        cfg["acquisition"]["exposure_us"] = exposure_us
        cfg["acquisition"]["gain_db"] = gain_db
        cfg["acquisition"]["binning_h"] = binning_h
        cfg["acquisition"]["binning_v"] = binning_v
        cfg["black_level"]["mode"] = self.black_level_mode_var.get()
        cfg["output"]["dir"] = self.outdir_var.get() or "./captures"
        return cfg

    def _capture_worker(self, cfg: dict) -> None:
        try:
            linear_results = capture.enforce_linear(self.node_map, cfg)
            adjustable_results = capture.apply_adjustable(self.node_map, cfg)
            arr, capture_info = capture.single_capture(self.ia, self.node_map, cfg)

            out_dir = Path(cfg["output"]["dir"])
            base_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            save_info = capture.save_image(arr, capture_info, out_dir, base_name, cfg)

            meta = capture.build_metadata(
                self.device_info, linear_results, adjustable_results, capture_info, save_info)
            with open(out_dir / f"{base_name}.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2, default=str)

            dtype = capture.UNPACKED_DTYPE[capture_info["data_format"]]
            image = arr.view(dtype).reshape(capture_info["height"], capture_info["width"])
            preview_u8 = focus_mod.to_u8_preview(image, downscale=8)

            self.msg_queue.put(("captured", save_info, preview_u8))
        except gc.ParameterError as e:
            self.msg_queue.put(("capture_error", f"Khong the ep/ap dung thong so: {e}"))
        except Exception as e:
            self.msg_queue.put(("capture_error", str(e)))
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
                    self.status_var.set("Da ket noi camera.")
                    self.device_var.set(
                        f"Model={info.get('model')}  Serial={info.get('serial')}  FW={info.get('firmware')}")
                    self.on_refresh_click()
                elif kind == "connect_error":
                    self.status_var.set("Loi ket noi camera.")
                    messagebox.showerror("Loi ket noi", item[1])
                elif kind == "captured":
                    save_info, preview_u8 = item[1], item[2]
                    self.status_var.set("Chup xong.")
                    self.last_capture_var.set(
                        f"{save_info['image_path']}  ({save_info['dtype']}, {save_info['shape']}, "
                        f"min={save_info['min']} max={save_info['max']})")
                    self._show_preview(preview_u8)
                    self.capture_btn.config(state="normal")
                elif kind == "capture_error":
                    self.status_var.set("Loi khi chup.")
                    self.capture_btn.config(state="normal")
                    messagebox.showerror("Loi chup anh", item[1])
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
    CameraGUI(root, config)
    root.mainloop()


if __name__ == "__main__":
    main()
