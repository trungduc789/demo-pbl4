import time
from dataclasses import dataclass

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