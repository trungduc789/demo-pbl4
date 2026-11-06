"""
Điểm khởi chạy ứng dụng Quản Trị Trung Tâm Admin (Admin Dashboard Entrypoint):
Quản lý tập trung toàn bộ đội ngũ máy trạm:
- Xem danh sách các máy con (Trạng thái Online/Offline, Tốc độ Upload/Download, IP, OS).
- Khung nhìn chi tiết khi click chọn máy con cụ thể.
- Can thiệp Firewall (Block/Unblock) và Giới hạn tốc độ (QoS Throttling) từ xa.

Sử dụng:
    python admin_main.py [--host 0.0.0.0] [--port 9988] [--no-db]
"""

import sys
import os
import argparse
from PyQt6.QtWidgets import QApplication

# Đảm bảo đường dẫn gốc của project có trong sys.path
if getattr(sys, "frozen", False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from utils.env_setup import setup_system_environment
from utils.safe_print import safe_print as print
from config.settings import SERVER_DEFAULT_HOST, SERVER_DEFAULT_PORT
from presentation.styles import setup_application_theme
from application.fleet_manager_service import FleetManagerService
from presentation.dashboard import BandwidthDashboard


def main():
    parser = argparse.ArgumentParser(description="Network Traffic Monitor - Centralized Admin Dashboard")
    parser.add_argument("--host", type=str, default=SERVER_DEFAULT_HOST, help="Địa chỉ IP lắng nghe (mặc định 0.0.0.0)")
    parser.add_argument("--port", type=int, default=SERVER_DEFAULT_PORT, help="Cổng TCP nhận kết nối từ các máy con")
    parser.add_argument("--no-db", action="store_true", help="Vô hiệu hóa lưu MySQL (dùng khi kiểm thử không có XAMPP/MySQL)")
    args = parser.parse_args()

    # 1. Khởi tạo môi trường hệ thống và mã hóa UTF-8
    setup_system_environment()

    print("=" * 75)
    print("    NETWORK TRAFFIC MONITOR & PACKET SHAPER - ADMIN DASHBOARD")
    print("=" * 75)
    print(f"[*] Máy chủ quản trị đang lắng nghe tại: {args.host}:{args.port}")
    print(f"[*] CSDL MySQL tập trung: {'Tắt' if args.no_db else 'Bật'}")
    print("[*] Đang khởi tạo giao diện quản trị Admin...\n")

    # 2. Khởi tạo dịch vụ FleetManagerService
    fleet_service = FleetManagerService(
        host=args.host,
        port=args.port,
        enable_db=not args.no_db,
    )
    fleet_service.start()

    # 3. Khởi tạo ứng dụng Qt và áp dụng giao diện GlassWire
    app = QApplication(sys.argv)
    setup_application_theme(app)

    # 4. Khởi tạo cửa sổ chính với chế độ Admin Fleet
    dashboard = BandwidthDashboard(
        monitor_service=None,
        fleet_service=fleet_service,
    )
    dashboard.setWindowTitle("Network Traffic Monitor & Packet Shaper - Admin Fleet Management")
    dashboard.show()

    # 5. Đảm bảo dịch vụ máy chủ dừng an toàn khi đóng ứng dụng Qt
    app.aboutToQuit.connect(fleet_service.stop)

    # 6. Bắt đầu vòng lặp sự kiện chính của ứng dụng
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
