"""
Các Mô hình Chính sách Bóp Băng Thông / Chặn Ứng dụng & Bộ Phân giải Tên Tiến trình (Shaper Models):
- AppThrottlePolicy: Trạng thái Token Bucket, hàng đợi và thống kê riêng cho từng ứng dụng.
- AppBlockPolicy: Thông tin ứng dụng bị chặn hoàn toàn.
- resolve_target_name_and_exe: Phân giải tên hiển thị thân thiện sang tên file .exe, đường dẫn và PID.
"""

from __future__ import annotations
import os
import time
import queue
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple
import psutil

PORT_TTL_SECONDS = 5.0
MAX_PER_APP_QUEUE_SIZE = 4096

ALIAS_MAP: Dict[str, str] = {
    "google chrome": "chrome",
    "microsoft edge": "msedge",
    "brave browser": "brave",
    "riot client": "riotclientux",
    "windows explorer": "explorer",
    "discord": "discord",
    "telegram": "telegram",
    "zalo": "zalo",
}


@dataclass
class AppThrottlePolicy:
    """Chính sách bóp băng thông độc lập cho một ứng dụng."""

    app_key: str
    target_app: str
    limit_kbps: float
    direction: str = "both"
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.perf_counter)
    queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=MAX_PER_APP_QUEUE_SIZE))
    stop_event: threading.Event = field(default_factory=threading.Event)
    worker_thread: Optional[threading.Thread] = None

    total_throttled_bytes: int = 0
    total_throttled_packets: int = 0
    total_down_bytes: int = 0
    total_up_bytes: int = 0
    _recent_down_bytes: int = 0
    _recent_up_bytes: int = 0
    _recent_ts: float = field(default_factory=time.time)
    current_speed_kbps: float = 0.0
    current_down_kbps: float = 0.0
    current_up_kbps: float = 0.0

    def update_speed_stats(self, packet_size: int, is_inbound: bool):
        now = time.time()
        if packet_size > 0:
            self.total_throttled_bytes += packet_size
            self.total_throttled_packets += 1
            if is_inbound:
                self.total_down_bytes += packet_size
                self._recent_down_bytes += packet_size
            else:
                self.total_up_bytes += packet_size
                self._recent_up_bytes += packet_size

        dt = now - self._recent_ts
        if dt >= 0.5:
            self.current_down_kbps = (self._recent_down_bytes / 1024.0) / dt
            self.current_up_kbps = (self._recent_up_bytes / 1024.0) / dt
            self.current_speed_kbps = ((self._recent_down_bytes + self._recent_up_bytes) / 1024.0) / dt
            self._recent_down_bytes = 0
            self._recent_up_bytes = 0
            self._recent_ts = now


@dataclass
class AppBlockPolicy:
    """Chính sách chặn hoàn toàn một ứng dụng."""

    app_key: str
    target_app: str
    direction: str = "both"
    exe_path: str = ""
    pids: Set[int] = field(default_factory=set)
    firewall_rule_name: str = ""


def resolve_target_name_and_exe(
    app_name: str,
    provided_exe: str = "",
    alias_map: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, Set[int]]:
    """Phân giải tên hiển thị thân thiện sang tên file .exe thật, đường dẫn thực thi và danh sách PID."""
    if not app_name:
        return "", "", set()

    amap = alias_map if alias_map is not None else ALIAS_MAP
    raw_lower = app_name.lower().strip()
    clean = raw_lower.replace(".exe", "").strip()
    alias = amap.get(clean, clean)

    exact_pids: Set[int] = set()
    sub_pids: Set[int] = set()
    found_exe = provided_exe if (provided_exe and os.path.exists(provided_exe)) else ""
    exact_name = ""
    substring_name = ""

    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            p_name_raw = p.info.get("name") or ""
            p_name = p_name_raw.lower()
            p_exe = p.info.get("exe") or ""
            p_exe_lower = p_exe.lower()
            p_base = p_name.replace(".exe", "")

            is_exact = (
                p_name == raw_lower
                or p_name == f"{alias}.exe"
                or p_base == clean
                or p_base == alias
            )
            is_sub = bool(alias and len(alias) >= 4 and (alias in p_name or alias in p_exe_lower))

            if is_exact:
                exact_pids.add(p.info["pid"])
                if p_exe and os.path.exists(p_exe):
                    found_exe = p_exe
                if not exact_name:
                    exact_name = p_name_raw
            elif is_sub:
                sub_pids.add(p.info["pid"])
                if p_exe and os.path.exists(p_exe) and not found_exe:
                    found_exe = p_exe
                if not substring_name:
                    substring_name = p_name_raw
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    found_pids = exact_pids if exact_pids else sub_pids

    if exact_name:
        canonical_name = exact_name
    elif raw_lower.endswith(".exe") or clean in amap:
        canonical_name = f"{alias}.exe" if not raw_lower.endswith(".exe") else app_name
    else:
        canonical_name = substring_name or f"{alias}.exe"

    return canonical_name, found_exe, found_pids
