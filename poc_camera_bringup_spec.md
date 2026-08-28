# Đặc tả PoC: Đưa camera GigE vào hoạt động bằng Python

Tài liệu này là đặc tả để giao cho Claude Code. Mục tiêu là dựng đường thu ảnh cơ bản, chạy được, xác minh được, trên cả Windows và Linux. Chưa động tới calibration, truyền dữ liệu, hay tính MOR.

---

## 1. Mục tiêu

Một chương trình Python:

1. Kết nối được camera GigE qua `harvesters`.
2. Đọc cấu hình từ file YAML.
3. Ép và xác minh các thông số linearity-critical về đúng trạng thái tắt.
4. Cho chỉnh các thông số cơ bản: exposure, gain, pixel format, binning.
5. Chụp đơn một khung theo lệnh và lưu ảnh raw kèm metadata.
6. Có chế độ canh nét trực tiếp, hoạt động cả khi có màn hình lẫn khi headless.

Thành công khi: cùng một codebase chạy trên Windows và Linux, chụp được ảnh Mono12 full-res, giá trị pixel giữ nguyên bit depth, và read-back xác nhận ISP đã tắt.

---

## 2. Phạm vi

**Trong scope**
- Kết nối, cấu hình, chụp đơn, lưu ảnh + metadata, canh nét.
- Xác minh trạng thái thông số bằng cách đọc lại từ camera.
- Tiện ích dump node map để xác minh tên node và các giá trị hỗ trợ.

**Ngoài scope (giai đoạn sau, không làm ở PoC này)**
- Dark frame / flat frame calibration.
- Kiểm chứng tuyến tính bằng chuỗi exposure.
- Trigger phần cứng qua chân GPIO.
- Truyền dữ liệu LTE, quản lý lưu trữ, gắn nhãn SYNOP.
- Tính toán MOR.
- Luồng streaming liên tục cho vận hành không người trực (chỉ preview canh nét là dùng luồng liên tục, và có giới hạn tải).

---

## 3. Môi trường và phụ thuộc

- Python 3.10 hoặc 3.11. Bắt buộc. `harvesters` chỉ có wheel biên dịch sẵn tới CPython 3.11 (manylinux2014_x86_64). Không dùng 3.12 trở lên.
- Thư viện thu ảnh: `harvesters`. Không dùng `harvesters-util` (đã bỏ). `genicam` là phụ thuộc mức thấp, tự động kéo theo `harvesters`.
- GenTL Producer: `MvProducerGEV.cti`, lấy từ Hikrobot MVS SDK. Phải đăng ký tường minh với Harvester.
- Đường dẫn `.cti` khác nhau giữa Windows và Linux. Không đoán đường dẫn. Đọc từ config; nếu không thấy file, dừng và báo lỗi kèm hướng dẫn định vị trong MVS SDK.
- Khuyến nghị chạy trong venv riêng để giữ Python pin đúng phiên bản.

README phải ghi rõ các bước: tạo venv, cài đặt, định vị `.cti`, điền vào config, kiểm tra kết nối. Ghi riêng cho Windows và Linux.

---

## 4. Phần cứng và mạng

- Camera: Hikrobot MV-CE200-10GM. 20MP mono, cảm biến Sony IMX183, GigE Vision, PoE.
- Camera IP mặc định trong lab: `192.168.100.253/24`. Host: `192.168.100.2`.
- Lưu ý mạng: host và camera không được trùng IP. Đây là lỗi đã gặp khiến `arv-tool` không dò được thiết bị. Nếu kết nối thất bại, kiểm tra và báo trùng IP như một nguyên nhân khả dĩ.
- Config cho phép chỉ định thiết bị theo serial hoặc IP. Nếu để trống thì lấy thiết bị đầu tiên dò được.

---

## 5. Yêu cầu chức năng

### 5.1 Kết nối
- Đăng ký `.cti` theo HĐH đang chạy, khởi tạo Harvester, cập nhật danh sách thiết bị.
- Mở camera theo serial hoặc IP trong config, hoặc thiết bị đầu tiên nếu không chỉ định.
- In ra: model, serial, firmware version, độ phân giải cảm biến.
- Nếu không dò được thiết bị: báo lỗi rõ, liệt kê nguyên nhân thường gặp (sai đường dẫn `.cti`, trùng IP host và camera, chưa cấp nguồn PoE, sai subnet).

### 5.2 Cấu hình YAML
- Toàn bộ thông số nạp từ một file YAML. Xem mẫu ở Mục 8.
- Thiếu trường thì dùng mặc định an toàn và ghi log rõ trường nào đang dùng mặc định.

### 5.3 Ép và xác minh thông số linearity-critical
Đây là phần cốt lõi của PoC. Các thông số sau phải được tắt và xác minh bằng read-back:
- Gamma: tắt.
- Noise reduction: tắt.
- Auto exposure: tắt.
- Auto gain: tắt.
- LUT: tắt.
- AWB: không áp dụng cho cảm biến mono, bỏ qua nhưng ghi log là đã bỏ qua.

