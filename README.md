# 🌐 ĐỒ ÁN PBL4: CÔNG CỤ GIÁM SÁT BĂNG THÔNG MẠNG (NETWORK MONITOR)

> **Tên đề tài:** Công cụ giám sát băng thông mạng (Network Monitor)  
> **Mô tả:** Thiết kế và triển khai công cụ giám sát băng thông và lưu lượng mạng theo thời gian thực trên hệ điều hành Windows theo mô hình Clean Architecture kết hợp tương tác Kernel (Event Tracing for Windows - ETW & Windows Defender Firewall / WinDivert Packet Shaper).

---

## 📌 1. Giới thiệu tổng quan

**Công cụ giám sát băng thông mạng (Network Monitor)** là ứng dụng desktop chạy trên nền tảng Windows, cung cấp khả năng đo lường, phân tích và trực quan hóa lưu lượng mạng theo thời gian thực với phong cách giao diện hiện đại (lấy cảm hứng từ *GlassWire*). 

Hệ thống được thiết kế theo kiến trúc **Clean Architecture** 4 tầng (Domain - Infrastructure - Application - Presentation), đảm bảo tính tách biệt, dễ bảo trì và hỗ trợ mô hình mạng quản lý tập trung nhiều máy Client-Agent trong mạng LAN.

---

## ✨ 2. Các tính năng chính

Ứng dụng bao gồm 5 phân hệ chức năng chính:

1. **📊 Giám sát Băng thông Thời gian thực (Traffic Monitor):**
   * Vẽ biểu đồ sóng Upload / Download mượt mà với thuật toán làm mịn Gaussian và nội suy Hermite chống rung giật.
   * Thống kê chi tiết tốc độ (KB/s, MB/s), tổng dung lượng và phân bổ tỷ lệ WAN / LAN.
   * **Timeline Scrubber:** Dễ dàng kéo tua lại thời gian, click vào một điểm bất kỳ trên biểu đồ để tạm dừng và xem chi tiết tốc độ tại mốc giờ cụ thể; bấm tiếp tục để quay lại dòng dữ liệu trực tiếp.
   * Danh sách tiến trình (Process Monitor) hiển thị chi tiết mức tiêu thụ băng thông của từng ứng dụng đang chạy.

2. **🛡️ Kiểm soát & Giới hạn Băng thông (Protect & QoS Policy):**
   * Can thiệp an ninh mạng với cơ chế Chặn (Block/Unblock) ứng dụng qua Windows Firewall.
   * Giới hạn băng thông (Bandwidth Throttling) thời gian thực thông qua driver tầng nhân WinDivert (Token Bucket).
   * Kiểm tra tính toàn vẹn của ứng dụng đang truyền dữ liệu.

3. **🔍 Quét Kết nối Mạng (Network Scanner):**
   * Liệt kê danh sách các kết nối socket đang hoạt động (Local IP/Port, Remote IP/Port, Giao thức TCP/UDP, Trạng thái `ESTABLISHED`, `LISTEN`,...).

4. **📜 Nhật ký Hoạt động & Chính sách Mạng (Log Analysis):**
   * Toàn bộ lịch sử thao tác an ninh (Chặn, Bỏ chặn, Bóp băng thông) được lưu trữ an toàn, đa luồng vào tệp nhật ký `logs/policies.log`.
   * Lịch sử kết nối TCP/UDP của từng tiến trình được lưu vào `logs/connections.log`.
   * Giao diện Log Analysis trực quan với bảng tra cứu, hỗ trợ tìm kiếm và lọc sự kiện theo tên tiến trình, hành động.

5. **💻 Giám sát Tài nguyên Phần cứng (Hardware Resources):**
   * Theo dõi thời gian thực mức tải CPU, dung lượng RAM khả dụng và tốc độ đọc/ghi dữ liệu ổ cứng (Disk I/O).

