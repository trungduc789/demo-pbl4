# 🌐 ĐỒ ÁN PBL4: CÔNG CỤ GIÁM SÁT BĂNG THÔNG MẠNG (NETWORK MONITOR)

> **Tên đề tài:** Công cụ giám sát băng thông mạng (Network Monitor)  
> **Mô tả:** Thiết kế và triển khai công cụ giám sát băng thông và lưu lượng mạng theo thời gian thực trên hệ điều hành Windows theo mô hình Clean Architecture kết hợp tương tác Kernel (Event Tracing for Windows - ETW & Windows QoS Policy).

---

## 📌 1. Giới thiệu tổng quan

**Công cụ giám sát băng thông mạng (Network Monitor)** là ứng dụng desktop chạy trên nền tảng Windows, cung cấp khả năng đo lường, phân tích và trực quan hóa lưu lượng mạng theo thời gian thực với phong cách giao diện hiện đại (lấy cảm hứng từ *GlassWire*). 

Hệ thống được thiết kế theo kiến trúc **Clean Architecture** 4 tầng (Domain - Infrastructure - Application - Presentation), đảm bảo tính tách biệt, dễ bảo trì và sẵn sàng mở rộng thành mô hình mạng quản lý tập trung nhiều máy Client-Agent trong mạng LAN.

---

## ✨ 2. Các tính năng chính

Ứng dụng bao gồm 5 phân hệ chức năng chính:

1. **📊 Giám sát Băng thông Thời gian thực (Traffic Monitor):**
   * Vẽ biểu đồ sóng Upload / Download mượt mà với thuật toán làm mịn Gaussian và nội suy Hermite chống rung giật.
   * Thống kê chi tiết tốc độ (KB/s, MB/s), tổng dung lượng và phân bổ tỷ lệ WAN / LAN.
   * **Timeline Scrubber:** Dễ dàng kéo tua lại thời gian, click vào một điểm bất kỳ trên biểu đồ để tạm dừng và xem chi tiết tốc độ tại mốc giờ cụ thể; bấm tiếp tục để quay lại dòng dữ liệu trực tiếp.
   * Danh sách tiến trình (Process Monitor) hiển thị chi tiết mức tiêu thụ băng thông của từng ứng dụng đang chạy.

2. **🛡️ Kiểm soát & Giới hạn Băng thông (Protect & QoS Policy):**
   * Tương tác với Windows QoS Policy (PowerShell `NetQosPolicy`) để thiết lập chính sách giới hạn tốc độ Upload/Throttling đối với từng tiến trình chỉ định.
   * Kiểm tra tính toàn vẹn của ứng dụng đang truyền dữ liệu.

3. **🔍 Quét Kết nối Mạng (Network Scanner):**
   * Liệt kê danh sách các kết nối socket đang hoạt động (Local IP/Port, Remote IP/Port, Giao thức TCP/UDP, Trạng thái `ESTABLISHED`, `LISTEN`,...).

4. **🗄️ Nhật ký & Xem lại Lịch sử (Log Analysis & Database History):**
   * Tích hợp cơ sở dữ liệu **MySQL** 6 bảng: `devices`, `network_interfaces`, `interface_traffic`, `processes`, `process_traffic`, `network_connections`.
   * Hỗ trợ chọn một ngày bất kỳ trong quá khứ để tải lại và tái hiện toàn bộ biểu đồ lưu lượng đã đo được của ngày hôm đó.
   * Tự động khởi tạo cấu trúc CSDL và các bảng khi chạy ứng dụng lần đầu.

5. **💻 Giám sát Tài nguyên Phần cứng (Hardware Resources):**
   * Theo dõi thời gian thực mức tải CPU, dung lượng RAM khả dụng và tốc độ đọc/ghi dữ liệu ổ cứng (Disk I/O).

---

## 🏗️ 3. Cấu trúc thư mục dự án

```text
NetworkMonitor/
├── main.py                          # File chạy chính của ứng dụng
├── requirements.txt                 # Danh sách thư viện Python cần thiết
├── demo.sql                         # File DDL cơ sở dữ liệu MySQL
├── database.txt                     # Mô tả sơ đồ quan hệ cơ sở dữ liệu
├── pharse1.txt                      # Kế hoạch và ghi chú yêu cầu kỹ thuật
├── tổng quan dự án .txt             # Bản tóm tắt tổng quan kiến trúc dự án
│
├── config/                          # Tầng cấu hình
│   └── settings.py                  # Cấu hình chu kỳ lấy mẫu, bộ đệm, thông số MySQL
│
├── models/                          # Tầng thực thể (Domain Model)
│   └── network_sample.py            # Cấu trúc dữ liệu đo lường băng thông & tiến trình
│
├── infrastructure/                  # Tầng hạ tầng & Tương tác Kernel
│   ├── network_collector.py         # Lấy số liệu lưu lượng mạng qua psutil
│   ├── database_manager.py          # Quản lý kết nối và truy vấn CSDL MySQL
│   ├── etw_consumer.py              # Đọc gói tin qua Event Tracing for Windows (ETW API)
│   └── qos_manager.py               # Quản lý chính sách điều tiết QoS Policy
│
├── application/                     # Tầng nghiệp vụ
│   ├── bandwidth_calculator.py      # Thuật toán tính toán delta tốc độ mạng
│   └── monitor_service.py           # Điều phối luồng dữ liệu trung tâm
│
├── presentation/                    # Tầng giao diện người dùng (GUI)
│   └── dashboard.py                 # Dashboard PyQt6 + PyQtGraph đa tab
│
└── utils/                           # Tiện ích dùng chung
    └── safe_print.py                # Hàm in log tương thích mã hóa UTF-8 tiếng Việt
```

