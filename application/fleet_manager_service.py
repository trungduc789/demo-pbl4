"""
Dịch vụ Quản lý Đội ngũ Máy con (Fleet Manager Service - Application Layer):
- Quản lý tập trung toàn bộ danh sách máy con và trạng thái thời gian thực (Online / Offline).
- Duy trì bộ đệm Telemetry và lịch sử 60 điểm cho đồ thị Admin (Zero Latency Display).
- Điều phối hàng đợi ghi CSDL bất đồng bộ vào MySQL (demo.sql).
- Cung cấp API truy vấn tổng quan và gửi lệnh can thiệp từ xa (Firewall & Bandwidth Throttling).
"""

import time
import queue
import threading
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
from infrastructure.database_manager import DatabaseManager
from utils.safe_print import safe_print as print


@dataclass
class FleetDeviceState:
    """Trạng thái thời gian thực và lịch sử lưu lượng của một máy con trong hệ thống."""
    device_uuid: str
    db_id: Optional[int] = None
    registration: Optional[DeviceRegistrationData] = None
    status: str = "offline"  # "online" hoặc "offline"
    last_seen: float = field(default_factory=time.time)
    ip_address: str = ""
    latest_telemetry: Optional[TelemetryPacket] = None
    download_history: deque = field(default_factory=lambda: deque(maxlen=MAX_DATA_POINTS))
    upload_history: deque = field(default_factory=lambda: deque(maxlen=MAX_DATA_POINTS))
    current_down_kbps: float = 0.0
    current_up_kbps: float = 0.0
    applied_policies: Dict[str, Dict] = field(default_factory=dict)  # app_name -> policy_info

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
        db_manager: Optional[DatabaseManager] = None,
        enable_db: bool = True,
    ):
        self.host = host
        self.port = port
        self.enable_db = enable_db

        # 1. Khởi tạo tầng hạ tầng mạng và CSDL
        self.server = NetworkServer(host=self.host, port=self.port)
        self.db = db_manager or (DatabaseManager() if self.enable_db else None)

        # 2. Bộ nhớ đệm danh bạ máy con
        self._devices: Dict[str, FleetDeviceState] = {}
        self._lock = threading.RLock()

        # 3. Hàng đợi ghi CSDL bất đồng bộ
        self._db_queue = queue.Queue(maxsize=300)
        self._db_running = True
        self._db_worker: Optional[threading.Thread] = None

        # 4. Các Callbacks phát tín hiệu cho Giao diện Admin (UI Notifications)
        self.on_fleet_updated: Optional[Callable[[], None]] = None
        self.on_telemetry_updated: Optional[Callable[[str, TelemetryPacket], None]] = None
        self.on_command_result: Optional[Callable[[CommandResultPacket], None]] = None

        # 5. Đăng ký callbacks từ NetworkServer
        self.server.on_device_registered = self._on_server_device_registered
        self.server.on_telemetry_received = self._on_server_telemetry_received
        self.server.on_command_result = self._on_server_command_result
        self.server.on_device_disconnected = self._on_server_device_disconnected

        # Nạp danh sách thiết bị có sẵn từ CSDL nếu có
        self._preload_devices_from_db()

    def _preload_devices_from_db(self):
        """Tải trước danh sách các máy đã từng đăng ký trong CSDL vào bộ đệm trạng thái offline."""
        if not self.db:
            return
        try:
            records = self.db.get_all_devices()
            with self._lock:
                for r in records:
                    uuid_val = r.get("device_uuid")
                    if uuid_val and uuid_val not in self._devices:
                        reg = DeviceRegistrationData(
                            device_uuid=uuid_val,
                            hostname=r.get("hostname", "Unknown"),
                            os_name=r.get("os", "Unknown"),
                            os_version=r.get("os_version", ""),
                            agent_version=r.get("agent_version", "1.0.0"),
                        )
                        state = FleetDeviceState(
                            device_uuid=uuid_val,
                            db_id=r.get("id"),
                            registration=reg,
                            status="offline",
                            last_seen=time.time(),
                        )
                        self._devices[uuid_val] = state
        except Exception as e:
            print(f"[FleetService Warning] Lỗi nạp thiết bị từ DB: {e}")

    def start(self) -> bool:
        """Khởi chạy máy chủ quản trị và worker luồng ghi CSDL."""
        if self.enable_db:
            self._db_running = True
            self._db_worker = threading.Thread(
                target=self._db_worker_loop, daemon=True, name="FleetService-DBWorker"
            )
            self._db_worker.start()

        ok = self.server.start()
        if ok:
            print("[FleetService] Fleet Manager Service đã khởi động hoàn tất.")
        return ok

    def stop(self):
        """Dừng toàn bộ dịch vụ và đóng kết nối an toàn."""
        self.server.stop()
        self._db_running = False
        try:
            self._db_queue.put_nowait(None)
        except Exception:
            pass

        if self.db:
            try:
                self.db.close()
            except Exception:
                pass
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

        # Đẩy tác vụ lưu thông tin máy vào CSDL bất đồng bộ
        if self.enable_db:
            try:
                self._db_queue.put_nowait(("device_register", reg_data))
            except queue.Full:
                pass

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
            state.latest_telemetry = telemetry
            state.current_down_kbps = down_speed
            state.current_up_kbps = up_speed
            state.download_history.append((ts, down_speed))
            state.upload_history.append((ts, up_speed))

        # Đẩy vào hàng đợi ghi CSDL bất đồng bộ
        if self.enable_db:
            try:
                self._db_queue.put_nowait(("telemetry", (device_uuid, telemetry)))
            except queue.Full:
                pass

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
            if state and result.status == "SUCCESS":
                pass

        # Ghi log can thiệp vào CSDL bất đồng bộ
        if self.enable_db:
            try:
                self._db_queue.put_nowait(("command_result", result))
            except queue.Full:
                pass

        if self.on_command_result:
            try:
                self.on_command_result(result)
            except Exception as e:
                print(f"[FleetService Callback Error on_command_result] {e}")

    def _on_server_device_disconnected(self, device_uuid: str):
        """Xử lý khi một máy con bị ngắt kết nối."""
        with self._lock:
            state = self._devices.get(device_uuid)
            if state:
                state.status = "offline"
                state.current_down_kbps = 0.0
                state.current_up_kbps = 0.0

        if self.enable_db:
            try:
                self._db_queue.put_nowait(("device_disconnect", device_uuid))
            except queue.Full:
                pass

        if self.on_fleet_updated:
            try:
                self.on_fleet_updated()
            except Exception as e:
                print(f"[FleetService Callback Error on_fleet_updated] {e}")

    # =========================================================================
    # HÀNG ĐỢI GHI CSDL BẤT ĐỒNG BỘ (ASYNC DB WORKER)
    # =========================================================================

    def _db_worker_loop(self):
        """Luồng ngầm rút dữ liệu từ hàng đợi để lưu vào MySQL không gây block giao tiếp."""
        while self._db_running:
            try:
                item = self._db_queue.get(timeout=0.5)
                if item is None:
                    break
                task_type, payload = item

                if self.db:
                    if task_type == "device_register":
                        reg_data: DeviceRegistrationData = payload
                        dev_id = self.db.upsert_remote_device(reg_data)
                        if dev_id:
                            with self._lock:
                                if reg_data.device_uuid in self._devices:
                                    self._devices[reg_data.device_uuid].db_id = dev_id

                    elif task_type == "telemetry":
                        uuid_val, telem = payload
                        dev_id = None
                        with self._lock:
                            st = self._devices.get(uuid_val)
                            dev_id = st.db_id if st else None
                        if dev_id:
                            self.db.save_remote_telemetry(dev_id, telem)

                    elif task_type == "command_result":
                        res: CommandResultPacket = payload
                        dev_id = None
                        with self._lock:
                            st = self._devices.get(res.device_uuid)
                            dev_id = st.db_id if st else None
                        if dev_id:
                            self.db.save_remote_policy_log(
                                device_id=dev_id,
                                action="REMOTE_EXEC",
                                target_app="",
                                status=res.status,
                                details=f"CMD ID {res.command_id}: {res.message}",
                            )

                    elif task_type == "device_disconnect":
                        uuid_val = payload
                        self.db.set_device_status(uuid_val, "offline")

                self._db_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[FleetService DB Worker Error] {e}")

    # =========================================================================
    # CÁC API TRUY VẤN VÀ ĐIỀU KHIỂN CHO GIAO DIỆN ADMIN (PHASE 4)
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
            # Ưu tiên các máy online xếp lên trước, sau đó sắp xếp theo hostname
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
        """
        Admin gửi lệnh can thiệp xuống máy con cụ thể:
        - action: BLOCK_APP, UNBLOCK_APP, UNBLOCK_ALL, THROTTLE_SET, REMOVE_LIMIT, PING
        - target_app: Ví dụ 'chrome.exe', 'brave.exe'
        - limit_kbps: Hạn mức băng thông (áp dụng cho THROTTLE_SET)
        - direction: 'both', 'upload', 'download'
        """
        with self._lock:
            state = self._devices.get(device_uuid)
            if not state:
                return False, f"Không tìm thấy máy con có UUID '{device_uuid}'"
            if state.status != "online":
                return False, f"Máy con '{state.hostname}' hiện đang Offline, không thể nhận lệnh."

        cmd_id = f"CMD-{int(time.time() * 1000)}"
        cmd = CommandPacket(
            command_id=cmd_id,
            target_device_uuid=device_uuid,
            action=action,
            target_app=target_app,
            limit_kbps=limit_kbps,
            direction=direction,
        )

        ok = self.server.send_command(device_uuid, cmd)
        if ok:
            # Cập nhật tạm thời chính sách trên bộ nhớ
            with self._lock:
                if state:
                    if action in (CommandAction.BLOCK_APP.value, CommandAction.THROTTLE_SET.value):
                        state.applied_policies[target_app.lower()] = {
                            "action": action,
                            "limit_kbps": limit_kbps,
                            "direction": direction,
                            "applied_at": time.time(),
                        }
                    elif action == CommandAction.UNBLOCK_APP.value:
                        state.applied_policies.pop(target_app.lower(), None)
                    elif action == CommandAction.UNBLOCK_ALL.value:
                        state.applied_policies.clear()
                    elif action == CommandAction.REMOVE_LIMIT.value:
                        if target_app:
                            state.applied_policies.pop(target_app.lower(), None)
                        else:
                            state.applied_policies.clear()

            return True, f"Đã gửi lệnh '{action}' thành công tới '{state.hostname}' (ID: {cmd_id})"
        else:
            return False, f"Gửi lệnh tới máy '{state.hostname}' thất bại qua kênh truyền thông."

    def get_policy_logs(self, limit: int = 50) -> List[Dict]:
        """Truy vấn nhật ký chính sách an ninh mạng từ CSDL."""
        if self.db:
            return self.db.get_policy_logs(limit=limit)
        return []
