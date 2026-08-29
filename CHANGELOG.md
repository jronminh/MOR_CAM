# Changelog

All notable changes to this project are documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 2026-08-29
- Documented that source code must be entirely in English (`CLAUDE.md`).
- Translated `README.md` to English.
- Simplified GUI logic in `gui.py`; updated `node_map_full.json`.

## 2026-08-28
- Translated `capture.py`/`gev_camera.py` comments and docstrings fully to English.
- Locked the 14 linearity-critical nodes per spec sections A.2/A.3/A.4/A.7; expanded config
  options; removed ROI support.
- Removed `config.yaml` as a hard dependency; switched `capture.py` to a full argparse CLI.
- Exposed all backend parameters in the GUI; added reconnect/retry; split the preview into
  its own panel.
- Added the Tkinter GUI (`gui.py`); added per-run log files for `capture.py`/`gui.py`;
  reorganized docs into `reference/`.
- Initial commit: PoC bringing the Hikrobot MV-CE200-10GM GigE camera into operation.
