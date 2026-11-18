"""
Bộ Tổng hợp Tiến trình, Làm giàu Kết nối TCP & Đồng bộ Tốc độ PacketShaper (Process Aggregator - Application Layer):
- Loại bỏ trùng lặp code (DRY) giữa MonitorService, ClientAgentService và FleetManagerService.
- Phân giải và cache PID -> (tên tiến trình, đường dẫn .exe).
- Làm giàu danh sách kết nối TCP thô thành từ điển hiển thị trên UI (kèm tùy chọn ghi FileLogger).
- Quy đổi byte ETW sang tốc độ từng tiến trình và đồng bộ tốc độ thực tế từ WinDivertPacketShaper.
"""

from typing import Any, Dict, List, Tuple
from models.network_protocol import ProcessMetricData
from utils.file_logger import FileLogger


def resolve_pid_info(
    collector: Any,
    proc_info_cache: Dict[int, Tuple[str, str]],
    pid: int,
    zero_pid_name: str = "System",
) -> Tuple[str, str]:
    """Tra cứu hoặc phân giải (process_name, executable_path) từ PID có sử dụng bộ nhớ đệm."""
    if pid in proc_info_cache:
        p_name, p_exe = proc_info_cache[pid]
        if p_exe or pid <= 0:
            return p_name, p_exe

    p_info = collector.get_process_info(pid) if collector else None
    if p_info:
        proc_name = p_info.name or (zero_pid_name if pid == 0 else f"PID {pid}")
        exe_path = p_info.executable_path or ""
    else:
        proc_name = zero_pid_name if pid == 0 else f"PID {pid}"
        exe_path = ""

    proc_info_cache[pid] = (proc_name, exe_path)
    return proc_name, exe_path


def resolve_executable_path_cached(
    collector: Any,
    proc_info_cache: Dict[int, Tuple[str, str]],
    pid: int,
) -> str:
    """Phân giải đường dẫn file .exe từ PID qua cache hoặc NetworkCollector."""
    if pid <= 0:
        return ""
    if pid in proc_info_cache:
        _, exe_path = proc_info_cache[pid]
        if exe_path:
            return exe_path
    _, exe_path = resolve_pid_info(collector, proc_info_cache, pid)
    return exe_path


def enrich_tcp_connections(
    collector: Any,
    proc_info_cache: Dict[int, Tuple[str, str]],
    log_to_file: bool = False,
) -> List[Dict[str, Any]]:
    """Lấy danh sách kết nối socket TCP từ collector và làm giàu tên tiến trình."""
    raw_conns = collector.collect_tcp_connections() if collector else []
    enriched: List[Dict[str, Any]] = []
    for conn in raw_conns:
        pid = conn.pid or 0
        if pid in proc_info_cache:
            proc_name, _ = proc_info_cache[pid]
        else:
            proc_name, _ = resolve_pid_info(collector, proc_info_cache, pid, zero_pid_name="System")

        if log_to_file:
            FileLogger.log_connection(
                proc_name=proc_name,
                pid=pid,
                protocol=conn.protocol,
                local_ip=conn.local_ip,
                local_port=conn.local_port,
                remote_ip=conn.remote_ip,
                remote_port=conn.remote_port,
                state=conn.state,
            )

        local_addr = f"{conn.local_ip}:{conn.local_port}" if conn.local_port else conn.local_ip
        if conn.remote_ip and conn.remote_port:
            remote_addr = f"{conn.remote_ip}:{conn.remote_port}"
        else:
            remote_addr = "*" if conn.state == "LISTEN" else "-"

        enriched.append(
            {
                "pid": pid,
                "process_name": proc_name,
                "protocol": conn.protocol,
                "local_addr": local_addr,
                "remote_addr": remote_addr,
                "state": conn.state,
            }
        )
    return enriched


