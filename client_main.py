"""
Điểm khởi chạy Agent Máy Con (Client Agent Entrypoint):
- Tự động kiểm tra quyền Quản trị viên (Windows UAC Admin) để hỗ trợ WinDivert & Firewall.
- Đọc cấu hình từ `config/client_config.json` (hỗ trợ ghi đè qua CLI flags).
- Khởi chạy nền System Tray Icon với menu điều khiển nhanh.
- Bắn thông báo Toast góc màn hình khi Admin can thiệp từ xa.
- Mở giao diện Cấu hình & Trạng thái kết nối (ClientStatusWindow).
- Hỗ trợ chế độ headless qua cờ `--headless` cho môi trường không có GUI.

Sử dụng:
    python client_main.py [--server 127.0.0.1] [--port 9988] [--show-window] [--no-elevate] [--headless]
"""

import sys
import os
import json
import time
import signal
import argparse

# Đảm bảo đường dẫn gốc của project có trong sys.path
if getattr(sys, "frozen", False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from utils.env_setup import setup_system_environment
from utils.safe_print import safe_print as print
from config.settings import SERVER_DEFAULT_PORT
from application.client_agent_service import ClientAgentService

CONFIG_FILE = os.path.join(CURRENT_DIR, "config", "client_config.json")


def is_admin() -> bool:
    """Kiểm tra tiến trình hiện tại có đang sở hữu đặc quyền Administrator hay không."""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def request_elevation():
    """Tự động kích hoạt hộp thoại UAC để yêu cầu quyền Administrator trên Windows."""
    if sys.platform != "win32":
        return
    import ctypes
    script = os.path.abspath(sys.argv[0])
    # Chuẩn bị tham số cho tiến trình con
    params = " ".join([f'"{arg}"' if " " in arg else arg for arg in sys.argv[1:]])
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{script}" {params}'.strip(), None, 1
    )
    if int(ret) > 32:
        # Tiến trình Admin đã khởi tạo thành công, tiến trình gốc thoát
        sys.exit(0)
    else:
        print("[ClientAgent Warning] Người dùng đã từ chối cấp quyền UAC Administrator. Agent sẽ tiếp tục chạy với quyền hạn hạn chế.")


def load_config() -> dict:
    """Đọc tệp cấu hình client_config.json nếu tồn tại."""
    defaults = {
        "server_host": "127.0.0.1",
        "server_port": SERVER_DEFAULT_PORT,
        "enable_etw": True,
        "show_toast_notifications": True,
    }
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception as e:
            print(f"[ClientConfig Warning] Lỗi đọc {CONFIG_FILE}: {e}")
    return defaults


def main():
    parser = argparse.ArgumentParser(description="Network Traffic Monitor & Packet Shaper - Client Agent")
    parser.add_argument("--server", type=str, default=None, help="Địa chỉ IP của máy chủ Admin (Server)")
    parser.add_argument("--port", type=int, default=None, help="Cổng TCP của máy chủ Admin")
    parser.add_argument("--no-etw", action="store_true", help="Vô hiệu hóa ETW Consumer")
    parser.add_argument("--no-elevate", action="store_true", help="Bỏ qua tự động yêu cầu nâng quyền Administrator (UAC)")
    parser.add_argument("--headless", action="store_true", help="Chạy ở chế độ dòng lệnh thuần túy không hiển thị System Tray/GUI")
    parser.add_argument("--show-window", action="store_true", help="Tự động mở cửa sổ Trạng thái ngay khi khởi chạy")
    args = parser.parse_args()

    # 1. Khởi tạo môi trường UTF-8
    setup_system_environment()

    # 2. Kiểm tra và yêu cầu quyền Admin nếu cần (trên Windows)
    if not args.no_elevate and not args.headless:
        if not is_admin() and sys.platform == "win32":
            request_elevation()

    # 3. Đọc cấu hình từ file
    cfg = load_config()
    server_host = args.server if args.server is not None else cfg.get("server_host", "127.0.0.1")
    server_port = args.port if args.port is not None else cfg.get("server_port", SERVER_DEFAULT_PORT)
    enable_etw = False if args.no_etw else cfg.get("enable_etw", True)
    show_toast = cfg.get("show_toast_notifications", True)

    print("=" * 70)
    print("    NETWORK TRAFFIC MONITOR & PACKET SHAPER - CLIENT AGENT")
    print("=" * 70)
    print(f"[*] Máy chủ Admin đích: {server_host}:{server_port}")
    print(f"[*] ETW Kernel Monitor: {'Bật' if enable_etw else 'Tắt'}")
    print(f"[*] Đặc quyền Administrator: {'Đã có' if is_admin() else 'Chưa có (hạn chế WinDivert)'}")
    print(f"[*] Chế độ hoạt động: {'Headless Daemon' if args.headless else 'System Tray App'}\n")

    # 4. Khởi tạo Client Agent Service
    agent = ClientAgentService(
        server_host=server_host,
        server_port=server_port,
        enable_etw=enable_etw,
    )
    agent.show_toast_notifications = show_toast

    if args.headless:
        # Chế độ chạy ngầm dòng lệnh không GUI
        def signal_handler(sig, frame):
            print("\n[*] Đang dừng Agent Service...")
            agent.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        agent.start()
        print("[*] Agent đang chạy ở chế độ Headless. Nhấn Ctrl+C để dừng.")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[*] Đã nhận tín hiệu dừng từ bàn phím.")
        finally:
            agent.stop()
    else:
        # Chế độ System Tray + GUI
        from PyQt6 import QtWidgets, QtCore
        from presentation.client_status_window import ClientStatusWindow
        from presentation.client_tray import ClientTrayManager

        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication(sys.argv)

        # Giữ ứng dụng chạy ngầm khi người dùng đóng cửa sổ cấu hình
        app.setQuitOnLastWindowClosed(False)

        # Cửa sổ Trạng thái & Khay hệ thống
        status_window = ClientStatusWindow(agent)
        tray_manager = ClientTrayManager(agent, status_window)

        # Khởi động agent
        agent.start()

        # Dừng agent an toàn khi thoát app
        app.aboutToQuit.connect(agent.stop)

        # Hỗ trợ nhận diện tín hiệu Ctrl+C từ terminal trong Qt
        sig_timer = QtCore.QTimer()
        sig_timer.setInterval(500)
        sig_timer.timeout.connect(lambda: None)
        sig_timer.start()

        if args.show_window:
            status_window.show()

        sys.exit(app.exec())


if __name__ == "__main__":
    main()
