"""
Script Tự Động Đóng Gói Ứng Dụng Client & Server (Packaging Automation Script):
- Sử dụng PyInstaller để đóng gói thành 2 gói thực thi độc lập (Standalone Executable Folders):
    1. dist/Admin_Server/Admin_Server.exe
    2. dist/Client_Agent/Client_Agent.exe
- Tự động tích hợp Windows UAC Administrator Manifest (--uac-admin) để cấp quyền WinDivert & Firewall.
- Tự động trích xuất và sao chép driver nhân Windows: WinDivert64.dll & WinDivert64.sys vào gói Client.
- Tự động đóng gói file cấu hình client_config.json và database demo.sql.
- Tạo sẵn các file hướng dẫn vận hành và file batch khởi chạy nhanh (.bat).
"""

import sys
import os
import shutil
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.env_setup import setup_system_environment
from utils.safe_print import safe_print as print

setup_system_environment()


def get_pydivert_binaries():
    """Tìm đường dẫn tới file WinDivert64.dll và WinDivert64.sys trong thư viện pydivert."""
    try:
        import pydivert
        dll_dir = os.path.join(os.path.dirname(pydivert.__file__), "windivert_dll")
        dll_file = os.path.join(dll_dir, "WinDivert64.dll")
        sys_file = os.path.join(dll_dir, "WinDivert64.sys")
        if os.path.isfile(dll_file) and os.path.isfile(sys_file):
            return dll_file, sys_file
    except Exception as e:
        print(f"[Build Warning] Không thể tự động tìm thư viện pydivert: {e}")
    return None, None


def run_command(cmd, desc):
    """Chạy lệnh dòng lệnh và kiểm tra mã lỗi."""
    print(f"\n{'='*75}\n[*] {desc}...\n{'='*75}")
    print(f"CommandLine: {' '.join(cmd)}\n")
    ret = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if ret.returncode != 0:
        print(f"\n[BUILD ERROR] Thất bại khi thực hiện: {desc} (Mã lỗi: {ret.returncode})")
        sys.exit(ret.returncode)
    print(f"[+] Thành công: {desc}!\n")


def build_admin():
    """Đóng gói Admin Server."""
    desc = "Đang đóng gói Máy Chủ Quản Trị (Admin_Server.exe)"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", "Admin_Server",
        "--uac-admin",
        "--add-data", f"{os.path.join(PROJECT_ROOT, 'demo.sql')};.",
        "--add-data", f"{os.path.join(PROJECT_ROOT, 'database.txt')};.",
        "--hidden-import", "mysql.connector",
        "--hidden-import", "mysql.connector.plugins",
        "--hidden-import", "mysql.connector.locales",
        "--hidden-import", "pyqtgraph",
        "--hidden-import", "PyQt6",
        "--hidden-import", "PyQt6.QtCore",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtGui",
        "admin_main.py",
    ]
    run_command(cmd, desc)

    # Hậu xử lý cho Admin_Server
    admin_dist = os.path.join(PROJECT_ROOT, "dist", "Admin_Server")
    os.makedirs(os.path.join(admin_dist, "logs"), exist_ok=True)

    # Copy demo.sql & database.txt sang dist/Admin_Server
    for f in ["demo.sql", "database.txt"]:
        src = os.path.join(PROJECT_ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(admin_dist, f))

    # Tạo file hướng dẫn cho Admin
    readme_admin = os.path.join(admin_dist, "HUONG_DAN_ADMIN.txt")
    with open(readme_admin, "w", encoding="utf-8") as f:
        f.write(
            "========================================================================\n"
            "         HƯỚNG DẪN VẬN HÀNH MÁY CHỦ QUẢN TRỊ (ADMIN SERVER)\n"
            "========================================================================\n\n"
            "1. Chuẩn bị Cơ sở dữ liệu MySQL (Tùy chọn):\n"
            "   - Khởi động MySQL (qua XAMPP hoặc MySQL Service).\n"
            "   - Import file 'demo.sql' trong thư mục này vào MySQL database tên 'demo'.\n"
            "   - Cấu hình mật khẩu root trong tệp config/settings.py nếu có thay đổi.\n\n"
            "2. Khởi chạy Máy Chủ Quản Trị:\n"
            "   - Nhấp đúp vào 'Admin_Server.exe' (hoặc chạy 'Chay_Admin.bat').\n"
            "   - Khi hộp thoại UAC hỏi cấp quyền Administrator, hãy chọn 'Yes'.\n"
            "   - Ứng dụng sẽ tự động mở giao diện quản trị GlassWire Dashboard và\n"
            "     lắng nghe kết nối TCP từ các máy con trên Cổng 9988.\n\n"
            "3. Giám sát & Can thiệp từ xa:\n"
            "   - Các máy con kết nối vào mạng sẽ xuất hiện tại tab 'Fleet Manager'.\n"
            "   - Bấm 'Xem & Điều khiển' để xem biểu đồ lưu lượng thời gian thực.\n"
            "   - Vào tab 'Protect' để can thiệp chặn hoặc bóp băng thông từ xa.\n"
        )

    # Tạo batch file chạy nhanh
    bat_admin = os.path.join(admin_dist, "Chay_Admin.bat")
    with open(bat_admin, "w", encoding="utf-8") as f:
        f.write("@echo off\ncd /d \"%~dp0\"\nstart Admin_Server.exe\n")


