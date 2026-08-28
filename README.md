# MOR CAM - PoC dua camera GigE vao hoat dong

Camera: **Hikrobot MV-CE200-10GM** (sensor Sony IMX183, 20MP mono, GigE Vision, PoE).
Dac ta day du: `reference/poc_camera_bringup_spec.md`. Cac file:

| File | Vai tro |
|---|---|
| `gev_camera.py` | Module dung chung: ket noi harvesters, cac patch bat buoc cho MvProducerGEV.cti, ham set/doc node co xac minh read-back. |
| `camera_info.py` | Dump toan bo node map (chi doc) - **chay truoc tien** khi dem camera moi/firmware moi ve. |
| `capture.py` | Chuong trinh chinh: ep thong so linearity-critical, chup mot khung, luu anh + metadata. Co subcommand `focus`. |
| `focus.py` | Che do canh net truc tiep (goi tu `capture.py focus`). |
| `gui.py` | GUI Tkinter don gian: theo doi trang thai, chinh thong so, an chup, xem preview - lop mong tren `capture.py`, khong viet lai logic. |
| `config.yaml` | Cau hinh mau, **da dien gia tri that** xac minh tren camera serial `00F67674995`, firmware `V3.1.1 200717`. |
| `node_map_full.json` | Dump verbatim toan bo 2997 node cua camera that (nguon tra cuu ten node). |
| `reference/` | Tai lieu tham khao: `camera_report.md` (tom tat node quan trong + doi chung datasheet), `capture_bindings_and_issues.md` (ghi chu ky thuat: ten node dung de set, van de BlackLevel, binding khong on-sensor), `poc_camera_bringup_spec.md` (dac ta goc), `claude_code_task_dump_nodemap.md` (nhiem vu dump node map ban dau). |

## 1. Cai dat

### 1.1 Yeu cau bat buoc
- **Python 3.10 hoac 3.11.** `harvesters` chi co wheel bien san toi CPython 3.11. Da xac minh trong du an nay: Python 3.11.16, `harvesters==1.4.3`, `genicam==1.5.1` (tu dong keo theo).
- Da cai **Hikrobot MVS SDK** (co `MvProducerGEV.cti`).
- Camera va host **khac subnet IP** (vd camera `192.168.100.253`, host `192.168.100.2`), da cap nguon PoE hoac 12VDC.

### 1.2 Windows

```powershell
# tao venv rieng, PIN dung Python 3.10/3.11 (vi du dung conda):
conda create -n cam311 python=3.11
conda activate cam311
pip install -r requirements.txt
```

`.cti` duoc tu dong dinh vi qua bien moi truong `GENICAM_GENTL64_PATH` (do bo cai MVS SDK
thiet lap san) hoac cac duong dan cai dat mac dinh. Da xac minh tren may nay:
`C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64\MvProducerGEV.cti`.
Neu khac, dien vao `config.yaml` -> `gentl.cti_windows`.

### 1.3 Linux

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**CHUA xac minh duong dan `.cti` tren Linux** (du an nay phat trien tren Windows). Sau khi
cai MVS SDK cho Linux, dinh vi `MvProducerGEV.cti` that (thuong o `/opt/MVS/lib/64/`) va dien
vao `config.yaml` -> `gentl.cti_linux`. Tren Linux, dat MTU cua NIC >= packet size (vd 9000)
de tranh mat goi o full-res - xem `reference/capture_bindings_and_issues.md` muc 5.

### 1.4 Kiem tra ket noi

```bash
python camera_info.py
```

Chi doc, khong doi setting nao. In ra model/serial/firmware va xuat `node_map_full.json` +
`camera_report.md`. Neu khong dò duoc thiet bi, script se in ro cac nguyen nhan thuong gap
(sai duong dan `.cti`, trung IP host/camera, chua cap nguon, sai subnet, firewall).

## 2. Chup mot anh

```bash
python capture.py capture --config config.yaml
```

Trinh tu: ket noi -> ep + xac minh read-back Gamma/AutoExposure/AutoGain/LUT (**dung neu bat
ky thong so nao khong ve dung trang thai**, khong chup) -> ap dung pixel format/exposure/
gain/binning/ROI (co xac minh) -> chup mot khung (`AcquisitionMode=SingleFrame`, khong
streaming lien tuc) -> luu `captures/<timestamp>.tiff` + `captures/<timestamp>.json`.

