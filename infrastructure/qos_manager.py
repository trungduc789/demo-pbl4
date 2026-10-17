import re
import subprocess
from typing import Dict, List, Optional, Tuple
from utils.safe_print import safe_print as print


class WindowsQoSManager:
    """
    Quản lý chính sách giới hạn băng thông Windows QoS (Quality of Service):
    - Áp dụng chính sách giới hạn tốc độ Upload qua PowerShell (New-NetQosPolicy).
    - Đo và đối chiếu tốc độ tức thời với giới hạn chính sách.
    - Gỡ bỏ chính sách theo từng tiến trình hoặc dọn dẹp toàn bộ khi dừng ứng dụng.
    - Kiểm tra trạng thái xác minh chính sách trong hệ điều hành.
    """

    POLICY_PREFIX = "PBL4_QoS_"

    def __init__(self):
        self.active_policies: Dict[str, float] = {}  # {app_name: limit_kbps}

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Chuẩn hóa tên tiến trình để làm tên policy Windows QoS an toàn."""
        clean = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        return clean[:30]

    def _run_ps_command(self, cmd: str) -> Tuple[int, str, str]:
        """Chạy lệnh PowerShell không hiển thị cửa sổ console."""
        try:
            startupinfo = None
            if hasattr(subprocess, 'STARTUPINFO'):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=8,
                startupinfo=startupinfo
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    @staticmethod
    def is_admin() -> bool:
        """Kiểm tra quyền Administrator trên hệ điều hành Windows."""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def _ensure_nla_configured(self):
        """
        Đảm bảo Windows thực thi QoS trên các máy cá nhân / WiFi không thuộc Active Directory Domain.
        Ghi registry: HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\QoS -> 'Do not use NLA' = '1'
        """
        cmd = """
        $key = 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\QoS'
        if (-not (Test-Path $key)) {
            New-Item -Path $key -Force | Out-Null
        }
        $val = (Get-ItemProperty -Path $key -Name 'Do not use NLA' -ErrorAction SilentlyContinue).'Do not use NLA'
        if ($val -ne '1') {
            Set-ItemProperty -Path $key -Name 'Do not use NLA' -Value '1' -Type String -Force
        }
        """
        self._run_ps_command(cmd)

    def apply_upload_limit(self, app_name: str, limit_kbps: float) -> Tuple[bool, str]:
        """
        Áp dụng chính sách Windows QoS để giới hạn tốc độ Upload (Outbound) cho tiến trình.
        - app_name: Tên file thực thi (ví dụ: 'chrome.exe', 'python.exe')
        - limit_kbps: Giới hạn tốc độ tải lên tính bằng KB/s (Kilobytes per second)
        """
        if not self.is_admin():
            user_msg = "Quyền bị từ chối: Cần chạy ứng dụng với quyền Administrator (chuột phải chọn 'Run as administrator') để Windows cho phép tạo chính sách QoS."
            print(f"[QoS Error] {user_msg}")
            return False, user_msg

        if limit_kbps <= 0:
            return False, "Giới hạn upload phải lớn hơn 0 KB/s."

        # Cấu hình NLA để Windows không bỏ qua QoS trên mạng Home/Workgroup WiFi
        self._ensure_nla_configured()

        # Đảm bảo app_name có đuôi .exe nếu chưa có
        target_app = app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe"
        safe_name = self._sanitize_name(target_app)
        policy_name = f"{self.POLICY_PREFIX}{safe_name}"

        # Quy đổi KB/s sang bits per second (Bps)
        # 1 KB = 1024 Bytes, 1 Byte = 8 bits
        throttle_bps = int(limit_kbps * 1024 * 8)

        # Lệnh PowerShell kiểm tra nếu đã tồn tại thì cập nhật (Set), chưa có thì tạo mới (New)
        # Thêm -NetworkProfile All để áp dụng cho tất cả mạng (Private/Public WiFi & Ethernet)
        ps_cmd = f"""
        $existing = Get-NetQosPolicy -Name '{policy_name}' -ErrorAction SilentlyContinue
        if ($existing) {{
            Set-NetQosPolicy -Name '{policy_name}' -ThrottleRateActionBitsPerSecond {throttle_bps} -NetworkProfile All -Confirm:$false
            Write-Output "UPDATED"
        }} else {{
            New-NetQosPolicy -Name '{policy_name}' -AppPathNameMatchCondition '{target_app}' -ThrottleRateActionBitsPerSecond {throttle_bps} -NetworkProfile All -Confirm:$false
            Write-Output "CREATED"
        }}
        """

        ret_code, stdout, stderr = self._run_ps_command(ps_cmd)
        if ret_code == 0 and ("CREATED" in stdout or "UPDATED" in stdout):
            self.active_policies[target_app] = limit_kbps
            action_str = "Cập nhật" if "UPDATED" in stdout else "Thiết lập mới"
            msg = f"Đã {action_str} chính sách QoS '{policy_name}' cho '{target_app}' giới hạn Upload {limit_kbps:,.1f} KB/s ({throttle_bps:,} bps)."
            print(f"[QoS] {msg}")
            return True, msg
        else:
            err_msg = stderr or stdout
            if any(kw in err_msg for kw in ["Access to a CIM resource", "Access is denied", "PermissionDenied", "CimException", "UnauthorizedAccessException"]):
                user_msg = "Quyền bị từ chối: Cần chạy ứng dụng dưới quyền Administrator ('Run as administrator') để tạo chính sách Windows QoS."
            else:
                user_msg = f"Không thể áp dụng chính sách QoS: {err_msg}"
            print(f"[QoS Error] {user_msg}")
            return False, user_msg

    def remove_upload_limit(self, app_name: str) -> Tuple[bool, str]:
        """
        Gỡ bỏ chính sách QoS của một tiến trình cụ thể.
        """
        target_app = app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe"
        safe_name = self._sanitize_name(target_app)
        policy_name = f"{self.POLICY_PREFIX}{safe_name}"

        ps_cmd = f"Remove-NetQosPolicy -Name '{policy_name}' -Confirm:$false -ErrorAction SilentlyContinue"
        ret_code, stdout, stderr = self._run_ps_command(ps_cmd)

        self.active_policies.pop(target_app, None)
        # Xác minh lại xem policy đã biến mất chưa
        is_gone = self.verify_policy_removed(target_app)
        if is_gone:
            msg = f"Đã gỡ bỏ chính sách QoS '{policy_name}' thành công. Đã xác minh chính sách không còn hiệu lực."
            print(f"[QoS] {msg}")
            return True, msg
        else:
            msg = f"Gỡ bỏ chính sách '{policy_name}' thất bại: {stderr or stdout}"
            print(f"[QoS Warning] {msg}")
            return False, msg

    def get_policy_status(self, app_name: str) -> Optional[Dict]:
        """
        Kiểm tra trạng thái chính sách QoS hiện hành của một ứng dụng từ hệ thống Windows.
        """
        target_app = app_name if app_name.lower().endswith(".exe") else f"{app_name}.exe"
        safe_name = self._sanitize_name(target_app)
        policy_name = f"{self.POLICY_PREFIX}{safe_name}"

        ps_cmd = f"""
        $p = Get-NetQosPolicy -Name '{policy_name}' -ErrorAction SilentlyContinue
        if ($p) {{
            Write-Output "$($p.Name)|$($p.AppPathNameMatchCondition)|$($p.ThrottleRateActionBitsPerSecond)"
        }}
        """
        ret_code, stdout, _ = self._run_ps_command(ps_cmd)
        if ret_code == 0 and stdout:
            parts = stdout.split("|")
            if len(parts) >= 3:
                try:
                    bps = int(parts[2])
                    limit_kbps = bps / (1024 * 8)
                except ValueError:
                    bps = 0
                    limit_kbps = 0.0

                return {
                    "policy_name": parts[0],
                    "app_match": parts[1],
                    "throttle_bps": bps,
                    "limit_kbps": limit_kbps,
                    "is_active": True
                }
        return None

    def verify_policy_removed(self, app_name: str) -> bool:
        """
        Kiểm tra và xác minh trạng thái: Trả về True nếu chính sách QoS KHÔNG CÒN tồn tại trên Windows.
        """
        status = self.get_policy_status(app_name)
        return status is None

    def remove_all_pbl4_policies(self) -> Tuple[bool, str]:
        """
        Gỡ bỏ toàn bộ tất cả chính sách QoS do ứng dụng PBL4 tạo ra và xác minh trạng thái sau khi dừng.
        """
        ps_cmd = f"""
        $policies = Get-NetQosPolicy -Name '{self.POLICY_PREFIX}*' -ErrorAction SilentlyContinue
        if ($policies) {{
            $policies | Remove-NetQosPolicy -Confirm:$false
            Write-Output "REMOVED_ALL"
        }} else {{
            Write-Output "NONE_FOUND"
        }}
        """
        ret_code, stdout, stderr = self._run_ps_command(ps_cmd)
        self.active_policies.clear()

        # Kiểm tra xác minh sau khi dọn dẹp
        check_cmd = f"(Get-NetQosPolicy -Name '{self.POLICY_PREFIX}*' -ErrorAction SilentlyContinue).Count"
        _, check_out, _ = self._run_ps_command(check_cmd)
        remaining = 0
        try:
            remaining = int(check_out.strip() or "0")
        except ValueError:
            remaining = 0

        if remaining == 0:
            msg = "Đã gỡ bỏ toàn bộ chính sách Windows QoS của ứng dụng và xác nhận trạng thái sạch sẽ (0 policy còn lại)."
            print(f"[QoS Cleanup] {msg}")
            return True, msg
        else:
            msg = f"Vẫn còn {remaining} chính sách QoS chưa được xóa sạch: {stderr}"
            print(f"[QoS Cleanup Warning] {msg}")
            return False, msg
