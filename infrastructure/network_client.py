"""
Bộ truyền thông mạng tại máy con (Client Network Transport - TCP):
- Quản lý kết nối socket bền vững đến máy chủ Admin.
- Tự động duy trì nhịp tim (Heartbeat / Keepalive).
- Cơ chế tự động kết nối lại (Auto-Reconnect) thông minh khi rớt mạng hoặc Server khởi động lại.
- Gửi Telemetry thời gian thực.
- Nhận lệnh điều khiển (Firewall, QoS) từ Admin và tự động phản hồi kết quả thực thi.
"""

import socket
import threading
import time
from typing import Callable, Optional, Any

from config.settings import (
    SERVER_DEFAULT_HOST,
    SERVER_DEFAULT_PORT,
    CLIENT_HEARTBEAT_INTERVAL,
    CLIENT_RECONNECT_DELAY,
)
from models.network_protocol import (
    MessageType,
    DeviceRegistrationData,
    TelemetryPacket,
    CommandPacket,
    CommandResultPacket,
    ProtocolMessage,
)
from infrastructure.protocol_framing import send_message, recv_message
from utils.safe_print import safe_print as print


class NetworkClient:
    """Quản lý kết nối mạng của Agent máy con tới máy chủ quản trị trung tâm."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = SERVER_DEFAULT_PORT,
        reg_data: Optional[DeviceRegistrationData] = None,
        heartbeat_interval: float = CLIENT_HEARTBEAT_INTERVAL,
        reconnect_delay: float = CLIENT_RECONNECT_DELAY,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.reg_data = reg_data
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay

        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._is_running = threading.Event()
        self._is_connected = threading.Event()

        # Callback khi nhận lệnh can thiệp từ Server
        # handler(command: CommandPacket) -> Optional[CommandResultPacket]
        self.on_command_received: Optional[
            Callable[[CommandPacket], Optional[CommandResultPacket]]
        ] = None

        # Callbacks thông báo trạng thái kết nối
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

        self._manager_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    def is_connected(self) -> bool:
        """Kiểm tra máy con hiện có kết nối trực tiếp đến Server hay không."""
        return self._is_connected.is_set()

    def start(self):
        """Bắt đầu luồng quản lý kết nối nền của máy con."""
        if self._is_running.is_set():
            return
        self._is_running.set()
        self._manager_thread = threading.Thread(
            target=self._connection_manager_loop, daemon=True, name="NetClient-Manager"
        )
        self._manager_thread.start()
        print(f"[NetClient] Đã kích hoạt Agent Network Client hướng tới {self.server_host}:{self.server_port}")

    def stop(self):
        """Dừng Agent Client và đóng kết nối an toàn."""
        self._is_running.clear()
        self._is_connected.clear()

        # Gửi thông báo DISCONNECT chủ động nếu socket còn sống
        with self._sock_lock:
            if self._sock:
                try:
                    disc_msg = ProtocolMessage(
                        msg_type=MessageType.DISCONNECT.value,
                        payload={"device_uuid": self.reg_data.device_uuid if self.reg_data else ""},
                    )
                    send_message(self._sock, disc_msg)
                except Exception:
                    pass
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

        print("[NetClient] Agent Network Client đã dừng.")

    def _connection_manager_loop(self):
        """Vòng lặp giám sát kết nối và tự động kết nối lại khi rớt mạng."""
        while self._is_running.is_set():
            try:
                # 1. Thử tạo kết nối TCP mới
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(5.0)
                s.connect((self.server_host, self.server_port))
                s.settimeout(None)  # Trả về chế độ blocking bình thường

                with self._sock_lock:
                    self._sock = s

                # 2. Gửi gói tin REGISTER để nhận diện máy
                if self.reg_data:
                    reg_msg = ProtocolMessage(
                        msg_type=MessageType.REGISTER.value,
                        payload=self.reg_data.to_dict(),
                        timestamp=time.time(),
                    )
                    if not send_message(s, reg_msg):
                        raise OSError("Không thể gửi gói tin đăng ký REGISTER")

                self._is_connected.set()
                print(f"[NetClient] Kết nối thành công tới Server {self.server_host}:{self.server_port}!")
                if self.on_connected:
                    try:
                        self.on_connected()
                    except Exception as e:
                        print(f"[NetClient Callback Error on_connected] {e}")

                # 3. Khởi động luồng Heartbeat riêng cho phiên kết nối này
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True, name="NetClient-Heartbeat"
                )
                self._heartbeat_thread.start()

                # 4. Luồng chính chờ nhận thông điệp (Commands) từ Server
                while self._is_running.is_set() and self._is_connected.is_set():
                    msg = recv_message(s)
                    if msg is None:
                        # Kết nối bị ngắt từ phía Server
                        break

                    if msg.msg_type == MessageType.COMMAND.value:
                        cmd = CommandPacket.from_dict(msg.payload)
                        print(f"[NetClient Command Recv] Nhận lệnh '{cmd.action}' cho app '{cmd.target_app}' (ID: {cmd.command_id})")
                        if self.on_command_received:
                            try:
                                res = self.on_command_received(cmd)
                                if res:
                                    self.send_command_result(res)
                            except Exception as e:
                                print(f"[NetClient Command Exec Error] {e}")
                                # Báo cáo lỗi về server
                                err_res = CommandResultPacket(
                                    command_id=cmd.command_id,
                                    device_uuid=self.reg_data.device_uuid if self.reg_data else "",
                                    status="FAILED",
                                    message=f"Lỗi thực thi lệnh: {e}",
                                    applied_at=time.time(),
                                )
                                self.send_command_result(err_res)

            except (socket.error, OSError) as e:
                pass
            except Exception as e:
                print(f"[NetClient Connection Warning] {e}")
            finally:
                self._cleanup_current_socket()

            # Chờ một khoảng thời gian trước khi thử kết nối lại
            if self._is_running.is_set():
                time.sleep(self.reconnect_delay)

    def _cleanup_current_socket(self):
        """Dọn dẹp socket hiện tại khi bị mất kết nối."""
        was_connected = self._is_connected.is_set()
        self._is_connected.clear()
        with self._sock_lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

        if was_connected:
            print("[NetClient] Mất kết nối tới máy chủ. Đang chờ kết nối lại...")
            if self.on_disconnected:
                try:
                    self.on_disconnected()
                except Exception as e:
                    print(f"[NetClient Callback Error on_disconnected] {e}")

    def _heartbeat_loop(self):
        """Gửi gói tin giữ kết nối Heartbeat theo chu kỳ."""
        while self._is_running.is_set() and self._is_connected.is_set():
            time.sleep(self.heartbeat_interval)
            if not self._is_connected.is_set():
                break

            hb_msg = ProtocolMessage(
                msg_type=MessageType.HEARTBEAT.value,
                payload={"device_uuid": self.reg_data.device_uuid if self.reg_data else ""},
                timestamp=time.time(),
            )
            with self._sock_lock:
                if self._sock and self._is_connected.is_set():
                    if not send_message(self._sock, hb_msg):
                        break

    def send_telemetry(self, telemetry: TelemetryPacket) -> bool:
        """Gửi gói tin số liệu đo đạc thời gian thực lên Server."""
        if not self._is_connected.is_set():
            return False

        msg = ProtocolMessage(
            msg_type=MessageType.TELEMETRY.value,
            payload=telemetry.to_dict(),
            timestamp=telemetry.timestamp,
        )
        with self._sock_lock:
            if not self._sock or not self._is_connected.is_set():
                return False
            return send_message(self._sock, msg)

    def send_command_result(self, result: CommandResultPacket) -> bool:
        """Gửi kết quả thực thi lệnh can thiệp lên Server."""
        if not self._is_connected.is_set():
            return False

        msg = ProtocolMessage(
            msg_type=MessageType.COMMAND_RESULT.value,
            payload=result.to_dict(),
            timestamp=result.applied_at,
        )
        with self._sock_lock:
            if not self._sock or not self._is_connected.is_set():
                return False
            ok = send_message(self._sock, msg)
            if ok:
                print(f"[NetClient Result Sent] Đã phản hồi kết quả lệnh {result.command_id}: {result.status}")
            return ok
