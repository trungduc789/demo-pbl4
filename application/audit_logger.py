"""
Quản lý Nhật ký Kiểm toán Nghiệp vụ An ninh (Audit Logger - Application Layer):
- Lưu trữ an toàn đa luồng tối đa 200 sự kiện kiểm toán gần nhất cho Client Agent.
- Chuẩn hóa mã hành động và mô tả chi tiết cho các lệnh từ Admin Server và thao tác tự bảo vệ cục bộ.
"""

import time
import threading
from typing import Any, Dict, List, Tuple
from models.network_protocol import CommandAction


def format_admin_command_audit(
    action: str,
    app_name: str,
    direction: str,
    limit_kbps: float,
    fallback_msg: str,
) -> Tuple[str, str]:
    """Trả về (audit_action_code, audit_description) cho lệnh điều khiển từ Admin Server."""
    if action == CommandAction.BLOCK_APP.value:
        return "BLOCK", f"Chặn hoàn toàn lưu lượng của ứng dụng '{app_name}' ({direction}) [🔒 Admin Locked]"
    if action == CommandAction.THROTTLE_SET.value:
        return "QoS", f"Giới hạn tốc độ '{app_name}' mức {limit_kbps:.1f} KB/s ({direction}) [🔒 Admin Locked]"
    if action == CommandAction.UNBLOCK_APP.value:
        return "UNBLOCK", f"Gỡ bỏ chặn cho ứng dụng '{app_name}'"
    if action == CommandAction.UNBLOCK_ALL.value:
        return "UNBLOCK_ALL", "Gỡ bỏ toàn bộ danh sách ứng dụng bị chặn"
    if action == CommandAction.REMOVE_LIMIT.value:
        return "REMOVE_LIMIT", f"Gỡ bỏ hạn mức QoS cho '{app_name or 'toàn bộ'}'"
    if action == CommandAction.PING.value:
        return "PING", "Admin Server gửi gói tin kiểm tra kết nối (Ping)"
    return action, fallback_msg


def append_audit_entry(
    audit_logs: List[Dict[str, Any]],
    audit_lock: threading.Lock,
    source: str,
    action: str,
    target: str,
    status: str,
    details: str,
    max_entries: int = 200,
):
    """Ghi nhận một sự kiện kiểm toán mới vào đầu danh sách với giới hạn kích thước cố định."""
    with audit_lock:
        entry = {
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "source": source,
            "action": action,
            "target": target,
            "status": status,
            "details": details,
        }
        audit_logs.insert(0, entry)
        if len(audit_logs) > max_entries:
            audit_logs.pop()
