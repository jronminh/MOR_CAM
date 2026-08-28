"""
focus.py - che do canh net truc tiep (live focus mode), goi tu
`python capture.py focus`.

Dung streaming lien tuc (AcquisitionMode=Continuous) nhung co gioi han tai:
giam do phan giai preview (downscale) va gioi han fps (fps_limit) o phia
client. Day la che do CHI dung khi co nguoi thao tac canh net, khong dung
cho van hanh khong nguoi truc (xem poc_camera_bringup_spec.md muc 5.6/6.1:
tren mini PC 2 nhan, GVSP xu ly o userspace lam nghen CPU neu streaming
lien tuc khong gioi han).

Diem sac net = variance of Laplacian (OpenCV). So cao hon = net hon.

Gioi han da biet: mode "gui" mo cua so cv2.imshow - can man hinh de xac
nhan bang mat, khong the kiem tra tu dong. Mode "headless_score" da kiem
tra duoc day du (in stdout + ghi file preview), day la mode duoc test
thuc te trong qua trinh phat trien.
"""
from __future__ import annotations

import logging
import os
import platform
import time
from pathlib import Path

import cv2
import numpy as np

import gev_camera as gc
from capture import UNPACKED_DTYPE

log = logging.getLogger("focus")


def has_display() -> bool:
    system = platform.system()
    if system in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def sharpness_score(gray_u8: np.ndarray) -> float:
    return float(cv2.Laplacian(gray_u8, cv2.CV_64F).var())


def to_u8_preview(image: np.ndarray, downscale: int) -> np.ndarray:
    if downscale > 1:
        image = image[::downscale, ::downscale]
    if image.dtype != np.uint8:
        max_val = np.iinfo(image.dtype).max
        image = (image.astype(np.float32) * (255.0 / max_val)).astype(np.uint8)
    return np.ascontiguousarray(image)


def run_focus(ia, node_map, config: dict) -> None:
    preview_cfg = config["preview"]
    mode = preview_cfg.get("mode") or "auto"
    if mode == "auto":
        mode = "gui" if has_display() else "headless_score"
    fps_limit = max(float(preview_cfg.get("fps_limit", 5)), 0.1)
    downscale = max(int(preview_cfg.get("downscale", 4)), 1)
    preview_path = Path(preview_cfg.get("preview_image_path", "./preview_latest.png"))

    log.warning(
        "Che do canh net: streaming LIEN TUC, tai CPU/mang cao hon binh thuong. "
        "CHI dung khi co nguoi thao tac, khong dung cho van hanh khong nguoi truc.")
    log.info("Focus mode = %s, fps_limit=%s, downscale=%s", mode, fps_limit, downscale)

    gc.set_enum_and_verify(node_map, ["AcquisitionMode"], "Continuous", "Acquisition mode")
    if node_map.has_node("TriggerMode"):
        gc.set_enum_and_verify(node_map, ["TriggerMode"], "Off", "Trigger mode")

    frame_interval = 1.0 / fps_limit
    window_name = "MOR CAM focus (q hoac Esc de thoat)"

    ia.start()
    try:
        last_frame_time = 0.0
        last_preview_write = 0.0
        while True:
            buffer = gc.fetch_buffer_retrying(ia, timeout_ms=5000)
            try:
                component = buffer.payload.components[0]
                width, height = component.width, component.height
                data_format = component.data_format
                dtype = UNPACKED_DTYPE.get(data_format)
                if dtype is None:
                    log.error(
                        "Focus mode: pixel_format=%s khong duoc ho tro (dung Mono8/10/12 khong packed).",
                        data_format)
                    return
                image = component.data.copy().view(dtype).reshape(height, width)
            finally:
                buffer.queue()

            now = time.time()
            if now - last_frame_time < frame_interval:
                continue
            last_frame_time = now

            preview = to_u8_preview(image, downscale)
            score = sharpness_score(preview)

            if mode == "gui":
                display = preview.copy()
                cv2.putText(display, f"sharpness={score:.1f}", (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,), 2)
                cv2.imshow(window_name, display)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27) or cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                    log.info("Da dong cua so preview, thoat focus mode.")
                    break
            else:  # headless_score
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"{ts} sharpness={score:.2f}", flush=True)
                if now - last_preview_write >= 1.0:
                    preview_path.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(preview_path), preview)
                    last_preview_write = now
    finally:
        ia.stop()
        if mode == "gui":
            cv2.destroyAllWindows()