def aggregate_etw_samples_to_dicts(
    etw_samples: Dict[int, Dict[str, int]],
    time_delta: float,
    collector: Any,
    proc_info_cache: Dict[int, Tuple[str, str]],
) -> Tuple[List[Dict[str, Any]], float, float]:
    """Quy đổi mẫu byte ETW theo PID thành danh sách dict tốc độ tiến trình."""
    proc_speeds: List[Dict[str, Any]] = []
    total_p_down = 0.0
    total_p_up = 0.0

    if not etw_samples:
        return proc_speeds, total_p_down, total_p_up

    for pid, b_info in etw_samples.items():
        rx = b_info.get("rx_bytes", 0)
        tx = b_info.get("tx_bytes", 0)
        down_kb = rx / (time_delta * 1024.0)
        up_kb = tx / (time_delta * 1024.0)
        total_p_down += down_kb
        total_p_up += up_kb

        if pid in proc_info_cache:
            p_name, p_exe = proc_info_cache[pid]
        else:
            p_name, p_exe = resolve_pid_info(collector, proc_info_cache, pid, zero_pid_name="PID 0")

        proc_speeds.append(
            {
                "pid": pid,
                "name": p_name,
                "exe": p_exe,
                "down_kb": down_kb,
                "up_kb": up_kb,
                "rx_bytes": rx,
                "tx_bytes": tx,
            }
        )

    return proc_speeds, total_p_down, total_p_up


def sync_shaper_speeds_to_proc_dicts(packet_shaper: Any, proc_speeds: List[Dict[str, Any]]):
    """Đồng bộ tốc độ thực tế từ PacketShaper vào danh sách dict tiến trình của MonitorService."""
    if not packet_shaper or not getattr(packet_shaper, "is_active", False):
        return

    shaper_status = packet_shaper.get_status()
    throttled_dict = shaper_status.get("throttled_apps", {})
    if throttled_dict:
        for app_k, app_info in throttled_dict.items():
            t_name = (app_info.get("target_app") or app_k).lower()
            t_base = t_name.replace(".exe", "").strip()
            s_down = app_info.get("current_down_kbps", 0.0)
            s_up = app_info.get("current_up_kbps", 0.0)
            s_dir = app_info.get("direction", "both")

            for p in proc_speeds:
                p_name = p.get("name", "").lower()
                if p_name == t_name or p_name.replace(".exe", "") == t_base or (t_base and t_base in p_name):
                    if s_dir in ("download", "both"):
                        p["down_kb"] = s_down
                    if s_dir in ("upload", "both"):
                        p["up_kb"] = s_up
    else:
        target_app = (shaper_status.get("target_app") or "").lower()
        target_base = target_app.replace(".exe", "").strip()
        shaper_down = shaper_status.get("current_down_kbps", 0.0)
        shaper_up = shaper_status.get("current_up_kbps", 0.0)
        shaper_dir = shaper_status.get("direction", "both")

        for p in proc_speeds:
            p_name = p.get("name", "").lower()
            if p_name == target_app or p_name.replace(".exe", "") == target_base or (target_base and target_base in p_name):
                if shaper_dir in ("download", "both"):
                    p["down_kb"] = shaper_down
                if shaper_dir in ("upload", "both"):
                    p["up_kb"] = shaper_up


def sync_shaper_speeds_to_metric_list(packet_shaper: Any, top_procs: List[ProcessMetricData]):
    """Đồng bộ tốc độ thực tế từ PacketShaper vào danh sách ProcessMetricData của ClientAgentService."""
    if not packet_shaper or not getattr(packet_shaper, "is_active", False):
        return

    try:
        shaper_status = packet_shaper.get_status()
        throttled_dict = shaper_status.get("throttled_apps", {})
        for app_k, app_info in throttled_dict.items():
            t_raw = app_info.get("target_app") or app_k
            t_name = t_raw.lower()
            t_base = t_name.replace(".exe", "").strip()
            s_down = round(float(app_info.get("current_down_kbps", 0.0)), 2)
            s_up = round(float(app_info.get("current_up_kbps", 0.0)), 2)
            s_dir = app_info.get("direction", "both")

            matched = False
            for p in top_procs:
                p_name = p.name.lower()
                if p_name == t_name or p_name.replace(".exe", "") == t_base:
                    if not matched:
                        if s_dir in ("download", "both"):
                            p.down_kbps = s_down
                        if s_dir in ("upload", "both"):
                            p.up_kbps = s_up
                        matched = True
                    else:
                        if s_dir in ("download", "both"):
                            p.down_kbps = 0.0
                        if s_dir in ("upload", "both"):
                            p.up_kbps = 0.0
            if not matched:
                top_procs.append(
                    ProcessMetricData(
                        name=t_raw,
                        pid=0,
                        down_kbps=s_down,
                        up_kbps=s_up,
                        connections_count=0,
                    )
                )
    except Exception:
        pass