Quy tắc:
- Sau khi set, đọc lại từng node để xác nhận trạng thái đã áp dụng.
- Nếu một node không tắt được, hoặc không tồn tại với tên đã thử: dừng và báo lỗi rõ, không chụp. Ảnh có ISP bật là ảnh vô dụng cho dự án này.
- Không hard-code tên node. Xem Mục 6.3.

### 5.4 Thông số chỉnh được
Nạp từ config và áp dụng, có read-back xác nhận:
- Pixel format: Mono8, Mono10, Mono12. Xác minh giá trị đúng từ node map.
- Exposure time.
- Gain.
- Binning: 1 hoặc 2. Chỉ bật nếu camera hỗ trợ on-sensor binning. Xác minh khả năng hỗ trợ trước, không giả định.
- ROI: tùy chọn, để mặc định full-frame ở PoC.

### 5.5 Chụp đơn
- Chụp một khung theo lệnh (software trigger hoặc single-frame acquisition), không dùng streaming liên tục.
- Lý do: trên mini PC 2 nhân, GVSP xử lý ở userspace làm nghẽn CPU khi streaming liên tục. Chụp đơn gần như loại bỏ tải này.
- Báo cáo tỉ lệ mất gói (packet loss) của khung vừa chụp nếu API cho biết. Ở Mono12 full-res, kỳ vọng zero packet loss. Nếu có mất gói, gợi ý chỉnh packet size hoặc bật jumbo frame (MTU 9000).

### 5.6 Canh nét trực tiếp
Chế độ này dùng luồng liên tục nhưng có giới hạn tải, và chỉ dùng khi có người thao tác. Tách theo nền tảng:
- Có màn hình (thường là Windows): mở cửa sổ hiển thị preview, cập nhật liên tục.
- Headless (thường là mini PC Linux): không mở cửa sổ. In điểm sắc nét (focus score) theo thời gian thực ra stdout, và ghi định kỳ một ảnh preview thu nhỏ ra đĩa để xem qua scp.
- Chọn chế độ: `auto` tự phát hiện có màn hình hay không; cho phép ép `gui` hoặc `headless_score` trong config.
- Điểm sắc nét: dùng một chỉ số dựa trên gradient hoặc variance of Laplacian, tính trên ảnh (hoặc vùng ROI) đã giảm độ phân giải. Số cao hơn là nét hơn.
- Giảm tải CPU: preview giảm độ phân giải (`downscale`) và giới hạn fps (`fps_limit`). Nêu rõ trong log rằng đây là chế độ tải cao tạm thời, không dùng cho vận hành.

### 5.7 Lưu ảnh và metadata
- Định dạng ảnh: giữ nguyên bit depth, không nén mất mát.
  - Mono8: lưu 8-bit.
  - Mono10 và Mono12: lưu 16-bit TIFF (giá trị nằm trong [0,1023] hoặc [0,4095], không cắt về 8-bit). Cho phép tùy chọn lưu thêm `.npy` để giữ giá trị chính xác.
- Với Mono10/12, xử lý đúng dữ liệu packed hay unpacked tùy pixel format thực tế. Xem Mục 6.2.
- Mỗi ảnh có một file metadata JSON cùng tên gốc, chứa tối thiểu:
  - timestamp host (UTC, có timezone).
  - timestamp camera nếu có, kèm độ phân giải.
  - model, serial, firmware.
  - pixel_format, exposure, gain, binning, ROI.
  - trạng thái read-back của các node linearity-critical.
  - tỉ lệ packet loss nếu có.

### 5.8 Dọn dẹp
- Dừng acquisition, đóng camera, giải phóng Harvester đúng thứ tự, kể cả khi lỗi hoặc Ctrl-C.

---

## 6. Ràng buộc và lưu ý kỹ thuật

### 6.1 Tải CPU trên Linux
- Không có kernel filter driver trên Linux như MVS trên Windows. GVSP receive, ráp khung, copy bộ nhớ đều chạy ở userspace, nặng với máy ít nhân.
- Hệ quả thiết kế: chụp đơn là mặc định. Streaming chỉ dùng cho preview canh nét, có giới hạn tải.

### 6.2 Bit depth và packed format
- Mono10/12 có thể truyền dạng packed hoặc unpacked tùy pixel format.
- Đọc pixel format thực tế của khung và giải nén đúng. Không giả định.
- Kiểm tra tính đúng bằng cách xác nhận dải giá trị hợp lệ ([0,4095] cho Mono12) và không có hiện tượng cắt hay tràn.
- Nếu có thể, ưu tiên format unpacked để tránh lỗi giải nén, và ghi rõ lựa chọn trong log.

