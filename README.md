# MOR CAM - PoC bringing a GigE camera into operation

Camera: **Hikrobot MV-CE200-10GM** (Sony IMX183 sensor, 20MP mono, GigE Vision, PoE).
Full spec: `reference/poc_camera_bringup_spec.md`. Files:

| File | Role |
|---|---|
| `gev_camera.py` | Shared module: harvesters connection, mandatory patches for MvProducerGEV.cti, node set/read helpers with read-back verification. |
| `camera_info.py` | Dumps the full node map (read-only) - **run this first** when a new camera/firmware arrives. |
| `capture.py` | Main program: enforces linearity-critical settings, captures one frame, saves image + metadata. Has a `focus` subcommand. |
| `focus.py` | Live focusing mode (invoked via `capture.py focus`). |
| `gui.py` | Simple Tkinter GUI: connection status, parameter editing, capture button, preview - a thin layer over `capture.py`, no duplicated logic. |
| `config.yaml` | Sample config, **filled with real values** verified on camera serial `00F67674995`, firmware `V3.1.1 200717`. |
| `node_map_full.json` | Verbatim dump of all 2997 nodes from the real camera (lookup source for node names). |
| `reference/` | Reference docs: `camera_report.md` (summary of key nodes + datasheet cross-check), `capture_bindings_and_issues.md` (technical notes: node names used for setting values, BlackLevel issue, non-on-sensor binning), `poc_camera_bringup_spec.md` (original spec), `claude_code_task_dump_nodemap.md` (original node-map-dump task). |

## 1. Setup

### 1.1 Hard requirements
- **Python 3.10 or 3.11.** `harvesters` only ships prebuilt wheels up to CPython 3.11. Verified in this project: Python 3.11.16, `harvesters==1.4.3`, `genicam==1.5.1` (pulled in automatically).
- **Hikrobot MVS SDK** installed (provides `MvProducerGEV.cti`).
- Camera and host on **different IP subnets** (e.g. camera `192.168.100.253`, host `192.168.100.2`), powered via PoE or 12VDC.

### 1.2 Windows

```powershell
# create a dedicated venv, PIN to Python 3.10/3.11 (e.g. via conda):
conda create -n cam311 python=3.11
conda activate cam311
pip install -r requirements.txt
```

The `.cti` file is located automatically via the `GENICAM_GENTL64_PATH` environment variable
(set up by the MVS SDK installer) or default install paths. Verified on this machine:
`C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64\MvProducerGEV.cti`.
If different, fill it in under `config.yaml` -> `gentl.cti_windows`.

### 1.3 Linux

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**The `.cti` path on Linux has NOT been verified** (this project was developed on Windows).
After installing the MVS SDK for Linux, locate the real `MvProducerGEV.cti` (usually under
`/opt/MVS/lib/64/`) and fill it in under `config.yaml` -> `gentl.cti_linux`. On Linux, set the
NIC's MTU >= packet size (e.g. 9000) to avoid packet loss at full resolution - see
`reference/capture_bindings_and_issues.md` section 5.

### 1.4 Connection check

```bash
python camera_info.py
```

Read-only, changes no settings. Prints model/serial/firmware and exports
`node_map_full.json` + `camera_report.md`. If the device isn't discovered, the script prints
the common causes clearly (wrong `.cti` path, host/camera IP collision, no power, wrong
subnet, firewall).

## 2. Capturing one image

```bash
python capture.py capture --config config.yaml
```

Sequence: connect -> enforce + verify read-back of Gamma/AutoExposure/AutoGain/LUT (**abort
if any setting doesn't land in the expected state**, no capture) -> apply pixel
format/exposure/gain/binning/ROI (with verification) -> capture one frame
(`AcquisitionMode=SingleFrame`, no continuous streaming) -> save
`captures/<timestamp>.tiff` + `captures/<timestamp>.json`.

Mono8 is saved as 8-bit; Mono10/Mono12 (unpacked) are saved as 16-bit TIFF preserving the
real ADC values (no scaling, no truncation to 8-bit). **`Mono10Packed`/`Mono12Packed` are not
supported yet** (see section 4) - use the unpacked variant, which is also what the original
spec recommends.

Example of real metadata (captured Mono12 at full resolution on this camera):

```json
{
  "pixel_format": "Mono12",
  "exposure_us": 10000.0,
  "black_level": {"mode": "keep_and_record", "value": 200, "enable": true},
  "linearity_readback": {"gamma_enable": {"value": false, "access": "RW"}, "...": "..."},
  "packet_loss": {"buffer_complete": true, "num_underrun": 0}
}
```

### Log file

Every run of `capture.py capture` or `capture.py focus`, besides console output (INFO level,
terse), also writes a **run-specific log file** to `logs/<command>_<timestamp>.log` (DEBUG
level, with timestamps + module names). This file records lines not printed to console, such
as the number of retries `fetch_buffer_retrying()` had to perform due to
`MvProducerGEV.cti`'s UnicodeDecodeError bug (section 5.1) - useful for debugging intermittent
issues later without re-enabling verbose logging by hand. Disable the log file with
`--log-dir ""`, or change the directory with `--log-dir DIR` or `logging.dir` in config.yaml.
`logs/` is not tracked in git (see `.gitignore`).

## 3. Simple GUI

```bash
python gui.py --config config.yaml
```

The window shows: connection status + model/serial/firmware, fields for pixel
format/exposure/gain/binning/black level/save directory, a **Capture** button, a preview of
the last captured image (downscaled 8x, shown as PNG), and a log panel. Capture runs the exact
same sequence as `capture.py capture`: enforce_linear -> apply_adjustable -> single_capture ->
save_image - **it reuses the same functions already tested in `capture.py`, with no separate
logic**. Capture runs on a background thread so the window doesn't freeze (a full-res Mono12
image takes about 1-2 seconds over GigE).

