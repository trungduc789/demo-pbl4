from collections import deque
from infrastructure.network_collector import NetworkCollector
from application.bandwidth_calculator import BandwidthCalculator
from models.network_sample import RawNetworkSample, BandwidthSpeed
from config.settings import MAX_DATA_POINTS

class MonitorService:
    """Điều phối thu thập dữ liệu và lưu lại lịch sử tốc độ[cite: 2, 3]"""
    def __init__(self, max_points: int = MAX_DATA_POINTS):
        self.collector = NetworkCollector()
        self.calculator = BandwidthCalculator()
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()
        
        # Cấu trúc hàng đợi lưu trữ data tốc độ phục vụ vẽ biểu đồ[cite: 2]
        self.upload_history = deque(maxlen=max_points)
        self.download_history = deque(maxlen=max_points)

    def fetch_latest_speed(self) -> BandwidthSpeed:
        curr_sample = self.collector.collect_io_counters()
        speed = self.calculator.calculate_speed(self.prev_sample, curr_sample)
        self.prev_sample = curr_sample

        self.upload_history.append(speed.upload_speed_kb)
        self.download_history.append(speed.download_speed_kb)
        return speed