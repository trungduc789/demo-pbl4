"""
Bộ điều phối và bóp băng thông mạng thời gian thực dựa trên WinDivert (Kernel Packet Divert).
Hỗ trợ kiến trúc Đa chính sách cô lập (Per-App Isolated Policy):
- Điều phối chính sách Chặn (Block) và Bóp băng thông (Throttle) độc lập cho từng ứng dụng.
- Ủy quyền theo dõi ánh xạ cổng (TTL 5s) cho `infrastructure.port_tracker`.
- Ủy quyền luồng bắt gói Kernel và Token Bucket Worker cho `infrastructure.divert_engine`.
"""

from __future__ import annotations
import time
import queue
import threading
from typing import Dict, Optional, Set, Tuple, Any

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
    remove_all_pbl4_firewall_rules,
    terminate_app_sockets,
)
from infrastructure.port_tracker import (
    update_shaper_target_ports,
    run_port_monitor_loop,
)
from infrastructure.divert_engine import (
    run_app_throttle_worker,
    run_windivert_capture_loop,
)
from utils.safe_print import safe_print as print


class WinDivertPacketShaper:
    """
    Bộ điều phối chính sách mạng (Block & Bandwidth Throttling) dựa trên WinDivert:
    - Quản lý danh bạ ứng dụng bị chặn (`blocked_apps`) và giới hạn tốc độ (`throttled_apps`).
    - Quản lý vòng đời luồng Capture, Port Monitor và các Worker Token Bucket độc lập.
    """

    ALIAS_MAP = ALIAS_MAP

    def __init__(self):
        self._lock = threading.RLock()
        self._engine_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self.is_active: bool = False

        # Danh sách chính sách độc lập
        self.throttled_apps: Dict[str, AppThrottlePolicy] = {}  # app_key -> AppThrottlePolicy
        self.blocked_apps: Dict[str, AppBlockPolicy] = {}       # app_key -> AppBlockPolicy

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

        # Tự động dọn sạch các rule PBL4_Block_* bị mồ côi trong Windows Firewall từ phiên chạy trước
        if self.is_admin():
            try:
                remove_all_pbl4_firewall_rules()
            except Exception:
                pass

    @staticmethod
    def is_admin() -> bool:
        """Kiểm tra quyền Administrator (bắt buộc cho WinDivert driver & Windows Firewall)."""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def _resolve_target_name_and_exe(self, app_name: str, provided_exe: str = "") -> Tuple[str, str, Set[int]]:
        """Phân giải tên hiển thị thân thiện sang tên file .exe thật, đường dẫn thực thi và danh sách PID."""
        return resolve_target_name_and_exe(app_name, provided_exe, self.ALIAS_MAP)

    def _find_pids_for_name(self, app_name: str) -> Set[int]:
        _, _, pids = self._resolve_target_name_and_exe(app_name)
        return pids

    # =========================================================================
    # CÁC PROPERTY TỔNG HỢP & TƯƠNG THÍCH NGƯỢC
    # =========================================================================
    def _latest_throttle_attr(self, attr: str, default: Any) -> Any:
        with self._lock:
            if self.throttled_apps:
                return getattr(next(reversed(self.throttled_apps.values())), attr)
            return default

    def _sum_throttle_attr(self, attr: str) -> Any:
        with self._lock:
            return sum(getattr(pol, attr) for pol in self.throttled_apps.values())

    @property
    def is_blocked(self) -> bool:
        with self._lock:
            return bool(self.blocked_apps)

    @is_blocked.setter
    def is_blocked(self, val: bool):
        pass

    @property
    def target_app(self) -> str:
        return self._latest_throttle_attr("target_app", "")

    @target_app.setter
    def target_app(self, val: str):
        pass

    @property
    def limit_kbps(self) -> float:
        return self._latest_throttle_attr("limit_kbps", 0.0)

    @limit_kbps.setter
    def limit_kbps(self, val: float):
        pass

    @property
    def direction(self) -> str:
        return self._latest_throttle_attr("direction", "both")

    @direction.setter
    def direction(self, val: str):
        pass

    @property
    def current_speed_kbps(self) -> float:
        return self._sum_throttle_attr("current_speed_kbps")

    @property
    def current_down_kbps(self) -> float:
        return self._sum_throttle_attr("current_down_kbps")

    @property
    def current_up_kbps(self) -> float:
        return self._sum_throttle_attr("current_up_kbps")

    @property
    def total_throttled_bytes(self) -> int:
        return self._sum_throttle_attr("total_throttled_bytes")

    @property
    def total_throttled_packets(self) -> int:
        return self._sum_throttle_attr("total_throttled_packets")

    @property
    def total_down_bytes(self) -> int:
        return self._sum_throttle_attr("total_down_bytes")

    @property
    def total_up_bytes(self) -> int:
        return self._sum_throttle_attr("total_up_bytes")

    # =========================================================================
    # ỦY QUYỀN PORT TRACKING & CAPTURE / WORKER ENGINE
    # =========================================================================
    def _update_target_ports(self):
        update_shaper_target_ports(self, self._find_pids_for_name)

    def _port_monitor_loop(self, stop_event: threading.Event):
        run_port_monitor_loop(stop_event, self._update_target_ports)

    def _app_throttle_worker_loop(self, policy: AppThrottlePolicy):
        run_app_throttle_worker(self, policy)

    def _capture_loop(self, stop_event: threading.Event):
        run_windivert_capture_loop(self, stop_event, pydivert_mod=pydivert)

    # =========================================================================
    # QUẢN LÝ VÒNG ĐỜI LUỒNG & ENGINE
    # =========================================================================
    def _ensure_engine_running(self):
        """Khởi động Engine WinDivert nếu chưa chạy."""
        with self._engine_lock:
            if not self.is_active or (self._capture_thread and not self._capture_thread.is_alive()):
                self._stop_engine_threads(preserve_workers=True)
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

    def _stop_engine_threads(self, preserve_workers: bool = False):
        """Dừng an toàn toàn bộ luồng và giải phóng tài nguyên WinDivert."""
        with self._engine_lock:
            self.is_active = False
            self._stop_event.set()

            if not preserve_workers:
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

            for t, timeout in ((self._capture_thread, 1.0), (self._port_monitor_thread, 0.5)):
                if t and t.is_alive() and t is not threading.current_thread():
                    try:
                        t.join(timeout=timeout)
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
        """Chặn hoàn toàn quyền truy cập mạng của ứng dụng (Windows Firewall + Socket Reset + WinDivert Drop)."""
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

        policy = AppBlockPolicy(
            app_key=app_key,
            target_app=target,
            direction=dir_norm,
            exe_path=real_exe,
            pids=pids,
            firewall_rule_name=self._clean_rule_name(target),
        )

        with self._lock:
            self.blocked_apps[app_key] = policy
            self.blocked_pids.update(pids)

        self._terminate_app_sockets(pids)
        fw_ok = self._apply_windows_firewall_rule(target, real_exe, action="block")
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

        self._remove_windows_firewall_rule(target)
        if app_name.lower().strip() != target.lower().strip():
            self._remove_windows_firewall_rule(app_name)

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
        remove_all_pbl4_firewall_rules()

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
        """Kích hoạt bóp băng thông cho tiến trình mục tiêu với Token Bucket và Worker riêng."""
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

            removed_blocks = []
            for k in list(self.blocked_apps.keys()):
                if k == app_key or k.replace(".exe", "") == app_key.replace(".exe", ""):
                    removed_blocks.append(self.blocked_apps.pop(k))
            if removed_blocks:
                self.blocked_ports.clear()
                self.blocked_pids.clear()

            policy = AppThrottlePolicy(
                app_key=app_key,
                target_app=target,
                limit_kbps=limit_kbps,
                direction=dir_norm,
                tokens=0.0,
                last_refill=time.perf_counter(),
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

        self._remove_windows_firewall_rule(target)
        if app_name.lower().strip() != target.lower().strip():
            self._remove_windows_firewall_rule(app_name)

        self._ensure_engine_running()

        worker = threading.Thread(
            target=self._app_throttle_worker_loop,
            args=(policy,),
            daemon=True,
            name=f"PacketShaper-Worker-{target}",
        )
        policy.worker_thread = worker
        worker.start()

        dir_str = "Upload & Download" if dir_norm == "both" else ("Upload" if dir_norm == "upload" else "Download")
        msg = f"Đã kích hoạt bóp băng thông WinDivert cho '{target}' thành công: Giới hạn {dir_str} {limit_kbps:,.1f} KB/s."
        print(f"[PacketShaper] {msg}")
        return True, msg

    def remove_limit(self, app_name: Optional[str] = None) -> Tuple[bool, str]:
        """Dừng giới hạn băng thông cho 1 ứng dụng cụ thể hoặc toàn bộ."""
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
    # TRẠNG THÁI VÀ TƯƠNG THÍCH START / STOP
    # =========================================================================
    def get_status(self) -> Dict:
        """Lấy thông tin trạng thái bóp băng thông hiện hành (chi tiết từng ứng dụng và tổng hợp)."""
        with self._lock:
            throttled_dict = {}
            for k, pol in self.throttled_apps.items():
                pol.update_speed_stats(0, False)
                throttled_dict[k] = {
                    "target_app": pol.target_app,
                    "limit_kbps": pol.limit_kbps,
                    "direction": pol.direction,
                    "current_speed_kbps": pol.current_speed_kbps,
                    "current_down_kbps": pol.current_down_kbps,
                    "current_up_kbps": pol.current_up_kbps,
                    "throttled_packets": pol.total_throttled_packets,
                    "throttled_bytes": pol.total_throttled_bytes,
                }

            return {
                "is_active": self.is_active,
                "target_app": self.target_app,
                "limit_kbps": self.limit_kbps,
                "direction": self.direction,
                "throttled_apps": throttled_dict,
                "blocked_apps": [pol.target_app for pol in self.blocked_apps.values()],
                "target_ports_count": len(self.target_ports),
                "throttled_packets": self.total_throttled_packets,
                "throttled_bytes": self.total_throttled_bytes,
                "total_down_bytes": self.total_down_bytes,
                "total_up_bytes": self.total_up_bytes,
                "current_speed_kbps": self.current_speed_kbps,
                "current_down_kbps": self.current_down_kbps,
                "current_up_kbps": self.current_up_kbps,
            }

    def start(self, target_app: str, limit_kbps: float = 0.0, direction: str = "both") -> Tuple[bool, str]:
        """Tự động phân nhánh sang block_app hoặc apply_limit tùy theo hạn mức."""
        if limit_kbps <= 1.0:
            return self.block_app(target_app, direction=direction)
        return self.apply_limit(target_app, limit_kbps=limit_kbps, direction=direction)

    def stop(self) -> Tuple[bool, str]:
        """Gỡ bỏ toàn bộ chặn và bóp băng thông."""
        self.unblock_all()
        return self.remove_limit()
