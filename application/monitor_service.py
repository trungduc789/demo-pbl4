from collections import deque
from typing import Dict, List, Optional, Tuple
from infrastructure.network_collector import NetworkCollector
from infrastructure.etw_consumer import ETWNetworkConsumer
from infrastructure.packet_shaper import WinDivertPacketShaper
from application.bandwidth_calculator import BandwidthCalculator
from models.network_sample import RawNetworkSample, BandwidthSpeed
from config.settings import MAX_DATA_POINTS
from utils.file_logger import FileLogger
from utils.safe_print import safe_print as print

class MonitorService:
    """Điều phối thu thập dữ liệu, giám sát ETW và điều khiển bóp băng thông WinDivert"""
    def __init__(self, max_points: int = MAX_DATA_POINTS):
        self.collector = NetworkCollector()
        self.calculator = BandwidthCalculator()
        self.packet_shaper = WinDivertPacketShaper()
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()

        # Khởi động ETW Consumer để theo dõi sự kiện TCP/IP cấp kernel
        self.etw_consumer = ETWNetworkConsumer()
        self.etw_consumer.start()

        # Cấu trúc hàng đợi lưu trữ data tốc độ phục vụ vẽ biểu đồ
        self.upload_history = deque(maxlen=max_points)
        self.download_history = deque(maxlen=max_points)
        self.sample_count = 0

        # Lưu trữ chỉ số mới nhất
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

        # 1. Thu thập mẫu lưu lượng từ ETW Consumer
        etw_samples = self.etw_consumer.flush_and_aggregate()

        # 2. Xử lý lưu lượng từ ETW Consumer theo từng tiến trình
        proc_speeds = []
        total_p_down = 0.0
        total_p_up = 0.0

        try:
            if etw_samples:
                for pid, b_info in etw_samples.items():
                    rx = b_info.get("rx_bytes", 0)
                    tx = b_info.get("tx_bytes", 0)
                    down_kb = rx / (time_delta * 1024.0)
                    up_kb = tx / (time_delta * 1024.0)
                    total_p_down += down_kb
                    total_p_up += up_kb

                    # Lấy thông tin tên và đường dẫn tiến trình
                    if pid in self.proc_info_cache:
                        p_name, p_exe = self.proc_info_cache[pid]
                    else:
                        p_info = self.collector.get_process_info(pid)
                        p_name = p_info.name if p_info else f"PID {pid}"
                        p_exe = p_info.executable_path if p_info and p_info.executable_path else ""
                        self.proc_info_cache[pid] = (p_name, p_exe)

                    proc_speeds.append({
                        "pid": pid,
                        "name": p_name,
                        "exe": p_exe,
                        "down_kb": down_kb,
                        "up_kb": up_kb,
                        "rx_bytes": rx,
                        "tx_bytes": tx
                    })

            # Đồng bộ chính xác tốc độ cho ứng dụng đang được bóp băng thông bởi PacketShaper
            if hasattr(self, 'packet_shaper') and self.packet_shaper and self.packet_shaper.is_active:
                shaper_status = self.packet_shaper.get_status()
                throttled_dict = shaper_status.get("throttled_apps", {})
                if throttled_dict:
                    for app_k, app_info in throttled_dict.items():
                        t_name = (app_info.get("target_app") or app_k).lower()
                        t_base = t_name.replace(".exe", "").strip()
                        s_down = app_info.get("current_down_kbps", 0.0)
                        s_up = app_info.get("current_up_kbps", 0.0)
                        s_dir = app_info.get("direction", "both")

                        for p in proc_speeds:
                            p_name = p.get("name", "").lower()
                            if p_name == t_name or p_name.replace(".exe", "") == t_base or (t_base and t_base in p_name):
                                if s_dir in ("download", "both"):
                                    p["down_kb"] = s_down
                                if s_dir in ("upload", "both"):
                                    p["up_kb"] = s_up
                else:
                    target_app = (shaper_status.get("target_app") or "").lower()
                    target_base = target_app.replace(".exe", "").strip()
                    shaper_down = shaper_status.get("current_down_kbps", 0.0)
                    shaper_up = shaper_status.get("current_up_kbps", 0.0)
                    shaper_dir = shaper_status.get("direction", "both")

                    for p in proc_speeds:
                        p_name = p.get("name", "").lower()
                        if p_name == target_app or p_name.replace(".exe", "") == target_base or (target_base and target_base in p_name):
                            if s_dir in ("download", "both"):
                                p["down_kb"] = shaper_down
                            if s_dir in ("upload", "both"):
                                p["up_kb"] = shaper_up

            # Sắp xếp tiến trình có lưu lượng mạng lớn nhất lên trên
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

    def get_connections(self) -> List[Dict]:
        """Lấy danh sách các kết nối socket TCP hiện tại kèm thông tin tiến trình."""
        raw_conns = self.collector.collect_tcp_connections()
        enriched = []
        for conn in raw_conns:
            pid = conn.pid or 0
            if pid in self.proc_info_cache:
                proc_name, _ = self.proc_info_cache[pid]
            else:
                p_info = self.collector.get_process_info(pid)
                proc_name = p_info.name if p_info else ("System" if pid == 0 else f"PID {pid}")
                exe_path = p_info.executable_path if p_info and p_info.executable_path else ""
                self.proc_info_cache[pid] = (proc_name, exe_path)

            FileLogger.log_connection(
                proc_name=proc_name,
                pid=pid,
                protocol=conn.protocol,
                local_ip=conn.local_ip,
                local_port=conn.local_port,
                remote_ip=conn.remote_ip,
                remote_port=conn.remote_port,
                state=conn.state
            )

            enriched.append({
                "pid": pid,
                "process_name": proc_name,
                "protocol": conn.protocol,
                "local_addr": f"{conn.local_ip}:{conn.local_port}" if conn.local_port else conn.local_ip,
                "remote_addr": f"{conn.remote_ip}:{conn.remote_port}" if conn.remote_ip and conn.remote_port else ("*" if conn.state == "LISTEN" else "-"),
                "state": conn.state
            })
        return enriched

    # =========================================================================
    # CÁC PHƯƠNG THỨC ĐIỀU KHIỂN BĂNG THÔNG WINDIVERT (PACKET SHAPER)
    # =========================================================================
    def block_application(self, app_name: str, direction: str = "both", exe_path: str = "") -> Tuple[bool, str]:
        """Chặn hoàn toàn quyền truy cập mạng của ứng dụng (Windows Firewall + WinDivert Drop)."""
        success, msg = self.packet_shaper.block_app(app_name, direction, exe_path)
        status_str = "SUCCESS" if success else "FAILED"
        FileLogger.log_policy(
            policy_type="FIREWALL_BLOCK",
            target_app=app_name,
            limit_kbps=0.0,
            direction=direction,
            action="BLOCK",
            throttled_packets=0,
            throttled_bytes=0,
            status=status_str,
            details=msg
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
            details=msg
        )
        return success, msg

    def unblock_all_applications(self) -> Tuple[bool, str]:
        """Gỡ chặn cho tất cả ứng dụng."""
        return self.packet_shaper.unblock_all()

    def apply_bandwidth_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        """Áp dụng bóp băng thông thực tế (Token Bucket qua WinDivert NDIS kernel)."""
        success, msg = self.packet_shaper.apply_limit(app_name, limit_kbps, direction)
        status_str = "SUCCESS" if success else "FAILED"
        FileLogger.log_policy(
            policy_type="BANDWIDTH_LIMIT",
            target_app=app_name,
            limit_kbps=limit_kbps,
            direction=direction,
            action="APPLY",
            throttled_packets=0,
            throttled_bytes=0,
            status=status_str,
            details=msg
        )
        return success, msg

    def remove_bandwidth_limit(self, app_name: Optional[str] = None) -> Tuple[bool, str]:
        """Gỡ bỏ bóp băng thông WinDivert (cho 1 ứng dụng hoặc toàn bộ)."""
        status = self.packet_shaper.get_status() if hasattr(self, 'packet_shaper') and self.packet_shaper else {}
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
                details=msg
            )
        return success, msg

    def get_bandwidth_limit_status(self) -> Dict:
        """Lấy trạng thái và thông số đo đạc của bộ bóp băng thông."""
        return self.packet_shaper.get_status()

    def get_policy_history(self, limit: int = 50) -> List[Dict]:
        """Lấy danh sách lịch sử các chính sách an ninh mạng đã áp dụng từ file log."""
        return FileLogger.read_policy_logs(limit)

    # Giữ tương thích ngược với tên hàm cũ nếu có nơi gọi
    def apply_qos_upload_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        return self.apply_bandwidth_limit(app_name, limit_kbps, direction)

    def remove_qos_upload_limit(self, app_name: str = "") -> Tuple[bool, str]:
        return self.remove_bandwidth_limit()

    def get_qos_status(self, app_name: str = "") -> Optional[Dict]:
        return self.get_bandwidth_limit_status()

    def stop(self):
        """Dọn dẹp toàn bộ tài nguyên: Dừng Packet Shaper và tắt ETW."""
        # 1. Dừng bóp băng thông WinDivert
        if hasattr(self, 'packet_shaper') and self.packet_shaper:
            success, msg = self.packet_shaper.remove_limit()
            print(f"[Shutdown PacketShaper] {msg}")

        # 2. Dừng ETW Consumer
        if hasattr(self, 'etw_consumer') and self.etw_consumer:
            self.etw_consumer.stop()



