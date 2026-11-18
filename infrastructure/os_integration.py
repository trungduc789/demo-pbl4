"""
Tích hợp Hệ điều hành, Định danh Thiết bị & Thăm dò Kết nối (OS Integration - Infrastructure Layer):
- Tạo định danh phần cứng ổn định (UUIDv5 từ Hostname + MAC Address) và thông tin OS.
- Đo độ trễ kết nối TCP (có DNS fallback) và phát hiện Card mạng (NIC) đang định tuyến chính.
- Quản lý khóa Registry khởi động cùng Windows (winreg) và mở thư mục trong File Explorer.
"""

import os
import sys
import time
import uuid
import socket
import platform
import subprocess
from typing import Any, Dict, Optional, Tuple
import psutil

from models.network_protocol import DeviceRegistrationData


def generate_device_info(agent_version: str = "1.0.0") -> Tuple[str, DeviceRegistrationData]:
    """Tạo UUID phần cứng ổn định và thu thập thông tin định danh hệ thống của máy trạm."""
    hostname = platform.node() or "unknown-host"
    os_name = platform.system() or "UnknownOS"
    os_version = platform.version() or "unknown"
    mac_num = uuid.getnode()
    mac_str = ":".join(f"{(mac_num >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))
    stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{hostname}-{mac_num}"))

    local_ip = "127.0.0.1"
    try:
        for _iface_name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == psutil.AF_LINK or not hasattr(addr, "address"):
                    continue
                if addr.address.startswith("127.") or addr.address == "::1":
                    continue
                if "." in addr.address:
                    local_ip = addr.address
                    break
            if local_ip != "127.0.0.1":
                break
    except Exception:
        pass

    reg_data = DeviceRegistrationData(
        device_uuid=stable_uuid,
        hostname=hostname,
        os_name=os_name,
        os_version=os_version,
        agent_version=agent_version,
        mac_address=mac_str,
        ip_address=local_ip,
    )
    return stable_uuid, reg_data


def measure_tcp_latency(
    host: str,
    port: int,
    timeout: float = 1.0,
    success_msg: str = "Kết nối TCP ổn định",
    dns_fallback_host: Optional[str] = None,
) -> Tuple[Optional[float], bool, str]:
    """Đo độ trễ kết nối TCP (hoặc DNS fallback) phục vụ chẩn đoán hạ tầng mạng."""
    t0 = time.perf_counter()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        latency = round((time.perf_counter() - t0) * 1000.0, 1)
        return latency, True, success_msg
    except Exception:
        if dns_fallback_host:
            try:
                t1 = time.perf_counter()
                socket.gethostbyname(dns_fallback_host)
                latency = round((time.perf_counter() - t1) * 1000.0, 1)
                return latency, True, "DNS Lookup thành công"
            except Exception:
                return None, False, "Không phản hồi mạng ngoài"
        return None, False, "Không thể kết nối tới đích"


def get_active_nic_info(default_ip: str = "127.0.0.1", mac_address: str = "") -> Dict[str, Any]:
    """Thu thập thông tin card mạng chính đang định tuyến lưu lượng."""
    routing_ip = default_ip
    nic_name = "Tự động phát hiện"
    nic_speed = 0
    link_type = "Ethernet / Wi-Fi"

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        routing_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for name, addr_list in addrs.items():
            for a in addr_list:
                if getattr(a, "address", "") == routing_ip:
                    nic_name = name
                    st = stats.get(name)
                    if st:
                        nic_speed = getattr(st, "speed", 0)
                    lower_name = name.lower()
                    if "wi-fi" in lower_name or "wireless" in lower_name or "wlan" in lower_name:
                        link_type = "Wi-Fi (Wireless 802.11)"
                    elif "ethernet" in lower_name or "eth" in lower_name:
                        link_type = "Ethernet (LAN Cable)"
                    break
    except Exception:
        pass

    return {
        "name": nic_name,
        "ip": routing_ip,
        "mac": mac_address,
        "speed_mbps": nic_speed if nic_speed > 0 else 100,
        "type": link_type,
        "gateway": "192.168.1.1",
    }


def is_windows_autostart(reg_value_name: str = "ClientNetworkAgent") -> bool:
    """Kiểm tra khóa Registry tự khởi động cùng Windows."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, reg_value_name)
            return True
    except Exception:
        return False


def set_windows_autostart(enable: bool, reg_value_name: str = "ClientNetworkAgent") -> bool:
    """Bật / tắt khóa Registry tự khởi động cùng Windows."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    app_exe = sys.executable
                else:
                    app_exe = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}" --hide'
                winreg.SetValueEx(key, reg_value_name, 0, winreg.REG_SZ, app_exe)
            else:
                try:
                    winreg.DeleteValue(key, reg_value_name)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        print(f"[Collector Autostart Error] {e}")
        return False


def open_folder_in_os(folder_path: str) -> bool:
    """Mở thư mục bằng trình quản lý tệp mặc định của hệ điều hành (Explorer / xdg-open)."""
    try:
        if not folder_path:
            return False
        if not os.path.exists(folder_path):
            os.makedirs(folder_path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder_path)
        else:
            subprocess.Popen(["xdg-open", folder_path])
        return True
    except Exception as e:
        print(f"[Collector OpenFolder Error] {e}")
        return False
