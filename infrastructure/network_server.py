"""
Máy chủ truyền thông mạng tập trung (Centralized Network Server - TCP):
- Quản lý đa kết nối từ các máy con (Multi-threaded TCP Socket Server).
- Xác thực bắt tay Challenge-Response (PSK + HMAC-SHA256) với hạn chót 5s và giới hạn pending auth.
- Gắn cứng danh tính phiên (Session Binding) vào device_uuid đã xác thực.
- Bảo vệ toàn vẹn, chống Replay & đảo thứ tự gói tin bằng Khóa phiên (K) + Sequence Number + HMAC.
- Tiếp nhận Heartbeat & Telemetry (phản hồi TELEMETRY_ACK) thời gian thực.
- Đẩy lệnh điều khiển đã xác thực xuống máy con.
"""

import hmac
import secrets
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from config.settings import (
    SERVER_DEFAULT_HOST,
    SERVER_DEFAULT_PORT,
    SERVER_CLIENT_TIMEOUT,
    NETWORK_PSK,
    AUTH_TIMEOUT,
    MAX_PENDING_AUTH,
    RECV_POLL_TIMEOUT,
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
    compute_auth_hmac,
    compute_server_proof,
    derive_session_key,
    recv_authenticated_message,
    recv_message,
    send_authenticated_message,
    send_message,
)
from utils.safe_print import safe_print as print