Mono8 luu 8-bit, Mono10/Mono12 (unpacked) luu 16-bit TIFF giu nguyen gia tri ADC that
(khong scale, khong cat ve 8-bit). **`Mono10Packed`/`Mono12Packed` chua duoc ho tro** (xem
muc 4) - dung ban unpacked, day cung la khuyen nghi cua dac ta goc.

Vi du metadata that (da chup Mono12 full-res tren camera nay):

```json
{
  "pixel_format": "Mono12",
  "exposure_us": 10000.0,
  "black_level": {"mode": "keep_and_record", "value": 200, "enable": true},
  "linearity_readback": {"gamma_enable": {"value": false, "access": "RW"}, "...": "..."},
  "packet_loss": {"buffer_complete": true, "num_underrun": 0}
}
```

### File log

Moi lan chay `capture.py capture` hoac `capture.py focus`, ngoai console (muc INFO, gon)
con ghi mot **file log rieng cho lan chay do** vao `logs/<command>_<timestamp>.log`
(muc DEBUG, co timestamp + ten module). File nay ghi ca cac dong console khong in ra, vi du
so lan `fetch_buffer_retrying()` phai thu lai do loi UnicodeDecodeError cua
`MvProducerGEV.cti` (muc 5.1) - huu ich khi debug loi khong on dinh sau nay ma khong can bat
lai tay. Tat file log bang `--log-dir ""`, hoac doi thu muc bang `--log-dir DIR` hoac
`logging.dir` trong config.yaml. `logs/` khong duoc dua vao git (xem `.gitignore`).

## 3. GUI don gian

```bash
python gui.py --config config.yaml
```

Cua so gom: trang thai ket noi + model/serial/firmware, cac o chinh pixel format/exposure/
gain/binning/black level/thu muc luu, nut **Chup anh**, khung preview anh vua chup (downscale
8x, hien qua PNG), va panel log. Bam Chup se chay dung trinh tu nhu `capture.py capture`:
enforce_linear -> apply_adjustable -> single_capture -> save_image - **dung lai chinh cac ham
da test trong `capture.py`, khong co logic rieng**. Chup chay trong thread nen de khong treo
cua so (anh full-res Mono12 mat khoang 1-2 giay qua GigE).

**Da test bang cach lai chuong trinh qua code** (ket noi that, bam nut Chup that, chup thanh
cong tren camera that, preview PNG tao thanh cong) nhung **chua the xac nhan bang mat** giao
dien hien thi dung nhu mong doi (khong co man hinh de kiem tra truc quan trong qua trinh phat
trien) - ban nen tu mo thu truoc khi dung that.

Tren Linux, tkinter co the can cai rieng: `sudo apt install python3-tk` (khong co trong
`requirements.txt` vi la module chuan cua Python, khong phai goi pip).

## 4. Canh net truc tiep

```bash
python capture.py focus --config config.yaml            # mode=auto: tu chon gui/headless
python capture.py focus --config config.yaml --mode headless_score
```

Streaming lien tuc nhung gioi han tai (`fps_limit`, `downscale` trong config) - **chi dung
khi co nguoi thao tac**, khong dung cho van hanh khong nguoi truc. Diem sac net = variance
of Laplacian.

- `headless_score`: in `sharpness=...` ra stdout moi frame, ghi de anh preview thu nho vao
  `preview_image_path` moi giay - **da test thuc te tren camera that**.
- `gui`: mo cua so `cv2.imshow`, nhan `q`/Esc de thoat - **chua the xac nhan bang mat** trong
  qua trinh phat trien nay (khong co man hinh de kiem tra truc quan); code da chay het
  nhanh logic (khong loi) nhung ban nen tu kiem tra cua so hien thi dung truoc khi dung that.

## 5. Van de da biet va cach xu ly

### 5.1 `MvProducerGEV.cti` (V3.1.1 200717) tra ve du lieu khong phai UTF-8 hop le

Xac minh truc tiep tren camera that (khong doan). Ba diem crash rieng biet trong
`harvesters`/`genicam`, deu da xu ly trong `gev_camera.py`:

