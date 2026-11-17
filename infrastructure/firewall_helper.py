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


def remove_windows_firewall_rule(app_name: str) -> bool:
    rule_name = clean_rule_name(app_name)
    try:
        subprocess.run(
            f'netsh advfirewall firewall delete rule name="{rule_name}_Out"',
            shell=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        subprocess.run(
            f'netsh advfirewall firewall delete rule name="{rule_name}_In"',
            shell=True,
            capture_output=True,
            text=True,
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
        r1 = subprocess.run(cmd_out, shell=True, capture_output=True, text=True, timeout=3)
        r2 = subprocess.run(cmd_in, shell=True, capture_output=True, text=True, timeout=3)
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
