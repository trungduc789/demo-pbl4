"""
Bộ Thu thập Chỉ số Hạ tầng Mạng & Tiến trình (NetworkCollector - Infrastructure Layer):
- Thu thập bộ đếm I/O mạng (bytes_sent, bytes_recv), danh sách kết nối TCP, thông tin PID và CPU/RAM.
- Ủy quyền các tác vụ định danh phần cứng, đo độ trễ TCP, thăm dò NIC và tích hợp OS cho `infrastructure.os_integration`.
"""

import time
from typing import Any, Dict, List, Optional, Tuple
import psutil

from models.network_sample import RawNetworkSample, ProcessInfo, TCPConnectionInfo
from models.network_protocol import DeviceRegistrationData
from infrastructure.os_integration import (
    generate_device_info,
    measure_tcp_latency,
    get_active_nic_info,
    is_windows_autostart,
    set_windows_autostart,
    open_folder_in_os,
)


class NetworkCollector:
    """
    Tầng Infrastructure: Thu thập chỉ số I/O mạng, kết nối TCP, tiến trình, tài nguyên hệ thống
    và cung cấp các hàm ủy quyền tích hợp hệ điều hành.
    """

    def collect_io_counters(self) -> RawNetworkSample:
        io = psutil.net_io_counters()
        return RawNetworkSample(
            bytes_sent=io.bytes_sent,
            bytes_recv=io.bytes_recv,
            timestamp=time.time(),
        )

    def collect_tcp_connections(self) -> List[TCPConnectionInfo]:
        """Dùng API kết nối mạng để lấy danh sách socket TCP (IPv4 & IPv6)."""
        results: List[TCPConnectionInfo] = []
        try:
            connections = psutil.net_connections(kind="tcp")
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
                        pid=conn.pid,
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
                executable_path=exe_path,
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
        return generate_device_info(agent_version=agent_version)

    def measure_tcp_latency(
        self,
        host: str,
        port: int,
        timeout: float = 1.0,
        success_msg: str = "Kết nối TCP ổn định",
        dns_fallback_host: Optional[str] = None,
    ) -> Tuple[Optional[float], bool, str]:
        return measure_tcp_latency(
            host=host,
            port=port,
            timeout=timeout,
            success_msg=success_msg,
            dns_fallback_host=dns_fallback_host,
        )

    def get_active_nic_info(self, default_ip: str = "127.0.0.1", mac_address: str = "") -> Dict[str, Any]:
        return get_active_nic_info(default_ip=default_ip, mac_address=mac_address)

    @staticmethod
    def is_windows_autostart(reg_value_name: str = "ClientNetworkAgent") -> bool:
        return is_windows_autostart(reg_value_name=reg_value_name)

    @staticmethod
    def set_windows_autostart(enable: bool, reg_value_name: str = "ClientNetworkAgent") -> bool:
        return set_windows_autostart(enable=enable, reg_value_name=reg_value_name)

    @staticmethod
    def open_folder_in_os(folder_path: str) -> bool:
        return open_folder_in_os(folder_path=folder_path)
