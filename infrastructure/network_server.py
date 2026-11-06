"""
Máy chủ truyền thông mạng tập trung (Centralized Network Server - TCP):
- Quản lý đa kết nối từ các máy con (Multi-threaded TCP Socket Server).
- Quản lý danh bạ máy con theo device_uuid.
- Tiếp nhận Heartbeat & Telemetry thời gian thực.
- Đẩy lệnh điều khiển (Firewall, Giới hạn băng thông) tức thời xuống máy con.
- Cơ chế Watchdog phát hiện máy mất kết nối (Dead Man's Switch).
"""

import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Any

from config.settings import (
    SERVER_DEFAULT_HOST,
    SERVER_DEFAULT_PORT,
    SERVER_CLIENT_TIMEOUT,
)
from models.network_protocol import (
    MessageType,
    CommandAction,
    DeviceRegistrationData,
    TelemetryPacket,
    CommandPacket,
    CommandResultPacket,
    ProtocolMessage,
)
from infrastructure.protocol_framing import send_message, recv_message
from utils.safe_print import safe_print as print


class ClientConnectionHandler:
    """Xử lý phiên kết nối của một máy con cụ thể."""

    def __init__(self, sock: socket.socket, addr: tuple, server: "NetworkServer"):
        self.sock = sock
        self.addr = addr
        self.server = server
        self.device_uuid: str = ""
        self.reg_data: Optional[DeviceRegistrationData] = None
        self.last_seen: float = time.time()
        self.is_active: bool = True
        self._lock = threading.Lock()

    def send(self, msg: ProtocolMessage) -> bool:
        """Gửi thông điệp xuống máy con này."""
        with self._lock:
            if not self.is_active or self.sock is None:
                return False
            return send_message(self.sock, msg)

    def close(self):
        """Đóng kết nối máy con an toàn."""
        with self._lock:
            if not self.is_active:
                return
            self.is_active = False
            try:
                if self.sock:
                    self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None


