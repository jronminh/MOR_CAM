"""
gui.py - form nhap tham so CLI cua capture.py + nut Run/Stop, khong tu
ket noi camera trong tien trinh GUI. Moi lan Run se spawn
`python capture.py <flags>` nhu chay tay ngoai terminal, log stdout/stderr
stream vao o log ben duoi. O nao de trong thi flag tuong ung bi bo qua,
capture.py tu ap dung default cua no.

Chay: python gui.py
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

CAPTURE_PY_PATH = Path(__file__).resolve().parent / "capture.py"

TRI_BOOL_VALUES = ["", "True", "False"]
IMAGE_FORMAT_VALUES = ["", "tiff16", "npy"]

# (nhom, nhan, kieu, prefill, flag_true, flag_false)
# kieu "entry": gia tri go vao duoc dua thang sau flag_true, de trong = bo qua flag.
# kieu "tribool"/"choice": Combobox; de trong ("") = bo qua ca flag_true (va flag_false neu co).
ARG_SPECS = [
    ("Camera", "IP", "entry", "", "--ip", None),
    ("Camera", "Serial", "entry", "", "--serial", None),
    ("Camera", "CTI path", "entry", "", "--cti", None),
    ("Site", "Exposure (us)", "entry", "5000.0", "--exposure-us", None),
    ("Site", "Gain (dB)", "entry", "0.0", "--gain-db", None),
    ("Site", "Packet size", "entry", "", "--packet-size", None),
    ("Site", "SCPD", "entry", "", "--scpd", None),
    ("Site", "Persistent IP", "entry", "", "--persistent-ip", None),
    ("Site", "Persistent subnet", "entry", "", "--persistent-subnet", None),
    ("Site", "Persistent gateway", "entry", "", "--persistent-gateway", None),
    ("Site", "DHCP", "tribool", "", "--dhcp", "--no-dhcp"),
    ("Site", "Persistent IP mode", "tribool", "", "--persistent-ip-mode", "--no-persistent-ip-mode"),
    ("Site", "Do not fragment", "tribool", "", "--do-not-fragment", "--no-do-not-fragment"),
    ("Site", "Heartbeat timeout (ms)", "entry", "", "--heartbeat-timeout-ms", None),
    ("Output", "Out dir", "entry", "./captures", "--outdir", None),
    ("Output", "Image format", "choice", "", "--image-format", None),
    ("Output", "Save npy", "tribool", "", "--save-npy", "--no-save-npy"),
    ("Output", "Metadata", "tribool", "", "--metadata", "--no-metadata"),
    ("Logging", "Log dir", "entry", "./logs", "--log-dir", None),
]

GROUP_ORDER = ["Camera", "Site", "Output", "Logging"]


def _build_command(fields: dict) -> list[str]:
    argv = [sys.executable, str(CAPTURE_PY_PATH)]
    for group, label, kind, _prefill, flag_true, flag_false in ARG_SPECS:
        value = fields[label].get().strip()
        if not value:
            continue
        if kind == "entry":
            argv.extend([flag_true, value])
        elif kind == "choice":
            argv.extend([flag_true, value])
        elif kind == "tribool":
            argv.append(flag_true if value == "True" else flag_false)
    return argv


class RunnerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MOR CAM - capture.py runner (tam thoi)")
        self.msg_queue: queue.Queue = queue.Queue()
        self.fields: dict[str, tk.StringVar] = {}
        self.proc: subprocess.Popen | None = None

        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_queue)

        # khoa kich thuoc toi thieu = kich thuoc can thiet de hien du form +
        # nut Run/Stop + log, tranh thu nho cua so lam nut bi cat mat.
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)

        form = ttk.Frame(self.root, padding=8)
        form.grid(row=0, column=0, sticky="nsew")

        groups: dict[str, ttk.LabelFrame] = {}
        for i, name in enumerate(GROUP_ORDER):
            lf = ttk.LabelFrame(form, text=name, padding=6)
            lf.grid(row=0, column=i, sticky="new", padx=(0 if i == 0 else 8, 0))
            groups[name] = lf

        rows_per_group = {name: 0 for name in GROUP_ORDER}
        for group, label, kind, prefill, _flag_true, _flag_false in ARG_SPECS:
            parent = groups[group]
            r = rows_per_group[group]
            ttk.Label(parent, text=label + ":").grid(row=r, column=0, sticky="w", pady=2, padx=(0, 4))

            var = tk.StringVar(value=prefill)
            if kind == "entry":
                widget = ttk.Entry(parent, textvariable=var, width=20)
            elif kind == "tribool":
                widget = ttk.Combobox(parent, textvariable=var, values=TRI_BOOL_VALUES,
                                       width=17, state="readonly")
            elif kind == "choice":
                widget = ttk.Combobox(parent, textvariable=var, values=IMAGE_FORMAT_VALUES,
                                       width=17, state="readonly")
            widget.grid(row=r, column=1, sticky="w", pady=2)

            self.fields[label] = var
            rows_per_group[group] = r + 1

        btn_row = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        btn_row.grid(row=1, column=0, sticky="new")
        self.run_btn = ttk.Button(btn_row, text="Run", command=self.on_run_click)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_row, text="Stop", command=self.on_stop_click, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="San sang.")
        ttk.Label(btn_row, textvariable=self.status_var).pack(side="left", padx=(12, 0))

        log_frame = ttk.LabelFrame(self.root, text="Log", padding=4)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.root.rowconfigure(2, weight=1)
        self.log_text = tk.Text(log_frame, height=24, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    # --------------------------------------------------------------- chay
    def on_run_click(self) -> None:
        if self.proc is not None:
            return  # dang chay, bo qua click them
        argv = _build_command(self.fields)
        self._append_log("$ " + " ".join(argv))
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Dang chay...")
        threading.Thread(target=self._run_worker, args=(argv,), daemon=True).start()

    def _run_worker(self, argv: list[str]) -> None:
        proc = subprocess.Popen(
            argv, cwd=str(CAPTURE_PY_PATH.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        self.proc = proc
        for line in iter(proc.stdout.readline, ""):
            self.msg_queue.put(("log", line.rstrip("\n")))
        proc.stdout.close()
        returncode = proc.wait()
        self.msg_queue.put(("done", returncode))

    def on_stop_click(self) -> None:
        if self.proc is not None:
            self.proc.terminate()

    # ------------------------------------------------- main-thread polling
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._append_log(f"--- Ket thuc, exit code={payload} ---")
                    self.status_var.set(f"Xong (exit code={payload}).")
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.proc = None
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _append_log(self, text: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # -------------------------------------------------------------- close
    def on_close(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    root.geometry("1000x700")
    RunnerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
