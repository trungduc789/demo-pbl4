"""
Bộ Quản lý Quy tắc Windows Defender Firewall & Đóng Kết nối TCP (Firewall Helper - Infrastructure Layer):
- Thực thi thêm / gỡ quy tắc netsh advfirewall cho ứng dụng bị chặn.
- Cắt đứt ngay lập tức các kết nối TCP đang mở của tiến trình qua SetTcpEntry (iphlpapi).
"""

import os
import subprocess
from typing import Set
import psutil
from utils.safe_print import safe_print as print


def clean_rule_name(app_name: str) -> str:
    raw = app_name.lower().strip().replace(".exe", "")
    return f"PBL4_Block_{raw}"


def remove_all_pbl4_firewall_rules() -> int:
    """
    Quét và gỡ bỏ toàn bộ các quy tắc tường lửa có tiền tố 'PBL4_Block_' đang tồn tại
    trong Windows Defender Firewall (phòng trường hợp tiến trình trước thoát đột ngột).
    """
    removed_count = 0
    try:
        res = subprocess.run(
            "netsh advfirewall firewall show rule name=all",
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
        if res.returncode == 0 and res.stdout:
            rule_names = set()
            for line in res.stdout.splitlines():
                if "PBL4_Block_" in line:
                    parts = line.split(":", 1)
                    rname = parts[1].strip() if len(parts) > 1 else line.strip()
                    if rname.startswith("PBL4_Block_"):
                        rule_names.add(rname)
            for rname in rule_names:
                subprocess.run(
                    f'netsh advfirewall firewall delete rule name="{rname}"',
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=3,
                )
                removed_count += 1
    except Exception:
        pass
    return removed_count


def remove_windows_firewall_rule(app_name: str) -> bool:
    rule_name = clean_rule_name(app_name)
    raw = app_name.lower().strip().replace(".exe", "")
    candidates = {f"{rule_name}_Out", f"{rule_name}_In", rule_name}
    if raw:
        candidates.add(f"PBL4_Block_{raw}_Out")
        candidates.add(f"PBL4_Block_{raw}_In")
    try:
        for rname in candidates:
            subprocess.run(
                f'netsh advfirewall firewall delete rule name="{rname}"',
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
            )
        return True
    except Exception:
        return False


def apply_windows_firewall_rule(app_name: str, exe_path: str = "", action: str = "block") -> bool:
    rule_name = clean_rule_name(app_name)
    if not exe_path or not os.path.exists(exe_path):
        target_lower = app_name.lower().strip()
        for p in psutil.process_iter(["name", "exe"]):
            try:
                p_name = (p.info.get("name") or "").lower()
                if p_name == target_lower or p_name.replace(".exe", "") == target_lower.replace(".exe", ""):
                    cand = p.info.get("exe", "")
                    if cand and os.path.exists(cand):
                        exe_path = cand
                        break
            except Exception:
                pass

    if not exe_path or not os.path.exists(exe_path):
        print(
            f"[Firewall Rule Warning] Chưa có đường dẫn .exe tuyệt đối của '{app_name}', "
            "WinDivert Kernel Dropper sẽ đảm nhiệm chặn toàn bộ gói tin."
        )
        return False

    remove_windows_firewall_rule(app_name)

    prog_param = f'program="{exe_path}"'
    cmd_out = f'netsh advfirewall firewall add rule name="{rule_name}_Out" dir=out action={action} {prog_param} enable=yes'
    cmd_in = f'netsh advfirewall firewall add rule name="{rule_name}_In" dir=in action={action} {prog_param} enable=yes'
    try:
        r1 = subprocess.run(cmd_out, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=3)
        r2 = subprocess.run(cmd_in, shell=True, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=3)
        return r1.returncode == 0 and r2.returncode == 0
    except Exception as e:
        print(f"[Firewall Rule Warning] {e}")
        return False


def terminate_app_sockets(app_pids: Set[int]):
    """Cắt đứt ngay lập tức các kết nối TCP hiện có của ứng dụng bằng SetTcpEntry."""
    try:
        import ctypes
        import socket
        from ctypes import wintypes

        class MIB_TCPROW(ctypes.Structure):
            _fields_ = [
                ("dwState", wintypes.DWORD),
                ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD),
            ]

        MIB_TCP_STATE_DELETE_TCB = 12
        for pid in app_pids:
            try:
                for conn in psutil.Process(pid).net_connections(kind="tcp"):
                    if conn.status == "ESTABLISHED" and conn.laddr and conn.raddr:
                        row = MIB_TCPROW()
                        row.dwState = MIB_TCP_STATE_DELETE_TCB
                        try:
                            row.dwLocalAddr = int.from_bytes(socket.inet_aton(conn.laddr.ip), "little")
                            row.dwLocalPort = socket.htons(conn.laddr.port)
                            row.dwRemoteAddr = int.from_bytes(socket.inet_aton(conn.raddr.ip), "little")
                            row.dwRemotePort = socket.htons(conn.raddr.port)
                            ctypes.windll.iphlpapi.SetTcpEntry(ctypes.byref(row))
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass
