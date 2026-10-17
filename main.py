import sys
import os
import faulthandler

# Đảm bảo console Windows hỗ trợ UTF-8 (tiếng Việt có dấu) tránh lỗi UnicodeEncodeError
if sys.platform.startswith("win"):
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

# Bật faulthandler để bắt lỗi bộ nhớ cấp C (nếu có)
faulthandler.enable()

def global_exception_handler(exc_type, exc_value, exc_traceback):
    """Ghi nhận mọi ngoại lệ Python chưa xử lý thay vì đóng ứng dụng im lặng."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    import traceback
    print("\n[CRITICAL ERROR] Ngoại lệ chưa xử lý:", file=sys.stderr)
    traceback.print_exception(exc_type, exc_value, exc_traceback)

sys.excepthook = global_exception_handler

from PyQt6.QtWidgets import QApplication
from application.monitor_service import MonitorService
from presentation.dashboard import BandwidthDashboard

def main():
    try:
        app = QApplication(sys.argv)
        
        # Khởi tạo service giám sát và giao diện đồ họa
        service = MonitorService()
        dashboard = BandwidthDashboard(monitor_service=service)
        dashboard.show()
        
        sys.exit(app.exec())
    except Exception as e:
        import traceback
        print(f"[Fatal App Error] {e}", file=sys.stderr)
        traceback.print_exc()

if __name__ == "__main__":
    main()