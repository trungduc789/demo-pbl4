"""
Bộ Theo dõi Ánh xạ Cổng Socket sang Tiến trình & Quản lý TTL (Port Tracker - Infrastructure Layer):
- Quét toàn bộ kết nối inet của hệ thống trong 1 lời gọi syscall O(1) (có fallback theo từng PID).
- Ánh xạ cổng local sang ứng dụng bị bóp băng thông (Throttle) hoặc bị chặn (Block).
- Thu hồi các cổng đã đóng sau PORT_TTL_SECONDS (5s) để chống nhận nhầm tiến trình.
"""

from __future__ import annotations
import time
import threading
from typing import Callable, Dict, List, Set

import psutil
from infrastructure.shaper_models import PORT_TTL_SECONDS


def scan_active_ports_for_pids(
    throttled_pids: Dict[int, str],
    blocked_pids: Dict[int, str],
) -> tuple[Dict[int, str], Set[int]]:
    """
    Quét toàn bộ socket của hệ thống trong 1 lời gọi duy nhất (O(1) syscall)
    và lọc theo tập PID mục tiêu. Fallback về quét từng PID nếu hệ điều hành hạn chế.
    """
    active_throttled_ports: Dict[int, str] = {}
    active_blocked_ports: Set[int] = set()

    try:
        for conn in psutil.net_connections(kind="inet"):
            pid = conn.pid
            if not pid or not conn.laddr:
                continue
            port = conn.laddr.port
            app_k = throttled_pids.get(pid)
            if app_k is not None:
                active_throttled_ports[port] = app_k
            if pid in blocked_pids:
                active_blocked_ports.add(port)
    except Exception:
        for pid, app_k in throttled_pids.items():
            try:
                for conn in psutil.Process(pid).net_connections(kind="all"):
                    if conn.laddr:
                        active_throttled_ports[conn.laddr.port] = app_k
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for pid in blocked_pids.keys():
            try:
                for conn in psutil.Process(pid).net_connections(kind="all"):
                    if conn.laddr:
                        active_blocked_ports.add(conn.laddr.port)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    return active_throttled_ports, active_blocked_ports


def update_shaper_target_ports(
    shaper: object,
    find_pids_fn: Callable[[str], Set[int]],
):
    """
    Cập nhật danh bạ PID và Port đang bị quản lý (Throttle / Block) trên đối tượng shaper,
    đồng thời thu hồi các cổng đã đóng quá thời hạn PORT_TTL_SECONDS.
    """
    lock = getattr(shaper, "_lock")
    with lock:
        throttled_keys: List[str] = list(getattr(shaper, "throttled_apps").keys())
        blocked_keys: List[str] = list(getattr(shaper, "blocked_apps").keys())

    if not throttled_keys and not blocked_keys:
        with lock:
            getattr(shaper, "target_pids").clear()
            getattr(shaper, "target_ports").clear()
            getattr(shaper, "blocked_pids").clear()
            getattr(shaper, "blocked_ports").clear()
            getattr(shaper, "port_to_throttle_app").clear()
            getattr(shaper, "_port_last_seen").clear()
        return

    new_throttled_pids: Dict[int, str] = {}
    for t_key in throttled_keys:
        for pid in find_pids_fn(t_key):
            new_throttled_pids[pid] = t_key

    new_blocked_pids: Dict[int, str] = {}
    for b_key in blocked_keys:
        for pid in find_pids_fn(b_key):
            new_blocked_pids[pid] = b_key

    now = time.time()
    active_throttled_ports, active_blocked_ports = scan_active_ports_for_pids(
        new_throttled_pids, new_blocked_pids
    )

    with lock:
        setattr(shaper, "target_pids", set(new_throttled_pids.keys()))
        setattr(shaper, "blocked_pids", set(new_blocked_pids.keys()))

        port_last_seen: Dict[int, float] = getattr(shaper, "_port_last_seen")
        port_to_app: Dict[int, str] = getattr(shaper, "port_to_throttle_app")

        for port, app_k in active_throttled_ports.items():
            port_last_seen[port] = now
            port_to_app[port] = app_k

        expired_ports = [
            p
            for p, t in port_last_seen.items()
            if (p not in active_throttled_ports) and (now - t > PORT_TTL_SECONDS)
        ]
        for p in expired_ports:
            del port_last_seen[p]
            port_to_app.pop(p, None)

        setattr(shaper, "target_ports", set(port_to_app.keys()))
        setattr(shaper, "blocked_ports", active_blocked_ports)


def run_port_monitor_loop(
    stop_event: threading.Event,
    update_ports_fn: Callable[[], None],
    interval_sec: float = 0.1,
):
    """Luồng quét danh bạ cổng định kỳ (100ms) trên tập PID mục tiêu."""
    while not stop_event.is_set():
        try:
            update_ports_fn()
        except Exception:
            pass
        stop_event.wait(interval_sec)
