"""
Bộ đóng gói (Framer), xác thực (HMAC-SHA256) và giải mã (Parser) thông điệp nhị phân trên đường truyền TCP.
Hỗ trợ 2 chế độ:
1. Gói tin bắt tay (Handshake): [4 Bytes Độ dài (Big-Endian UInt32)] + [Dữ liệu JSON UTF-8]
2. Gói tin phiên đã xác thực (Authenticated Session):
   [4 Bytes Độ dài] + [8 Bytes Sequence Number (>Q)] + [32 Bytes HMAC-SHA256] + [Payload JSON UTF-8]
   - MAC = HMAC-SHA256(K, direction || seq || payload)
   - Kiểm tra MAC (constant-time) và Sequence Number trước khi giải mã JSON (Verify-then-Parse).
"""

import hashlib
import hmac
import json
import socket
import struct
from typing import Optional

from models.network_protocol import ProtocolMessage
from utils.safe_print import safe_print as print

HEADER_FORMAT = ">I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

AUTH_HEADER_FORMAT = ">IQ32s"
AUTH_HEADER_SIZE = struct.calcsize(AUTH_HEADER_FORMAT)  # 4 + 8 + 32 = 44 bytes

MAX_PAYLOAD_SIZE = 10 * 1024 * 1024  # 10 MB trần an toàn chống tràn bộ nhớ
MAX_HANDSHAKE_SIZE = 64 * 1024       # 64 KB trần an toàn cho giai đoạn chưa xác thực

DIR_CLIENT_TO_SERVER = b"\x01"
DIR_SERVER_TO_CLIENT = b"\x02"


class RecvTimeoutError(Exception):
    """Ngoại lệ báo hiệu socket.timeout khi đang chờ đọc byte đầu tiên (idle poll)."""
    pass


# =============================================================================
# CÁC HÀM MẬT MÃ HỌC (PSK + HMAC-SHA256 + SESSION KEY DERIVATION)
# =============================================================================
def compute_auth_hmac(psk: bytes, nonce_server: str, device_uuid: str) -> str:
    """Tính HMAC(PSK, nonce_server || device_uuid) cho bước REGISTER."""
    msg = f"{nonce_server}|{device_uuid}".encode("utf-8")
    return hmac.new(psk, msg, hashlib.sha256).hexdigest()


def derive_session_key(psk: bytes, nonce_server: str, nonce_agent: str) -> bytes:
    """Dẫn xuất khóa phiên K = HMAC(PSK, nonce_server || nonce_agent)."""
    msg = f"{nonce_server}|{nonce_agent}".encode("utf-8")
    return hmac.new(psk, msg, hashlib.sha256).digest()


def compute_server_proof(session_key: bytes, nonce_agent: str) -> str:
    """Tính bằng chứng xác thực Server (Mutual Authentication) cho gói REGISTER_OK."""
    msg = f"SERVER_OK|{nonce_agent}".encode("utf-8")
    return hmac.new(session_key, msg, hashlib.sha256).hexdigest()


def compute_packet_mac(session_key: bytes, direction: bytes, seq: int, payload_bytes: bytes) -> bytes:
    """Tính MAC cho từng gói tin: HMAC(K, chiều || seq || payload)."""
    seq_bytes = struct.pack(">Q", seq)
    return hmac.new(session_key, direction + seq_bytes + payload_bytes, hashlib.sha256).digest()