6. **🌐 Quản lý Tập trung & Bảo mật Giao thức (Admin Fleet & Client Agent):**
   * Quản lý nhiều máy trạm Client Agent trong mạng LAN từ Admin Dashboard (`admin_main.py`), hỗ trợ xem telemetry thời gian thực và áp đặt chính sách Firewall / QoS từ xa.
   * **Bảo mật giao thức TCP:** Bắt tay xác thực 2 chiều Challenge-Response (PSK + HMAC-SHA256), dẫn xuất khóa phiên, bảo vệ toàn vẹn và chống replay theo `[Length][Seq][HMAC]`.
   * **Phân tầng chính sách 2 lớp:** Chính sách do Admin áp đặt (`Admin Enforced`) được khóa cứng, ưu tiên cao hơn chính sách cục bộ (`Local Self-Protect`) trên máy con.

---

## 🏗️ 3. Cấu trúc thư mục dự án

```text
NetworkMonitor/
├── main.py                              # Khởi chạy ứng dụng độc lập (Standalone)
├── admin_main.py                        # Khởi chạy Máy Chủ Quản Trị (Admin Server)
├── client_main.py                       # Khởi chạy Máy Con (Client Agent: Tray / GUI / Headless)
├── build_dist.py                        # Script đóng gói PyInstaller (.exe) kèm driver WinDivert
├── requirements.txt                     # Danh sách thư viện Python cần thiết
│
├── config/                              # Tầng cấu hình
│   ├── settings.py                      # Chu kỳ lấy mẫu, cổng TCP, cấu hình bảo mật PSK/timeout
│   └── client_config.json               # Cấu hình kết nối mặc định của Client Agent
│
├── models/                              # Tầng thực thể (Domain Model)
│   ├── network_sample.py                # Cấu trúc dữ liệu đo lường băng thông & tiến trình
│   └── network_protocol.py              # Giao thức truyền thông TCP giữa Admin & Client
│
├── infrastructure/                      # Tầng hạ tầng & Tương tác Kernel / Mạng
│   ├── network_collector.py             # Lấy số liệu lưu lượng mạng qua psutil
│   ├── etw_consumer.py                  # Đọc gói tin qua Event Tracing for Windows (ETW API)
│   ├── packet_shaper.py                 # Driver WinDivert bóp băng thông đa chính sách cô lập & Firewall
│   ├── protocol_framing.py              # Đóng gói nhị phân, HMAC-SHA256 & kiểm tra sequence chống replay
│   ├── network_server.py                # TCP Server quản lý đa kết nối & xác thực Client
│   └── network_client.py                # TCP Client tự động kết nối lại, Heartbeat & khử trùng lặp lệnh
│
├── application/                         # Tầng nghiệp vụ
│   ├── bandwidth_calculator.py          # Thuật toán tính toán delta tốc độ mạng
│   ├── monitor_service.py               # Điều phối luồng dữ liệu chế độ Standalone
│   ├── fleet_manager_service.py         # Quản lý đội ngũ máy trạm tập trung cho Admin
│   └── client_agent_service.py          # Dịch vụ Agent trên máy trạm & phân tầng chính sách 2 lớp
│
├── presentation/                        # Tầng giao diện người dùng (GUI)
│   ├── dashboard.py                     # Dashboard PyQt6 + PyQtGraph đa tab
│   ├── widgets/                         # Các widget đồ họa tách rời khỏi Dashboard
│   │   └── dashboard_widgets.py         # ArcGauge, WanLanWidget, TimelineScrubber, InspectionBar,...
│   ├── admin_fleet_view.py              # Giao diện quản lý danh sách máy trạm tập trung
│   ├── protect_view.py                  # Giao diện điều khiển chính sách chặn & bóp băng thông
│   ├── traffic_usage_view.py            # Giao diện thống kê lịch sử sử dụng lưu lượng
│   ├── client_status_window.py          # Cửa sổ trạng thái & chẩn đoán dành cho Client Agent
│   ├── client_tray.py                   # Quản lý System Tray Icon & thông báo Toast trên Client
│   ├── glasswire_icons.py               # Bộ icon vector cho giao diện
│   └── styles.py                        # Theme & stylesheet PyQt6
│
├── logs/                                # Thư mục lưu nhật ký hệ thống (hỗ trợ xoay vòng tự động)
│   ├── policies.log                     # Lịch sử các thao tác chính sách mạng (Block/QoS)
│   └── connections.log                  # Lịch sử socket kết nối mạng
│
├── tests/                               # Bộ kiểm thử tự động (unittest)
└── utils/                               # Tiện ích dùng chung
    ├── env_setup.py                     # Thiết lập môi trường hệ thống & UTF-8
    ├── file_logger.py                   # Ghi nhật ký file văn bản thread-safe có xoay vòng (5MB x 3)
    └── safe_print.py                    # Hàm in log tương thích mã hóa UTF-8 tiếng Việt
```

