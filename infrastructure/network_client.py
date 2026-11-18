"""
Bộ truyền thông mạng tại máy con (Client Network Transport - TCP):
- Quản lý kết nối socket bền vững đến máy chủ Admin.
- Ủy quyền bắt tay xác thực 2 chiều Challenge-Response (PSK + HMAC-SHA256) cho `infrastructure.auth_handshake`.
- Xác thực toàn vẹn mọi gói tin bằng [Length][Seq][HMAC], khử trùng lặp lệnh theo command_id.
- Nhận xác nhận TELEMETRY_ACK, duy trì nhịp tim (Heartbeat) và tự động kết nối lại (Auto-Reconnect).
"""

from collections import deque
import socket
import threading
import time
from typing import Callable, Optional

from config.settings import (
    SERVER_DEFAULT_PORT,
    CLIENT_HEARTBEAT_INTERVAL,
    CLIENT_RECONNECT_DELAY,
    NETWORK_PSK,
    AUTH_TIMEOUT,
)
from models.network_protocol import (
    MessageType,
    DeviceRegistrationData,
    TelemetryPacket,
    CommandPacket,
    CommandResultPacket,
    ProtocolMessage,
)
from infrastructure.protocol_framing import (
    DIR_CLIENT_TO_SERVER,
    DIR_SERVER_TO_CLIENT,
    RecvTimeoutError,
    recv_authenticated_message,
    send_authenticated_message,
)
from infrastructure.auth_handshake import perform_client_handshake
from utils.safe_print import safe_print as print


