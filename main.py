import sys
from PyQt6.QtWidgets import QApplication

from utils.env_setup import setup_system_environment
from presentation.styles import setup_application_theme
from application.monitor_service import MonitorService
from presentation.dashboard import BandwidthDashboard


def main():
    """Điểm khởi chạy chính của ứng dụng Network Traffic Monitor & Packet Shaper."""
    # 1. Khởi tạo môi trường hệ thống, UTF-8 và bộ xử lý lỗi
    setup_system_environment()

    # 2. Khởi tạo ứng dụng Qt và áp dụng giao diện
    app = QApplication(sys.argv)
    setup_application_theme(app)

    # 3. Khởi chạy tầng nghiệp vụ (Service) và giao diện người dùng (Dashboard)
    service = MonitorService()
    dashboard = BandwidthDashboard(monitor_service=service)
    dashboard.show()

    # 4. Bắt đầu vòng lặp sự kiện chính của ứng dụng
    sys.exit(app.exec())


if __name__ == "__main__":
    main()