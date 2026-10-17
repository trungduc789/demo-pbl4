import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class RawNetworkSample:
    """Lưu trữ số byte thô gửi/nhận lấy từ kernel[cite: 1, 2]"""
    bytes_sent: int
    bytes_recv: int
    timestamp: float = time.time()

@dataclass
class BandwidthSpeed:
    """Lưu trữ dữ liệu tốc độ mạng đã qua tính toán[cite: 2]"""
    upload_speed_kb: float
    download_speed_kb: float
    timestamp: float = time.time()

@dataclass
class ProcessInfo:
    """Thông tin tiến trình hệ thống lấy qua Windows Process API / psutil"""
    pid: int
    name: str
    executable_path: Optional[str] = None

@dataclass
class TCPConnectionInfo:
    """Thông tin kết nối socket TCP đối chiếu giữa kernel và tiến trình"""
    protocol: str
    local_ip: str
    local_port: Optional[int] = None
    remote_ip: Optional[str] = None
    remote_port: Optional[int] = None
    state: str = "UNKNOWN"
    pid: Optional[int] = None
