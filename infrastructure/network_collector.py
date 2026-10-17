import time
from typing import List, Optional
import psutil
from models.network_sample import RawNetworkSample, ProcessInfo, TCPConnectionInfo

class NetworkCollector:
    """Giao tiếp với kernel để thu thập chỉ số I/O mạng, kết nối TCP và tiến trình[cite: 1, 3]"""
    def collect_io_counters(self) -> RawNetworkSample:
        # psutil.net_io_counters() truy vấn trực tiếp thông số từ kernel[cite: 1, 2]
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