def build_client():
    """Đóng gói Client Agent."""
    desc = "Đang đóng gói Máy Con (Client_Agent.exe)"
    pydivert_dll, pydivert_sys = get_pydivert_binaries()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", "Client_Agent",
        "--uac-admin",
        "--add-data", f"{os.path.join(PROJECT_ROOT, 'config', 'client_config.json')};config",
        "--hidden-import", "pydivert",
        "--hidden-import", "pydivert.windivert_dll",
        "--hidden-import", "pydivert.windivert_dll.structs",
        "--hidden-import", "psutil",
        "--hidden-import", "PyQt6",
        "--hidden-import", "PyQt6.QtCore",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtGui",
    ]

    if pydivert_dll and pydivert_sys:
        cmd.extend([
            "--add-data", f"{pydivert_dll};pydivert/windivert_dll",
            "--add-data", f"{pydivert_sys};pydivert/windivert_dll",
        ])

    cmd.append("client_main.py")
    run_command(cmd, desc)

    # Hậu xử lý cho Client_Agent
    client_dist = os.path.join(PROJECT_ROOT, "dist", "Client_Agent")
    os.makedirs(os.path.join(client_dist, "logs"), exist_ok=True)
    os.makedirs(os.path.join(client_dist, "config"), exist_ok=True)

    # Đảm bảo client_config.json có trong thư mục config của Client_Agent
    cfg_src = os.path.join(PROJECT_ROOT, "config", "client_config.json")
    if os.path.isfile(cfg_src):
        shutil.copy2(cfg_src, os.path.join(client_dist, "config", "client_config.json"))

    # Copy WinDivert DLL và SYS trực tiếp ra ngoài thư mục chạy của Client_Agent
    if pydivert_dll and os.path.isfile(pydivert_dll):
        shutil.copy2(pydivert_dll, os.path.join(client_dist, "WinDivert64.dll"))
    if pydivert_sys and os.path.isfile(pydivert_sys):
        shutil.copy2(pydivert_sys, os.path.join(client_dist, "WinDivert64.sys"))

    # Tạo file hướng dẫn cho Client
    readme_client = os.path.join(client_dist, "HUONG_DAN_CLIENT.txt")
    with open(readme_client, "w", encoding="utf-8") as f:
        f.write(
            "========================================================================\n"
            "         HƯỚNG DẪN VẬN HÀNH MÁY CON (CLIENT AGENT)\n"
            "========================================================================\n\n"
            "1. Cấu hình Địa chỉ IP của Máy Chủ Admin:\n"
            "   - Mở tệp 'config/client_config.json' bằng Notepad.\n"
            "   - Đổi giá trị 'server_host' thành địa chỉ IP mạng LAN của máy Admin.\n"
            "     (Ví dụ: \"server_host\": \"192.168.1.15\", \"server_port\": 9988)\n"
            "   - Lưu tệp lại. (Bạn cũng có thể đổi trực tiếp trên giao diện của Client).\n\n"
            "2. Khởi chạy Máy Con:\n"
            "   - Nhấp đúp vào 'Client_Agent.exe' (hoặc 'Chay_Client.bat').\n"
            "   - Khi hộp thoại UAC hỏi quyền Administrator, chọn 'Yes' để cấp quyền\n"
            "     can thiệp driver WinDivert và tường lửa Windows Firewall.\n\n"
            "3. Trạng thái hoạt động:\n"
            "   - Ứng dụng sẽ chạy ngầm dưới khay hệ thống (System Tray Taskbar).\n"
            "   - Biểu tượng Khiên mạng chuyển sang màu XANH LÁ khi kết nối Server thành công.\n"
            "   - Nhấp đúp vào biểu tượng khay để mở Bảng Trạng Thái & Cấu Hình.\n"
            "   - Nhấp chuột phải vào biểu tượng khay để xem menu tốc độ mạng tức thời.\n"
            "   - Khi Admin can thiệp từ xa, thông báo Toast sẽ tự động hiện lên góc màn hình.\n"
        )

    # Tạo batch file chạy nhanh
    bat_client = os.path.join(client_dist, "Chay_Client.bat")
    with open(bat_client, "w", encoding="utf-8") as f:
        f.write("@echo off\ncd /d \"%~dp0\"\nstart Client_Agent.exe --show-window\n")


def main():
    print("=" * 75)
    print("    PBL4 - DỰ ÁN GIÁM SÁT LƯU LƯỢNG MẠNG & ĐIỀU PHỐI GÓI TIN")
    print("    TIẾN TRÌNH TỰ ĐỘNG ĐÓNG GÓI CLIENT & SERVER THÀNH FILE .EXE")
    print("=" * 75)

    build_admin()
    build_client()

    print("=" * 75)
    print("    [HOÀN TẤT ĐÓNG GÓI THÀNH CÔNG!]")
    print(f"    1. Thư mục Admin:  {os.path.join(PROJECT_ROOT, 'dist', 'Admin_Server')}")
    print(f"    2. Thư mục Client: {os.path.join(PROJECT_ROOT, 'dist', 'Client_Agent')}")
    print("=" * 75)


if __name__ == "__main__":
    main()