class ClientConnectionHandler:
    """Xử lý phiên kết nối đã xác thực của một máy con cụ thể."""

    def __init__(self, sock: socket.socket, addr: tuple, server: "NetworkServer"):
        self.sock = sock
        self.addr = addr
        self.server = server
        self.device_uuid: str = ""
        self.reg_data: Optional[DeviceRegistrationData] = None
        self.last_seen: float = time.time()
        self.is_active: bool = True
        self.is_authenticated: bool = False
        self.session_key: Optional[bytes] = None
        self.tx_seq: int = 1
        self.rx_seq: int = 1
        self._lock = threading.Lock()

    def send(self, msg: ProtocolMessage) -> bool:
        """Gửi thông điệp đã ký HMAC với sequence number tăng dần xuống máy con này."""
        with self._lock:
            if not self.is_active or self.sock is None or not self.session_key:
                return False
            ok = send_authenticated_message(
                self.sock,
                msg,
                self.session_key,
                DIR_SERVER_TO_CLIENT,
                self.tx_seq,
            )
            if ok:
                self.tx_seq += 1
            return ok

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

    def __init__(
        self,
        host: str = SERVER_DEFAULT_HOST,
        port: int = SERVER_DEFAULT_PORT,
        psk: str = NETWORK_PSK,
    ):
        self.host = host
        self.port = port
        self.psk_bytes = psk.encode("utf-8")
        self._server_sock: Optional[socket.socket] = None
        self._is_running = threading.Event()
        self._clients_lock = threading.RLock()
        self.clients: Dict[str, ClientConnectionHandler] = {}  # device_uuid -> Handler

        # Giới hạn kết nối chưa xác thực (chống DoS cạn kiệt tài nguyên)
        self._pending_auth_lock = threading.Lock()
        self._pending_auth_count: int = 0

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

                with self._pending_auth_lock:
                    if self._pending_auth_count >= MAX_PENDING_AUTH:
                        print(f"[NetServer Security] Đạt ngưỡng kết nối chưa xác thực ({MAX_PENDING_AUTH}). Từ chối {addr}")
                        try:
                            sock.close()
                        except Exception:
                            pass
                        continue
                    self._pending_auth_count += 1

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

    def _perform_handshake(self, handler: ClientConnectionHandler) -> Optional[DeviceRegistrationData]:
        """Thực hiện bắt tay Challenge-Response trong thời hạn AUTH_TIMEOUT (5s)."""
        sock = handler.sock
        if not sock:
            return None

        sock.settimeout(AUTH_TIMEOUT)
        nonce_server = secrets.token_hex(16)

        challenge_msg = ProtocolMessage(
            msg_type=MessageType.CHALLENGE.value,
            payload={"nonce_server": nonce_server},
            timestamp=time.time(),
        )
        if not send_message(sock, challenge_msg):
            return None

        reg_msg = recv_message(sock, timeout_is_fatal=True)
        if not reg_msg or reg_msg.msg_type != MessageType.REGISTER.value:
            print(f"[NetServer Auth Failed] Không nhận được gói REGISTER hợp lệ từ {handler.addr} (hết hạn hoặc sai định dạng)")
            return None

        reg = DeviceRegistrationData.from_dict(reg_msg.payload)
        nonce_agent = str(reg_msg.payload.get("nonce_agent", ""))
        auth_mac = str(reg_msg.payload.get("auth_mac", ""))

        if not reg.device_uuid or len(nonce_agent) < 16 or not auth_mac:
            print(f"[NetServer Auth Failed] Thiếu thông tin xác thực từ {handler.addr}")
            return None

        expected_mac = compute_auth_hmac(self.psk_bytes, nonce_server, reg.device_uuid)
        if not hmac.compare_digest(auth_mac, expected_mac):
            print(f"[NetServer Auth Failed] Sai chữ ký HMAC xác thực từ {handler.addr} (UUID: {reg.device_uuid})")
            return None

        if not reg.ip_address:
            reg.ip_address = handler.addr[0]

        # Dẫn xuất khóa phiên K = HMAC(PSK, nonce_server || nonce_agent)
        session_key = derive_session_key(self.psk_bytes, nonce_server, nonce_agent)
        handler.session_key = session_key
        handler.tx_seq = 1
        handler.rx_seq = 1
        handler.device_uuid = reg.device_uuid
        handler.reg_data = reg
        handler.is_authenticated = True
        handler.last_seen = time.time()

        # Chuyển sang poll timeout để vòng lặp kiểm tra _is_running / is_active mượt mà
        sock.settimeout(RECV_POLL_TIMEOUT)

        # Gửi REGISTER_OK có ký HMAC bằng khóa phiên (Mutual Authentication)
        ok_msg = ProtocolMessage(
            msg_type=MessageType.REGISTER_OK.value,
            payload={
                "status": "OK",
                "device_uuid": reg.device_uuid,
                "server_proof": compute_server_proof(session_key, nonce_agent),
            },
            timestamp=time.time(),
        )
        if not handler.send(ok_msg):
            return None

        return reg

    def _handle_client_session(self, handler: ClientConnectionHandler):
        """Xử lý luồng dữ liệu độc lập của một máy con."""
        registered_uuid = ""
        pending_released = False
        try:
            reg = self._perform_handshake(handler)
            with self._pending_auth_lock:
                self._pending_auth_count = max(0, self._pending_auth_count - 1)
                pending_released = True

            if reg is None:
                return

            registered_uuid = reg.device_uuid
            with self._clients_lock:
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

            while self._is_running.is_set() and handler.is_active:
                try:
                    msg = recv_authenticated_message(
                        handler.sock,
                        handler.session_key,
                        DIR_CLIENT_TO_SERVER,
                        handler.rx_seq,
                        timeout_is_fatal=False,
                    )
                except RecvTimeoutError:
                    continue

                if msg is None:
                    break

                handler.rx_seq += 1
                handler.last_seen = time.time()

                if msg.msg_type == MessageType.REGISTER.value:
                    # Mỗi socket chỉ được đăng ký đúng 1 lần
                    print(f"[NetServer Security] Máy con '{registered_uuid}' gửi lại REGISTER bất hợp lệ. Đóng phiên.")
                    break

                elif msg.msg_type == MessageType.HEARTBEAT.value:
                    handler.last_seen = time.time()

                elif msg.msg_type == MessageType.TELEMETRY.value:
                    handler.last_seen = time.time()
                    telem = TelemetryPacket.from_dict(msg.payload)
                    # Gắn cứng danh tính phiên: bỏ qua device_uuid tự khai báo trong payload
                    telem.device_uuid = registered_uuid

                    if self.on_telemetry_received:
                        try:
                            self.on_telemetry_received(registered_uuid, telem)
                        except Exception as e:
                            print(f"[NetServer Callback Error on_telemetry_received] {e}")

                    # Xác nhận đã tiếp nhận Telemetry thành công
                    ack_msg = ProtocolMessage(
                        msg_type=MessageType.TELEMETRY_ACK.value,
                        payload={"timestamp": telem.timestamp, "status": "OK"},
                        timestamp=time.time(),
                    )
                    handler.send(ack_msg)

                elif msg.msg_type == MessageType.COMMAND_RESULT.value:
                    handler.last_seen = time.time()
                    if self.on_command_result:
                        res = CommandResultPacket.from_dict(msg.payload)
                        # Gắn cứng danh tính phiên: chỉ dùng UUID của phiên đã xác thực
                        res.device_uuid = registered_uuid
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
            if not pending_released:
                with self._pending_auth_lock:
                    self._pending_auth_count = max(0, self._pending_auth_count - 1)

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
        """Gửi lệnh điều khiển đã ký HMAC tức thì tới một máy con cụ thể."""
        with self._clients_lock:
            handler = self.clients.get(device_uuid)
            if not handler or not handler.is_active or not handler.is_authenticated:
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
