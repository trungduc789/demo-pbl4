from models.network_sample import RawNetworkSample, BandwidthSpeed
from config.settings import UNIT_DIVIDER

class BandwidthCalculator:
    """Tính toán tốc độ mạng thông qua độ chênh lệch dữ liệu sau thời gian t[cite: 2]"""
    @staticmethod
    def calculate_speed(prev_sample: RawNetworkSample, curr_sample: RawNetworkSample) -> BandwidthSpeed:
        time_delta = curr_sample.timestamp - prev_sample.timestamp
        if time_delta <= 0:
            return BandwidthSpeed(upload_speed_kb=0.0, download_speed_kb=0.0, timestamp=curr_sample.timestamp)

        # Công thức: Speed (KB/s) = (Bytes_hiện_tại - Bytes_trước) / (delta_t * 1024)[cite: 1, 2]
        upload_speed = (curr_sample.bytes_sent - prev_sample.bytes_sent) / (time_delta * UNIT_DIVIDER)
        download_speed = (curr_sample.bytes_recv - prev_sample.bytes_recv) / (time_delta * UNIT_DIVIDER)

        return BandwidthSpeed(
            upload_speed_kb=max(0.0, upload_speed),
            download_speed_kb=max(0.0, download_speed),
            timestamp=curr_sample.timestamp
        )