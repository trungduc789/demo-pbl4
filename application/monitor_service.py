"""
Dịch vụ Giám sát Mạng Cục bộ & Điều khiển WinDivert (MonitorService - Application Layer):
- Điều phối thu thập tốc độ card mạng, tốc độ từng tiến trình (ETW), và điều khiển bóp băng thông WinDivert.
- Ủy quyền tổng hợp tiến trình, làm giàu kết nối TCP và đồng bộ tốc độ Shaper cho `application.process_aggregator`.
"""

from collections import deque
from typing import Dict, List, Optional, Tuple
from infrastructure.network_collector import NetworkCollector
from infrastructure.etw_consumer import ETWNetworkConsumer
from infrastructure.packet_shaper import WinDivertPacketShaper
from application.bandwidth_calculator import BandwidthCalculator
from application.process_aggregator import (
    resolve_executable_path_cached,
    enrich_tcp_connections,
    aggregate_etw_samples_to_dicts,
    sync_shaper_speeds_to_proc_dicts,
)
from models.network_sample import RawNetworkSample, BandwidthSpeed, TCPConnectionInfo
from config.settings import MAX_DATA_POINTS
from utils.file_logger import FileLogger, LOGS_DIR
from utils.safe_print import safe_print as print


class MonitorService:
    """Điều phối thu thập dữ liệu, giám sát ETW và điều khiển bóp băng thông WinDivert."""

    def __init__(self, max_points: int = MAX_DATA_POINTS):
        self.collector = NetworkCollector()
        self.calculator = BandwidthCalculator()
        self.packet_shaper = WinDivertPacketShaper()
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()

        self.etw_consumer = ETWNetworkConsumer()
        self.etw_consumer.start()

        self.upload_history = deque(maxlen=max_points)
        self.download_history = deque(maxlen=max_points)
        self.sample_count = 0

        self.latest_speed: Optional[BandwidthSpeed] = None
        self.latest_proc_speeds: List[Dict] = []
        self.latest_total_proc_down: float = 0.0
        self.latest_total_proc_up: float = 0.0
        self.proc_info_cache: Dict[int, Tuple[str, str]] = {}

    def fetch_latest_speed(self) -> Tuple[BandwidthSpeed, List[Dict], float, float]:
        """
        Thu thập tốc độ card mạng, tốc độ từng tiến trình (ETW),
        và tổng lưu lượng tiến trình để so sánh.
        """
        curr_sample = self.collector.collect_io_counters()
        speed = self.calculator.calculate_speed(self.prev_sample, curr_sample)
        time_delta = max(0.1, curr_sample.timestamp - self.prev_sample.timestamp)
        self.prev_sample = curr_sample
        self.sample_count += 1

        etw_samples = self.etw_consumer.flush_and_aggregate()

        try:
            proc_speeds, _, _ = aggregate_etw_samples_to_dicts(
                etw_samples, time_delta, self.collector, self.proc_info_cache
            )
            sync_shaper_speeds_to_proc_dicts(getattr(self, "packet_shaper", None), proc_speeds)

            proc_speeds.sort(key=lambda x: (x["down_kb"] + x["up_kb"]), reverse=True)
            self.latest_proc_speeds = proc_speeds
            self.latest_total_proc_down = sum(x["down_kb"] for x in proc_speeds)
            self.latest_total_proc_up = sum(x["up_kb"] for x in proc_speeds)
        except Exception as e:
            print(f"[ETW Aggregate Error] {e}")

        self.latest_speed = speed
        self.upload_history.append(speed.upload_speed_kb)
        self.download_history.append(speed.download_speed_kb)
        return speed, self.latest_proc_speeds, self.latest_total_proc_down, self.latest_total_proc_up

    def get_raw_tcp_connections(self) -> List[TCPConnectionInfo]:
        """Lấy danh sách đối tượng kết nối TCP thô từ tầng hạ tầng (phục vụ phân tích WAN/LAN)."""
        return self.collector.collect_tcp_connections()

    def resolve_executable_path(self, pid: int) -> str:
        """Phân giải đường dẫn file .exe từ PID thông qua bộ nhớ đệm hoặc NetworkCollector."""
        return resolve_executable_path_cached(self.collector, self.proc_info_cache, pid)

    def get_connections(self) -> List[Dict]:
        """Lấy danh sách các kết nối socket TCP hiện tại kèm thông tin tiến trình."""
        return enrich_tcp_connections(self.collector, self.proc_info_cache, log_to_file=True)

    # =========================================================================
    # CÁC PHƯƠNG THỨC ĐIỀU KHIỂN BĂNG THÔNG WINDIVERT (PACKET SHAPER)
    # =========================================================================
    def block_application(self, app_name: str, direction: str = "both", exe_path: str = "") -> Tuple[bool, str]:
        """Chặn hoàn toàn quyền truy cập mạng của ứng dụng (Windows Firewall + WinDivert Drop)."""
        success, msg = self.packet_shaper.block_app(app_name, direction, exe_path)
        FileLogger.log_policy(
            policy_type="FIREWALL_BLOCK",
            target_app=app_name,
            limit_kbps=0.0,
            direction=direction,
            action="BLOCK",
            throttled_packets=0,
            throttled_bytes=0,
            status="SUCCESS" if success else "FAILED",
            details=msg,
        )
        return success, msg

    def unblock_application(self, app_name: str) -> Tuple[bool, str]:
        """Gỡ bỏ chặn truy cập mạng cho ứng dụng."""
        success, msg = self.packet_shaper.unblock_app(app_name)
        FileLogger.log_policy(
            policy_type="FIREWALL_BLOCK",
            target_app=app_name,
            limit_kbps=0.0,
            direction="both",
            action="UNBLOCK",
            throttled_packets=0,
            throttled_bytes=0,
            status="SUCCESS" if success else "FAILED",
            details=msg,
        )
        return success, msg

    def unblock_all_applications(self) -> Tuple[bool, str]:
        """Gỡ chặn cho tất cả ứng dụng."""
        return self.packet_shaper.unblock_all()

    def apply_bandwidth_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        """Áp dụng bóp băng thông thực tế (Token Bucket qua WinDivert NDIS kernel)."""
        success, msg = self.packet_shaper.apply_limit(app_name, limit_kbps, direction)
        FileLogger.log_policy(
            policy_type="BANDWIDTH_LIMIT",
            target_app=app_name,
            limit_kbps=limit_kbps,
            direction=direction,
            action="APPLY",
            throttled_packets=0,
            throttled_bytes=0,
            status="SUCCESS" if success else "FAILED",
            details=msg,
        )
        return success, msg

    def remove_bandwidth_limit(self, app_name: Optional[str] = None) -> Tuple[bool, str]:
        """Gỡ bỏ bóp băng thông WinDivert (cho 1 ứng dụng hoặc toàn bộ)."""
        status = self.packet_shaper.get_status() if hasattr(self, "packet_shaper") and self.packet_shaper else {}
        target_app = app_name or status.get("target_app", "")
        limit_kbps = status.get("limit_kbps", 0.0)
        direction = status.get("direction", "both")
        throttled_pkts = status.get("throttled_packets", 0)
        throttled_bytes = status.get("throttled_bytes", 0)

        try:
            success, msg = self.packet_shaper.remove_limit(app_name)
        except TypeError:
            success, msg = self.packet_shaper.remove_limit()

        if target_app:
            FileLogger.log_policy(
                policy_type="BANDWIDTH_LIMIT",
                target_app=target_app,
                limit_kbps=limit_kbps,
                direction=direction,
                action="REMOVE",
                throttled_packets=throttled_pkts,
                throttled_bytes=throttled_bytes,
                status="SUCCESS" if success else "FAILED",
                details=msg,
            )
        return success, msg

    def get_bandwidth_limit_status(self) -> Dict:
        """Lấy trạng thái và thông số đo đạc của bộ bóp băng thông."""
        return self.packet_shaper.get_status()

    def get_policy_history(self, limit: int = 50) -> List[Dict]:
        """Lấy danh sách lịch sử các chính sách an ninh mạng đã áp dụng từ file log."""
        return FileLogger.read_policy_logs(limit)

    def get_connection_history_text(self, limit: int = 200) -> str:
        """Lấy nội dung nhật ký kết nối mạng gần nhất từ file log."""
        return FileLogger.read_connection_logs(limit)

    @staticmethod
    def get_logs_directory() -> str:
        """Trả về đường dẫn thư mục lưu trữ nhật ký hệ thống."""
        return LOGS_DIR

    def apply_qos_upload_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        return self.apply_bandwidth_limit(app_name, limit_kbps, direction)

    def remove_qos_upload_limit(self, app_name: str = "") -> Tuple[bool, str]:
        return self.remove_bandwidth_limit()

    def get_qos_status(self, app_name: str = "") -> Optional[Dict]:
        return self.get_bandwidth_limit_status()

    def stop(self):
        """Dọn dẹp toàn bộ tài nguyên: Dừng Packet Shaper và tắt ETW."""
        if hasattr(self, "packet_shaper") and self.packet_shaper:
            _success, msg = self.packet_shaper.remove_limit()
            print(f"[Shutdown PacketShaper] {msg}")

        if hasattr(self, "etw_consumer") and self.etw_consumer:
            self.etw_consumer.stop()
