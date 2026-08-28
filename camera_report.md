# Camera report: node map dump

Thoi gian chay: 2026-08-28T03:51:55.156460+00:00

## 1. Thong tin ket noi

- File .cti su dung: `C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64\MvProducerGEV.cti`
- Nguon xac dinh .cti: bien moi truong GENICAM_GENTL64_PATH
- Quyen mo thiet bi: DEVICE_ACCESS_READONLY (chi doc)
- Host IP (adapter ket noi camera): 192.168.100.2
- harvesters version: 1.4.3
- genicam version: 1.5.1
- Python: 3.11.16

## 2. Thong tin thiet bi (doc tu node map)

- Model (`DeviceModelName`): MV-CE200-10GM
- Serial (`DeviceSerialNumber`): 00F67674995
- Firmware (`DeviceFirmwareVersion`): V3.1.1 200717,19062001
- Vendor (`DeviceVendorName`): Hikrobot
- Device version (`DeviceVersion`): V3.1.1 200717 469308
- User ID (`DeviceUserID`): hikrobot
- Camera IP hien tai (`GevCurrentIPAddress`, doi ra dang thap phan): 192.168.100.253

## 3. Node quan trong theo nhom (chi liet ke node co is_feature=True)

### Exposure

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `ExposureMode` | Enumeration | RW | Timed | Timed |
| `ExposureTimeMode` | Enumeration | NI | (khong doc duoc / khong co quyen) |  |
| `ExposureTime` | Float | RW | 5000.0 | [46.0, 2000000.0] |
| `ExposureAuto` | Enumeration | RW | Off | Off, Once, Continuous |
| `AutoExposureTimeLowerLimit` | Integer | RW | 46 | [46, 172117] |
| `AutoExposureTimeUpperLimit` | Integer | RW | 172117 | [46, 2000000] |
| `EventExposureStartData` | Category | NI | (category, 3 con) |  |
| `EventExposureEndData` | Category | NI | (category, 3 con) |  |
| `EventExposureStart` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureStartFrameID` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureStartTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureEnd` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureEndFrameID` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureEndTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `ChunkExposure` | Integer | NA | (khong doc duoc / khong co quyen) | [None, None] |

### Gain

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `GainShutPrior` | Enumeration | NI | (khong doc duoc / khong co quyen) |  |
| `HDRGain` | Float | NI | (khong doc duoc / khong co quyen) | [None, None] dB |
| `ChunkGain` | Integer | NA | (khong doc duoc / khong co quyen) | [None, None] |
| `PreampGain` | Enumeration | NI | (khong doc duoc / khong co quyen) |  |
| `Gain` | Float | RW | 0.0 | [0.0, 19.9963] dB |
| `GainAuto` | Enumeration | RW | Off | Off, Once, Continuous |
| `AutoGainLowerLimit` | Float | RW | 0.0 | [0.0, 19.9963] dB |
| `AutoGainUpperLimit` | Float | RW | 19.9963 | [0.0, 19.9963] dB |
| `ADCGainEnable` | Boolean | NI | (khong doc duoc / khong co quyen) |  |

### Gamma

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `Gamma` | Float | RO | 0.7 | [0.0, 4.0] |
| `GammaSelector` | Enumeration | RW | User | User, sRGB |
| `GammaEnable` | Boolean | RW | False |  |

### PixelFormat

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `PixelFormat` | Enumeration | RW | Mono12 | Mono8, Mono10, Mono10Packed, Mono12, Mono12Packed |
| `ChunkPixelFormat` | Enumeration | NA | (khong doc duoc / khong co quyen) | Mono8, Mono10, Mono10Packed, Mono12, Mono12Packed |

### Binning

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `BinningSelector` | Enumeration | RW | Region0 | Region0 |
| `BinningHorizontal` | Enumeration | RW | BinningHorizontal1 | BinningHorizontal1, BinningHorizontal2, BinningHorizontal4 |
| `BinningVertical` | Enumeration | RW | BinningVertical1 | BinningVertical1, BinningVertical2, BinningVertical4 |

Ghi chu: `BinningSelector` co dinh nghia entry `Sensor` (binning tren cam bien) voi access_mode=`NI`. Access mode NI (Not Implemented) nghia la engine binning-tren-cam-bien KHONG duoc firmware nay ho tro thuc te, du node co ton tai trong XML; chi engine `Region0` (binning digital/ISP, khong phai on-sensor) la kha dung. BinningHorizontal2 + BinningVertical2 o Region0 cho hieu ung 2x2 nhung khong phai on-sensor binning that su.

### BlackLevel

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `BlackLevel` | Integer | RW | 200 | [0, 4095] |
| `BlackLevelEnable` | Boolean | RW | True |  |
| `BlackLevelAuto` | Enumeration | NI | (khong doc duoc / khong co quyen) | Off |

### Noise/Denoise

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `TZDenoiseOpen` | Boolean | NI | (khong doc duoc / khong co quyen) |  |
| `TZDenoiseCoef` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `DigitalNoiseReductionMode` | Enumeration | NI | (khong doc duoc / khong co quyen) | Off, Normal, Expert |
| `NoiseReduction` | Integer | NA | (khong doc duoc / khong co quyen) | [None, None] |
| `AirspaceNoiseReduction` | Integer | NA | (khong doc duoc / khong co quyen) | [None, None] |
| `TemporalNoiseReduction` | Integer | NA | (khong doc duoc / khong co quyen) | [None, None] |

### LUT

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `LUTControl` | Category | RO | (category, 4 con) |  |
| `LUTSelector` | Enumeration | RW | Luminance | Luminance |
| `LUTEnable` | Boolean | RW | False |  |
| `LUTIndex` | Integer | RW | 0 | [0, 1023] |
| `LUTValue` | Integer | RW | 0 | [0, 4095] |

### Timestamp

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `EventAcquisitionStartTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventAcquisitionEndTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventFrameStartTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventFrameEndTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventFrameBurstStartTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventFrameBurstEndTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureStartTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventExposureEndTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventLine0RisingEdgeTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventLine0FallingEdgeTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventErrorTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventTestTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventOverTemperatureTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventOverRunTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `EventFrameStartOverTriggerTimestamp` | Integer | NI | (khong doc duoc / khong co quyen) | [None, None] |
| `ChunkTimestamp` | Integer | NA | (khong doc duoc / khong co quyen) | [None, None] |
| `GevTimestampTickFrequency` | Integer | RO | 100000000 | [-9223372036854775808, 9223372036854775807] |
| `GevTimestampControlLatch` | Command | WO | (command, is_done=True) |  |
| `GevTimestampControlReset` | Command | WO | (command, is_done=True) |  |
| `GevTimestampControlLatchReset` | Command | WO | (command, is_done=True) |  |
| `GevTimestampValue` | Integer | RO | 0 | [-9223372036854775808, 9223372036854775807] |