# =============================================================================
# ĐỌC SOCKET AN TOÀN (PHÂN BIỆT IDLE TIMEOUT VÀ STALL/DEADLINE TIMEOUT)
# =============================================================================
def recv_exact(
    sock: socket.socket,
    num_bytes: int,
    timeout_is_fatal: bool = True,
) -> Optional[bytes]:
    """
    Đọc chính xác num_bytes từ socket.
    - Nếu timeout_is_fatal=True (vd: bắt tay 5s): bất kỳ socket.timeout nào đều trả về None (ngắt kết nối).
    - Nếu timeout_is_fatal=False (phiên đã kết nối):
      + Nếu chưa đọc byte nào (len(buf) == 0) và timeout: ném RecvTimeoutError để vòng lặp kiểm tra trạng thái dừng.
      + Nếu đã đọc dở gói tin (len(buf) > 0) mà timeout: trả về None (ngắt kết nối chống Slowloris/treo).
    """
    buf = bytearray()
    while len(buf) < num_bytes:
        try:
            chunk = sock.recv(num_bytes - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        except socket.timeout:
            if timeout_is_fatal or len(buf) > 0:
                return None
            raise RecvTimeoutError()
        except (BlockingIOError, socket.error, OSError):
            return None
    return bytes(buf)


# =============================================================================
# FRAMING GIAI ĐOẠN BẮT TAY (CHALLENGE / REGISTER)
# =============================================================================
def send_message(sock: socket.socket, message: ProtocolMessage) -> bool:
    """Đóng gói và gửi ProtocolMessage thô (dùng cho bước Handshake)."""
    try:
        raw_json = json.dumps(message.to_dict(), ensure_ascii=False).encode("utf-8")
        payload_len = len(raw_json)
        if payload_len > MAX_PAYLOAD_SIZE:
            print(f"[Framing Error] Kích thước gói tin vượt ngưỡng: {payload_len} bytes")
            return False

        header = struct.pack(HEADER_FORMAT, payload_len)
        sock.sendall(header + raw_json)
        return True
    except (socket.error, OSError):
        return False
    except Exception as e:
        print(f"[Framing Send Error] {e}")
        return False


def recv_message(
    sock: socket.socket,
    timeout_is_fatal: bool = True,
    max_size: int = MAX_HANDSHAKE_SIZE,
) -> Optional[ProtocolMessage]:
    """Đọc và giải mã ProtocolMessage thô (dùng cho bước Handshake)."""
    try:
        header_bytes = recv_exact(sock, HEADER_SIZE, timeout_is_fatal=timeout_is_fatal)
        if not header_bytes:
            return None

        (payload_len,) = struct.unpack(HEADER_FORMAT, header_bytes)
        if payload_len <= 0 or payload_len > max_size:
            print(f"[Framing Error] Kích thước payload bất thường: {payload_len} bytes")
            return None

        payload_bytes = recv_exact(sock, payload_len, timeout_is_fatal=True)
        if not payload_bytes:
            return None

        data_dict = json.loads(payload_bytes.decode("utf-8"))
        return ProtocolMessage.from_dict(data_dict)
    except RecvTimeoutError:
        raise
    except (socket.error, OSError):
        return None
    except json.JSONDecodeError as e:
        print(f"[Framing Error] Lỗi giải mã JSON: {e}")
        return None
    except Exception as e:
        print(f"[Framing Recv Error] {e}")
        return None


# =============================================================================
# FRAMING PHIÊN ĐÃ XÁC THỰC: [Length (4B)][Seq (8B)][MAC (32B)] + [Payload]
# =============================================================================
def send_authenticated_message(
    sock: socket.socket,
    message: ProtocolMessage,
    session_key: bytes,
    direction: bytes,
    seq: int,
) -> bool:
    """Đóng gói, tính HMAC(K, chiều || seq || payload) và gửi thông điệp đã xác thực."""
    try:
        raw_json = json.dumps(message.to_dict(), ensure_ascii=False).encode("utf-8")
        payload_len = len(raw_json)
        if payload_len > MAX_PAYLOAD_SIZE:
            print(f"[Framing Error] Kích thước gói tin vượt ngưỡng: {payload_len} bytes")
            return False

        mac = compute_packet_mac(session_key, direction, seq, raw_json)
        header = struct.pack(AUTH_HEADER_FORMAT, payload_len, seq, mac)
        sock.sendall(header + raw_json)
        return True
    except (socket.error, OSError):
        return False
    except Exception as e:
        print(f"[Framing Auth Send Error] {e}")
        return False


def recv_authenticated_message(
    sock: socket.socket,
    session_key: bytes,
    direction: bytes,
    expected_seq: int,
    timeout_is_fatal: bool = False,
) -> Optional[ProtocolMessage]:
    """
    Đọc và xác thực thông điệp theo nguyên tắc Verify-then-Parse:
    1. Đọc 44 bytes header: [Length][Seq][MAC].
    2. Kiểm tra Seq == expected_seq (chống replay, đảo thứ tự, mất đoạn).
    3. Đọc đủ Length bytes payload.
    4. Kiểm tra HMAC(K, direction || seq || payload) == MAC bằng compare_digest.
    5. Nếu hợp lệ mới giải mã JSON.
    """
    try:
        header_bytes = recv_exact(sock, AUTH_HEADER_SIZE, timeout_is_fatal=timeout_is_fatal)
        if not header_bytes:
            return None

        payload_len, seq, recv_mac = struct.unpack(AUTH_HEADER_FORMAT, header_bytes)

        if payload_len <= 0 or payload_len > MAX_PAYLOAD_SIZE:
            print(f"[Security Error] Kích thước payload bất thường: {payload_len} bytes")
            return None

        if seq != expected_seq:
            print(f"[Security Error] Sai số thứ tự gói tin (nhận seq={seq}, kỳ vọng={expected_seq}) -> Nghi ngờ replay/mất gói!")
            return None

        # Đã đọc header thì phải đọc đủ payload trong timeout ngắn (tránh treo nửa chừng)
        payload_bytes = recv_exact(sock, payload_len, timeout_is_fatal=True)
        if not payload_bytes:
            return None

        calc_mac = compute_packet_mac(session_key, direction, seq, payload_bytes)
        if not hmac.compare_digest(calc_mac, recv_mac):
            print("[Security Error] Sai chữ ký HMAC gói tin -> Từ chối xử lý!")
            return None

        data_dict = json.loads(payload_bytes.decode("utf-8"))
        return ProtocolMessage.from_dict(data_dict)
    except RecvTimeoutError:
        raise
    except (socket.error, OSError):
        return None
    except json.JSONDecodeError as e:
        print(f"[Framing Error] Lỗi giải mã JSON: {e}")
        return None
    except Exception as e:
        print(f"[Framing Auth Recv Error] {e}")
        return None