---

## ⚙️ 4. Yêu cầu hệ thống

* **Hệ điều hành:** Windows 10 hoặc Windows 11 (64-bit).
* **Python:** Phiên bản 3.10 trở lên.
* **Cơ sở dữ liệu:** MySQL 8.0+ hoặc MariaDB (Khuyến nghị dùng qua **XAMPP**).
* **Quyền thực thi:** Khuyến nghị chạy dưới quyền **Administrator** để kích hoạt đầy đủ các tính năng ETW và QoS.

---

## 🚀 5. Hướng dẫn cài đặt & Khởi chạy

### Bước 1: Khởi động Cơ sở dữ liệu (XAMPP)
1. Mở **XAMPP Control Panel**.
2. Nhấn **Start** cho 2 dịch vụ:
   * **MySQL** (cổng mặc định `3306`)
   * **Apache** (cổng `80` hoặc `8080`)
3. Kiểm tra cấu hình kết nối trong file [config/settings.py](config/settings.py):
   ```python
   DB_CONFIG = {
       "host": "127.0.0.1",
       "port": 3306,
       "user": "root",
       "password": "",         # Điền mật khẩu MySQL nếu có đặt
       "database": "demo",
       "charset": "utf8mb4",
       "autocommit": True,
       "use_pure": True,
   }
   ```
   *(Ứng dụng sẽ tự động khởi tạo database `demo` và các bảng khi chạy lần đầu).*

### Bước 2: Thiết lập môi trường ảo Python & Cài đặt thư viện
Mở PowerShell tại thư mục dự án:
```powershell
# 1. Tạo môi trường ảo (nếu chưa có)
python -m venv venv

# 2. Kích hoạt môi trường ảo
.\venv\Scripts\Activate.ps1

# 3. Cài đặt các thư viện cần thiết
pip install -r requirements.txt
```

### Bước 3: Khởi chạy ứng dụng
Chạy lệnh:
```powershell
python main.py
```
> [!TIP]
> Để ứng dụng theo dõi được lưu lượng chi tiết của từng ứng dụng/tiến trình thông qua kernel ETW và thiết lập QoS Policy, hãy mở terminal với tùy chọn **Run as Administrator**.

---

## 📦 6. Hướng dẫn Đóng gói thành file thực thi (.EXE)

Dự án có thể đóng gói thành một file thực thi độc lập (không cần cài Python trên máy khác) thông qua `PyInstaller`:

```powershell
.\venv\Scripts\pyinstaller --noconsole --onefile main.py
```
* File `.exe` sau khi build xong sẽ nằm trong thư mục: `dist/main.exe`.

---

## 📊 7. Sơ đồ cơ sở dữ liệu (Database Schema)

```text
                  ┌─────────────────────┐
                  │       DEVICES       │
                  └──────────┬──────────┘
                             │
            ┌────────────────┼────────────────┐
           1:N              1:N              1:N
            │                │                │
            ▼                ▼                ▼
   ┌────────────────┐ ┌──────────────┐ ┌────────────────────┐
   │    NETWORK     │ │   PROCESSES  │ │ NETWORK_CONNECTIONS│
   │   INTERFACES   │ └──────┬───────┘ └────────────────────┘
   └───────┬────────┘        │
          1:N               1:N
           │                 │
           ▼                 ▼
┌────────────────────┐ ┌────────────────────┐
│ INTERFACE_TRAFFIC  │ │  PROCESS_TRAFFIC   │
└────────────────────┘ └────────────────────┘
```

---

## 🛠️ 8. Công nghệ sử dụng

* **UI Framework:** [PyQt6](https://www.riverbankcomputing.com/software/pyqt/)
* **Real-time Charting:** [PyQtGraph](https://www.pyqtgraph.org/)
* **System & Kernel Metrics:** [psutil](https://github.com/giampaolo/psutil), Windows ETW (Event Tracing for Windows), ctypes Win32 API
* **Database Driver:** [mysql-connector-python](https://dev.mysql.com/doc/connector-python/en/)
* **Numeric Processing:** [NumPy](https://numpy.org/)
