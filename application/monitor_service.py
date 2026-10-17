import queue
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple
from infrastructure.network_collector import NetworkCollector
from infrastructure.database_manager import DatabaseManager
from infrastructure.etw_consumer import ETWNetworkConsumer
from infrastructure.qos_manager import WindowsQoSManager
from application.bandwidth_calculator import BandwidthCalculator
from models.network_sample import RawNetworkSample, BandwidthSpeed
from config.settings import MAX_DATA_POINTS
from utils.safe_print import safe_print as print

class MonitorService:
    """Điều phối thu thập dữ liệu, giám sát ETW, điều khiển QoS và lưu trữ database bất đồng bộ"""
    def __init__(self, max_points: int = MAX_DATA_POINTS):
        self.collector = NetworkCollector()
        self.calculator = BandwidthCalculator()
        self.db_manager = DatabaseManager()
        self.qos_manager = WindowsQoSManager()
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()

        # Khởi động ETW Consumer để theo dõi sự kiện TCP/IP cấp kernel
        self.etw_consumer = ETWNetworkConsumer()
        self.etw_consumer.start()

        # Cấu trúc hàng đợi lưu trữ data tốc độ phục vụ vẽ biểu đồ
        self.upload_history = deque(maxlen=max_points)
        self.download_history = deque(maxlen=max_points)
        self.sample_count = 0

        # Lưu trữ chỉ số mới nhất
        self.latest_proc_speeds: List[Dict] = []
        self.latest_total_proc_down: float = 0.0
        self.latest_total_proc_up: float = 0.0
        self.proc_info_cache: Dict[int, Tuple[str, str]] = {}

        # Luồng ghi CSDL chạy ngầm bất đồng bộ hoàn toàn (không bao giờ block lấy mẫu tốc độ)
        self._db_queue = queue.Queue(maxsize=150)
        self._db_running = True
        self._db_worker = threading.Thread(target=self._db_worker_loop, daemon=True, name="DB-Worker-Thread")
        self._db_worker.start()

    def _db_worker_loop(self):
        """Worker ngầm chuyên xử lý các tác vụ ghi MySQL (giao diện và vòng lặp lấy mẫu không bị khựng)."""
        while self._db_running:
            try:
                task = self._db_queue.get(timeout=0.5)
                if task is None:
                    break
                task_type, args = task
                if task_type == "traffic":
                    ts, etw_samples = args
                    try:
                        self.db_manager.save_interface_traffic(ts)
                        if etw_samples:
                            self.db_manager.save_process_traffic_samples(etw_samples, ts)
                    except Exception as e:
                        print(f"[DB Async Error] {e}")
                elif task_type == "proc_conn":
                    ts = args
                    try:
                        self.db_manager.save_processes_and_connections(ts)
                    except Exception as e:
                        print(f"[DB Async Error] {e}")
                self._db_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[DB Loop Error] {e}")


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

        # 2. Đẩy tác vụ lưu mẫu mạng vào luồng ghi CSDL ngầm (hoàn toàn không block lấy mẫu)
        try:
            self._db_queue.put_nowait(("traffic", (curr_sample.timestamp, etw_samples)))
        except queue.Full:
            pass

        # 3. Xử lý lưu lượng từ ETW Consumer theo từng tiến trình
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

            # Sắp xếp tiến trình có lưu lượng mạng lớn nhất lên trên
            proc_speeds.sort(key=lambda x: (x["down_kb"] + x["up_kb"]), reverse=True)
            self.latest_proc_speeds = proc_speeds
            self.latest_total_proc_down = total_p_down
            self.latest_total_proc_up = total_p_up
        except Exception as e:
            print(f"[ETW Aggregate Error] {e}")

        # 3. Đẩy tác vụ lưu vết tiến trình và kết nối vào worker ngầm mỗi 5 chu kỳ
        if self.sample_count % 5 == 0:
            try:
                self._db_queue.put_nowait(("proc_conn", curr_sample.timestamp))
            except queue.Full:
                pass


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

            enriched.append({
                "pid": pid,
                "process_name": proc_name,
                "protocol": conn.protocol,
                "local_addr": f"{conn.local_ip}:{conn.local_port}" if conn.local_port else conn.local_ip,
                "remote_addr": f"{conn.remote_ip}:{conn.remote_port}" if conn.remote_ip and conn.remote_port else ("*" if conn.state == "LISTEN" else "-"),
                "state": conn.state
            })
        return enriched

    def get_database_verification(self) -> Tuple[Dict, List]:
        """Truy vấn thông số thống kê và lịch sử từ database để kiểm tra tính toàn vẹn."""
        stats = self.db_manager.get_database_stats()
        recent_logs = self.db_manager.get_recent_history(limit=15)
        return stats, recent_logs

    def get_recorded_dates(self) -> List[str]:
        """Lấy danh sách các ngày có ghi nhận lưu lượng trong database."""
        return self.db_manager.get_recorded_dates()

    def get_daily_traffic_series(self, target_date_str: str) -> List[Dict]:
        """Lấy chuỗi lưu lượng và tốc độ của một ngày cụ thể."""
        return self.db_manager.get_daily_traffic_series(target_date_str)

    def get_processes_around_time(self, target_datetime, tolerance_seconds: float = 3.0) -> List[Dict]:
        """Lấy danh sách tiến trình truyền nhận mạng tại một mốc thời gian."""
        return self.db_manager.get_processes_around_time(target_datetime, tolerance_seconds)

    # =========================================================================
    # CÁC PHƯƠNG THỨC ĐIỀU KHIỂN CHÍNH SÁCH WINDOWS QOS (UPLOAD THROTTLING)
    # =========================================================================
    def apply_qos_upload_limit(self, app_name: str, limit_kbps: float) -> Tuple[bool, str]:
        """Áp dụng chính sách giới hạn tốc độ Upload cho tiến trình."""
        return self.qos_manager.apply_upload_limit(app_name, limit_kbps)

    def remove_qos_upload_limit(self, app_name: str) -> Tuple[bool, str]:
        """Gỡ bỏ chính sách QoS của tiến trình."""
        return self.qos_manager.remove_upload_limit(app_name)

    def get_qos_status(self, app_name: str) -> Optional[Dict]:
        """Kiểm tra thông số chính sách QoS của tiến trình."""
        return self.qos_manager.get_policy_status(app_name)

    def stop(self):
        """Dọn dẹp toàn bộ tài nguyên: Gỡ chính sách QoS, tắt ETW và đóng MySQL."""
        # 1. Gỡ bỏ toàn bộ chính sách Windows QoS và kiểm tra trạng thái sạch sẽ
        if hasattr(self, 'qos_manager') and self.qos_manager:
            success, msg = self.qos_manager.remove_all_pbl4_policies()
            print(f"[Shutdown QoS] {msg}")

        # 2. Dừng ETW Consumer
        if hasattr(self, 'etw_consumer') and self.etw_consumer:
            self.etw_consumer.stop()

        # 3. Dừng luồng ghi CSDL ngầm
        self._db_running = False
        try:
            self._db_queue.put_nowait(None)
        except Exception:
            pass

        # 4. Đóng kết nối Database
        if hasattr(self, 'db_manager') and self.db_manager:
            self.db_manager.close()


