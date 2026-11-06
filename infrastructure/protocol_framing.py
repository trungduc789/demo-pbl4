"""
Bộ đóng gói (Framer) và giải mã (Parser) thông điệp nhị phân trên đường truyền TCP.
Định dạng: [4 Bytes Độ dài Payload (Big-Endian UInt32)] + [Dữ liệu JSON mã hóa UTF-8].
Xử lý triệt để hiện tượng phân mảnh gói tin TCP (TCP Stream Fragmentation & Coalescing).
"""

import json
import socket
import struct
from typing import Optional
from models.network_protocol import ProtocolMessage
from utils.safe_print import safe_print as print

HEADER_FORMAT = ">I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MAX_PAYLOAD_SIZE = 10 * 1024 * 1024  # 10 MB trần an toàn chống tràn bộ nhớ


def send_message(sock: socket.socket, message: ProtocolMessage) -> bool:
    """Đóng gói và gửi một ProtocolMessage qua socket an toàn."""
    try:
        raw_json = json.dumps(message.to_dict(), ensure_ascii=False).encode("utf-8")
        payload_len = len(raw_json)
        if payload_len > MAX_PAYLOAD_SIZE:
            print(f"[Framing Error] Kích thước gói tin vượt ngưỡng: {payload_len} bytes")
            return False

        header = struct.pack(HEADER_FORMAT, payload_len)
        sock.sendall(header + raw_json)
        return True
    except (socket.error, OSError) as e:
        return False
    except Exception as e:
        print(f"[Framing Send Error] {e}")
        return False


def recv_exact(sock: socket.socket, num_bytes: int) -> Optional[bytes]:
    """Đọc chính xác num_bytes từ socket, lặp đọc liên tục cho đến khi đủ hoặc đóng kết nối."""
    buf = bytearray()
    while len(buf) < num_bytes:
        try:
            chunk = sock.recv(num_bytes - len(buf))
            if not chunk:
                # Đối phương đã đóng socket
                return None
            buf.extend(chunk)
        except (socket.timeout, BlockingIOError):
            continue
        except (socket.error, OSError):
            return None
    return bytes(buf)


def recv_message(sock: socket.socket) -> Optional[ProtocolMessage]:
    """Đọc và giải mã một ProtocolMessage hoàn chỉnh từ socket."""
    try:
        header_bytes = recv_exact(sock, HEADER_SIZE)
        if not header_bytes:
            return None

        (payload_len,) = struct.unpack(HEADER_FORMAT, header_bytes)
        if payload_len <= 0 or payload_len > MAX_PAYLOAD_SIZE:
            print(f"[Framing Error] Kích thước payload bất thường: {payload_len} bytes")
            return None

        payload_bytes = recv_exact(sock, payload_len)
        if not payload_bytes:
            return None

        data_dict = json.loads(payload_bytes.decode("utf-8"))
        return ProtocolMessage.from_dict(data_dict)
    except (socket.error, OSError):
        return None
    except json.JSONDecodeError as e:
        print(f"[Framing Error] Lỗi giải mã JSON: {e}")
        return None
    except Exception as e:
        print(f"[Framing Recv Error] {e}")
        return None
