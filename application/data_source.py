"""
Mẫu thiết kế Strategy cho Nguồn Dữ Liệu & Điều Khiển Chính Sách (DashboardDataSource - Application Layer):
- Tách biệt hoàn toàn giao diện (Presentation) khỏi việc phân nhánh `is_admin_mode` / `is_client_mode` / `local`.
- Đóng gói luồng Worker gửi lệnh bất đồng bộ (`AsyncCommandDispatcher`) ở tầng Application,
  giúp tầng Presentation không phải tự quản lý `threading.Thread` hay `queue.Queue`.
"""

import queue
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from infrastructure.network_collector import NetworkCollector
from utils.file_logger import FileLogger, LOGS_DIR
from utils.safe_print import safe_print as print


class AsyncCommandDispatcher:
    """Bộ điều phối thực thi lệnh chính sách trên luồng nền (Background Worker Queue) tại tầng Application."""

    def __init__(self, worker_name: str = "App-PolicyCommandWorker"):
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._worker = threading.Thread(target=self._loop, daemon=True, name=worker_name)
        self._worker.start()

    def _loop(self):
        while True:
            task = self._queue.get()
            if task is None:
                break
            try:
                task()
            except Exception as e:
                print(f"[CommandDispatcher Error] {e}")
            finally:
                self._queue.task_done()

    def submit(self, fn: Callable[[], None]):
        """Đưa một tác vụ thực thi chính sách vào hàng đợi nền."""
        self._queue.put(fn)

    def join(self):
        """Đợi toàn bộ tác vụ trong hàng đợi nền hoàn tất."""
        self._queue.join()


class DashboardDataSource(ABC):
    """Giao diện Strategy trừu tượng cung cấp dữ liệu và thực thi chính sách cho UI."""

    def __init__(self, dispatcher: Optional[AsyncCommandDispatcher] = None):
        self.dispatcher = dispatcher or AsyncCommandDispatcher()

    @property
    @abstractmethod
    def mode_name(self) -> str:
        """Tên chế độ: 'local', 'client', hoặc 'remote'."""

    @property
    def has_fleet_management(self) -> bool:
        return False

    @abstractmethod
    def get_connections(self) -> List[Dict]:
        """Lấy danh sách kết nối TCP đã làm giàu tên tiến trình."""

    @abstractmethod
    def get_raw_tcp_connections(self) -> List[Any]:
        """Lấy danh sách đối tượng kết nối TCP thô."""

    @abstractmethod
    def resolve_executable_path(self, pid: int) -> str:
        """Phân giải đường dẫn file .exe từ PID."""

    def get_policy_history(self, limit: int = 200) -> List[Dict]:
        return FileLogger.read_policy_logs(limit)

    def get_connection_history_text(self, limit: int = 200) -> str:
        return FileLogger.read_connection_logs(limit)

    def get_logs_directory(self) -> str:
        return LOGS_DIR

    def open_logs_folder(self) -> bool:
        return NetworkCollector.open_folder_in_os(self.get_logs_directory())

    def is_locked_by_admin(self, app_name: str) -> bool:
        return False

    @abstractmethod
    def apply_app_policy(
        self,
        app_name: str,
        effective_status: str,
        prev_effective: str,
        direction: str,
        limit_kbps: float,
        is_firewall_active: bool = True,
    ):
        """Thực thi chặn / bóp băng thông / gỡ bỏ cho một ứng dụng."""

    @abstractmethod
    def unblock_all_policies(self):
        """Gỡ bỏ toàn bộ chính sách chặn và giới hạn tốc độ."""

    @abstractmethod
    def set_firewall_enabled(
        self,
        enabled: bool,
        app_policies: Dict[str, Dict],
        default_throttle_kbps: float,
    ):
        """Bật hoặc tắt tổng thể tường lửa cục bộ."""


