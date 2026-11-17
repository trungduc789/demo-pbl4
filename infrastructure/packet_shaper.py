"""
Bộ điều phối và bóp băng thông mạng thời gian thực dựa trên WinDivert (Kernel Packet Divert).
Hỗ trợ kiến trúc Đa chính sách cô lập (Per-App Isolated Policy):
- Mỗi ứng dụng bị bóp băng thông sở hữu riêng 1 Token Bucket, 1 Hàng đợi riêng biệt, và 1 Worker Thread độc lập.
- Triệt tiêu 100% hiện tượng Head-of-Line Blocking giữa các ứng dụng (Chrome 500 KB/s không làm chậm Discord 200 KB/s).
- Không gọi psutil trực tiếp trong luồng bắt gói (Capture Loop), đảm bảo Zero-latency pass-through cho lưu lượng thường.
- Port Tracking thông minh với TTL 5s, tự động thu hồi cổng khi kết nối đóng, chống nhận nhầm tiến trình.
- Quản lý danh bạ chặn (Block) và bóp băng thông (Throttle) độc lập hoàn toàn.
"""

from __future__ import annotations
import time
import queue
import threading
from typing import Dict, List, Optional, Set, Tuple, Any

import psutil
try:
    import pydivert
except ImportError:
    pydivert = None

from infrastructure.shaper_models import (
    PORT_TTL_SECONDS,
    MAX_PER_APP_QUEUE_SIZE,
    ALIAS_MAP,
    AppThrottlePolicy,
    AppBlockPolicy,
    resolve_target_name_and_exe,
)
from infrastructure.firewall_helper import (
    clean_rule_name,
    apply_windows_firewall_rule,
    remove_windows_firewall_rule,
    terminate_app_sockets,
)
from utils.safe_print import safe_print as print


