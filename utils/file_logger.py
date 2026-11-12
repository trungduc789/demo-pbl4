import os
import threading
from datetime import datetime
from typing import Optional
import sys

# Thư mục logs nằm tại thư mục gốc của project hoặc cạnh file .exe khi đóng gói
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

class FileLogger:
    """
    Quản lý ghi file log văn bản thời gian thực (Thread-Safe):
    - logs/connections.log: Lịch sử IP và kết nối TCP/UDP của từng tiến trình
    - logs/policies.log: Lịch sử áp dụng và gỡ bỏ các chính sách bóp băng thông/chặn mạng
    """
    _lock = threading.Lock()
    _seen_connections = set()
    _max_seen_cache = 5000

    @classmethod
    def _ensure_dir(cls):
        if not os.path.exists(LOGS_DIR):
            try:
                os.makedirs(LOGS_DIR, exist_ok=True)
            except Exception:
                pass

    @classmethod
    def log_connection(
        cls,
        proc_name: str,
        pid: Optional[int],
        protocol: str,
        local_ip: str,
        local_port: Optional[int],
        remote_ip: Optional[str],
        remote_port: Optional[int],
        state: Optional[str]
    ):
        """
        Ghi nhật ký kết nối mạng IP của tiến trình.
        Dùng bộ đệm (cache) để chỉ ghi khi có kết nối mới hoặc thay đổi trạng thái,
        tránh spam hàng nghìn dòng giống hệt nhau mỗi giây.
        """
        conn_key = (proc_name, pid, protocol, local_ip, local_port, remote_ip, remote_port, state)
        with cls._lock:
            if conn_key in cls._seen_connections:
                return
            if len(cls._seen_connections) > cls._max_seen_cache:
                cls._seen_connections.clear()
            cls._seen_connections.add(conn_key)

            cls._ensure_dir()
            log_path = os.path.join(LOGS_DIR, "connections.log")
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pid_str = f"PID={pid}" if pid else "PID=N/A"
            r_str = f"{remote_ip}:{remote_port}" if remote_ip else "*:*"
            l_str = f"{local_ip}:{local_port}" if local_port else f"{local_ip}:*"
            st_str = f"[{state}]" if state else ""

            line = f"[{timestamp_str}] [{protocol.upper()}] {proc_name} ({pid_str}) | {l_str} -> {r_str} {st_str}\n"
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    @classmethod
    def log_policy(
        cls,
        action: str,  # "APPLY", "REMOVE", "BLOCK", "UNBLOCK"
        policy_type: str,
        target_app: str,
        limit_kbps: float = 0.0,
        direction: str = "both",
        throttled_packets: int = 0,
        throttled_bytes: int = 0,
        status: str = "SUCCESS",
        details: str = ""
    ):
        """
        Ghi nhật ký áp dụng / gỡ bỏ chính sách an ninh mạng (bóp băng thông / chặn).
        """
        with cls._lock:
            cls._ensure_dir()
            log_path = os.path.join(LOGS_DIR, "policies.log")
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            dir_map = {
                "both": "Upload & Download (Cả 2 chiều)",
                "upload": "Chỉ Upload (Tải lên)",
                "download": "Chỉ Download (Tải về)"
            }
            dir_str = dir_map.get(direction.lower(), direction)

            th_kb = throttled_bytes / 1024.0
            th_str = f"{throttled_packets:,} gói ({th_kb:,.1f} KB)" if throttled_packets > 0 else "0 gói"

            act_upper = action.upper()
            if act_upper in ("APPLY", "BLOCK"):
                tag = "POLICY_BLOCK" if act_upper == "BLOCK" else "POLICY_APPLY"
                line = (
                    f"[{timestamp_str}] [{tag}] Loại: {policy_type} | "
                    f"Tiến trình: {target_app} | Hướng: {dir_str} | "
                    f"Mức trần: {limit_kbps:,.1f} KB/s | Trạng thái: {status}"
                )
            else:
                tag = "POLICY_UNBLOCK" if act_upper == "UNBLOCK" else "POLICY_REMOVE"
                line = (
                    f"[{timestamp_str}] [{tag}] Loại: {policy_type} | "
                    f"Tiến trình: {target_app} | Đã điều phối: {th_str} | Trạng thái: {status}"
                )

            if details:
                line += f" | Chi tiết: {details}"
            line += "\n"

            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    @classmethod
    def read_policy_logs(cls, limit: int = 50) -> list:
        """Đọc các dòng nhật ký chính sách gần nhất từ logs/policies.log."""
        log_path = os.path.join(LOGS_DIR, "policies.log")
        if not os.path.exists(log_path):
            return []
        try:
            with cls._lock:
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            recent = lines[-limit:] if limit > 0 else lines
            recent.reverse()
            return [{"raw": ln} for ln in recent]
        except Exception:
            return []
