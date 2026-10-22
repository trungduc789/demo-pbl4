import time
import queue
import threading
from typing import Dict, List, Optional, Set, Tuple
import psutil
import pydivert
from utils.safe_print import safe_print as print


class WinDivertPacketShaper:
    """
    Bộ điều phối và bóp băng thông mạng thời gian thực dựa trên WinDivert (Kernel Packet Divert):
    - Đánh chặn gói tin tầng NDIS (TCP/UDP) trước khi ra/vào card mạng.
    - Nhận diện gói tin thuộc tiến trình mục tiêu thông qua bảng ánh xạ cổng (Socket Port Mapping).
    - Áp dụng thuật toán Token Bucket để giới hạn tốc độ truyền tải chính xác đến từng KB/s.
    - Hỗ trợ cả 3 chế độ: Giới hạn Upload (Gửi), Download (Nhận), hoặc Cả hai chiều.
    - Các tiến trình khác không bị ảnh hưởng (Zero Latency Pass-through).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.target_app: str = ""
        self.target_pids: Set[int] = set()
        self.target_ports: Set[int] = set()
        self._port_last_seen: Dict[int, float] = {}
        self.limit_kbps: float = 0.0
        self.direction: str = "both"  # "upload", "download", "both"
        self.is_active: bool = False

        # Quản lý luồng và hàng đợi
        self._divert_handle: Optional[pydivert.WinDivert] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._throttle_worker_thread: Optional[threading.Thread] = None
        self._port_monitor_thread: Optional[threading.Thread] = None
        self._packet_queue: queue.Queue = queue.Queue(maxsize=1500)
        self._stop_event = threading.Event()

        # Thống kê đo đạc thời gian thực
        self.total_throttled_bytes: int = 0
        self.total_throttled_packets: int = 0
        self.total_down_bytes: int = 0
        self.total_up_bytes: int = 0
        self._recent_down_bytes: int = 0
        self._recent_up_bytes: int = 0
        self._recent_ts: float = time.time()
        self.current_speed_kbps: float = 0.0
        self.current_down_kbps: float = 0.0
        self.current_up_kbps: float = 0.0

    @staticmethod
    def is_admin() -> bool:
        """Kiểm tra quyền Administrator (bắt buộc cho WinDivert driver)."""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def _find_target_pids(self) -> Set[int]:
        """Tìm tất cả PID thuộc ứng dụng mục tiêu (kể cả tiến trình con)."""
        if not self.target_app:
            return set()
        target_lower = self.target_app.lower()
        target_base = target_lower.replace(".exe", "").strip()

        found = set()
        for p in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                p_name = (p.info.get('name') or "").lower()
                p_exe = (p.info.get('exe') or "").lower()
                if p_name == target_lower or p_name.replace(".exe", "") == target_base:
                    found.add(p.info['pid'])
                elif target_base and (target_base in p_name or target_base in p_exe):
                    found.add(p.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

    def _update_target_ports(self):
        """Quét và cập nhật danh sách local port thuộc tiến trình mục tiêu."""
        if not self.target_app:
            return

        found_pids = self._find_target_pids()
        with self._lock:
            self.target_pids = found_pids

        now = time.time()
        new_ports = set()

        # Quét cổng trực tiếp theo PID để đạt tốc độ tối đa (< 2ms)
        for pid in found_pids:
            try:
                proc = psutil.Process(pid)
                for conn in proc.net_connections(kind='all'):
                    if conn.laddr:
                        port = conn.laddr.port
                        new_ports.add(port)
                        self._port_last_seen[port] = now
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Quét bổ sung toàn hệ thống để không sót socket nào của target_pids
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.pid in found_pids and conn.laddr:
                    port = conn.laddr.port
                    new_ports.add(port)
                    self._port_last_seen[port] = now
        except Exception:
            pass

        # Giữ lại các cổng đã thấy trong vòng 120 giây (tránh mất cổng khi kết nối Keep-Alive nhàn rỗi)
        expired_ports = [p for p, t in self._port_last_seen.items() if now - t > 120.0]
        for p in expired_ports:
            del self._port_last_seen[p]

        with self._lock:
            self.target_ports = set(self._port_last_seen.keys())

    def _port_monitor_loop(self):
        """Luồng chạy ngầm quét cổng mới mở của tiến trình mục tiêu tần số cao (50ms)."""
        while not self._stop_event.is_set():
            try:
                self._update_target_ports()
            except Exception:
                pass
            self._stop_event.wait(0.05)

    def _update_speed_stats(self, packet_size: int, is_inbound: bool):
        """Tính toán tốc độ tức thời mượt mà cho 2 chiều upload/download."""
        now = time.time()
        with self._lock:
            if packet_size > 0:
                self.total_throttled_bytes += packet_size
                self.total_throttled_packets += 1
                if is_inbound:
                    self.total_down_bytes += packet_size
                    self._recent_down_bytes += packet_size
                else:
                    self.total_up_bytes += packet_size
                    self._recent_up_bytes += packet_size

            dt = now - self._recent_ts
            if dt >= 0.5:
                self.current_down_kbps = (self._recent_down_bytes / 1024.0) / dt
                self.current_up_kbps = (self._recent_up_bytes / 1024.0) / dt
                self.current_speed_kbps = ((self._recent_down_bytes + self._recent_up_bytes) / 1024.0) / dt
                self._recent_down_bytes = 0
                self._recent_up_bytes = 0
                self._recent_ts = now

    def _throttle_worker_loop(self):
        """
        Luồng Token Bucket: Lấy các gói tin của tiến trình mục tiêu từ hàng đợi,
        tính toán độ trễ cần thiết để giữ tốc độ dưới mức trần, rồi inject lại vào mạng.
        """
        last_refill = time.perf_counter()
        tokens = 0.0

        while not self._stop_event.is_set():
            try:
                item = self._packet_queue.get(timeout=0.05)
                if item is None:
                    break
            except queue.Empty:
                self._update_speed_stats(0, False)
                continue

            packet, packet_size, is_inbound = item
            handle = self._divert_handle

            if not handle or not handle.is_open:
                break

            rate_bytes_per_sec = max(1024.0, self.limit_kbps * 1024.0)
            # Dung lượng burst tối đa 80ms hoặc 2KB để không bị nghẽn giật
            burst_capacity = max(2048.0, rate_bytes_per_sec * 0.08)

            now = time.perf_counter()
            elapsed = now - last_refill
            tokens = min(burst_capacity, tokens + elapsed * rate_bytes_per_sec)
            last_refill = now

            if tokens < packet_size:
                needed = packet_size - tokens
                sleep_sec = needed / rate_bytes_per_sec
                if sleep_sec > 0.0002:
                    time.sleep(sleep_sec)
                tokens = 0.0
                last_refill = time.perf_counter()
            else:
                tokens -= packet_size

            # Bơm gói tin trở lại ngăn xếp mạng
            try:
                handle.send(packet, recalculate_checksum=False)
                self._update_speed_stats(packet_size, is_inbound)
            except Exception:
                pass

    def _capture_loop(self):
        """
        Luồng chính bắt gói tin tầng mạng:
        - Bắt các gói tin TCP/UDP không phải loopback.
        - Phát hiện cổng socket mới tức thì ngay trên gói tin outbound đầu tiên.
        - Gói tin của các app khác: Gửi đi NGAY LẬP TỨC (Zero latency pass-through).
        - Gói tin của tiến trình mục tiêu: Đưa vào hàng đợi Token Bucket để bóp đúng trần.
        """
        filter_str = "!loopback and (tcp or udp)"
        try:
            with pydivert.WinDivert(filter=filter_str, priority=0) as w:
                self._divert_handle = w
                print(f"[PacketShaper] Đã kích hoạt WinDivert thành công cho '{self.target_app}' (Trần: {self.limit_kbps} KB/s)")

                while not self._stop_event.is_set():
                    try:
                        packet = w.recv()
                        if not packet:
                            continue
                    except Exception:
                        if self._stop_event.is_set():
                            break
                        continue

                    try:
                        is_target = False
                        with self._lock:
                            ports = self.target_ports
                            pids = self.target_pids

                        if packet.is_outbound:
                            if packet.src_port in ports:
                                if self.direction in ("upload", "both"):
                                    is_target = True
                            else:
                                # Nhận diện tức thì: cổng outbound mới có thuộc target_pids không?
                                # Ngăn ngừa 100% tình trạng vài trăm gói tin đầu bị lọt trước khi luồng quét phát hiện
                                try:
                                    for pid in pids:
                                        try:
                                            for conn in psutil.Process(pid).net_connections():
                                                if conn.laddr and conn.laddr.port == packet.src_port:
                                                    with self._lock:
                                                        self.target_ports.add(packet.src_port)
                                                        self._port_last_seen[packet.src_port] = time.time()
                                                    if self.direction in ("upload", "both"):
                                                        is_target = True
                                                    break
                                            if is_target:
                                                break
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                        elif packet.is_inbound:
                            if packet.dst_port in ports:
                                if self.direction in ("download", "both"):
                                    is_target = True

                        if is_target:
                            packet_len = len(packet.raw) if packet.raw else 64
                            try:
                                self._packet_queue.put_nowait((packet, packet_len, packet.is_inbound))
                            except queue.Full:
                                # Khi hàng đợi đầy, drop gói tin để ép TCP/QUIC hạ cửa sổ tắc nghẽn (cwnd)
                                pass
                        else:
                            # Gói tin của các app khác -> Cho qua NGAY LẬP TỨC
                            try:
                                w.send(packet, recalculate_checksum=False)
                            except Exception:
                                pass
                    except Exception:
                        try:
                            w.send(packet, recalculate_checksum=False)
                        except Exception:
                            pass
        except Exception as e:
            if not self._stop_event.is_set():
                print(f"[PacketShaper Error] WinDivert capture dừng: {e}")
        finally:
            self._divert_handle = None

    def apply_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        """
        Kích hoạt bóp băng thông cho tiến trình mục tiêu.
        - app_name: Tên tiến trình (ví dụ: 'brave.exe', 'chrome.exe', 'brave')
        - limit_kbps: Mức trần băng thông tính bằng KB/s
        - direction: 'upload', 'download', hoặc 'both'
        """
        if not self.is_admin():
            msg = "Cần khởi chạy ứng dụng với quyền Administrator để WinDivert có quyền can thiệp gói tin mạng."
            print(f"[PacketShaper Error] {msg}")
            return False, msg

        if limit_kbps <= 0:
            return False, "Mức giới hạn băng thông phải lớn hơn 0 KB/s."

        self.remove_limit()

        target = app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe"
        self.target_app = target
        self.limit_kbps = limit_kbps
        self.direction = direction.lower()
        self.is_active = True
        self._stop_event.clear()
        self.total_throttled_bytes = 0
        self.total_throttled_packets = 0
        self.total_down_bytes = 0
        self.total_up_bytes = 0
        self._recent_down_bytes = 0
        self._recent_up_bytes = 0
        self._recent_ts = time.time()
        self.current_speed_kbps = 0.0
        self.current_down_kbps = 0.0
        self.current_up_kbps = 0.0
        self._port_last_seen.clear()

        # Quét cổng lần đầu
        self._update_target_ports()

        # Khởi động luồng quét cổng
        self._port_monitor_thread = threading.Thread(
            target=self._port_monitor_loop, daemon=True, name="PacketShaper-PortMonitor"
        )
        self._port_monitor_thread.start()

        # Khởi động luồng Token Bucket
        self._throttle_worker_thread = threading.Thread(
            target=self._throttle_worker_loop, daemon=True, name="PacketShaper-TokenBucket"
        )
        self._throttle_worker_thread.start()

        # Khởi động luồng bắt gói tin WinDivert
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="PacketShaper-Capture"
        )
        self._capture_thread.start()

        dir_str = "Upload & Download" if self.direction == "both" else ("Upload" if self.direction == "upload" else "Download")
        msg = f"Đã kích hoạt bóp băng thông WinDivert cho '{target}' thành công: Giới hạn {dir_str} {limit_kbps:,.1f} KB/s."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def remove_limit(self) -> Tuple[bool, str]:
        """Dừng can thiệp và giải phóng toàn bộ tài nguyên WinDivert."""
        if not self.is_active:
            return True, "Không có chính sách bóp băng thông nào đang chạy."

        app = self.target_app
        self.is_active = False
        self._stop_event.set()

        # Đóng handle để ngắt vòng lặp recv
        if self._divert_handle:
            try:
                self._divert_handle.close()
            except Exception:
                pass

        # Dọn dẹp hàng đợi
        while not self._packet_queue.empty():
            try:
                self._packet_queue.get_nowait()
            except Exception:
                break

        self.target_app = ""
        self.target_pids.clear()
        self.target_ports.clear()
        self._port_last_seen.clear()
        self.limit_kbps = 0.0
        self.current_speed_kbps = 0.0
        self.current_down_kbps = 0.0
        self.current_up_kbps = 0.0

        msg = f"Đã gỡ bỏ giới hạn băng thông WinDivert cho '{app}' thành công. Toàn bộ lưu lượng trở lại bình thường."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def get_status(self) -> Dict:
        """Lấy thông tin trạng thái bóp băng thông hiện hành."""
        with self._lock:
            return {
                "is_active": self.is_active,
                "target_app": self.target_app,
                "limit_kbps": self.limit_kbps,
                "direction": self.direction,
                "target_ports_count": len(self.target_ports),
                "throttled_packets": self.total_throttled_packets,
                "throttled_bytes": self.total_throttled_bytes,
                "total_down_bytes": self.total_down_bytes,
                "total_up_bytes": self.total_up_bytes,
                "current_speed_kbps": self.current_speed_kbps,
                "current_down_kbps": self.current_down_kbps,
                "current_up_kbps": self.current_up_kbps,
            }
