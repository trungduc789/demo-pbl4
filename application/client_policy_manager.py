"""
Quản lý Chính sách An ninh 2 Lớp (2-Tier Policy) & Nhật ký Kiểm toán (Audit Log) cho Client Agent:
- Lớp 1 (Admin Enforced 🔒): Lệnh từ Admin Server, khóa cứng không cho phép người dùng máy con gỡ bỏ.
- Lớp 2 (Local Self-Protect 🔓): Người dùng máy con tự thiết lập cho các tiến trình chưa bị Admin khóa.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from models.network_protocol import (
    CommandAction,
    CommandPacket,
    CommandResultPacket,
)
from utils.safe_print import safe_print as print


class ClientPolicyMixin:
    """Mixin cung cấp bộ xử lý lệnh từ xa, chính sách 2 lớp và nhật ký kiểm toán cho ClientAgentService."""

    def _init_initial_audit_log(self):
        """Khởi tạo mục nhật ký ban đầu khi khởi động Agent."""
        self.add_audit_log(
            source="Client Agent",
            action="STARTUP",
            target=self.registration_data.hostname,
            status="SUCCESS",
            details=f"Khởi động tiến trình Agent Node thành công (UUID: {self.device_uuid[:8]}...)",
        )

    def add_audit_log(self, source: str, action: str, target: str, status: str, details: str):
        """Ghi nhận sự kiện kiểm toán nghiệp vụ an ninh vào bộ đệm."""
        with self._audit_lock:
            entry = {
                "timestamp": time.time(),
                "time_str": time.strftime("%H:%M:%S"),
                "source": source,
                "action": action,
                "target": target,
                "status": status,
                "details": details,
            }
            self.audit_logs.insert(0, entry)
            if len(self.audit_logs) > 200:
                self.audit_logs.pop()

    def get_audit_logs(self) -> List[Dict[str, Any]]:
        """Lấy danh sách nhật ký kiểm toán cho giao diện."""
        with self._audit_lock:
            return list(self.audit_logs)

    def handle_remote_command(self, cmd: CommandPacket) -> Optional[CommandResultPacket]:
        """Xử lý lệnh can thiệp từ xa do Admin gửi tới (Lớp 1 - Admin Enforced 🔒)."""
        action = cmd.action
        app_name = cmd.target_app.strip()
        direction = cmd.direction or "both"
        limit_kbps = cmd.limit_kbps

        print(f"[ClientAgent Command Executing] Hành động: '{action}' | App: '{app_name}' | Limit: {limit_kbps} KB/s")

        success = False
        message = ""

        try:
            if action == CommandAction.BLOCK_APP.value:
                if not app_name:
                    success, message = False, "Tên ứng dụng cần chặn không được để trống."
                else:
                    existing_pol = self._find_policy_entry(app_name)
                    if existing_pol and existing_pol.get("action") == "THROTTLE":
                        try:
                            self.packet_shaper.remove_limit(app_name)
                        except TypeError:
                            self.packet_shaper.remove_limit()
                    success, message = self.packet_shaper.block_app(app_name, direction=direction)

            elif action == CommandAction.UNBLOCK_APP.value:
                if not app_name:
                    success, message = False, "Tên ứng dụng cần gỡ chặn không được để trống."
                else:
                    success, message = self.packet_shaper.unblock_app(app_name)

            elif action == CommandAction.UNBLOCK_ALL.value:
                success, message = self.packet_shaper.unblock_all()

            elif action == CommandAction.THROTTLE_SET.value:
                if not app_name:
                    success, message = False, "Tên ứng dụng cần bóp băng thông không được để trống."
                elif limit_kbps <= 0:
                    success, message = False, "Hạn mức băng thông phải lớn hơn 0 KB/s."
                else:
                    existing_pol = self._find_policy_entry(app_name)
                    if existing_pol and existing_pol.get("action") == "BLOCK":
                        self.packet_shaper.unblock_app(app_name)
                    success, message = self.packet_shaper.apply_limit(
                        app_name, limit_kbps=limit_kbps, direction=direction
                    )

            elif action == CommandAction.REMOVE_LIMIT.value:
                try:
                    success, message = self.packet_shaper.remove_limit(app_name if app_name else None)
                except TypeError:
                    success, message = self.packet_shaper.remove_limit()

            elif action == CommandAction.PING.value:
                success, message = (
                    True,
                    f"Pong from Client '{self.registration_data.hostname}' at {time.strftime('%H:%M:%S')}",
                )

            else:
                success, message = False, f"Hành động không xác định: {action}"

        except Exception as e:
            success, message = False, f"Ngoại lệ khi thực thi lệnh: {e}"

        status_str = "SUCCESS" if success else "FAILED"
        print(f"[ClientAgent Command Finished] {cmd.command_id} -> {status_str}: {message}")

        audit_act = action
        if action == CommandAction.BLOCK_APP.value:
            audit_act = "BLOCK"
            audit_desc = f"Chặn hoàn toàn lưu lượng của ứng dụng '{app_name}' ({direction}) [🔒 Admin Locked]"
        elif action == CommandAction.THROTTLE_SET.value:
            audit_act = "QoS"
            audit_desc = f"Giới hạn tốc độ '{app_name}' mức {limit_kbps:.1f} KB/s ({direction}) [🔒 Admin Locked]"
        elif action == CommandAction.UNBLOCK_APP.value:
            audit_act = "UNBLOCK"
            audit_desc = f"Gỡ bỏ chặn cho ứng dụng '{app_name}'"
        elif action == CommandAction.UNBLOCK_ALL.value:
            audit_act = "UNBLOCK_ALL"
            audit_desc = "Gỡ bỏ toàn bộ danh sách ứng dụng bị chặn"
        elif action == CommandAction.REMOVE_LIMIT.value:
            audit_act = "REMOVE_LIMIT"
            audit_desc = f"Gỡ bỏ hạn mức QoS cho '{app_name or 'toàn bộ'}'"
        elif action == CommandAction.PING.value:
            audit_act = "PING"
            audit_desc = "Admin Server gửi gói tin kiểm tra kết nối (Ping)"
        else:
            audit_desc = message

        self.add_audit_log(
            source="Admin Server",
            action=audit_act,
            target=app_name or "*",
            status=status_str,
            details=audit_desc,
        )

        if success:
            app_key = app_name.lower()
            if action == CommandAction.BLOCK_APP.value:
                self._remove_policy_entry(app_key)
                self.active_policies[app_key] = {
                    "action": "BLOCK",
                    "app": app_name,
                    "limit_kbps": 0.0,
                    "direction": direction,
                    "time": time.time(),
                    "source": "ADMIN",
                    "locked_by_admin": True,
                }
            elif action == CommandAction.THROTTLE_SET.value:
                self._remove_policy_entry(app_key)
                self.active_policies[app_key] = {
                    "action": "THROTTLE",
                    "app": app_name,
                    "limit_kbps": limit_kbps,
                    "direction": direction,
                    "time": time.time(),
                    "source": "ADMIN",
                    "locked_by_admin": True,
                }
            elif action == CommandAction.UNBLOCK_APP.value:
                self._remove_policy_entry(app_key)
            elif action == CommandAction.UNBLOCK_ALL.value:
                for k in list(self.active_policies.keys()):
                    if self.active_policies[k].get("action") == "BLOCK":
                        del self.active_policies[k]
            elif action == CommandAction.REMOVE_LIMIT.value:
                if app_key:
                    pol = self._find_policy_entry(app_key)
                    if pol and pol.get("action") == "THROTTLE":
                        self._remove_policy_entry(app_key)
                else:
                    for k in list(self.active_policies.keys()):
                        if self.active_policies[k].get("action") == "THROTTLE":
                            del self.active_policies[k]

            if self.on_policy_applied_locally:
                try:
                    self.on_policy_applied_locally(action, app_name, status_str, limit_kbps)
                except Exception as ex:
                    print(f"[ClientAgent Toast Error] {ex}")

        return CommandResultPacket(
            command_id=cmd.command_id,
            device_uuid=self.device_uuid,
            status=status_str,
            message=message,
            applied_at=time.time(),
        )

    def _find_policy_entry(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Tìm chính sách đang áp dụng cho ứng dụng (khớp cả tên có/không có .exe)."""
        if not app_name:
            return None
        raw = app_name.lower().strip()
        base = raw[:-4] if raw.endswith(".exe") else raw
        for c in (raw, f"{base}.exe", base):
            if c in self.active_policies:
                return self.active_policies[c]
        return None

    def _remove_policy_entry(self, app_name: str):
        """Xóa chính sách của ứng dụng khỏi bộ nhớ đệm theo mọi biến thể tên."""
        if not app_name:
            return
        raw = app_name.lower().strip()
        base = raw[:-4] if raw.endswith(".exe") else raw
        for c in (raw, f"{base}.exe", base):
            self.active_policies.pop(c, None)

    def get_policy_for_app(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Trả về thông tin chính sách hiện hành của một ứng dụng (nếu có)."""
        return self._find_policy_entry(app_name)

    def is_locked_by_admin(self, app_name: str) -> bool:
        """Kiểm tra ứng dụng có đang bị khóa cứng bởi chính sách từ Admin Server hay không."""
        pol = self._find_policy_entry(app_name)
        if not pol:
            return False
        return bool(pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN"))

    def apply_local_policy(
        self,
        app_name: str,
        action: str = "BLOCK",
        limit_kbps: float = 0.0,
        direction: str = "both",
    ) -> Tuple[bool, str]:
        """Người dùng tại máy con tự áp dụng chính sách bảo vệ cục bộ (Lớp 2 - Local Self-Protect)."""
        app_name = app_name.strip()
        if not app_name:
            return False, "Tên ứng dụng không được để trống."

        if self.is_locked_by_admin(app_name):
            msg = (
                f"Từ chối thao tác: Ứng dụng '{app_name}' đang chịu sự kiểm soát bắt buộc từ Admin Server (🔒 Admin Locked). "
                "Máy con không được phép thay đổi hoặc ghi đè chính sách của Quản trị viên!"
            )
            self.add_audit_log(
                source="Local User",
                action=f"DENIED_{action.upper()}",
                target=app_name,
                status="DENIED",
                details=msg,
            )
            return False, msg

        act_upper = action.upper()
        existing_local = self._find_policy_entry(app_name)
        if act_upper == "BLOCK":
            if existing_local and existing_local.get("action") == "THROTTLE":
                try:
                    self.packet_shaper.remove_limit(app_name)
                except TypeError:
                    self.packet_shaper.remove_limit()
            ok, msg = self.packet_shaper.block_app(app_name, direction=direction)
            if ok:
                self._remove_policy_entry(app_name)
                self.active_policies[app_name.lower()] = {
                    "action": "BLOCK",
                    "app": app_name,
                    "limit_kbps": 0.0,
                    "direction": direction,
                    "time": time.time(),
                    "source": "LOCAL",
                    "locked_by_admin": False,
                }
                self.add_audit_log(
                    source="Local User",
                    action="LOCAL_BLOCK",
                    target=app_name,
                    status="SUCCESS",
                    details=f"Người dùng máy con chủ động chặn cục bộ '{app_name}' ({direction})",
                )
                if self.on_policy_applied_locally:
                    try:
                        self.on_policy_applied_locally("LOCAL_BLOCK", app_name, "SUCCESS", 0.0)
                    except Exception:
                        pass
            return ok, msg

        if act_upper in ("THROTTLE", "QOS"):
            if limit_kbps <= 0:
                return False, "Hạn mức băng thông phải lớn hơn 0 KB/s."
            if existing_local and existing_local.get("action") == "BLOCK":
                self.packet_shaper.unblock_app(app_name)
            ok, msg = self.packet_shaper.apply_limit(app_name, limit_kbps=limit_kbps, direction=direction)
            if ok:
                self._remove_policy_entry(app_name)
                self.active_policies[app_name.lower()] = {
                    "action": "THROTTLE",
                    "app": app_name,
                    "limit_kbps": float(limit_kbps),
                    "direction": direction,
                    "time": time.time(),
                    "source": "LOCAL",
                    "locked_by_admin": False,
                }
                self.add_audit_log(
                    source="Local User",
                    action="LOCAL_QoS",
                    target=app_name,
                    status="SUCCESS",
                    details=f"Người dùng máy con tự giới hạn tốc độ '{app_name}' mức {limit_kbps:.1f} KB/s ({direction})",
                )
                if self.on_policy_applied_locally:
                    try:
                        self.on_policy_applied_locally("LOCAL_THROTTLE", app_name, "SUCCESS", float(limit_kbps))
                    except Exception:
                        pass
            return ok, msg

        return False, f"Hành động cục bộ không hợp lệ: {action}"

    def remove_local_policy(self, app_name: str) -> Tuple[bool, str]:
        """Người dùng tại máy con tự gỡ bỏ chính sách cục bộ do chính họ tạo."""
        app_name = app_name.strip()
        if not app_name:
            return False, "Tên ứng dụng không hợp lệ."

        if self.is_locked_by_admin(app_name):
            msg = (
                f"Từ chối gỡ bỏ: Chính sách đối với '{app_name}' được áp đặt bởi Admin Server (🔒 Admin Locked). "
                "Chỉ Quản trị viên mới có quyền mở khóa ứng dụng này!"
            )
            self.add_audit_log(
                source="Local User",
                action="DENIED_UNBLOCK",
                target=app_name,
                status="DENIED",
                details=msg,
            )
            return False, msg

        pol = self._find_policy_entry(app_name)
        if pol and pol.get("action") == "THROTTLE":
            try:
                ok, msg = self.packet_shaper.remove_limit(app_name)
            except TypeError:
                ok, msg = self.packet_shaper.remove_limit()
        else:
            ok, msg = self.packet_shaper.unblock_app(app_name)

        self._remove_policy_entry(app_name)
        self.add_audit_log(
            source="Local User",
            action="LOCAL_UNBLOCK",
            target=app_name,
            status="SUCCESS" if ok else "FAILED",
            details=f"Người dùng máy con đã gỡ bỏ chính sách tự vệ cục bộ cho '{app_name}'",
        )
        if self.on_policy_applied_locally:
            try:
                self.on_policy_applied_locally("LOCAL_UNBLOCK", app_name, "SUCCESS" if ok else "FAILED", 0.0)
            except Exception:
                pass
        return ok, msg

    def clear_all_local_policies(self) -> Tuple[int, int]:
        """Chỉ gỡ bỏ các chính sách cục bộ do máy con tự tạo (LOCAL), giữ nguyên chính sách Admin."""
        removed = 0
        kept_admin = 0
        for key, pol in list(self.active_policies.items()):
            if pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN"):
                kept_admin += 1
                continue
            app_name = pol.get("app", key)
            if pol.get("action") == "THROTTLE":
                try:
                    self.packet_shaper.remove_limit(app_name)
                except TypeError:
                    self.packet_shaper.remove_limit()
            else:
                self.packet_shaper.unblock_app(app_name)
            del self.active_policies[key]
            removed += 1

        if removed > 0:
            self.add_audit_log(
                source="Local User",
                action="LOCAL_CLEAR_ALL",
                target="*",
                status="SUCCESS",
                details=f"Đã gỡ {removed} chính sách cục bộ (Giữ nguyên {kept_admin} chính sách khóa bởi Admin).",
            )
            if self.on_policy_applied_locally:
                try:
                    self.on_policy_applied_locally("LOCAL_CLEAR_ALL", "*", "SUCCESS", 0.0)
                except Exception:
                    pass
        return removed, kept_admin
