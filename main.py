import sys
from PyQt6.QtWidgets import QApplication
from application.monitor_service import MonitorService
from presentation.dashboard import BandwidthDashboard

def main():
    app = QApplication(sys.argv)
    
    # Khởi tạo service giám sát và giao diện đồ họa[cite: 3]
    service = MonitorService()
    dashboard = BandwidthDashboard(monitor_service=service)
    dashboard.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()