### Packet

| Node | Kieu | Access | Gia tri hien tai | Dai hop le / enum |
|---|---|---|---|---|
| `DeviceStreamChannelPacketSize` | Integer | RW | 8164 | [220, 9156] |
| `GevSCPSFireTestPacket` | Boolean | RW | False |  |
| `PacketUnorderSupport` | Boolean | NI | (khong doc duoc / khong co quyen) |  |
| `GevSCPSPacketSize` | Integer | RW | 8164 | [220, 9156] |

## 4. Doi chung voi datasheet

| Thong so | Datasheet / catalog | Doc tu node map | Ket luan |
|---|---|---|---|
| Do phan giai | 5472 x 3648 (20MP) | 5472 x 3648 | khop |
| Dai exposure | ~46 us den 2 s | 46.0 - 2000000.0 us | khop |
| Frame rate | 5.9 fps (full-res) | AcquisitionFrameRate hien tai=5.8099, max=100000.0 | can chup thu o full-res de xac nhan fps thuc te dat duoc (max node chi la gioi han ly thuyet cua node, khong phai fps dat duoc lien tuc) |
| Pixel format | Mono 8/10/10p/12/12p | Mono10, Mono10Packed, Mono12, Mono12Packed, Mono8 | khop ve tap gia tri (5 format mono), NHUNG ten node that dung 'Mono10Packed'/'Mono12Packed' thay vi 'Mono10p'/'Mono12p' theo GenICam SFNC chuan; ban unpacked Mono10/Mono12 co ton tai |
| Giao tiep mang | GigE (1000 Mbps) | DeviceLinkSpeed=1000 Mbps | khop |
| Model | Hikrobot MV-CE200-10GM | MV-CE200-10GM | khop |
| Nguon PoE / 12VDC, dai nhiet 0~50C | PoE + 12VDC, 0~50C | khong co node GenICam tuong ung de doc lai qua node map | khong kiem duoc bang node map, phai xac minh bang datasheet/do thuc te |
| Binning (khong co trong danh sach thong so da xac minh, kiem tra them) | khong ro tu catalog | BinningHorizontal1, BinningHorizontal2, BinningHorizontal4 | ghi nhan de tham khao khi lam capture.py |

## 5. Ghi chu ky thuat

- MvProducerGEV.cti (Hikrobot MVS SDK, xac minh voi ban V3.1.1 build 200717) tra ve URL khong phai UTF-8 hop le khi harvesters doc node map cua *local TL device* va khi `ImageAcquirer` dang ky module event. `camera_info.py` da vong qua bang cach: (1) bo qua loi giai ma URL cua local device (khong anh huong node map remote device/camera), va (2) tu mo `Device` + `RemoteDevice` bang API noi bo cua harvesters thay vi goi `Harvester.create()`/`ImageAcquirer`, vi cong cu nay chi can doc node map, khong can streaming. Chi tiet: xem docstring dau file `camera_info.py`. `capture.py` (buoc sau, co dung ImageAcquirer de chup anh) se can ap dung patch tuong tu cho phan local-device URL, hoac kiem tra lai xem lien quan viec dang ky event co con crash hay khong.
- Tong so node trong node map: 2997, trong do 381 node duoc danh dau la feature (is_feature=True); phan con lai la cac node phu tro cap thap (anh xa thanh ghi/thanh doc, hau to `_Reg`, `_Inq`, `EnumEntry_*`, v.v.) trong XML cua Hikrobot.