### 6.3 Không đoán tên node GenICam
- Tên node từng thông số khác nhau giữa các dòng camera (ví dụ có thể là `ExposureTime` hoặc `ExposureTimeAbs`; gamma có thể là một node bật/tắt riêng hoặc gộp trong nhóm khác).
- Yêu cầu: dump node map của camera thật, đọc tên và kiểu node, rồi mới thao tác. Không hard-code tên đoán mò.
- Với mỗi thông số cần set, tra danh sách node ứng viên hợp lý, chọn node tồn tại trên camera này, và ghi log tên node đã dùng.

### 6.4 Cross-platform
- Một codebase. Khác biệt giữa Windows và Linux chỉ nằm ở: đường dẫn `.cti`, phát hiện màn hình cho preview. Cô lập hai điểm này, không rẽ nhánh logic ở nơi khác.

---

## 7. Cấu trúc mã và sản phẩm giao

Đề xuất, Claude Code có thể điều chỉnh nếu có lý do:

- `camera_info.py`: tiện ích dump toàn bộ node map (tên node, kiểu, giá trị hiện tại, dải hợp lệ, các enum hỗ trợ). Làm việc này trước tiên. Nó de-risk toàn bộ phần tên node, binning, pixel format.
- `capture.py`: chương trình chính. Kết nối, nạp config, ép thông số, chụp đơn, lưu.
- `focus.py`: chế độ canh nét trực tiếp (có thể gộp vào `capture.py` dưới dạng subcommand).
- `config.yaml`: file cấu hình mẫu.
- `README.md`: hướng dẫn cài đặt và chạy cho Windows và Linux.
- `requirements.txt` hoặc tương đương: ghi rõ Python 3.10/3.11 và các phụ thuộc. Không ghim phiên bản đoán mò; xác minh phiên bản `harvesters` khả dụng khi triển khai.

---

## 8. config.yaml mẫu

Các giá trị dưới đây là minh họa. Đường dẫn `.cti` và tên node phải điền theo môi trường và camera thật.

```yaml
camera:
  serial: null            # null -> lấy thiết bị đầu tiên; hoặc điền serial/IP
  ip: 192.168.100.253

gentl:
  # Đường dẫn .cti theo HĐH. Định vị trong MVS SDK, không đoán.
  cti_windows: "ĐIỀN_SAU_KHI_ĐỊNH_VỊ/MvProducerGEV.cti"
  cti_linux: "ĐIỀN_SAU_KHI_ĐỊNH_VỊ/MvProducerGEV.cti"

acquisition:
  pixel_format: Mono12    # Mono8 | Mono10 | Mono12 (xác minh giá trị từ node map)
  exposure_us: 10000
  gain: 0.0
  binning: 1              # 1 hoặc 2; chỉ bật nếu camera hỗ trợ, xác minh trước

enforce_linear:
  gamma: off
  noise_reduction: off
  auto_exposure: off
  auto_gain: off
  lut: off
  # awb: bỏ qua với cảm biến mono

output:
  dir: ./captures
  image_format: tiff16    # tiff16 | npy
  also_save_npy: false
  write_metadata_json: true

preview:
  enable: true
  mode: auto              # auto | gui | headless_score
  fps_limit: 5
  downscale: 4
  preview_image_path: ./preview_latest.png   # dùng cho chế độ headless
```

---

## 9. Tiêu chí nghiệm thu

- [ ] Cùng codebase chạy trên Windows và Linux, chỉ khác `.cti` trong config.
- [ ] Kết nối camera, in đúng model, serial, firmware.
- [ ] Read-back xác nhận gamma, noise reduction, auto exposure, auto gain, LUT ở trạng thái tắt. Nếu không tắt được, chương trình dừng và báo lỗi rõ, không chụp.
- [ ] Chụp đơn Mono8 lưu đúng 8-bit.
- [ ] Chụp đơn Mono12 lưu 16-bit, giá trị trong [0,4095], không bị cắt về 8-bit.
- [ ] Metadata JSON đủ trường ở Mục 5.7.
- [ ] Preview GUI chạy trên máy có màn hình.
- [ ] Chế độ headless in focus score thời gian thực và ghi ảnh preview thu nhỏ.
- [ ] Một khung Mono12 full-res trên Linux đạt zero packet loss, hoặc báo cáo tỉ lệ mất gói kèm gợi ý khắc phục.
- [ ] Dọn dẹp sạch khi thoát bình thường và khi Ctrl-C.

---

## 10. Việc cần xác minh khi triển khai (không đoán)

Ghi lại kết quả xác minh vào README hoặc log:

- Tên node GenICam chính xác cho từng thông số trên camera này.
- Camera có hỗ trợ on-sensor 2x2 binning không, và ở pixel format nào.
- Mono10/12 truyền packed hay unpacked, và cách giải nén đúng.
- Packet size và MTU tối ưu; có cần jumbo frame để tránh mất gói ở full-res không.
- Camera timestamp có sẵn không và độ phân giải bao nhiêu.
- Phiên bản `harvesters` khả dụng cho Python 3.10/3.11 tại thời điểm cài.
