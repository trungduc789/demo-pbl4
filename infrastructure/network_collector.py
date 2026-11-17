import os
import sys
import time
import uuid
import socket
import platform
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import psutil
from models.network_sample import RawNetworkSample, ProcessInfo, TCPConnectionInfo
from models.network_protocol import DeviceRegistrationData


class NetworkCollector:
    """
    Tầng Infrastructure: Giao tiếp trực tiếp với hệ điều hành, Kernel, Socket và Registry
    để thu thập chỉ số I/O mạng, kết nối TCP, tiến trình, tài nguyên hệ thống và chẩn đoán mạng.
    """

    def collect_io_counters(self) -> RawNetworkSample:
        # psutil.net_io_counters() truy vấn trực tiếp thông số từ kernel
        io = psutil.net_io_counters()
        return RawNetworkSample(
            bytes_sent=io.bytes_sent,
            bytes_recv=io.bytes_recv,
            timestamp=time.time()
        )

    def collect_tcp_connections(self) -> List[TCPConnectionInfo]:
        """Dùng API kết nối mạng để lấy danh sách socket TCP (IPv4 & IPv6)."""
        results: List[TCPConnectionInfo] = []
        try:
            # psutil.net_connections(kind='tcp') truy vấn bảng kết nối TCP từ kernel
            connections = psutil.net_connections(kind='tcp')
            for conn in connections:
                local_ip = conn.laddr.ip if conn.laddr else ""
                local_port = conn.laddr.port if conn.laddr else None
                remote_ip = conn.raddr.ip if conn.raddr else None
                remote_port = conn.raddr.port if conn.raddr else None
                state = conn.status or "UNKNOWN"

                results.append(
                    TCPConnectionInfo(
                        protocol="TCP",
                        local_ip=local_ip,
                        local_port=local_port,
                        remote_ip=remote_ip,
                        remote_port=remote_port,
                        state=state,
                        pid=conn.pid
                    )
                )
        except Exception as exc:
            print(f"[Collector] Lỗi khi đọc kết nối TCP: {exc}")
        return results

    def get_process_info(self, pid: Optional[int]) -> Optional[ProcessInfo]:
        """Dùng Windows Process API / psutil để lấy PID, tên tiến trình và đường dẫn thực thi."""
        if pid is None or pid <= 0:
            return None
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
            exe_path = None
            try:
                exe_path = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe_path = None

            return ProcessInfo(
                pid=pid,
                name=proc_name,
                executable_path=exe_path
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    def collect_system_stats(self) -> Dict[str, float]:
        """Thu thập phần trăm sử dụng CPU và RAM hiện tại của hệ thống."""
        try:
            cpu_pct = float(psutil.cpu_percent(interval=None))
            ram_pct = float(psutil.virtual_memory().percent)
        except Exception:
            cpu_pct = 0.0
            ram_pct = 0.0
        return {
            "cpu_percent": cpu_pct,
            "ram_percent": ram_pct,
        }

    def generate_device_info(self, agent_version: str = "1.0.0") -> Tuple[str, DeviceRegistrationData]:
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
        self,
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

    def get_active_nic_info(self, default_ip: str = "127.0.0.1", mac_address: str = "") -> Dict[str, Any]:
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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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