class WinDivertPacketShaper:
    """
    Bộ điều phối và bóp băng thông mạng thời gian thực dựa trên WinDivert (Kernel Packet Divert):
    - Đánh chặn gói tin tầng NDIS (TCP/UDP) trước khi ra/vào card mạng.
    - Nhận diện gói tin thuộc tiến trình mục tiêu thông qua bảng ánh xạ cổng (Socket Port Mapping).
    - Áp dụng thuật toán Token Bucket độc lập cho từng ứng dụng, loại bỏ Head-of-Line Blocking.
    - Hỗ trợ cả 3 chế độ: Giới hạn Upload (Gửi), Download (Nhận), hoặc Cả hai chiều.
    - Các tiến trình khác không bị ảnh hưởng (Zero Latency Pass-through).
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._engine_lock = threading.RLock()
        self.is_active: bool = False

        # Danh sách chính sách độc lập
        self.throttled_apps: Dict[str, AppThrottlePolicy] = {}  # app_key -> AppThrottlePolicy
        self.blocked_apps: Dict[str, AppBlockPolicy] = {}        # app_key -> AppBlockPolicy

        # Ánh xạ cổng & PID
        self.port_to_throttle_app: Dict[int, str] = {}          # port -> app_key
        self.blocked_ports: Set[int] = set()
        self.target_ports: Set[int] = set()                     # Tập hợp cổng của tất cả throttled apps
        self.blocked_pids: Set[int] = set()
        self.target_pids: Set[int] = set()
        self._port_last_seen: Dict[int, float] = {}             # port -> timestamp

        # Quản lý luồng và handle WinDivert
        self._divert_handle: Optional[Any] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._port_monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @staticmethod
    def is_admin() -> bool:
        """Kiểm tra quyền Administrator (bắt buộc cho WinDivert driver & Windows Firewall)."""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    ALIAS_MAP = ALIAS_MAP

    def _resolve_target_name_and_exe(self, app_name: str, provided_exe: str = "") -> Tuple[str, str, Set[int]]:
        """Phân giải tên hiển thị thân thiện sang tên file .exe thật, đường dẫn thực thi và danh sách PID."""
        return resolve_target_name_and_exe(app_name, provided_exe, self.ALIAS_MAP)

    def _find_pids_for_name(self, app_name: str) -> Set[int]:
        _, _, pids = self._resolve_target_name_and_exe(app_name)
        return pids

    # =========================================================================
    # CÁC PROPERTY TƯƠNG THÍCH NGƯỢC (BACKWARD COMPATIBILITY)
    # =========================================================================
    @property
    def is_blocked(self) -> bool:
        with self._lock:
            return bool(self.blocked_apps)

    @is_blocked.setter
    def is_blocked(self, val: bool):
        pass

    @property
    def target_app(self) -> str:
        with self._lock:
            if self.throttled_apps:
                return next(reversed(self.throttled_apps.values())).target_app
            return ""

    @target_app.setter
    def target_app(self, val: str):
        pass

    @property
    def limit_kbps(self) -> float:
        with self._lock:
            if self.throttled_apps:
                return next(reversed(self.throttled_apps.values())).limit_kbps
            return 0.0

    @limit_kbps.setter
    def limit_kbps(self, val: float):
        pass

    @property
    def direction(self) -> str:
        with self._lock:
            if self.throttled_apps:
                return next(reversed(self.throttled_apps.values())).direction
            return "both"

    @direction.setter
    def direction(self, val: str):
        pass

    @property
    def current_speed_kbps(self) -> float:
        with self._lock:
            return sum(pol.current_speed_kbps for pol in self.throttled_apps.values())

    @property
    def current_down_kbps(self) -> float:
        with self._lock:
            return sum(pol.current_down_kbps for pol in self.throttled_apps.values())

    @property
    def current_up_kbps(self) -> float:
        with self._lock:
            return sum(pol.current_up_kbps for pol in self.throttled_apps.values())

    @property
    def total_throttled_bytes(self) -> int:
        with self._lock:
            return sum(pol.total_throttled_bytes for pol in self.throttled_apps.values())

    @property
    def total_throttled_packets(self) -> int:
        with self._lock:
            return sum(pol.total_throttled_packets for pol in self.throttled_apps.values())

    @property
    def total_down_bytes(self) -> int:
        with self._lock:
            return sum(pol.total_down_bytes for pol in self.throttled_apps.values())

    @property
    def total_up_bytes(self) -> int:
        with self._lock:
            return sum(pol.total_up_bytes for pol in self.throttled_apps.values())

    # =========================================================================
    # PORT TRACKING VÀ GIẢI PHÓNG PORT AN TOÀN (TTL 5S)
    # =========================================================================
    def _update_target_ports(self):
        """Quét và cập nhật danh sách local port thuộc các tiến trình đang bị quản lý."""
        with self._lock:
            throttled_keys = list(self.throttled_apps.keys())
            blocked_keys = list(self.blocked_apps.keys())

        if not throttled_keys and not blocked_keys:
            with self._lock:
                self.target_pids.clear()
                self.target_ports.clear()
                self.blocked_pids.clear()
                self.blocked_ports.clear()
                self.port_to_throttle_app.clear()
                self._port_last_seen.clear()
            return

        new_throttled_pids: Dict[int, str] = {}
        for t_key in throttled_keys:
            pids = self._find_pids_for_name(t_key)
            for pid in pids:
                new_throttled_pids[pid] = t_key

        new_blocked_pids: Dict[int, str] = {}
        for b_key in blocked_keys:
            pids = self._find_pids_for_name(b_key)
            for pid in pids:
                new_blocked_pids[pid] = b_key

        now = time.time()
        active_throttled_ports: Dict[int, str] = {}
        active_blocked_ports: Set[int] = set()

        # Quét socket của các tiến trình bị bóp băng thông
        for pid, app_k in new_throttled_pids.items():
            try:
                proc = psutil.Process(pid)
                for conn in proc.net_connections(kind='all'):
                    if conn.laddr:
                        port = conn.laddr.port
                        active_throttled_ports[port] = app_k
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Quét socket của các tiến trình bị chặn hoàn toàn
        for pid in new_blocked_pids.keys():
            try:
                proc = psutil.Process(pid)
                for conn in proc.net_connections(kind='all'):
                    if conn.laddr:
                        active_blocked_ports.add(conn.laddr.port)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        with self._lock:
            self.target_pids = set(new_throttled_pids.keys())
            self.blocked_pids = set(new_blocked_pids.keys())

            # Cập nhật các cổng đang active
            for port, app_k in active_throttled_ports.items():
                self._port_last_seen[port] = now
                self.port_to_throttle_app[port] = app_k

            # Thu hồi các cổng đã đóng sau TTL 5s để tránh nhận nhầm tiến trình
            expired_ports = [
                p for p, t in self._port_last_seen.items()
                if (p not in active_throttled_ports) and (now - t > PORT_TTL_SECONDS)
            ]
            for p in expired_ports:
                del self._port_last_seen[p]
                self.port_to_throttle_app.pop(p, None)

            self.target_ports = set(self.port_to_throttle_app.keys())
            self.blocked_ports = active_blocked_ports

    def _port_monitor_loop(self, stop_event: threading.Event):
        """Luồng quét danh bạ cổng định kỳ (100ms) trên tập PID mục tiêu."""
        while not stop_event.is_set():
            try:
                self._update_target_ports()
            except Exception:
                pass
            stop_event.wait(0.1)

    # =========================================================================
    # WORKER TOKEN BUCKET ĐỘC LẬP TỪNG APP (CHỐNG HEAD-OF-LINE BLOCKING)
    # =========================================================================
    def _app_throttle_worker_loop(self, policy: AppThrottlePolicy):
        """
        Luồng Token Bucket riêng cho một ứng dụng:
        Lấy các gói tin từ hàng đợi của riêng ứng dụng đó, tính độ trễ rồi bơm lại vào mạng.
        Ứng dụng này ngủ sẽ KHÔNG làm ảnh hưởng bất kỳ ứng dụng nào khác!
        """
        rate_bytes_per_sec = max(1024.0, policy.limit_kbps * 1024.0)
        burst_capacity = max(2048.0, rate_bytes_per_sec * 0.08)
        policy.last_refill = time.perf_counter()

        while not policy.stop_event.is_set() and not self._stop_event.is_set():
            try:
                item = policy.queue.get(timeout=0.05)
            except queue.Empty:
                policy.update_speed_stats(0, False)
                continue

            if item is None:
                break

            packet, packet_size, is_inbound = item

            handle = self._divert_handle
            if not handle or not getattr(handle, "is_open", True):
                break

            now = time.perf_counter()
            elapsed = now - policy.last_refill
            tokens = min(burst_capacity, policy.tokens + elapsed * rate_bytes_per_sec)
            policy.last_refill = now

            if tokens < packet_size:
                needed = packet_size - tokens
                sleep_sec = needed / rate_bytes_per_sec
                if sleep_sec > 0.0002:
                    time.sleep(sleep_sec)
                after_sleep = time.perf_counter()
                actual_elapsed = max(0.0, after_sleep - now)
                refilled = min(burst_capacity, tokens + actual_elapsed * rate_bytes_per_sec)
                policy.tokens = max(0.0, refilled - packet_size)
                policy.last_refill = after_sleep
            else:
                policy.tokens = tokens - packet_size

            try:
                handle.send(packet, recalculate_checksum=False)
                policy.update_speed_stats(packet_size, is_inbound)
            except Exception:
                pass

    # =========================================================================
    # LUỒNG CAPTURE WINDIVERT (TỐI ƯU HÓA HIỆU NĂNG CAO, KHÔNG GỌI PSUTIL)
    # =========================================================================
    def _capture_loop(self, stop_event: threading.Event):
        """
        Luồng chính bắt gói tin tầng mạng:
        - Sử dụng tra cứu O(1) trong bộ nhớ, tuyệt đối không gọi psutil.
        - Gói tin của app bị Block: Drop ngay tại driver.
        - Gói tin của app bị Throttle: Phân phối vào đúng hàng đợi của app đó.
        - Gói tin bình thường khác: Cho qua tức thì (Zero latency pass-through).
        """
        if pydivert is None:
            print("[PacketShaper Error] Thư viện pydivert chưa được cài đặt!")
            return

        filter_str = "!loopback and (tcp or udp)"
        w = None
        try:
            with pydivert.WinDivert(filter=filter_str, priority=0) as w:
                self._divert_handle = w
                print(f"[PacketShaper] Đã kích hoạt WinDivert Kernel Filtering thành công.")

                while not stop_event.is_set():
                    try:
                        packet = w.recv()
                        if not packet:
                            continue
                    except Exception:
                        if stop_event.is_set():
                            break
                        continue

                    try:
                        is_inbound = packet.is_inbound
                        port = packet.dst_port if is_inbound else packet.src_port

                        # Tra cứu O(1)
                        with self._lock:
                            if port in self.blocked_ports:
                                # 1. Thuộc ứng dụng bị BLOCK -> Hủy gói tại chỗ
                                continue

                            app_key = self.port_to_throttle_app.get(port)
                            policy = self.throttled_apps.get(app_key) if app_key else None

                        if policy is not None:
                            # 2. Thuộc ứng dụng bị THROTTLE -> Kiểm tra hướng áp dụng
                            direction = policy.direction
                            match_dir = (
                                direction == "both" or
                                (direction == "upload" and not is_inbound) or
                                (direction == "download" and is_inbound)
                            )
                            if match_dir:
                                packet_len = len(packet.raw) if packet.raw else 64
                                try:
                                    policy.queue.put_nowait((packet, packet_len, is_inbound))
                                    self._port_last_seen[port] = time.time()
                                    continue
                                except queue.Full:
                                    # Tail drop riêng cho app này khi hàng đợi quá đầy
                                    continue

                        # 3. Gói tin bình thường -> Cho qua tức thì
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
            if not stop_event.is_set():
                print(f"[PacketShaper Error] WinDivert capture dừng: {e}")
        finally:
            if self._divert_handle is w:
                self._divert_handle = None

    # =========================================================================
    # QUẢN LÝ VÒNG ĐỜI LUỒNG & ENGINE
    # =========================================================================
    def _ensure_engine_running(self):
        """Khởi động Engine WinDivert nếu chưa chạy."""
        with self._engine_lock:
            if not self.is_active or (self._capture_thread and not self._capture_thread.is_alive()):
                self._stop_engine_threads()
                self.is_active = True
                stop_ev = threading.Event()
                self._stop_event = stop_ev
                self._update_target_ports()

                self._port_monitor_thread = threading.Thread(
                    target=self._port_monitor_loop, args=(stop_ev,), daemon=True, name="PacketShaper-PortMonitor"
                )
                self._port_monitor_thread.start()

                self._capture_thread = threading.Thread(
                    target=self._capture_loop, args=(stop_ev,), daemon=True, name="PacketShaper-Capture"
                )
                self._capture_thread.start()
            else:
                self._update_target_ports()

    def _stop_engine_threads(self):
        """Dừng an toàn toàn bộ luồng và giải phóng tài nguyên WinDivert."""
        with self._engine_lock:
            self.is_active = False
            self._stop_event.set()

            # Dừng các worker riêng lẻ
            with self._lock:
                for pol in self.throttled_apps.values():
                    pol.stop_event.set()
                    while not pol.queue.empty():
                        try:
                            pol.queue.get_nowait()
                        except Exception:
                            break

            handle = self._divert_handle
            self._divert_handle = None
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

            cap_t = self._capture_thread
            if cap_t and cap_t.is_alive() and cap_t is not threading.current_thread():
                try:
                    cap_t.join(timeout=1.0)
                except Exception:
                    pass

            pm_t = self._port_monitor_thread
            if pm_t and pm_t.is_alive() and pm_t is not threading.current_thread():
                try:
                    pm_t.join(timeout=0.5)
                except Exception:
                    pass

            self._capture_thread = None
            self._port_monitor_thread = None

    # =========================================================================
    # QUY TẮC FIREWALL & ĐÓNG SOCKET TCP
    # =========================================================================
    @staticmethod
    def _clean_rule_name(app_name: str) -> str:
        return clean_rule_name(app_name)

    @staticmethod
    def _apply_windows_firewall_rule(app_name: str, exe_path: str = "", action: str = "block") -> bool:
        return apply_windows_firewall_rule(app_name, exe_path, action)

    @staticmethod
    def _remove_windows_firewall_rule(app_name: str) -> bool:
        return remove_windows_firewall_rule(app_name)

    @staticmethod
    def _terminate_app_sockets(app_pids: Set[int]):
        terminate_app_sockets(app_pids)

    # =========================================================================
    # CÁC HÀM CÔNG KHAI: BLOCK & UNBLOCK (CHẶN HOÀN TOÀN)
    # =========================================================================
    def block_app(self, app_name: str, direction: str = "both", exe_path: str = "") -> Tuple[bool, str]:
        """
        Chặn hoàn toàn quyền truy cập mạng của ứng dụng:
        1. Kiểm tra quyền Administrator.
        2. Thêm quy tắc Windows Defender Firewall (Inbound & Outbound) cho file .exe.
        3. Cắt đứt các kết nối socket TCP đang mở.
        4. Kích hoạt WinDivert Kernel Dropper để hủy ngay các gói tin.
        """
        if not self.is_admin():
            err_msg = (
                f"Không thể chặn '{app_name}' vì ứng dụng chưa chạy với quyền Administrator!\n\n"
                "Hệ điều hành Windows yêu cầu quyền 'Run as administrator' để có thể cấu hình "
                "Windows Defender Firewall và driver WinDivert."
            )
            print(f"[PacketShaper Admin Error] {err_msg}")
            return False, err_msg

        canonical_name, real_exe, pids = self._resolve_target_name_and_exe(app_name, exe_path)
        target = canonical_name or app_name
        app_key = target.lower().strip()
        dir_norm = direction.lower().strip() or "both"

        rule_name = self._clean_rule_name(target)
        policy = AppBlockPolicy(
            app_key=app_key,
            target_app=target,
            direction=dir_norm,
            exe_path=real_exe,
            pids=pids,
            firewall_rule_name=rule_name
        )

        with self._lock:
            self.blocked_apps[app_key] = policy
            self.blocked_pids.update(pids)

        # Cắt các socket đang kết nối
        self._terminate_app_sockets(pids)

        # Kích hoạt Firewall
        fw_ok = self._apply_windows_firewall_rule(target, real_exe, action="block")

        # Kích hoạt Engine WinDivert
        self._ensure_engine_running()

        msg = f"Đã kích hoạt chặn hoàn toàn truy cập mạng cho ứng dụng '{target}' ({len(pids)} tiến trình, Firewall: {'Bật' if fw_ok else 'WinDivert'})."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def unblock_app(self, app_name: str) -> Tuple[bool, str]:
        """Gỡ bỏ chặn cho một ứng dụng cụ thể (không ảnh hưởng đến các ứng dụng khác)."""
        canonical_name, _, _ = self._resolve_target_name_and_exe(app_name)
        target = canonical_name or app_name
        raw_lower = app_name.lower().strip()
        candidate_keys = {
            raw_lower,
            f"{raw_lower}.exe" if not raw_lower.endswith(".exe") else raw_lower[:-4],
            target.lower().strip(),
        }

        removed_policies = []
        with self._lock:
            for k in list(self.blocked_apps.keys()):
                if k in candidate_keys or k.replace(".exe", "") in candidate_keys:
                    removed_policies.append(self.blocked_apps.pop(k))

        for pol in removed_policies:
            self._remove_windows_firewall_rule(pol.target_app)

        with self._lock:
            has_remaining = bool(self.blocked_apps or self.throttled_apps)

        if not has_remaining:
            self._stop_engine_threads()
        else:
            self._update_target_ports()

        msg = f"Đã gỡ chặn mạng cho ứng dụng '{target}' thành công."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def unblock_all(self) -> Tuple[bool, str]:
        """Gỡ bỏ chặn cho tất cả các ứng dụng."""
        with self._lock:
            all_policies = list(self.blocked_apps.values())
            self.blocked_apps.clear()
            self.blocked_ports.clear()
            self.blocked_pids.clear()
            has_remaining = bool(self.throttled_apps)

        for pol in all_policies:
            self._remove_windows_firewall_rule(pol.target_app)

        if not has_remaining:
            self._stop_engine_threads()
        else:
            self._update_target_ports()

        msg = "Đã gỡ bỏ toàn bộ chính sách chặn mạng cho tất cả ứng dụng."
        print(f"[PacketShaper] {msg}")
        return True, msg

    # =========================================================================
    # CÁC HÀM CÔNG KHAI: BÓP BĂNG THÔNG ĐỘC LẬP (APPLY & REMOVE LIMIT)
    # =========================================================================
    def apply_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        """
        Kích hoạt bóp băng thông cho tiến trình mục tiêu.
        Hỗ trợ nhiều ứng dụng đồng thời, mỗi ứng dụng có Token Bucket và Worker riêng.
        """
        if not self.is_admin():
            msg = "Cần khởi chạy ứng dụng với quyền Administrator để WinDivert có quyền can thiệp gói tin mạng."
            print(f"[PacketShaper Error] {msg}")
            return False, msg

        if limit_kbps <= 0:
            return False, "Mức giới hạn băng thông phải lớn hơn 0 KB/s."

        canonical_name, _, _ = self._resolve_target_name_and_exe(app_name)
        target = canonical_name or (app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe")
        app_key = target.lower().strip()
        dir_norm = direction.lower().strip() or "both"

        with self._lock:
            existing = self.throttled_apps.get(app_key)
            if existing and existing.worker_thread and existing.worker_thread.is_alive():
                existing.stop_event.set()

            policy = AppThrottlePolicy(
                app_key=app_key,
                target_app=target,
                limit_kbps=limit_kbps,
                direction=dir_norm,
                tokens=0.0,
                last_refill=time.perf_counter()
            )
            if existing is not None:
                while not existing.queue.empty():
                    try:
                        pkt_item = existing.queue.get_nowait()
                        if pkt_item is not None:
                            policy.queue.put_nowait(pkt_item)
                    except (queue.Empty, queue.Full):
                        break
            self.throttled_apps[app_key] = policy

        # Đảm bảo Engine chính chạy
        self._ensure_engine_running()

        # Khởi động worker riêng cho app này
        worker = threading.Thread(
            target=self._app_throttle_worker_loop,
            args=(policy,),
            daemon=True,
            name=f"PacketShaper-Worker-{target}"
        )
        policy.worker_thread = worker
        worker.start()

        dir_str = "Upload & Download" if dir_norm == "both" else ("Upload" if dir_norm == "upload" else "Download")
        msg = f"Đã kích hoạt bóp băng thông WinDivert cho '{target}' thành công: Giới hạn {dir_str} {limit_kbps:,.1f} KB/s."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def remove_limit(self, app_name: Optional[str] = None) -> Tuple[bool, str]:
        """
        Dừng giới hạn băng thông cho 1 ứng dụng cụ thể hoặc toàn bộ.
        Không làm ảnh hưởng đến các ứng dụng khác vẫn đang chạy chính sách.
        """
        with self._lock:
            had_limit = bool(self.throttled_apps)
            removed_policies = []

            if app_name and app_name.strip():
                raw_lower = app_name.lower().strip()
                canonical_name, _, _ = self._resolve_target_name_and_exe(app_name)
                candidate_keys = {
                    raw_lower,
                    f"{raw_lower}.exe" if not raw_lower.endswith(".exe") else raw_lower[:-4],
                    canonical_name.lower().strip() if canonical_name else "",
                }
                for k in list(self.throttled_apps.keys()):
                    if k in candidate_keys or k.replace(".exe", "") in candidate_keys:
                        removed_policies.append(self.throttled_apps.pop(k))
                app_label = app_name
            else:
                removed_policies = list(self.throttled_apps.values())
                self.throttled_apps.clear()
                app_label = "tất cả ứng dụng"

            # Dọn sạch cổng của các app bị gỡ
            for pol in removed_policies:
                pol.stop_event.set()
                for p, a_key in list(self.port_to_throttle_app.items()):
                    if a_key == pol.app_key:
                        self.port_to_throttle_app.pop(p, None)
                        self._port_last_seen.pop(p, None)

            has_remaining = bool(self.blocked_apps or self.throttled_apps)

        if not self.is_active and not had_limit:
            return True, "Không có chính sách bóp băng thông nào đang chạy."

        if not has_remaining:
            self._stop_engine_threads()
        else:
            self._update_target_ports()

        msg = f"Đã gỡ bỏ giới hạn băng thông WinDivert cho '{app_label}' thành công. Toàn bộ lưu lượng trở lại bình thường."
        print(f"[PacketShaper] {msg}")
        return True, msg

    # =========================================================================
    # HÀM LẤY TRẠNG THÁI VÀ BÁO CÁO (GET_STATUS)
    # =========================================================================
    def get_status(self) -> Dict:
        """Lấy thông tin trạng thái bóp băng thông hiện hành (chi tiết từng ứng dụng và tổng hợp)."""
        with self._lock:
            throttled_dict = {
                k: {
                    "target_app": pol.target_app,
                    "limit_kbps": pol.limit_kbps,
                    "direction": pol.direction,
                    "current_speed_kbps": pol.current_speed_kbps,
                    "current_down_kbps": pol.current_down_kbps,
                    "current_up_kbps": pol.current_up_kbps,
                    "throttled_packets": pol.total_throttled_packets,
                    "throttled_bytes": pol.total_throttled_bytes,
                } for k, pol in self.throttled_apps.items()
            }

            return {
                "is_active": self.is_active,
                "target_app": self.target_app,
                "limit_kbps": self.limit_kbps,
                "direction": self.direction,
                "throttled_apps": throttled_dict,
                "blocked_apps": [pol.target_app for pol in self.blocked_apps.values()],
                "target_ports_count": len(self.target_ports),
                "throttled_packets": sum(pol.total_throttled_packets for pol in self.throttled_apps.values()),
                "throttled_bytes": sum(pol.total_throttled_bytes for pol in self.throttled_apps.values()),
                "total_down_bytes": sum(pol.total_down_bytes for pol in self.throttled_apps.values()),
                "total_up_bytes": sum(pol.total_up_bytes for pol in self.throttled_apps.values()),
                "current_speed_kbps": sum(pol.current_speed_kbps for pol in self.throttled_apps.values()),
                "current_down_kbps": sum(pol.current_down_kbps for pol in self.throttled_apps.values()),
                "current_up_kbps": sum(pol.current_up_kbps for pol in self.throttled_apps.values()),
            }

    # =========================================================================
    # CÁC HÀM TƯƠNG THÍCH KHÁC (START & STOP)
    # =========================================================================
    def start(self, target_app: str, limit_kbps: float = 0.0, direction: str = "both") -> Tuple[bool, str]:
        """Tự động phân nhánh sang block_app hoặc apply_limit tùy theo hạn mức."""
        if limit_kbps <= 1.0:
            return self.block_app(target_app, direction=direction)
        else:
            return self.apply_limit(target_app, limit_kbps=limit_kbps, direction=direction)

    def stop(self) -> Tuple[bool, str]:
        """Gỡ bỏ toàn bộ chặn và bóp băng thông."""
        self.unblock_all()
        return self.remove_limit()
