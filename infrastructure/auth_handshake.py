"""
Quy trình Bắt tay Xác thực 2 Chiều Challenge-Response (Auth Handshake - Infrastructure Layer):
- perform_server_handshake: Phát CHALLENGE, xác minh REGISTER + HMAC(PSK), dẫn xuất khóa phiên K, gửi REGISTER_OK kèm server_proof.
- perform_client_handshake: Nhận CHALLENGE, ký REGISTER bằng PSK, dẫn xuất khóa phiên K, xác minh REGISTER_OK từ Server.
"""

import hmac
import secrets
import socket
import time
from typing import Any, Optional, Tuple

from config.settings import AUTH_TIMEOUT, RECV_POLL_TIMEOUT
from models.network_protocol import (
    MessageType,
    DeviceRegistrationData,
    ProtocolMessage,
)
from infrastructure.protocol_framing import (
    DIR_SERVER_TO_CLIENT,
    compute_auth_hmac,
    compute_server_proof,
    derive_session_key,
    recv_authenticated_message,
    recv_message,
    send_message,
)
from utils.safe_print import safe_print as print


def perform_server_handshake(
    handler: Any,
    psk_bytes: bytes,
) -> Optional[DeviceRegistrationData]:
    """Thực hiện bắt tay Challenge-Response phía Server trong thời hạn AUTH_TIMEOUT (5s)."""
    sock: Optional[socket.socket] = handler.sock
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
        print(
            f"[NetServer Auth Failed] Không nhận được gói REGISTER hợp lệ từ {handler.addr} (hết hạn hoặc sai định dạng)"
        )
        return None

    reg = DeviceRegistrationData.from_dict(reg_msg.payload)
    nonce_agent = str(reg_msg.payload.get("nonce_agent", ""))
    auth_mac = str(reg_msg.payload.get("auth_mac", ""))

    if not reg.device_uuid or len(nonce_agent) < 16 or not auth_mac:
        print(f"[NetServer Auth Failed] Thiếu thông tin xác thực từ {handler.addr}")
        return None

    expected_mac = compute_auth_hmac(psk_bytes, nonce_server, reg.device_uuid)
    if not hmac.compare_digest(auth_mac, expected_mac):
        print(f"[NetServer Auth Failed] Sai chữ ký HMAC xác thực từ {handler.addr} (UUID: {reg.device_uuid})")
        return None

    if not reg.ip_address:
        reg.ip_address = handler.addr[0]

    session_key = derive_session_key(psk_bytes, nonce_server, nonce_agent)
    handler.session_key = session_key
    handler.tx_seq = 1
    handler.rx_seq = 1
    handler.device_uuid = reg.device_uuid
    handler.reg_data = reg
    handler.is_authenticated = True
    handler.last_seen = time.time()

    sock.settimeout(RECV_POLL_TIMEOUT)

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


def perform_client_handshake(
    sock: socket.socket,
    reg_data: Optional[DeviceRegistrationData],
    psk_bytes: bytes,
) -> Optional[Tuple[bytes, int, int]]:
    """
    Thực hiện bắt tay Challenge-Response & xác thực 2 chiều phía Client.
    Trả về (session_key, initial_tx_seq=1, initial_rx_seq=2) nếu thành công, hoặc None nếu thất bại.
    """
    sock.settimeout(AUTH_TIMEOUT)

    chal_msg = recv_message(sock, timeout_is_fatal=True)
    if not chal_msg or chal_msg.msg_type != MessageType.CHALLENGE.value:
        print("[NetClient Auth Error] Không nhận được gói CHALLENGE hợp lệ từ Server.")
        return None

    nonce_server = str(chal_msg.payload.get("nonce_server", ""))
    if len(nonce_server) < 16:
        print("[NetClient Auth Error] Nonce CHALLENGE từ Server không hợp lệ.")
        return None

    nonce_agent = secrets.token_hex(16)
    device_uuid = reg_data.device_uuid if reg_data else ""
    auth_mac = compute_auth_hmac(psk_bytes, nonce_server, device_uuid)

    reg_payload = reg_data.to_dict() if reg_data else {"device_uuid": device_uuid}
    reg_payload["nonce_agent"] = nonce_agent
    reg_payload["auth_mac"] = auth_mac

    reg_msg = ProtocolMessage(
        msg_type=MessageType.REGISTER.value,
        payload=reg_payload,
        timestamp=time.time(),
    )
    if not send_message(sock, reg_msg):
        return None

    session_key = derive_session_key(psk_bytes, nonce_server, nonce_agent)

    ok_msg = recv_authenticated_message(
        sock,
        session_key,
        DIR_SERVER_TO_CLIENT,
        expected_seq=1,
        timeout_is_fatal=True,
    )
    if not ok_msg or ok_msg.msg_type != MessageType.REGISTER_OK.value:
        print("[NetClient Auth Error] Server từ chối đăng ký hoặc sai khóa PSK.")
        return None

    server_proof = str(ok_msg.payload.get("server_proof", ""))
    expected_proof = compute_server_proof(session_key, nonce_agent)
    if not hmac.compare_digest(server_proof, expected_proof):
        print("[NetClient Auth Error] Server không chứng minh được khóa PSK (nghi ngờ giả mạo Server).")
        return None

    sock.settimeout(RECV_POLL_TIMEOUT)
    return session_key, 1, 2