class NetworkServer:
    """Máy chủ TCP quản lý kết nối và điều phối dữ liệu với toàn bộ máy con."""

    def __init__(self, host: str = SERVER_DEFAULT_HOST, port: int = SERVER_DEFAULT_PORT):
        self.host = host
        self.port = port
        self._server_sock: Optional[socket.socket] = None
        self._is_running = threading.Event()
        self._clients_lock = threading.RLock()
        self.clients: Dict[str, ClientConnectionHandler] = {}  # device_uuid -> Handler

        # Các Callback phục vụ tầng nghiệp vụ (Application Layer)
        self.on_device_registered: Optional[Callable[[DeviceRegistrationData], None]] = None
        self.on_telemetry_received: Optional[Callable[[str, TelemetryPacket], None]] = None
        self.on_command_result: Optional[Callable[[CommandResultPacket], None]] = None
        self.on_device_disconnected: Optional[Callable[[str], None]] = None

        self._accept_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Khởi động máy chủ TCP."""
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind((self.host, self.port))
            self._server_sock.listen(128)
            self._is_running.set()

            self._accept_thread = threading.Thread(
                target=self._accept_loop, daemon=True, name="NetServer-Accept"
            )
            self._accept_thread.start()

            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="NetServer-Watchdog"
            )
            self._watchdog_thread.start()

            print(f"[NetServer] Máy chủ quản trị đã khởi động thành công trên {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"[NetServer Error] Không thể khởi động máy chủ: {e}")
            self.stop()
            return False

    def stop(self):
        """Dừng máy chủ và giải phóng toàn bộ kết nối."""
        self._is_running.clear()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None

        with self._clients_lock:
            for uuid_val, handler in list(self.clients.items()):
                handler.close()
            self.clients.clear()

        print("[NetServer] Máy chủ quản trị đã dừng hoạt động.")

    def _accept_loop(self):
        """Vòng lặp tiếp nhận kết nối mới từ các máy con."""
        while self._is_running.is_set():
            try:
                if not self._server_sock:
                    break
                sock, addr = self._server_sock.accept()
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                handler = ClientConnectionHandler(sock, addr, self)

                client_t = threading.Thread(
                    target=self._handle_client_session,
                    args=(handler,),
                    daemon=True,
                    name=f"NetServer-Client-{addr[0]}:{addr[1]}",
                )
                client_t.start()
            except socket.error:
                if not self._is_running.is_set():
                    break
            except Exception as e:
                if self._is_running.is_set():
                    print(f"[NetServer Accept Warning] {e}")

    def _handle_client_session(self, handler: ClientConnectionHandler):
        """Xử lý luồng dữ liệu độc lập của một máy con."""
        registered_uuid = ""
        try:
            while self._is_running.is_set() and handler.is_active:
                msg = recv_message(handler.sock)
                if msg is None:
                    break

                handler.last_seen = time.time()

                if msg.msg_type == MessageType.REGISTER.value:
                    reg = DeviceRegistrationData.from_dict(msg.payload)
                    # Nếu chưa có IP thì tự gán theo địa chỉ socket của Client
                    if not reg.ip_address:
                        reg.ip_address = handler.addr[0]
                    handler.device_uuid = reg.device_uuid
                    handler.reg_data = reg
                    registered_uuid = reg.device_uuid

                    with self._clients_lock:
                        # Nếu máy này trước đó đã có kết nối cũ thì ngắt kết nối cũ
                        if registered_uuid in self.clients:
                            old = self.clients[registered_uuid]
                            if old != handler:
                                old.close()
                        self.clients[registered_uuid] = handler

                    print(f"[NetServer] Đã đăng ký máy con: '{reg.hostname}' ({registered_uuid}) từ {handler.addr[0]}")
                    if self.on_device_registered:
                        try:
                            self.on_device_registered(reg)
                        except Exception as e:
                            print(f"[NetServer Callback Error on_device_registered] {e}")

                elif msg.msg_type == MessageType.HEARTBEAT.value:
                    handler.last_seen = time.time()

                elif msg.msg_type == MessageType.TELEMETRY.value:
                    handler.last_seen = time.time()
                    if self.on_telemetry_received:
                        telem = TelemetryPacket.from_dict(msg.payload)
                        uuid_val = telem.device_uuid or registered_uuid
                        try:
                            self.on_telemetry_received(uuid_val, telem)
                        except Exception as e:
                            print(f"[NetServer Callback Error on_telemetry_received] {e}")

                elif msg.msg_type == MessageType.COMMAND_RESULT.value:
                    handler.last_seen = time.time()
                    if self.on_command_result:
                        res = CommandResultPacket.from_dict(msg.payload)
                        try:
                            self.on_command_result(res)
                        except Exception as e:
                            print(f"[NetServer Callback Error on_command_result] {e}")

                elif msg.msg_type == MessageType.DISCONNECT.value:
                    print(f"[NetServer] Máy con '{registered_uuid}' chủ động ngắt kết nối.")
                    break

        except Exception as e:
            if self._is_running.is_set():
                print(f"[NetServer Session Warning] Kết nối {handler.addr} dừng: {e}")
        finally:
            handler.close()
            if registered_uuid:
                with self._clients_lock:
                    if self.clients.get(registered_uuid) == handler:
                        del self.clients[registered_uuid]
                print(f"[NetServer] Máy con '{registered_uuid}' đã ngắt kết nối.")
                if self.on_device_disconnected:
                    try:
                        self.on_device_disconnected(registered_uuid)
                    except Exception as e:
                        print(f"[NetServer Callback Error on_device_disconnected] {e}")

    def _watchdog_loop(self):
        """Giám sát timeout định kỳ để loại bỏ các máy bị mất nguồn hoặc đứt cáp đột ngột."""
        while self._is_running.is_set():
            time.sleep(2.0)
            now = time.time()
            stale_handlers: List[ClientConnectionHandler] = []

            with self._clients_lock:
                for uuid_val, handler in list(self.clients.items()):
                    if now - handler.last_seen > SERVER_CLIENT_TIMEOUT:
                        stale_handlers.append(handler)

            for h in stale_handlers:
                print(f"[NetServer Watchdog] Phát hiện máy '{h.device_uuid}' quá hạn phản hồi (Timeout > {SERVER_CLIENT_TIMEOUT}s). Đóng kết nối.")
                h.close()

    def send_command(self, device_uuid: str, command: CommandPacket) -> bool:
        """Gửi lệnh điều khiển tức thì tới một máy con cụ thể."""
        with self._clients_lock:
            handler = self.clients.get(device_uuid)
            if not handler or not handler.is_active:
                print(f"[NetServer Command Error] Không tìm thấy kết nối active của máy '{device_uuid}'")
                return False

        msg = ProtocolMessage(
            msg_type=MessageType.COMMAND.value,
            payload=command.to_dict(),
            timestamp=time.time(),
        )
        ok = handler.send(msg)
        if ok:
            print(f"[NetServer Command Sent] Đã gửi lệnh '{command.action}' cho app '{command.target_app}' tới máy '{device_uuid}'")
        else:
            print(f"[NetServer Command Failed] Gửi lệnh tới máy '{device_uuid}' thất bại.")
        return ok

    def get_connected_uuids(self) -> List[str]:
        """Trả về danh sách UUID các máy đang có kết nối trực tiếp đến Server."""
        with self._clients_lock:
            return list(self.clients.keys())

    def get_client_info(self, device_uuid: str) -> Optional[DeviceRegistrationData]:
        """Lấy thông tin đăng ký của một máy con đang online."""
        with self._clients_lock:
            handler = self.clients.get(device_uuid)
            return handler.reg_data if handler else None
