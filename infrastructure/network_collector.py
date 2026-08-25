import time
import psutil
from models.network_sample import RawNetworkSample

class NetworkCollector:
    """Giao tiếp với kernel để thu thập chỉ số I/O mạng[cite: 1, 3]"""
    def collect_io_counters(self) -> RawNetworkSample:
        # psutil.net_io_counters() truy vấn trực tiếp thông số từ kernel[cite: 1, 2]
        io = psutil.net_io_counters()
        return RawNetworkSample(
            bytes_sent=io.bytes_sent,
            bytes_recv=io.bytes_recv,
            timestamp=time.time()
        )