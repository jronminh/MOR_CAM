# Nhiệm vụ Claude Code: Dump node map camera để xác minh thông số

## Mục tiêu
Viết và chạy một tiện ích Python đọc toàn bộ node map (GenICam feature) của camera GigE, rồi xuất ra file. Chỉ đọc, không đổi bất kỳ setting nào. Kết quả dùng để điền tên node và giá trị pixel format thật vào đặc tả PoC.

## Model và thông số đã xác minh (dùng làm đối chứng)
Camera: Hikrobot MV-CE200-10GM.
- Cảm biến IMX183, 1", CMOS, rolling shutter.
- Độ phân giải 5472 x 3648, 20MP, 5.9 fps, pixel size 2.4 μm.
- Giao tiếp GigE, cấp nguồn PoE và 12VDC, dải nhiệt 0~50°C.
- Dải exposure khoảng 46 μs đến 2 giây.
- Pixel format hỗ trợ theo catalog: Mono 8 / 10 / 10p / 12 / 12p.

Nếu node map thật khác các con số này, ghi lại điểm khác biệt. Đó là tín hiệu cần kiểm tra (nhầm model, firmware lạ).

Datasheet: https://www.rmaelectronics.com/content/HikRobot/CameraDatasheets/MV-CE200-10GMGC_en.pdf

Lưu ý pixel format: có cả bản unpacked (Mono10, Mono12) lẫn packed (Mono10p, Mono12p). Xác nhận bản unpacked có tồn tại; nó tránh được bước giải nén khi lưu ảnh.

## Chuẩn bị môi trường
- Python 3.10 hoặc 3.11. harvesters chỉ có wheel biên dịch sẵn tới CPython 3.11. Không dùng 3.12 trở lên.
- Cài `harvesters` (không dùng `harvesters-util`). `genicam` tự kéo theo.
- Định vị `MvProducerGEV.cti` trong Hikrobot MVS SDK. Không đoán đường dẫn; tìm file thật rồi đăng ký với Harvester.
- Kiểm tra host và camera không trùng IP. Camera thường ở 192.168.100.253; host phải khác (ví dụ 192.168.100.2). Trùng IP là lỗi đã gặp khiến không dò được thiết bị.

## Việc cần làm
1. Viết `camera_info.py`: đăng ký `.cti`, kết nối camera mà không đổi setting nào, duyệt toàn bộ node map.
2. Với mỗi node, ghi: tên, kiểu (Integer / Float / Enumeration / Boolean / Command / String), giá trị hiện tại, min/max nếu có, danh sách enum nếu có, quyền truy cập (RO/RW/WO), và mô tả nếu có.
3. Chạy và xuất hai file ở Mục "Định dạng đầu ra".

## Trích riêng các node quan trọng
Ngoài dump đầy đủ, lọc riêng các node liên quan tới dự án. Không đoán tên. Tìm node có tên chứa các từ khóa sau rồi ghi lại tên thật, kiểu, giá trị hiện tại, và dải hợp lệ:
- Exposure: từ khóa "Exposure" (thời gian phơi sáng, ExposureAuto, ExposureMode).
- Gain: từ khóa "Gain" (gain và GainAuto).
- Gamma: từ khóa "Gamma" (node bật/tắt và giá trị).
- Pixel format: "PixelFormat". Liệt kê đủ danh sách enum hỗ trợ.
- Binning: "Binning". Có hỗ trợ không, các mode, có on-sensor 2x2 không.
- Black level: "BlackLevel" (và BlackLevelAuto nếu có).
- Noise reduction: từ khóa "Noise" hoặc "Denoise".
- LUT: "LUT" (LUTEnable, LUTSelector).
- Timestamp: "Timestamp". Có sẵn không, độ phân giải bao nhiêu.
- Packet: "PacketSize" và các node chứa "Packet" (để chỉnh jumbo frame sau).
- Thiết bị: model, serial, firmware (DeviceModelName, DeviceSerialNumber, DeviceFirmwareVersion hoặc tương đương).

Với mỗi mục, ghi rõ node có tồn tại hay không. Nếu một chức năng không có node tương ứng, ghi "không tìm thấy", không để trống.

## Định dạng đầu ra
- `node_map_full.txt` hoặc `.json`: dump đầy đủ, verbatim.
- `camera_report.md`: bản tóm tắt gồm thông tin thiết bị, bảng các node quan trọng ở trên, và phần đối chứng với thông số datasheet (khớp hay lệch).

## Ràng buộc
- Chỉ đọc. Không set, không ghi node nào ở bước này.
- Không hard-code hay đoán tên node. Đọc từ node map thật.
- Xác minh phiên bản `harvesters` khả dụng cho Python 3.10/3.11 tại thời điểm cài.
- Nếu không kết nối được, báo nguyên nhân thường gặp: sai đường dẫn `.cti`, trùng IP host và camera, chưa cấp nguồn PoE, sai subnet.

## Nguồn tra cứu
- Datasheet model (URL ở trên).
- Tài liệu MVS SDK của Hikrobot: feature reference, tên node GenICam.
- Chuẩn GenICam SFNC cho quy ước đặt tên node chung.