1. Doc node map cua **local TL device** (truoc khi cham toi node map camera that) -> vong
   qua bang cach coi nhu local device khong co URL (khong anh huong node map that).
2. `ImageAcquirer` **dang ky module event** (System/Interface/Device) -> vong qua bang cach
   coi UnicodeDecodeError nhu "event khong duoc ho tro" (harvesters da co san nhanh xu ly
   nay cho NotImplementedException, chi can chuyen doi loai loi).
3. **Fetch buffer anh dau tien** sau `ia.start()` -> khac ban chat, khong the patch (loi nam
   trong ham C++ da bien dich cua `genicam`, xay ra truoc khi buffer duoc gan). Da do thuc
   nghiem: fail 0 den ~19 lan roi thanh cong binh thuong, khong lien quan kich thuoc/noi dung
   anh. Xu ly bang **retry ngay lap tuc** (khong restart acquisition) trong
   `gev_camera.fetch_buffer_retrying()`, gioi han theo tong thoi gian thay vi so lan.

`camera_info.py` chi can patch (1) vi khong dung `ImageAcquirer`. `capture.py`/`focus.py`
can ca ba.

### 5.2 Binning khong phai on-sensor

`BinningSelector` chi co entry `Region0` kha dung (`Sensor` co access `NI`). Nghia la
`BinningHorizontal2`/`BinningVertical2` la **gop pixel digital sau ADC**, khong phai gop
dien tich tren cam bien - khong cai thien SNR nhu binning that. `capture.py` van cho phep
bat (ghi log canh bao ro) nhung khong nen trong cay vao no de tang SNR vat moc xa.

### 5.3 BlackLevel = 200 (pedestal)

Mac dinh camera cong offset 200 DN (thang 0-4095) vao moi pixel (`BlackLevelEnable=True`).
Offset nay **khong triet tieu trong do tuong phan Weber**. `capture.py` mac dinh
(`black_level.mode: keep_and_record`) giu nguyen va ghi vao moi file metadata; buoc
calibration sau (dark frame) phai tru gia tri nay. Doi `mode: set_zero` trong config neu
muon dat ve 0 (chua kiem chung anh huong toi tuyen tinh o che do nay).

### 5.4 Noise reduction

Cac node lien quan (`DigitalNoiseReductionMode`, `NoiseReduction`, `TZDenoiseOpen`, ...) deu
co access `NI`/`NA` tren firmware nay - khong set duoc va khong co gi de tat. `capture.py`
coi day la **binh thuong, khong phai loi** (ISP khong co pipeline noise reduction hoat dong
de lo).

## 6. Doi chung datasheet (tom tat, chi tiet o `reference/camera_report.md`)

| Thong so | Datasheet | Do tu node map | Ket luan |
|---|---|---|---|
| Do phan giai | 5472x3648 (20MP) | 5472x3648 | khop |
| Dai exposure | ~46us - 2s | 46 - 2,000,000 us | khop |
| Pixel format | Mono 8/10/10p/12/12p | Mono8/10/10Packed/12/12Packed | khop tap gia tri; ten khac SFNC chuan (`...Packed` khong phai `...p`) |
| Giao tiep | GigE | DeviceLinkSpeed=1000 Mbps | khop |
| Model | MV-CE200-10GM | MV-CE200-10GM | khop |
| Nguon PoE/12VDC, 0~50C | co | khong co node de doc lai | xac minh bang datasheet/do thuc te |

## 7. Chua lam trong PoC nay

- Giai nen `Mono10Packed`/`Mono12Packed` (dung ban unpacked de thay the).
- Xac minh `.cti` tren Linux (chi test tren Windows).
- Xac nhan bang mat che do `focus --mode gui` va giao dien `gui.py` (da test logic bang cach
  lai chuong trinh qua code, chua xac nhan hien thi truc quan dung nhu mong doi).
- Dark frame / flat frame calibration, kiem chung tuyen tinh bang chuoi exposure, trigger
  phan cung, truyen du lieu LTE, tinh MOR - deu ngoai pham vi PoC nay (xem
  `reference/poc_camera_bringup_spec.md` muc 2).