class LocalMonitorDataSource(DashboardDataSource):
    """Chiến lược nguồn dữ liệu khi chạy độc lập trên máy cục bộ (Standalone Mode)."""

    def __init__(self, monitor_service: Any, dispatcher: Optional[AsyncCommandDispatcher] = None):
        super().__init__(dispatcher=dispatcher)
        self.service = monitor_service

    @property
    def mode_name(self) -> str:
        return "local"

    def get_connections(self) -> List[Dict]:
        if self.service and hasattr(self.service, "get_connections"):
            return self.service.get_connections()
        return []

    def get_raw_tcp_connections(self) -> List[Any]:
        if self.service and hasattr(self.service, "get_raw_tcp_connections"):
            return self.service.get_raw_tcp_connections()
        return []

    def resolve_executable_path(self, pid: int) -> str:
        if self.service and hasattr(self.service, "resolve_executable_path"):
            return self.service.resolve_executable_path(pid)
        return ""

    def apply_app_policy(
        self,
        app_name: str,
        effective_status: str,
        prev_effective: str,
        direction: str,
        limit_kbps: float,
        is_firewall_active: bool = True,
    ):
        if not is_firewall_active or not self.service:
            return

        def _task():
            if effective_status == "blocked":
                if prev_effective == "throttled":
                    self.service.remove_bandwidth_limit(app_name)
                _ok, msg = self.service.block_application(app_name, direction=direction)
                print(f"[Protect] BLOCK {app_name} ({direction}): {msg}")
            elif effective_status == "throttled":
                if prev_effective == "blocked":
                    self.service.unblock_application(app_name)
                _ok, msg = self.service.apply_bandwidth_limit(app_name, limit_kbps=limit_kbps, direction=direction)
                print(f"[Protect] THROTTLE {app_name} ({direction}, {limit_kbps} KB/s): {msg}")
            else:
                self.service.unblock_application(app_name)
                if prev_effective == "throttled":
                    self.service.remove_bandwidth_limit(app_name)
                print(f"[Protect] ALLOW {app_name}: Đã gỡ bỏ mọi hạn chế mạng")

        self.dispatcher.submit(_task)

    def unblock_all_policies(self):
        if not self.service:
            return

        def _task():
            self.service.unblock_all_applications()
            self.service.remove_bandwidth_limit()
            print("[Protect] Đã gỡ bỏ TẤT CẢ chính sách chặn và giới hạn băng thông")

        self.dispatcher.submit(_task)

    def set_firewall_enabled(
        self,
        enabled: bool,
        app_policies: Dict[str, Dict],
        default_throttle_kbps: float,
    ):
        if not self.service:
            return
        if not enabled:
            self.unblock_all_policies()
            return

        def _reapply():
            for app_name, policy in list(app_policies.items()):
                in_st = policy.get("in", "allowed")
                out_st = policy.get("out", "allowed")
                if in_st == "allowed" and out_st == "allowed":
                    continue
                eff_st = "blocked" if ("blocked" in (in_st, out_st)) else "throttled"
                in_act = in_st in ("blocked", "throttled")
                out_act = out_st in ("blocked", "throttled")
                eff_dir = "both" if (in_act and out_act) else ("download" if in_act else ("upload" if out_act else "both"))
                if eff_st == "blocked":
                    self.service.block_application(app_name, direction=eff_dir)
                else:
                    lim = policy.get("limit_kbps", default_throttle_kbps)
                    self.service.apply_bandwidth_limit(app_name, lim, eff_dir)

        self.dispatcher.submit(_reapply)


class ClientAgentDataSource(LocalMonitorDataSource):
    """Chiến lược nguồn dữ liệu cho máy con có cơ chế bảo vệ 2 lớp (2-Tier Policy: Admin Lock + Local Self-Protect)."""

    def __init__(self, monitor_service: Any, agent_service: Any, dispatcher: Optional[AsyncCommandDispatcher] = None):
        super().__init__(monitor_service=monitor_service, dispatcher=dispatcher)
        self.agent_service = agent_service

    @property
    def mode_name(self) -> str:
        return "client"

    def is_locked_by_admin(self, app_name: str) -> bool:
        if self.agent_service and hasattr(self.agent_service, "is_locked_by_admin"):
            return bool(self.agent_service.is_locked_by_admin(app_name))
        return False

    def apply_app_policy(
        self,
        app_name: str,
        effective_status: str,
        prev_effective: str,
        direction: str,
        limit_kbps: float,
        is_firewall_active: bool = True,
    ):
        if not is_firewall_active or not self.agent_service:
            return

        def _task():
            if effective_status == "blocked":
                if prev_effective == "throttled":
                    self.agent_service.remove_local_policy(app_name)
                self.agent_service.apply_local_policy(app_name, action="BLOCK", limit_kbps=0.0, direction=direction)
            elif effective_status == "throttled":
                if prev_effective == "blocked":
                    self.agent_service.remove_local_policy(app_name)
                self.agent_service.apply_local_policy(
                    app_name, action="THROTTLE", limit_kbps=limit_kbps, direction=direction
                )
            else:
                self.agent_service.remove_local_policy(app_name)

        self.dispatcher.submit(_task)

    def unblock_all_policies(self):
        if not self.agent_service:
            return
        self.dispatcher.submit(lambda: self.agent_service.clear_all_local_policies())

    def set_firewall_enabled(
        self,
        enabled: bool,
        app_policies: Dict[str, Dict],
        default_throttle_kbps: float,
    ):
        if not self.agent_service:
            return
        if not enabled:
            self.unblock_all_policies()
            return

        def _reapply():
            for app_name, policy in list(app_policies.items()):
                if policy.get("locked_by_admin"):
                    continue
                in_st = policy.get("in", "allowed")
                out_st = policy.get("out", "allowed")
                if in_st == "allowed" and out_st == "allowed":
                    continue
                eff_st = "blocked" if ("blocked" in (in_st, out_st)) else "throttled"
                in_act = in_st in ("blocked", "throttled")
                out_act = out_st in ("blocked", "throttled")
                eff_dir = "both" if (in_act and out_act) else ("download" if in_act else ("upload" if out_act else "both"))
                if eff_st == "blocked":
                    self.agent_service.apply_local_policy(app_name, action="BLOCK", limit_kbps=0.0, direction=eff_dir)
                else:
                    lim = policy.get("limit_kbps", default_throttle_kbps)
                    self.agent_service.apply_local_policy(app_name, action="THROTTLE", limit_kbps=lim, direction=eff_dir)

        self.dispatcher.submit(_reapply)


