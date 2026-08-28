# capture.py: ràng buộc node thật và vấn đề đã biết

Nguồn: node map dump ngày 2026-08-28. Camera MV-CE200-10GM, firmware V3.1.1 200717. Stack: harvesters 1.4.3, genicam 1.5.1, Python 3.11.16. Chạy trên Windows.

## 1. Tên node thật để set và đọc lại

| Chức năng | Node | Kiểu | Thao tác |
|---|---|---|---|
| Pixel format | `PixelFormat` | Enum | set = Mono12 (unpacked) |
| Exposure | `ExposureTime` | Float (us) | set giá trị; dải [46, 2000000] |
| Exposure mode | `ExposureMode` | Enum | set = Timed (chỉ có giá trị này) |
| Auto exposure | `ExposureAuto` | Enum | set = Off |
| Gain | `Gain` | Float (dB) | set giá trị; dải [0, 19.9963] |
| Auto gain | `GainAuto` | Enum | set = Off |
| Gamma | `GammaEnable` | Boolean | set = False |
| LUT | `LUTEnable` | Boolean | set = False |
| Black level | `BlackLevel` (Int), `BlackLevelEnable` (Bool) | ghim + ghi metadata (xem mục 2) |
| Binning ngang | `BinningHorizontal` | Enum | {BinningHorizontal1, 2, 4} |
| Binning dọc | `BinningVertical` | Enum | {BinningVertical1, 2, 4} |
| Packet size | `GevSCPSPacketSize` | Int | giữ 8164 hoặc theo MTU |
| Timestamp tick | `GevTimestampTickFrequency` | Int RO | 100 MHz (10 ns/tick) |
| Model/serial/fw | `DeviceModelName`, `DeviceSerialNumber`, `DeviceFirmwareVersion` | String RO | ghi metadata |

Read-back bắt buộc sau khi set: `GammaEnable`, `ExposureAuto`, `GainAuto`, `LUTEnable`. Nếu không về đúng trạng thái thì dừng, không chụp.

Lưu ý enum binning: giá trị là chuỗi `BinningHorizontal1`/`BinningHorizontal2`/`BinningHorizontal4`, không phải số trần. Ánh xạ từ config (1/2/4) sang tên enum khi set.

## 2. Black level là điểm then chốt

- Hiện trạng: `BlackLevel` = 200 trên thang [0,4095], `BlackLevelEnable` = True. Camera cộng offset 200 DN vào mọi pixel.
- Trong độ tương phản Weber, offset này không triệt tiêu. Hệ số nhân k triệt tiêu, offset thì không. Kết quả: độ tương phản đo được thấp hơn thật, mức lệch phụ thuộc độ sáng nền trời.
- Bắt buộc: ghi `BlackLevel` và `BlackLevelEnable` vào metadata mỗi ảnh. Bước calibration sau trừ dark frame.
- Quyết định để mở (config `black_level.mode`): giữ pedestal 200 (khuyến nghị, tránh cắt phần dưới của phân bố nhiễu tối) hay đặt 0 (rủi ro cắt). Dù chọn gì cũng phải ghi lại và trừ.

## 3. On-sensor binning không có

- `BinningSelector` chỉ có `Region0` khả dụng. Entry `Sensor` có access NI (Not Implemented).
- Nghĩa là 2x2 chỉ là binning digital sau khi đọc ảnh, không phải gộp điện tích trên cảm biến.
- Hệ quả: binning digital giảm dung lượng (~4x ở 2x2) nhưng không giảm nhiễu đọc như on-sensor. Lợi ích SNR ở vật mốc xa gần như không có.
- Kết luận cho open item của dự án: không trông cậy binning để tăng SNR vật mốc xa. Nếu bật, chỉ để tiết kiệm lưu trữ, và cần kiểm tra binning digital có giữ tuyến tính không.

## 4. Vấn đề đã biết: crash khi dùng ImageAcquirer

- `camera_info.py` chỉ đọc node map và cố tình không dùng `ImageAcquirer`, nên tránh được lỗi.
- Lỗi: `MvProducerGEV.cti` (bản đi kèm firmware V3.1.1) trả về URL không phải UTF-8 hợp lệ khi harvesters đọc node map của local TL device và khi `ImageAcquirer` đăng ký module event.
- `capture.py` bắt buộc dùng `ImageAcquirer` để chụp, nên sẽ gặp lại lỗi này.
- Phải xử lý. Các hướng để thử, không đoán trước cái nào đúng: vá phần giải mã URL của local-device, hoặc tránh đăng ký event module không cần thiết, hoặc thử cấu hình harvesters khác. Tham chiếu cách `camera_info.py` đã vòng qua. Thử và xác minh, không hard-code giả định.

## 5. Timestamp và mạng

- Không có chunk timestamp (`ChunkTimestamp` = NA). Lấy timestamp mỗi khung từ buffer GenTL (harvesters expose timestamp của buffer).
- `GevTimestampTickFrequency` = 100 MHz, tick 10 ns. Có lệnh `GevTimestampControlLatch`/`Reset` nếu cần đồng bộ tuyệt đối.
- Cho gắn nhãn SYNOP: neo theo giờ host (NTP). Timestamp camera dùng cho thứ tự tương đối và đo trễ. Ghi cả hai vào metadata.
- `GevSCPSPacketSize` = 8164 (jumbo). Trên Linux đặt MTU của NIC >= packet size để đạt zero packet loss. Đẩy lên tối đa 9156 thì cần MTU ~9014, một số NIC chặn ở 9000, nên 8164 là mức an toàn.

## 6. Đối chứng datasheet (từ dump)

- Độ phân giải 5472 x 3648, khớp.
- Dải exposure 46 us đến 2 s, khớp.
- Link speed 1000 Mbps, khớp.
- Frame rate: node hiện tại ~5.81 fps, phù hợp mức ~5.9 fps của datasheet. fps thực tế bền vững ở full-res cần chụp thử để xác nhận.
- Nguồn PoE/12VDC và dải nhiệt 0~50C: không có node GenICam để đọc lại, xác minh bằng datasheet và đo thực tế.
