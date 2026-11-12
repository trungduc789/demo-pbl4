import os
import subprocess
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
        self._lock = threading.RLock()
        self._engine_lock = threading.RLock()
        self.target_app: str = ""
        self.target_pids: Set[int] = set()
        self.target_ports: Set[int] = set()
        self._port_last_seen: Dict[int, float] = {}
        self.limit_kbps: float = 0.0
        self.direction: str = "both"  # "upload", "download", "both"
        self.is_active: bool = False
        self.is_blocked: bool = False

        # Danh sách các ứng dụng bị CHẶN HOÀN TOÀN: {app_name.lower(): {"direction": "both"|"upload"|"download", "exe": exe_path}}
        self.blocked_apps: Dict[str, Dict] = {}
        self.blocked_ports: Set[int] = set()
        self.blocked_pids: Set[int] = set()

        # Danh sách các ứng dụng bị BÓP BĂNG THÔNG (Multi-App QoS):
        # {app_key: {"target_app": str, "limit_kbps": float, "direction": str, "tokens": float, "last_refill": float}}
        self.throttled_apps: Dict[str, Dict] = {}
        self.port_to_throttle_app: Dict[int, str] = {}
        self.pid_to_throttle_app: Dict[int, str] = {}

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

    ALIAS_MAP = {
        "google chrome": "chrome",
        "microsoft edge": "msedge",
        "brave browser": "brave",
        "riot client": "riotclientux",
        "windows explorer": "explorer",
        "discord": "discord",
        "telegram": "telegram",
        "zalo": "zalo",
    }

    def _resolve_target_name_and_exe(self, app_name: str, provided_exe: str = "") -> Tuple[str, str, Set[int]]:
        """Phân giải tên hiển thị thân thiện sang tên file .exe thật, đường dẫn thực thi và danh sách PID."""
        if not app_name:
            return "", "", set()

        raw_lower = app_name.lower().strip()
        clean = raw_lower.replace(".exe", "").strip()
        alias = self.ALIAS_MAP.get(clean, clean)

        found_pids = set()
        found_exe = provided_exe if (provided_exe and os.path.exists(provided_exe)) else ""
        canonical_name = ""

        for p in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                p_name = (p.info.get('name') or "").lower()
                p_exe = p.info.get('exe') or ""
                p_exe_lower = p_exe.lower()
                p_base = p_name.replace(".exe", "")

                is_match = (
                    p_name == raw_lower or
                    p_name == f"{alias}.exe" or
                    p_base == clean or
                    p_base == alias or
                    (alias and (alias in p_name or alias in p_exe_lower))
                )

                if is_match:
                    found_pids.add(p.info['pid'])
                    if not found_exe and p_exe and os.path.exists(p_exe):
                        found_exe = p_exe
                    if not canonical_name:
                        canonical_name = p.info.get('name') or ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not canonical_name:
            canonical_name = f"{alias}.exe" if not raw_lower.endswith(".exe") else app_name

        return canonical_name, found_exe, found_pids

    def _find_pids_for_name(self, app_name: str) -> Set[int]:
        _, _, pids = self._resolve_target_name_and_exe(app_name)
        return pids

    def _find_target_pids(self) -> Set[int]:
        """Tìm tất cả PID thuộc ứng dụng mục tiêu (kể cả tiến trình con)."""
        if not self.target_app:
            return set()
        _, _, pids = self._resolve_target_name_and_exe(self.target_app)
        return pids

    def _update_target_ports(self):
        """Quét và cập nhật danh sách local port thuộc tiến trình mục tiêu hoặc ứng dụng bị chặn."""
        with self._lock:
            throttled_keys = list(self.throttled_apps.keys())
            has_target = bool(self.target_app or throttled_keys)
            blocked_names = list(self.blocked_apps.keys())

        if not has_target and not blocked_names:
            with self._lock:
                self.target_pids.clear()
                self.target_ports.clear()
                self.pid_to_throttle_app.clear()
                self.port_to_throttle_app.clear()
                self.blocked_pids.clear()
                self.blocked_ports.clear()
            return

        pid_to_app: Dict[int, str] = {}
        found_pids: Set[int] = set()
        if throttled_keys:
            for t_key in throttled_keys:
                pids = self._find_pids_for_name(t_key)
                found_pids.update(pids)
                for p in pids:
                    pid_to_app[p] = t_key
        elif self.target_app:
            found_pids = self._find_target_pids()
            for p in found_pids:
                pid_to_app[p] = self.target_app.lower()

        # Tìm PIDs cho tất cả các app bị chặn
        all_blocked_pids = set()
        for b_name in blocked_names:
            all_blocked_pids.update(self._find_pids_for_name(b_name))

        with self._lock:
            self.target_pids = found_pids
            self.pid_to_throttle_app = pid_to_app
            self.blocked_pids = all_blocked_pids

        now = time.time()
        new_ports = set()

        # Quét cổng trực tiếp theo PID cho target (đang bị bóp băng thông)
        for pid in found_pids:
            try:
                proc = psutil.Process(pid)
                app_k = pid_to_app.get(pid, self.target_app.lower())
                for conn in proc.net_connections(kind='all'):
                    if conn.laddr:
                        port = conn.laddr.port
                        new_ports.add(port)
                        with self._lock:
                            self._port_last_seen[port] = now
                            if app_k:
                                self.port_to_throttle_app[port] = app_k
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Quét cổng cho tất cả các PID bị chặn hoàn toàn (blocked_pids)
        new_blocked_ports = set()
        for pid in all_blocked_pids:
            try:
                proc = psutil.Process(pid)
                for conn in proc.net_connections(kind='all'):
                    if conn.laddr:
                        new_blocked_ports.add(conn.laddr.port)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        with self._lock:
            # Giữ lại các cổng đã thấy trong vòng 120 giây
            expired_ports = [p for p, t in self._port_last_seen.items() if now - t > 120.0]
            for p in expired_ports:
                del self._port_last_seen[p]
                self.port_to_throttle_app.pop(p, None)

            self.target_ports = set(self._port_last_seen.keys())
            self.blocked_ports = new_blocked_ports

    def _port_monitor_loop(self, stop_event: Optional[threading.Event] = None):
        """Luồng chạy ngầm quét cổng mới mở của tiến trình mục tiêu tần số cao (50ms)."""
        ev = stop_event or self._stop_event
        while not ev.is_set():
            try:
                self._update_target_ports()
            except Exception:
                pass
            ev.wait(0.05)

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

    def _throttle_worker_loop(self, stop_event: Optional[threading.Event] = None):
        """
        Luồng Token Bucket: Lấy các gói tin của tiến trình mục tiêu từ hàng đợi,
        tính toán độ trễ cần thiết để giữ tốc độ dưới mức trần riêng cho từng app, rồi inject lại vào mạng.
        """
        ev = stop_event or self._stop_event
        fallback_last_refill = time.perf_counter()
        fallback_tokens = 0.0

        while not ev.is_set():
            try:
                item = self._packet_queue.get(timeout=0.05)
                if item is None:
                    break
            except queue.Empty:
                self._update_speed_stats(0, False)
                continue

            if len(item) == 4:
                packet, packet_size, is_inbound, app_key = item
            else:
                packet, packet_size, is_inbound = item
                app_key = ""

            handle = self._divert_handle
            if not handle or not handle.is_open:
                break

            with self._lock:
                app_cfg = self.throttled_apps.get(app_key) if app_key else None
                limit_val = app_cfg["limit_kbps"] if app_cfg else self.limit_kbps

            rate_bytes_per_sec = max(1024.0, limit_val * 1024.0)
            # Dung lượng burst tối đa 80ms hoặc 2KB để không bị nghẽn giật
            burst_capacity = max(2048.0, rate_bytes_per_sec * 0.08)

            now = time.perf_counter()
            if app_cfg is not None:
                elapsed = now - app_cfg.get("last_refill", now)
                tokens = min(burst_capacity, app_cfg.get("tokens", 0.0) + elapsed * rate_bytes_per_sec)
                app_cfg["last_refill"] = now
                if tokens < packet_size:
                    needed = packet_size - tokens
                    sleep_sec = needed / rate_bytes_per_sec
                    if sleep_sec > 0.0002:
                        time.sleep(sleep_sec)
                    app_cfg["tokens"] = 0.0
                    app_cfg["last_refill"] = time.perf_counter()
                else:
                    app_cfg["tokens"] = tokens - packet_size
            else:
                elapsed = now - fallback_last_refill
                fallback_tokens = min(burst_capacity, fallback_tokens + elapsed * rate_bytes_per_sec)
                fallback_last_refill = now
                if fallback_tokens < packet_size:
                    needed = packet_size - fallback_tokens
                    sleep_sec = needed / rate_bytes_per_sec
                    if sleep_sec > 0.0002:
                        time.sleep(sleep_sec)
                    fallback_tokens = 0.0
                    fallback_last_refill = time.perf_counter()
                else:
                    fallback_tokens -= packet_size

            # Bơm gói tin trở lại ngăn xếp mạng
            try:
                handle.send(packet, recalculate_checksum=False)
                self._update_speed_stats(packet_size, is_inbound)
            except Exception:
                pass

    def _capture_loop(self, stop_event: Optional[threading.Event] = None):
        """
        Luồng chính bắt gói tin tầng mạng:
        - Bắt các gói tin TCP/UDP không phải loopback.
        - Phát hiện cổng socket mới tức thì ngay trên gói tin outbound đầu tiên.
        - Gói tin của các app khác: Gửi đi NGAY LẬP TỨC (Zero latency pass-through).
        - Gói tin của tiến trình mục tiêu: Đưa vào hàng đợi Token Bucket để bóp đúng trần.
        """
        ev = stop_event or self._stop_event
        filter_str = "!loopback and (tcp or udp)"
        w = None
        try:
            with pydivert.WinDivert(filter=filter_str, priority=0) as w:
                self._divert_handle = w
                print(f"[PacketShaper] Đã kích hoạt WinDivert thành công cho '{self.target_app or 'Blocked Apps'}' (Trần: {self.limit_kbps} KB/s)")

                while not ev.is_set():
                    try:
                        packet = w.recv()
                        if not packet:
                            continue
                    except Exception:
                        if ev.is_set():
                            break
                        continue

                    try:
                        is_target = False
                        is_drop = False
                        matched_app_key = ""
                        with self._lock:
                            ports = set(self.target_ports)
                            pids = set(self.target_pids)
                            blocked_ports = set(self.blocked_ports)
                            blocked_pids = set(self.blocked_pids)
                            has_blocked = bool(self.blocked_apps)
                            port_map = dict(self.port_to_throttle_app)
                            pid_map = dict(self.pid_to_throttle_app)

                        if packet.is_outbound:
                            if packet.src_port in blocked_ports:
                                is_drop = True
                            elif packet.src_port in ports:
                                matched_app_key = port_map.get(packet.src_port, "")
                                app_dir = self.throttled_apps.get(matched_app_key, {}).get("direction", self.direction)
                                if app_dir in ("upload", "both"):
                                    is_target = True
                            else:
                                # Nhận diện tức thì qua PID của target hoặc blocked app
                                all_watched = pids | blocked_pids
                                if all_watched:
                                    try:
                                        for pid in all_watched:
                                            try:
                                                for conn in psutil.Process(pid).net_connections():
                                                    if conn.laddr and conn.laddr.port == packet.src_port:
                                                        with self._lock:
                                                            if pid in blocked_pids:
                                                                self.blocked_ports.add(packet.src_port)
                                                                is_drop = True
                                                            if pid in pids:
                                                                matched_app_key = pid_map.get(pid, self.target_app.lower())
                                                                self.target_ports.add(packet.src_port)
                                                                self._port_last_seen[packet.src_port] = time.time()
                                                                if matched_app_key:
                                                                    self.port_to_throttle_app[packet.src_port] = matched_app_key
                                                                app_dir = self.throttled_apps.get(matched_app_key, {}).get("direction", self.direction)
                                                                if app_dir in ("upload", "both"):
                                                                    is_target = True
                                                        break
                                                if is_target or is_drop:
                                                    break
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                        elif packet.is_inbound:
                            if packet.dst_port in blocked_ports:
                                is_drop = True
                            elif packet.dst_port in ports:
                                matched_app_key = port_map.get(packet.dst_port, "")
                                app_dir = self.throttled_apps.get(matched_app_key, {}).get("direction", self.direction)
                                if app_dir in ("download", "both"):
                                    is_target = True

                        # 1. Kiểm tra nếu gói tin thuộc về ứng dụng bị CHẶN HOÀN TOÀN (BLOCK)
                        if not is_drop and has_blocked and is_target and self.is_blocked and not self.throttled_apps and self.limit_kbps <= 0:
                            is_drop = True

                        if is_drop:
                            # DROP GÓI TIN NGAY TẠI KERNEL DRIVER! KHÔNG GỬI LẠI!
                            p_len = len(packet.raw) if packet.raw else 64
                            self._update_speed_stats(p_len, packet.is_inbound)
                            continue

                        # 2. Nếu là ứng dụng chỉ bị BÓP BĂNG THÔNG (THROTTLE)
                        if is_target and (self.limit_kbps > 0 or bool(self.throttled_apps)):
                            packet_len = len(packet.raw) if packet.raw else 64
                            try:
                                self._packet_queue.put_nowait((packet, packet_len, packet.is_inbound, matched_app_key))
                            except queue.Full:
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
            if not ev.is_set():
                print(f"[PacketShaper Error] WinDivert capture dừng: {e}")
        finally:
            if self._divert_handle is w:
                self._divert_handle = None

    def _ensure_engine_running(self, need_throttle: bool = False):
        """Khởi động hoặc cập nhật các luồng WinDivert an toàn, tránh tạo luồng trùng lặp."""
        with self._engine_lock:
            if not self.is_active or (self._capture_thread and not self._capture_thread.is_alive()):
                # Đảm bảo phiên cũ (nếu có) đã được đóng sạch trước khi mở handle mới
                self._stop_engine_threads()
                self.is_active = True
                stop_ev = threading.Event()
                self._stop_event = stop_ev
                self._update_target_ports()

                self._port_monitor_thread = threading.Thread(
                    target=self._port_monitor_loop, args=(stop_ev,), daemon=True, name="PacketShaper-PortMonitor"
                )
                self._port_monitor_thread.start()

                if need_throttle:
                    self._throttle_worker_thread = threading.Thread(
                        target=self._throttle_worker_loop, args=(stop_ev,), daemon=True, name="PacketShaper-TokenBucket"
                    )
                    self._throttle_worker_thread.start()

                self._capture_thread = threading.Thread(
                    target=self._capture_loop, args=(stop_ev,), daemon=True, name="PacketShaper-Capture"
                )
                self._capture_thread.start()
            else:
                if need_throttle and (not self._throttle_worker_thread or not self._throttle_worker_thread.is_alive()):
                    stop_ev = self._stop_event
                    self._throttle_worker_thread = threading.Thread(
                        target=self._throttle_worker_loop, args=(stop_ev,), daemon=True, name="PacketShaper-TokenBucket"
                    )
                    self._throttle_worker_thread.start()
                self._update_target_ports()

    def _stop_engine_threads(self):
        """Đóng handle WinDivert và chờ luồng bắt gói tin dừng hẳn."""
        with self._engine_lock:
            self.is_active = False
            old_ev = self._stop_event
            old_ev.set()

            handle = self._divert_handle
            self._divert_handle = None
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

            while not self._packet_queue.empty():
                try:
                    self._packet_queue.get_nowait()
                except Exception:
                    break

            cap_t = self._capture_thread
            if cap_t and cap_t.is_alive() and cap_t is not threading.current_thread():
                try:
                    cap_t.join(timeout=0.25)
                except Exception:
                    pass
            self._capture_thread = None
            self._throttle_worker_thread = None
            self._port_monitor_thread = None

    @staticmethod
    def _apply_windows_firewall_rule(app_name: str, exe_path: str = "", action: str = "block") -> bool:
        """Thêm quy tắc vào Windows Defender Firewall để chặn/cho phép ứng dụng."""
        rule_name = f"PBL4_Block_{app_name}"
        if not exe_path or not os.path.exists(exe_path):
            target_lower = app_name.lower()
            for p in psutil.process_iter(['name', 'exe']):
                try:
                    if (p.info.get('name') or "").lower() == target_lower:
                        exe_path = p.info.get('exe', '')
                        if exe_path and os.path.exists(exe_path):
                            break
                except Exception:
                    pass

        if not exe_path or not os.path.exists(exe_path):
            print(f"[Firewall Rule Warning] Chưa có đường dẫn .exe tuyệt đối của '{app_name}', WinDivert Kernel Dropper sẽ đảm nhiệm chặn toàn bộ gói tin.")
            return False

        prog_param = f'program="{exe_path}"'
        cmd_out = f'netsh advfirewall firewall add rule name="{rule_name}_Out" dir=out action={action} {prog_param} enable=yes'
        cmd_in = f'netsh advfirewall firewall add rule name="{rule_name}_In" dir=in action={action} {prog_param} enable=yes'
        try:
            r1 = subprocess.run(cmd_out, shell=True, capture_output=True, text=True, timeout=3)
            r2 = subprocess.run(cmd_in, shell=True, capture_output=True, text=True, timeout=3)
            return r1.returncode == 0 and r2.returncode == 0
        except Exception as e:
            print(f"[Firewall Rule Warning] {e}")
            return False

    @staticmethod
    def _remove_windows_firewall_rule(app_name: str) -> bool:
        """Xóa quy tắc chặn của ứng dụng khỏi Windows Defender Firewall."""
        rule_name = f"PBL4_Block_{app_name}"
        try:
            subprocess.run(f'netsh advfirewall firewall delete rule name="{rule_name}_Out"', shell=True, capture_output=True, text=True, timeout=3)
            subprocess.run(f'netsh advfirewall firewall delete rule name="{rule_name}_In"', shell=True, capture_output=True, text=True, timeout=3)
            return True
        except Exception:
            return False

    @staticmethod
    def _terminate_app_sockets(app_pids: Set[int]):
        """Cắt đứt ngay lập tức các kết nối TCP hiện có của ứng dụng bằng Windows SetTcpEntry."""
        try:
            import ctypes
            from ctypes import wintypes
            class MIB_TCPROW(ctypes.Structure):
                _fields_ = [
                    ("dwState", wintypes.DWORD),
                    ("dwLocalAddr", wintypes.DWORD),
                    ("dwLocalPort", wintypes.DWORD),
                    ("dwRemoteAddr", wintypes.DWORD),
                    ("dwRemotePort", wintypes.DWORD),
                ]
            MIB_TCP_STATE_DELETE_TCB = 12
            for pid in app_pids:
                try:
                    for conn in psutil.Process(pid).net_connections(kind='tcp'):
                        if conn.status == 'ESTABLISHED' and conn.laddr and conn.raddr:
                            row = MIB_TCPROW()
                            row.dwState = MIB_TCP_STATE_DELETE_TCB
                            try:
                                import socket
                                row.dwLocalAddr = int.from_bytes(socket.inet_aton(conn.laddr.ip), 'little')
                                row.dwLocalPort = socket.htons(conn.laddr.port)
                                row.dwRemoteAddr = int.from_bytes(socket.inet_aton(conn.raddr.ip), 'little')
                                row.dwRemotePort = socket.htons(conn.raddr.port)
                                ctypes.windll.iphlpapi.SetTcpEntry(ctypes.byref(row))
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

    def block_app(self, app_name: str, direction: str = "both", exe_path: str = "") -> Tuple[bool, str]:
        """
        Chặn hoàn toàn quyền truy cập mạng của ứng dụng:
        1. Kiểm tra quyền Administrator (bắt buộc cho Windows Firewall & WinDivert).
        2. Thêm quy tắc Windows Defender Firewall (Inbound & Outbound) cho file .exe.
        3. Kích hoạt WinDivert Kernel Dropper: Hủy bỏ (Drop) ngay lập tức mọi gói tin gửi/nhận.
        4. Cắt đứt các kết nối socket TCP đang mở.
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
        target = canonical_name
        target_key = target.lower()

        # 1. Thêm vào danh sách chặn
        with self._lock:
            self.blocked_apps[target_key] = {"direction": direction.lower(), "exe": real_exe}
            self.blocked_pids.update(pids)
            self.is_blocked = True

        # 2. Cắt các socket đang kết nối
        self._terminate_app_sockets(pids)

        # 3. Kích hoạt Windows Defender Firewall (với exe thực tế)
        fw_ok = self._apply_windows_firewall_rule(target, real_exe, action="block")

        # 4. Kích hoạt luồng WinDivert để drop các gói tin tại driver
        self._ensure_engine_running(need_throttle=(self.limit_kbps > 0))

        msg = f"Đã kích hoạt chặn hoàn toàn truy cập mạng cho ứng dụng '{target}' ({len(pids)} tiến trình, Firewall: {'Bật' if fw_ok else 'WinDivert'})."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def unblock_app(self, app_name: str) -> Tuple[bool, str]:
        """Gỡ bỏ chặn cho một ứng dụng cụ thể."""
        canonical_name, _, _ = self._resolve_target_name_and_exe(app_name)
        raw_lower = app_name.lower().strip()
        target = canonical_name or (app_name if raw_lower.endswith(".exe") else f"{app_name}.exe")
        candidate_keys = {
            raw_lower,
            f"{raw_lower}.exe" if not raw_lower.endswith(".exe") else raw_lower[:-4],
            target.lower(),
        }

        removed_names = {target}
        with self._lock:
            for k in list(self.blocked_apps.keys()):
                if k in candidate_keys or k.replace(".exe", "") in candidate_keys:
                    removed_names.add(k)
                    del self.blocked_apps[k]

        # Xóa rule Windows Firewall
        for r_name in removed_names:
            self._remove_windows_firewall_rule(r_name)

        should_stop = False
        with self._lock:
            if not self.blocked_apps:
                self.is_blocked = False
                self.blocked_pids.clear()
                self.blocked_ports.clear()
                if self.limit_kbps <= 0:
                    should_stop = True

        if should_stop:
            self._stop_engine_threads()
        else:
            self._update_target_ports()

        msg = f"Đã gỡ chặn mạng cho ứng dụng '{target}' thành công."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def unblock_all(self) -> Tuple[bool, str]:
        """Gỡ bỏ chặn cho tất cả các ứng dụng."""
        with self._lock:
            all_targets = list(self.blocked_apps.keys())
            self.blocked_apps.clear()
            self.blocked_ports.clear()
            self.blocked_pids.clear()
            self.is_blocked = False
            should_stop = (self.limit_kbps <= 0)

        for target in all_targets:
            self._remove_windows_firewall_rule(target)

        if should_stop:
            self._stop_engine_threads()
        else:
            self._update_target_ports()

        msg = "Đã gỡ bỏ toàn bộ chính sách chặn mạng cho tất cả ứng dụng."
        print(f"[PacketShaper] {msg}")
        return True, msg

    # Các hàm tương thích để hỗ trợ gọi linh hoạt
    def start(self, target_app: str, limit_kbps: float = 0.0, direction: str = "both") -> Tuple[bool, str]:
        """Tương thích gọi start(): tự động chuyển thành block_app hoặc apply_limit."""
        if limit_kbps <= 1.0:
            return self.block_app(target_app, direction=direction)
        else:
            return self.apply_limit(target_app, limit_kbps=limit_kbps, direction=direction)

    def stop(self) -> Tuple[bool, str]:
        """Tương thích gọi stop(): gỡ toàn bộ chặn và bóp băng thông."""
        self.unblock_all()
        return self.remove_limit()

    def apply_limit(self, app_name: str, limit_kbps: float, direction: str = "both") -> Tuple[bool, str]:
        """
        Kích hoạt bóp băng thông cho tiến trình mục tiêu (hỗ trợ nhiều ứng dụng đồng thời).
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

        canonical_name, _, _ = self._resolve_target_name_and_exe(app_name)
        target = canonical_name or (app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe")
        target_key = target.lower()
        dir_norm = direction.lower()

        with self._lock:
            self.throttled_apps[target_key] = {
                "target_app": target,
                "limit_kbps": limit_kbps,
                "direction": dir_norm,
                "tokens": 0.0,
                "last_refill": time.perf_counter(),
            }
            self.target_app = target
            self.limit_kbps = limit_kbps
            self.direction = dir_norm
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

        self._ensure_engine_running(need_throttle=True)

        dir_str = "Upload & Download" if dir_norm == "both" else ("Upload" if dir_norm == "upload" else "Download")
        msg = f"Đã kích hoạt bóp băng thông WinDivert cho '{target}' thành công: Giới hạn {dir_str} {limit_kbps:,.1f} KB/s."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def remove_limit(self, app_name: Optional[str] = None) -> Tuple[bool, str]:
        """Dừng can thiệp giới hạn băng thông cho 1 ứng dụng hoặc toàn bộ (giữ nguyên WinDivert nếu vẫn còn ứng dụng khác)."""
        with self._lock:
            had_limit = bool(self.target_app or self.limit_kbps > 0 or self.throttled_apps)
            if app_name and app_name.strip():
                raw_lower = app_name.lower().strip()
                canonical_name, _, _ = self._resolve_target_name_and_exe(app_name)
                candidate_keys = {
                    raw_lower,
                    f"{raw_lower}.exe" if not raw_lower.endswith(".exe") else raw_lower[:-4],
                    canonical_name.lower() if canonical_name else "",
                }
                removed_app = app_name
                for k in list(self.throttled_apps.keys()):
                    if k in candidate_keys or k.replace(".exe", "") in candidate_keys:
                        removed_app = self.throttled_apps[k].get("target_app", k)
                        del self.throttled_apps[k]
                # Xóa các port ánh xạ tới ứng dụng vừa gỡ
                for p, a_key in list(self.port_to_throttle_app.items()):
                    if a_key in candidate_keys or a_key.replace(".exe", "") in candidate_keys:
                        self.port_to_throttle_app.pop(p, None)
                        self._port_last_seen.pop(p, None)
                app = removed_app
            else:
                app = self.target_app or "tất cả ứng dụng"
                self.throttled_apps.clear()
                self.port_to_throttle_app.clear()
                self.pid_to_throttle_app.clear()
                self._port_last_seen.clear()

            if self.throttled_apps:
                # Vẫn còn ứng dụng khác đang bị giới hạn băng thông
                last_k = next(reversed(self.throttled_apps))
                last_cfg = self.throttled_apps[last_k]
                self.target_app = last_cfg["target_app"]
                self.limit_kbps = last_cfg["limit_kbps"]
                self.direction = last_cfg["direction"]
            else:
                self.target_app = ""
                self.target_pids.clear()
                self.target_ports.clear()
                self.limit_kbps = 0.0
                self.current_speed_kbps = 0.0
                self.current_down_kbps = 0.0
                self.current_up_kbps = 0.0

            has_remaining_work = bool(self.blocked_apps or self.throttled_apps)

        if not self.is_active and not had_limit:
            return True, "Không có chính sách bóp băng thông nào đang chạy."

        if not has_remaining_work:
            self._stop_engine_threads()
        else:
            if not self.throttled_apps:
                while not self._packet_queue.empty():
                    try:
                        self._packet_queue.get_nowait()
                    except Exception:
                        break
            self._update_target_ports()

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
                "throttled_apps": {k: dict(v) for k, v in self.throttled_apps.items()},
                "target_ports_count": len(self.target_ports),
                "throttled_packets": self.total_throttled_packets,
                "throttled_bytes": self.total_throttled_bytes,
                "total_down_bytes": self.total_down_bytes,
                "total_up_bytes": self.total_up_bytes,
                "current_speed_kbps": self.current_speed_kbps,
                "current_down_kbps": self.current_down_kbps,
                "current_up_kbps": self.current_up_kbps,
            }