class RemoteFleetDataSource(DashboardDataSource):
    """Chiến lược nguồn dữ liệu cho Admin Server điều khiển máy trạm từ xa (Fleet Manager Mode)."""

    def __init__(
        self,
        fleet_service: Any,
        device_uuid: Optional[str] = None,
        commander_func: Optional[Callable] = None,
        dispatcher: Optional[AsyncCommandDispatcher] = None,
    ):
        super().__init__(dispatcher=dispatcher)
        self.fleet_service = fleet_service
        self.device_uuid = device_uuid
        self.commander = commander_func or (
            getattr(fleet_service, "send_remote_command", None) if fleet_service else None
        )

    @property
    def mode_name(self) -> str:
        return "remote"

    @property
    def has_fleet_management(self) -> bool:
        return self.fleet_service is not None

    def set_target_device(self, device_uuid: Optional[str], commander_func: Optional[Callable] = None):
        self.device_uuid = device_uuid
        if commander_func is not None:
            self.commander = commander_func
        elif self.fleet_service:
            self.commander = getattr(self.fleet_service, "send_remote_command", None)

    def get_connections(self) -> List[Dict]:
        if self.fleet_service and hasattr(self.fleet_service, "get_connections"):
            return self.fleet_service.get_connections()
        return []

    def get_raw_tcp_connections(self) -> List[Any]:
        if self.fleet_service and hasattr(self.fleet_service, "get_raw_tcp_connections"):
            return self.fleet_service.get_raw_tcp_connections()
        return []

    def resolve_executable_path(self, pid: int) -> str:
        if self.fleet_service and hasattr(self.fleet_service, "resolve_executable_path"):
            return self.fleet_service.resolve_executable_path(pid)
        return ""

    def apply_app_policy(
        self,
        app_name: str,
        effective_status: str,
        prev_effective: str,
        direction: str,
        limit_kbps: float,
        is_firewall_active: bool = True,
    ):
        uid = self.device_uuid
        commander = self.commander
        if not uid or not commander:
            return

        def _task():
            if effective_status == "blocked":
                if prev_effective == "throttled":
                    commander(uid, "REMOVE_LIMIT", app_name, 0.0, direction)
                commander(uid, "BLOCK_APP", app_name, 0.0, direction)
            elif effective_status == "throttled":
                if prev_effective == "blocked":
                    commander(uid, "UNBLOCK_APP", app_name, 0.0, direction)
                commander(uid, "THROTTLE_SET", app_name, limit_kbps, direction)
            else:
                commander(uid, "UNBLOCK_APP", app_name, 0.0, direction)
                if prev_effective == "throttled":
                    commander(uid, "REMOVE_LIMIT", app_name, 0.0, direction)

        self.dispatcher.submit(_task)

    def unblock_all_policies(self):
        uid = self.device_uuid
        commander = self.commander
        if not uid or not commander:
            return

        def _task():
            commander(uid, "UNBLOCK_ALL", "", 0.0, "both")
            commander(uid, "REMOVE_LIMIT", "", 0.0, "both")
            print(f"[Protect Remote] UNBLOCK_ALL gửi tới {uid}")

        self.dispatcher.submit(_task)

    def set_firewall_enabled(
        self,
        enabled: bool,
        app_policies: Dict[str, Dict],
        default_throttle_kbps: float,
    ):
        if not enabled:
            self.unblock_all_policies()


def create_data_source(
    monitor_service: Any = None,
    fleet_service: Any = None,
    agent_service: Any = None,
    dispatcher: Optional[AsyncCommandDispatcher] = None,
) -> DashboardDataSource:
    """Factory khởi tạo Strategy phù hợp theo dịch vụ đầu vào."""
    if fleet_service is not None:
        return RemoteFleetDataSource(fleet_service=fleet_service, dispatcher=dispatcher)
    if agent_service is not None:
        return ClientAgentDataSource(
            monitor_service=monitor_service,
            agent_service=agent_service,
            dispatcher=dispatcher,
        )
    return LocalMonitorDataSource(monitor_service=monitor_service, dispatcher=dispatcher)