**Tested by driving the program through code** (real connection, real button click, real
capture succeeded on the real camera, preview PNG generated successfully) but **visual
confirmation of the UI rendering as expected is still pending** (no display was available to
check it visually during development) - try it yourself before relying on it.

On Linux, tkinter may need a separate install: `sudo apt install python3-tk` (not listed in
`requirements.txt` since it's a Python standard-library module, not a pip package).

## 4. Live focusing

```bash
python capture.py focus --config config.yaml            # mode=auto: picks gui/headless automatically
python capture.py focus --config config.yaml --mode headless_score
```

Continuous streaming but rate-limited (`fps_limit`, `downscale` in config) - **only for use
while an operator is present**, not for unattended operation. Sharpness score = variance of
Laplacian.

- `headless_score`: prints `sharpness=...` to stdout every frame, overwrites the downscaled
  preview image at `preview_image_path` every second - **tested for real on the actual
  camera**.
- `gui`: opens a `cv2.imshow` window, press `q`/Esc to quit - **visual confirmation still
  pending** during this development cycle (no display was available to check it visually);
  the code ran through the full logic path with no errors, but verify the window renders
  correctly yourself before relying on it.

## 5. Known issues and workarounds

### 5.1 `MvProducerGEV.cti` (V3.1.1 200717) returns invalid UTF-8 data

Verified directly on the real camera (not guessed). Three separate crash points in
`harvesters`/`genicam`, all handled in `gev_camera.py`:

1. Reading the node map of the **local TL device** (before ever touching the real camera's
   node map) -> worked around by treating the local device as having no URL (does not affect
   the real node map).
2. `ImageAcquirer` **registering module events** (System/Interface/Device) -> worked around by
   treating UnicodeDecodeError as "event not supported" (harvesters already has a fast path
   for NotImplementedException, so only the error type needs converting).
3. **Fetching the first image buffer** after `ia.start()` -> different in nature, cannot be
   patched (the error is inside a compiled C++ function of `genicam`, occurring before the
   buffer is assigned). Empirically measured: fails 0 to ~19 times before succeeding normally,
   unrelated to image size/content. Handled with **immediate retry** (no acquisition restart)
   in `gev_camera.fetch_buffer_retrying()`, bounded by total elapsed time rather than a retry
   count.

`camera_info.py` only needs patch (1) since it doesn't use `ImageAcquirer`. `capture.py`/
`focus.py` need all three.

### 5.2 Binning is not on-sensor

`BinningSelector` only has `Region0` available (`Sensor` has access `NI`). This means
`BinningHorizontal2`/`BinningVertical2` are **digital pixel combining after the ADC**, not
combining sensor area before it - it does not improve SNR the way real binning does.
`capture.py` still allows enabling it (with a clear warning logged) but it should not be
relied on to improve SNR down the line.

### 5.3 BlackLevel = 200 (pedestal)

By default the camera adds a 200 DN offset (0-4095 scale) to every pixel
(`BlackLevelEnable=True`). This offset is **not cancelled out in Weber contrast**.
`capture.py` defaults (`black_level.mode: keep_and_record`) to keeping it as-is and recording
it in every metadata file; a later calibration step (dark frame) must subtract this value.
Change `mode: set_zero` in config to set it to 0 (its effect on linearity in this mode has not
been verified).

### 5.4 Noise reduction

The related nodes (`DigitalNoiseReductionMode`, `NoiseReduction`, `TZDenoiseOpen`, ...) all
have access `NI`/`NA` on this firmware - they cannot be set and there is nothing to disable.
`capture.py` treats this as **expected, not a bug** (the ISP has no active noise-reduction
pipeline to expose).

## 6. Datasheet cross-check (summary; full detail in `reference/camera_report.md`)

| Parameter | Datasheet | Read from node map | Conclusion |
|---|---|---|---|
| Resolution | 5472x3648 (20MP) | 5472x3648 | matches |
| Exposure range | ~46us - 2s | 46 - 2,000,000 us | matches |
| Pixel format | Mono 8/10/10p/12/12p | Mono8/10/10Packed/12/12Packed | value set matches; naming differs from standard SFNC (`...Packed` instead of `...p`) |
| Interface | GigE | DeviceLinkSpeed=1000 Mbps | matches |
| Model | MV-CE200-10GM | MV-CE200-10GM | matches |
| Power PoE/12VDC, 0~50C | yes | no node to read back | verify via datasheet/physical testing |

## 7. Not done in this PoC

- Decoding `Mono10Packed`/`Mono12Packed` (use the unpacked variant instead).
- Verifying the `.cti` path on Linux (only tested on Windows).
- Visual confirmation of `focus --mode gui` and the `gui.py` interface (logic tested by
  driving the program through code, visual rendering not yet confirmed as expected).
- Dark frame / flat frame calibration, linearity verification via an exposure sweep, hardware
  triggering, LTE data transmission, MOR computation - all out of scope for this PoC (see
  `reference/poc_camera_bringup_spec.md` section 2).
