"""
Dịch vụ Quản lý Đội ngũ Máy con (Fleet Manager Service - Application Layer):
- Quản lý tập trung toàn bộ danh sách máy con và trạng thái thời gian thực (Online / Offline).
- Duy trì bộ đệm Telemetry và lịch sử 60 điểm cho đồ thị Admin (Zero Latency Display).
- Cung cấp API truy vấn tổng quan và gửi lệnh can thiệp từ xa (Firewall & Bandwidth Throttling).
"""

import time
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any, Callable

from config.settings import (
    SERVER_DEFAULT_HOST,
    SERVER_DEFAULT_PORT,
    MAX_DATA_POINTS,
)
from models.network_protocol import (
    CommandAction,
    DeviceRegistrationData,
    TelemetryPacket,
    CommandPacket,
    CommandResultPacket,
)
from infrastructure.network_server import NetworkServer
from infrastructure.network_collector import NetworkCollector
from application.process_aggregator import (
    resolve_executable_path_cached,
    enrich_tcp_connections,
)
from utils.file_logger import FileLogger, LOGS_DIR
from utils.safe_print import safe_print as print


@dataclass
class FleetDeviceState:
    """Trạng thái thời gian thực và lịch sử lưu lượng của một máy con trong hệ thống."""
    device_uuid: str
    registration: Optional[DeviceRegistrationData] = None
    status: str = "offline"
    last_seen: float = field(default_factory=time.time)
    offline_at: float = 0.0
    ip_address: str = ""
    latest_telemetry: Optional[TelemetryPacket] = None
    download_history: deque = field(default_factory=lambda: deque(maxlen=MAX_DATA_POINTS))
    upload_history: deque = field(default_factory=lambda: deque(maxlen=MAX_DATA_POINTS))
    current_down_kbps: float = 0.0
    current_up_kbps: float = 0.0
    applied_policies: Dict[str, Dict] = field(default_factory=dict)

    @property
    def hostname(self) -> str:
        return self.registration.hostname if self.registration else "Unknown"

    @property
    def os_info(self) -> str:
        if not self.registration:
            return "Unknown"
        return f"{self.registration.os_name} {self.registration.os_version}".strip()