---

## ⚙️ 4. Yêu cầu hệ thống

* **Hệ điều hành:** Windows 10 hoặc Windows 11 (64-bit).
* **Python:** Phiên bản 3.10 trở lên.
* **Quyền thực thi:** Khuyến nghị chạy dưới quyền **Administrator** để kích hoạt đầy đủ các tính năng ETW, Firewall và driver WinDivert.

---

## 🚀 5. Hướng dẫn cài đặt & Khởi chạy

### Bước 1: Thiết lập môi trường ảo Python & Cài đặt thư viện
Mở PowerShell tại thư mục dự án:
```powershell
# 1. Tạo môi trường ảo (nếu chưa có)
python -m venv venv

# 2. Kích hoạt môi trường ảo
.\venv\Scripts\Activate.ps1

# 3. Cài đặt các thư viện cần thiết
pip install -r requirements.txt
```

### Bước 2: Khởi chạy ứng dụng
- **Chạy chế độ Độc lập (Standalone Monitor):**
  ```powershell
  python main.py
  ```
- **Chạy Máy Chủ Quản Trị (Admin Server):**
  ```powershell
  python admin_main.py --port 9988
  ```
- **Chạy Máy Con (Client Agent):**
  ```powershell
  python client_main.py --server 127.0.0.1 --port 9988
  ```
- **Thiết lập khóa bảo mật PSK riêng (khuyến nghị khi triển khai LAN):**
  ```powershell
  $env:PBL4_PSK = "Your-Strong-PreShared-Key"
  ```
- **Chạy kiểm thử tự động & Đóng gói `.exe`:**
  ```powershell
  python -m unittest discover -s tests -q
  python build_dist.py
  ```

> [!TIP]
> Để ứng dụng theo dõi được lưu lượng chi tiết của từng ứng dụng/tiến trình thông qua kernel ETW và thiết lập can thiệp mạng WinDivert, hãy mở terminal với tùy chọn **Run as Administrator**.

---

## 📜 6. Quản lý Nhật ký Hệ thống (File Logging)

Hệ thống sử dụng cơ chế ghi nhật ký phân tầng trực tiếp vào thư mục `logs/` (tự động xoay vòng khi vượt quá 5 MB, giữ tối đa 3 file sao lưu `.1`–`.3`):
* **`logs/policies.log`**: Lưu trữ toàn bộ lịch sử các thao tác bảo mật gồm: Thời gian, Loại thao tác (`POLICY_BLOCK`, `POLICY_UNBLOCK`, `POLICY_APPLY`, `POLICY_REMOVE`), Tên tiến trình, Hướng can thiệp, Mức trần tốc độ và Trạng thái thực thi.
* **`logs/connections.log`**: Lưu trữ các phiên kết nối mạng TCP/UDP mới của các tiến trình kèm địa chỉ Local, Remote và trạng thái socket.

---

## 🛠️ 7. Công nghệ sử dụng

* **UI Framework:** [PyQt6](https://www.riverbankcomputing.com/software/pyqt/)
* **Real-time Charting:** [PyQtGraph](https://www.pyqtgraph.org/)
* **System & Kernel Metrics:** [psutil](https://github.com/giampaolo/psutil), Windows ETW (Event Tracing for Windows), ctypes Win32 API
* **Kernel Packet Shaping:** [pydivert](https://github.com/ffalcinelli/pydivert) (WinDivert 64-bit kernel driver)
* **Numeric Processing:** [NumPy](https://numpy.org/)
* **Logging:** Thread-safe file-based logging (`logs/policies.log`, `logs/connections.log`)