class NetworkClient:
    """Quản lý kết nối mạng đã xác thực của Agent máy con tới máy chủ quản trị trung tâm."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = SERVER_DEFAULT_PORT,
        reg_data: Optional[DeviceRegistrationData] = None,
        heartbeat_interval: float = CLIENT_HEARTBEAT_INTERVAL,
        reconnect_delay: float = CLIENT_RECONNECT_DELAY,
        psk: str = NETWORK_PSK,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.reg_data = reg_data
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay
        self.psk_bytes = psk.encode("utf-8")

        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._is_running = threading.Event()
        self._is_connected = threading.Event()

        self._session_key: Optional[bytes] = None
        self._tx_seq: int = 1
        self._rx_seq: int = 1

        self._processed_command_ids: deque = deque(maxlen=256)

        self.last_telemetry_ack_ts: float = 0.0
        self.telemetry_ack_count: int = 0
        self.on_telemetry_ack: Optional[Callable[[float], None]] = None

        self.on_command_received: Optional[
            Callable[[CommandPacket], Optional[CommandResultPacket]]
        ] = None

        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

        self._manager_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    def is_connected(self) -> bool:
        """Kiểm tra máy con hiện có kết nối đã xác thực đến Server hay không."""
        return self._is_connected.is_set()

    def start(self):
        """Bắt đầu luồng quản lý kết nối nền của máy con."""
        if self._is_running.is_set():
            return
        if self.server_host in ("127.0.0.1", "localhost"):
            print(
                "[NetClient Notice] Đang trỏ tới 127.0.0.1 (chỉ dùng nội bộ máy này). "
                "Nếu Admin Server chạy trên máy khác trong LAN, hãy cập nhật IP LAN của Admin trong config/client_config.json."
            )
        self._is_running.set()
        self._manager_thread = threading.Thread(
            target=self._connection_manager_loop, daemon=True, name="NetClient-Manager"
        )
        self._manager_thread.start()
        print(f"[NetClient] Đã kích hoạt Agent Network Client hướng tới {self.server_host}:{self.server_port}")

    def stop(self):
        """Dừng Agent Client và đóng kết nối an toàn."""
        self._is_running.clear()

        with self._sock_lock:
            if self._sock and self._session_key and self._is_connected.is_set():
                try:
                    disc_msg = ProtocolMessage(
                        msg_type=MessageType.DISCONNECT.value,
                        payload={"device_uuid": self.reg_data.device_uuid if self.reg_data else ""},
                    )
                    send_authenticated_message(
                        self._sock,
                        disc_msg,
                        self._session_key,
                        DIR_CLIENT_TO_SERVER,
                        self._tx_seq,
                    )
                    self._tx_seq += 1
                except Exception:
                    pass
            self._is_connected.clear()
            self._session_key = None
            if self._sock:
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

    def _perform_handshake(self, sock: socket.socket) -> bool:
        """Thực hiện bắt tay Challenge-Response & xác thực 2 chiều với Admin Server."""
        result = perform_client_handshake(sock, self.reg_data, self.psk_bytes)
        if result is None:
            return False
        session_key, tx_seq, rx_seq = result
        with self._sock_lock:
            self._session_key = session_key
            self._tx_seq = tx_seq
            self._rx_seq = rx_seq
        return True

    def _connection_manager_loop(self):
        """Vòng lặp giám sát kết nối và tự động kết nối lại khi rớt mạng."""
        while self._is_running.is_set():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(AUTH_TIMEOUT)
                s.connect((self.server_host, self.server_port))

                with self._sock_lock:
                    self._sock = s

                if not self._perform_handshake(s):
                    raise OSError("Bắt tay xác thực PSK/HMAC thất bại")

                self._is_connected.set()
                print(f"[NetClient] Kết nối thành công tới Server {self.server_host}:{self.server_port}!")
                if self.on_connected:
                    try:
                        self.on_connected()
                    except Exception as e:
                        print(f"[NetClient Callback Error on_connected] {e}")

                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True, name="NetClient-Heartbeat"
                )
                self._heartbeat_thread.start()

                while self._is_running.is_set() and self._is_connected.is_set():
                    try:
                        msg = recv_authenticated_message(
                            s,
                            self._session_key,
                            DIR_SERVER_TO_CLIENT,
                            self._rx_seq,
                            timeout_is_fatal=False,
                        )
                    except RecvTimeoutError:
                        continue

                    if msg is None:
                        break

                    self._rx_seq += 1

                    if msg.msg_type == MessageType.COMMAND.value:
                        cmd = CommandPacket.from_dict(msg.payload)
                        if cmd.command_id and cmd.command_id in self._processed_command_ids:
                            print(f"[NetClient Security] Bỏ qua lệnh trùng lặp (ID: {cmd.command_id})")
                            continue
                        if cmd.command_id:
                            self._processed_command_ids.append(cmd.command_id)

                        print(f"[NetClient Command Recv] Nhận lệnh '{cmd.action}' cho app '{cmd.target_app}' (ID: {cmd.command_id})")
                        if self.on_command_received:
                            try:
                                res = self.on_command_received(cmd)
                                if res:
                                    self.send_command_result(res)
                            except Exception as e:
                                print(f"[NetClient Command Exec Error] {e}")
                                err_res = CommandResultPacket(
                                    command_id=cmd.command_id,
                                    device_uuid=self.reg_data.device_uuid if self.reg_data else "",
                                    status="FAILED",
                                    message=f"Lỗi thực thi lệnh: {e}",
                                    applied_at=time.time(),
                                )
                                self.send_command_result(err_res)

                    elif msg.msg_type == MessageType.TELEMETRY_ACK.value:
                        self.last_telemetry_ack_ts = float(msg.payload.get("timestamp", time.time()))
                        self.telemetry_ack_count += 1
                        if self.on_telemetry_ack:
                            try:
                                self.on_telemetry_ack(self.last_telemetry_ack_ts)
                            except Exception:
                                pass

            except (socket.error, OSError):
                pass
            except Exception as e:
                print(f"[NetClient Connection Warning] {e}")
            finally:
                self._cleanup_current_socket()

            if self._is_running.is_set():
                time.sleep(self.reconnect_delay)

    def _cleanup_current_socket(self):
        """Dọn dẹp socket hiện tại khi bị mất kết nối."""
        was_connected = self._is_connected.is_set()
        self._is_connected.clear()
        with self._sock_lock:
            self._session_key = None
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

    def _send_authed(self, msg: ProtocolMessage) -> bool:
        """Gửi thông điệp đã ký HMAC với sequence number tăng dần dưới socket lock."""
        with self._sock_lock:
            if not self._sock or not self._session_key or not self._is_connected.is_set():
                return False
            ok = send_authenticated_message(
                self._sock,
                msg,
                self._session_key,
                DIR_CLIENT_TO_SERVER,
                self._tx_seq,
            )
            if ok:
                self._tx_seq += 1
            return ok

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
            if not self._send_authed(hb_msg):
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
        return self._send_authed(msg)

    def send_command_result(self, result: CommandResultPacket) -> bool:
        """Gửi kết quả thực thi lệnh can thiệp lên Server."""
        if not self._is_connected.is_set():
            return False

        msg = ProtocolMessage(
            msg_type=MessageType.COMMAND_RESULT.value,
            payload=result.to_dict(),
            timestamp=result.applied_at,
        )
        return self._send_authed(msg)