class FleetManagerService:
    """Dịch vụ trung tâm chạy tại Server điều phối toàn bộ kết nối và dữ liệu máy con."""

    def __init__(
        self,
        host: str = SERVER_DEFAULT_HOST,
        port: int = SERVER_DEFAULT_PORT,
    ):
        self.host = host
        self.port = port

        self.server = NetworkServer(host=self.host, port=self.port)
        self.collector = NetworkCollector()
        self.proc_info_cache: Dict[int, Tuple[str, str]] = {}

        self._devices: Dict[str, FleetDeviceState] = {}
        self._pending_commands: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        self.on_fleet_updated: Optional[Callable[[], None]] = None
        self.on_telemetry_updated: Optional[Callable[[str, TelemetryPacket], None]] = None
        self.on_command_result: Optional[Callable[[CommandResultPacket], None]] = None

        self.server.on_device_registered = self._on_server_device_registered
        self.server.on_telemetry_received = self._on_server_telemetry_received
        self.server.on_command_result = self._on_server_command_result
        self.server.on_device_disconnected = self._on_server_device_disconnected

    def start(self) -> bool:
        """Khởi chạy máy chủ quản trị."""
        ok = self.server.start()
        if ok:
            print("[FleetService] Fleet Manager Service đã khởi động hoàn tất.")
        return ok

    def stop(self):
        """Dừng toàn bộ dịch vụ và đóng kết nối an toàn."""
        self.server.stop()
        print("[FleetService] Fleet Manager Service đã dừng.")

    # =========================================================================
    # SỰ KIỆN TỪ NETWORK SERVER
    # =========================================================================

    def _on_server_device_registered(self, reg_data: DeviceRegistrationData):
        """Xử lý khi có máy con đăng ký kết nối."""
        uuid_val = reg_data.device_uuid
        with self._lock:
            state = self._devices.get(uuid_val)
            if not state:
                state = FleetDeviceState(device_uuid=uuid_val)
                self._devices[uuid_val] = state

            state.registration = reg_data
            state.ip_address = reg_data.ip_address
            state.status = "online"
            state.last_seen = time.time()
            state.offline_at = 0.0

        if self.on_fleet_updated:
            try:
                self.on_fleet_updated()
            except Exception as e:
                print(f"[FleetService Callback Error on_fleet_updated] {e}")

    def _on_server_telemetry_received(self, device_uuid: str, telemetry: TelemetryPacket):
        """Xử lý khi nhận gói tin số liệu đo đạc thời gian thực từ máy con."""
        down_speed = telemetry.speeds.download_kbps
        up_speed = telemetry.speeds.upload_kbps
        ts = telemetry.timestamp

        with self._lock:
            state = self._devices.get(device_uuid)
            if not state:
                state = FleetDeviceState(device_uuid=device_uuid)
                self._devices[device_uuid] = state

            state.status = "online"
            state.last_seen = time.time()
            state.offline_at = 0.0
            state.latest_telemetry = telemetry
            state.current_down_kbps = down_speed
            state.current_up_kbps = up_speed
            state.download_history.append((ts, down_speed))
            state.upload_history.append((ts, up_speed))

        if self.on_telemetry_updated:
            try:
                self.on_telemetry_updated(device_uuid, telemetry)
            except Exception as e:
                print(f"[FleetService Callback Error on_telemetry_updated] {e}")

    def _on_server_command_result(self, result: CommandResultPacket):
        """Xử lý khi nhận phản hồi kết quả thực thi lệnh can thiệp từ máy con."""
        uuid_val = result.device_uuid
        with self._lock:
            state = self._devices.get(uuid_val)
            cmd_meta = self._pending_commands.pop(result.command_id, None)
            if state and cmd_meta:
                app_key = (cmd_meta.get("target_app") or "").lower()
                if result.status == "SUCCESS":
                    if app_key and app_key in state.applied_policies:
                        state.applied_policies[app_key]["status"] = "SUCCESS"
                else:
                    prev_pol = cmd_meta.get("prev_policy")
                    if prev_pol is not None and app_key:
                        state.applied_policies[app_key] = prev_pol
                    elif app_key and app_key in state.applied_policies:
                        state.applied_policies[app_key]["status"] = "FAILED"
                        state.applied_policies[app_key]["error"] = result.message

        if self.on_command_result:
            try:
                self.on_command_result(result)
            except Exception as e:
                print(f"[FleetService Callback Error on_command_result] {e}")

    def _on_server_device_disconnected(self, device_uuid: str):
        """Xử lý khi một máy con bị ngắt kết nối."""
        now_ts = time.time()
        with self._lock:
            state = self._devices.get(device_uuid)
            if state:
                state.status = "offline"
                state.offline_at = now_ts
                state.current_down_kbps = 0.0
                state.current_up_kbps = 0.0
                last_ts = state.download_history[-1][0] if state.download_history else now_ts
                drop_ts = max(last_ts + 1.0, now_ts)
                if not state.download_history or state.download_history[-1][1] != 0.0:
                    state.download_history.append((drop_ts, 0.0))
                if not state.upload_history or state.upload_history[-1][1] != 0.0:
                    state.upload_history.append((drop_ts, 0.0))

        if self.on_fleet_updated:
            try:
                self.on_fleet_updated()
            except Exception as e:
                print(f"[FleetService Callback Error on_fleet_updated] {e}")

    # =========================================================================
    # CÁC API TRUY VẤN VÀ ĐIỀU KHIỂN CHO GIAO DIỆN ADMIN
    # =========================================================================

    def get_fleet_summary(self) -> Dict[str, Any]:
        """Tổng hợp thông số toàn bộ hệ thống phục vụ hiển thị thẻ HUD trên Admin UI."""
        with self._lock:
            total = len(self._devices)
            online = sum(1 for d in self._devices.values() if d.status == "online")
            offline = total - online
            total_down = sum(d.current_down_kbps for d in self._devices.values() if d.status == "online")
            total_up = sum(d.current_up_kbps for d in self._devices.values() if d.status == "online")

            return {
                "total_devices": total,
                "online_devices": online,
                "offline_devices": offline,
                "total_down_kbps": round(total_down, 2),
                "total_up_kbps": round(total_up, 2),
            }

    def get_all_device_states(self) -> List[FleetDeviceState]:
        """Lấy danh sách tất cả các máy con đang quản lý."""
        with self._lock:
            return sorted(
                list(self._devices.values()),
                key=lambda d: (0 if d.status == "online" else 1, d.hostname.lower()),
            )

    def get_device_state(self, device_uuid: str) -> Optional[FleetDeviceState]:
        """Lấy thông tin chi tiết của một máy con cụ thể."""
        with self._lock:
            return self._devices.get(device_uuid)

    def send_remote_command(
        self,
        device_uuid: str,
        action: str,
        target_app: str = "",
        limit_kbps: float = 0.0,
        direction: str = "both",
    ) -> Tuple[bool, str]:
        """Admin gửi lệnh can thiệp xuống máy con cụ thể."""
        with self._lock:
            state = self._devices.get(device_uuid)
            if not state:
                return False, f"Không tìm thấy máy con có UUID '{device_uuid}'"
            if state.status != "online":
                return False, f"Máy con '{state.hostname}' hiện đang Offline, không thể nhận lệnh."

        cmd_id = f"CMD-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        cmd = CommandPacket(
            command_id=cmd_id,
            target_device_uuid=device_uuid,
            action=action,
            target_app=target_app,
            limit_kbps=limit_kbps,
            direction=direction,
        )

        ok = self.server.send_command(device_uuid, cmd)
        if not ok:
            return False, f"Gửi lệnh tới máy '{state.hostname}' thất bại qua kênh truyền thông."

        with self._lock:
            if state:
                app_key = target_app.lower()
                prev_pol = dict(state.applied_policies[app_key]) if app_key in state.applied_policies else None
                self._pending_commands[cmd_id] = {
                    "device_uuid": device_uuid,
                    "action": action,
                    "target_app": target_app,
                    "prev_policy": prev_pol,
                }
                if action in (CommandAction.BLOCK_APP.value, CommandAction.THROTTLE_SET.value):
                    state.applied_policies[app_key] = {
                        "action": action,
                        "limit_kbps": limit_kbps,
                        "direction": direction,
                        "applied_at": time.time(),
                        "status": "PENDING",
                    }
                elif action == CommandAction.UNBLOCK_APP.value:
                    state.applied_policies.pop(app_key, None)
                elif action == CommandAction.UNBLOCK_ALL.value:
                    for k in list(state.applied_policies.keys()):
                        if state.applied_policies[k].get("action") == CommandAction.BLOCK_APP.value:
                            del state.applied_policies[k]
                elif action == CommandAction.REMOVE_LIMIT.value:
                    if target_app:
                        state.applied_policies.pop(app_key, None)
                    else:
                        for k in list(state.applied_policies.keys()):
                            if state.applied_policies[k].get("action") == CommandAction.THROTTLE_SET.value:
                                del state.applied_policies[k]

        return True, f"Đã gửi lệnh '{action}' thành công tới '{state.hostname}' (ID: {cmd_id})"

    def get_raw_tcp_connections(self) -> List[Any]:
        """Lấy danh sách đối tượng kết nối TCP thô từ tầng hạ tầng."""
        return self.collector.collect_tcp_connections()

    def resolve_executable_path(self, pid: int) -> str:
        """Phân giải đường dẫn file .exe từ PID qua NetworkCollector."""
        return resolve_executable_path_cached(self.collector, self.proc_info_cache, pid)

    def get_connections(self) -> List[Dict]:
        """Lấy danh sách các kết nối socket TCP kèm thông tin tiến trình."""
        return enrich_tcp_connections(self.collector, self.proc_info_cache, log_to_file=False)

    def get_policy_history(self, limit: int = 200) -> List[Dict]:
        """Đọc lịch sử chính sách an ninh mạng từ FileLogger."""
        return FileLogger.read_policy_logs(limit)

    def get_connection_history_text(self, limit: int = 200) -> str:
        """Đọc nội dung nhật ký kết nối mạng từ FileLogger."""
        return FileLogger.read_connection_logs(limit)

    @staticmethod
    def get_logs_directory() -> str:
        """Trả về đường dẫn thư mục lưu trữ nhật ký."""
        return LOGS_DIR
