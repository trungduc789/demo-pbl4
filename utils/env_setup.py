import os
import sys
import faulthandler
import traceback

def global_exception_handler(exc_type, exc_value, exc_traceback):
    """Ghi nhận mọi ngoại lệ Python chưa xử lý thay vì đóng ứng dụng im lặng."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    print("\n[CRITICAL ERROR] Ngoại lệ chưa xử lý:", file=sys.stderr)
    traceback.print_exception(exc_type, exc_value, exc_traceback)

def setup_system_environment():
    """Thiết lập môi trường thực thi hệ thống, bảng mã UTF-8 và bộ xử lý lỗi C/Python."""
    # 1. Đảm bảo console Windows hỗ trợ UTF-8 (tiếng Việt có dấu) tránh lỗi UnicodeEncodeError
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

    # 2. Bật faulthandler để bắt lỗi bộ nhớ cấp C (nếu có từ socket / WinDivert driver)
    try:
        faulthandler.enable()
    except Exception:
        pass

    # 3. Đăng ký bộ xử lý ngoại lệ toàn cục
    sys.excepthook = global_exception_handler
