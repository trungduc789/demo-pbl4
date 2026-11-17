"""
Các hàm tính toán và xử lý dữ liệu thuần túy (Pure Functions - Không phụ thuộc PyQt6):
1. Định dạng đơn vị dung lượng & tốc độ mạng (Formatters).
2. Thuật toán làm mượt sóng lưu lượng (Gaussian Filtering + Monotonic Cubic Hermite PCHIP) & Thang đo trần (Hysteresis).
3. Tổng hợp lưu lượng theo khung thời gian trượt (Sliding Window Aggregation) & Tra cứu mẫu theo mốc thời gian.
4. Phân loại địa chỉ IP WAN / LAN, Vùng mạng và Giao thức theo cổng kết nối.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import numpy as np


# =============================================================================
# 1. CẤU HÌNH THANG THỜI GIAN & THANG ĐO TRẦN CHUẨN GLASSWIRE
# =============================================================================

TIME_SCALE_CONFIGS: Dict[float, Dict[str, Any]] = {
    30.0: {
        "label": "30 seconds",
        "step": 10,
        "fmt": "%H:%M:%S",
        "sigma": 1.1,
        "radius_seconds": 3,
        "dense_points": 700,
    },
    60.0: {
        "label": "1 minute",
        "step": 15,
        "fmt": "%H:%M:%S",
        "sigma": 1.5,
        "radius_seconds": 4,
        "dense_points": 750,
    },
    300.0: {
        "label": "5 minutes",
        "step": 60,
        "fmt": "%H:%M",
        "sigma": 2.4,
        "radius_seconds": 7,
        "dense_points": 850,
    },
    600.0: {
        "label": "10 minutes",
        "step": 120,
        "fmt": "%H:%M",
        "sigma": 3.2,
        "radius_seconds": 10,
        "dense_points": 900,
    },
    3600.0: {
        "label": "1 hour",
        "step": 600,
        "fmt": "%H:%M",
        "sigma": 5.0,
        "radius_seconds": 15,
        "dense_points": 950,
    },
    86400.0: {
        "label": "24 hours",
        "step": 14400,
        "fmt": "%H:%M",
        "sigma": 8.0,
        "radius_seconds": 30,
        "dense_points": 1000,
    },
}

CEILING_TIERS_KB: List[float] = [
    50.0,
    100.0,
    250.0,
    500.0,
    1024.0,       # 1 MB
    2048.0,       # 2 MB
    3072.0,       # 3 MB
    5120.0,       # 5 MB
    10240.0,      # 10 MB
    25600.0,      # 25 MB
    51200.0,      # 50 MB
    102400.0,     # 100 MB
]


# =============================================================================
# 2. HÀM ĐỊNH DẠNG DUNG LƯỢNG & TỐC ĐỘ (FORMATTERS)
# =============================================================================

def format_bytes(num_bytes: float) -> str:
    """Quy đổi số byte sang chuỗi B, KB, MB, GB dễ đọc chuẩn GlassWire."""
    val = max(0.0, float(num_bytes))
    if val < 1024.0:
        return f"{val:,.0f} B"
    if val < 1024.0 * 1024.0:
        return f"{val / 1024.0:,.1f} KB"
    if val < 1024.0 * 1024.0 * 1024.0:
        return f"{val / (1024.0 * 1024.0):,.1f} MB"
    return f"{val / (1024.0 * 1024.0 * 1024.0):,.2f} GB"


def format_speed(speed_kb: float) -> str:
    """Quy đổi tốc độ KB/s sang chuỗi B/s, KB/s, MB/s."""
    val = max(0.0, float(speed_kb))
    if val <= 0.0:
        return "0 B/s"
    if val < 1.0:
        return f"{val * 1024.0:,.0f} B/s"
    if val < 1024.0:
        return f"{val:,.1f} KB/s"
    return f"{val / 1024.0:,.1f} MB/s"


def format_ceiling_label(target_tier_kb: float) -> str:
    """Định dạng nhãn trần trục Y (ví dụ '500 KB ▾' hoặc '10 MB ▾')."""
    if target_tier_kb >= 1024.0:
        mb_val = target_tier_kb / 1024.0
        if mb_val.is_integer():
            str_val = f"{int(mb_val)} MB"
        else:
            str_val = f"{mb_val:,.1f} MB"
    else:
        str_val = f"{int(target_tier_kb)} KB"
    return f"{str_val} ▾"


# =============================================================================
# 3. THUẬT TOÁN LÀM MƯỢT SÓNG & CHỐNG RUNG THANG ĐO (SMOOTHING & HYSTERESIS)
# =============================================================================

def select_y_ceiling_tier(
    peak_val_kb: float,
    current_y_max: float,
    now_ts: float,
    hold_until_ts: float,
    hold_duration_sec: float = 8.0,
) -> Tuple[float, float, bool]:
    """
    Tính toán mức trần trục Y mới có cơ chế chống rung nhảy (Hysteresis Hold-Down Timer):
    - Khi lưu lượng đỉnh vượt trần: Mở rộng ngay lập tức và giữ ít nhất `hold_duration_sec` giây.
    - Khi lưu lượng giảm: Chỉ hạ trần sau khi đã qua thời gian giữ `hold_until_ts`.
    Trả về: (new_y_max, new_hold_until_ts, changed)
    """
    target_tier = CEILING_TIERS_KB[-1]
    for tier in CEILING_TIERS_KB:
        if tier >= peak_val_kb * 1.15:
            target_tier = tier
            break

    if target_tier > current_y_max:
        return target_tier, now_ts + hold_duration_sec, True
    if target_tier < current_y_max and now_ts >= hold_until_ts:
        return target_tier, hold_until_ts, True
    return current_y_max, hold_until_ts, False


def generate_glasswire_wave(
    x_raw: np.ndarray,
    y_raw: np.ndarray,
    x_dense: np.ndarray,
    window_duration: float,
) -> np.ndarray:
    """
    Nội suy sóng GlassWire siêu mượt mà không có góc nhọn hay đoạn thẳng:
    1. Lọc Gauss làm mềm các xung raw theo tham số riêng biệt của từng mốc thời gian (30s / 1m / 5m / 10m).
    2. Nội suy Hermite khối (Cubic Hermite PCHIP) trực tiếp lên lưới x_dense dày mịn:
       - Tiếp tuyến tại đỉnh (apex) = 0.0 -> Đỉnh luôn là vòm tròn (dome).
       - Tiếp tuyến tại đáy (valley) = 0.0 -> Đáy cong tròn (round trough).
       - Tiếp tuyến tại trục nền = 0.0 -> Sóng rời mặt đất nằm ngang êm ái.
    """
    try:
        if len(x_raw) < 2:
            return np.zeros_like(x_dense)

        cfg = TIME_SCALE_CONFIGS.get(window_duration, TIME_SCALE_CONFIGS[300.0])
        sigma = float(cfg["sigma"])
        radius = int(cfg["radius_seconds"])

        t_span = max(1.0, float(x_dense[-1] - x_dense[0]))
        t_pad = max(float(radius * 2.0), t_span * 0.05)
        t_min = x_dense[0] - t_pad
        t_max = x_dense[-1] + t_pad
        mask = (x_raw >= t_min) & (x_raw <= t_max)
        x_sub = x_raw[mask]
        y_sub = y_raw[mask]

        if len(x_sub) < 2:
            return np.zeros_like(x_dense)

        ux, uidx = np.unique(x_sub, return_index=True)
        uy = y_sub[uidx]
        n_pts = len(ux)
        if n_pts < 2:
            return np.zeros_like(x_dense)

        eff_radius = min(radius, max(1, (n_pts - 1) // 2))

        if eff_radius >= 1 and n_pts > 3:
            kx = np.arange(-eff_radius, eff_radius + 1)
            kernel = np.exp(-0.5 * (kx / sigma) ** 2)
            kernel /= np.sum(kernel)
            decay = uy[-1] * np.exp(-np.arange(1, eff_radius + 1) / 2.0)
            left_pad = np.full(eff_radius, uy[0])
            uy_padded = np.concatenate([left_pad, uy, decay])
            uy_smooth = np.convolve(uy_padded, kernel, mode="same")[eff_radius:-eff_radius]
        else:
            uy_smooth = uy.copy()

        h = np.diff(ux)
        h = np.where(h == 0, 1e-6, h)
        delta = np.diff(uy_smooth) / h

        d = np.zeros(n_pts)
        d[0] = delta[0]
        d[-1] = delta[-1]
        for i in range(1, n_pts - 1):
            if delta[i - 1] * delta[i] > 0:
                d[i] = 2.0 / (1.0 / delta[i - 1] + 1.0 / delta[i])
            else:
                d[i] = 0.0

        for i in range(n_pts):
            if uy_smooth[i] <= 0.05:
                d[i] = 0.0

        idx = np.searchsorted(ux, x_dense) - 1
        idx = np.clip(idx, 0, n_pts - 2)

        x0 = ux[idx]
        x1 = ux[idx + 1]
        y0 = uy_smooth[idx]
        y1 = uy_smooth[idx + 1]
        d0 = d[idx]
        d1 = d[idx + 1]

        hi = np.maximum(1e-6, x1 - x0)
        u = np.clip((x_dense - x0) / hi, 0.0, 1.0)
        u2 = u * u
        u3 = u2 * u

        h00 = 2 * u3 - 3 * u2 + 1
        h10 = u3 - 2 * u2 + u
        h01 = -2 * u3 + 3 * u2
        h11 = u3 - u2

        y_res = h00 * y0 + h10 * hi * d0 + h01 * y1 + h11 * hi * d1
        y_res = np.where((x_dense < ux[0]) | (x_dense > ux[-1]), 0.0, y_res)
        return np.nan_to_num(np.maximum(0.0, y_res), nan=0.0)
    except Exception:
        try:
            return np.nan_to_num(np.interp(x_dense, x_raw, y_raw, left=0.0, right=0.0), nan=0.0)
        except Exception:
            return np.zeros_like(x_dense)


# =============================================================================
# 4. TỔNG HỢP CỬA SỔ DỮ LIỆU & TÌM MẪU GẦN NHẤT (WINDOW AGGREGATION)
# =============================================================================

def compute_window_traffic(
    window_sample_history: Sequence[Dict[str, Any]],
    win_duration: float,
    anchor_ts: float,
    fallback_down: float = 0.0,
    fallback_up: float = 0.0,
    fallback_apps: Optional[Dict[str, Dict[str, float]]] = None,
) -> Tuple[float, float, Dict[str, Dict[str, float]]]:
    """
    Tính tổng lưu lượng download, upload toàn hệ thống và theo từng tiến trình
    trong khung thời gian [anchor_ts - win_duration, anchor_ts].
    """
    if not window_sample_history:
        return fallback_down, fallback_up, (fallback_apps if fallback_apps is not None else {})

    cutoff = anchor_ts - win_duration
    tot_down = 0.0
    tot_up = 0.0
    app_bytes: Dict[str, Dict[str, float]] = {}

    for s in reversed(window_sample_history):
        st = s.get("t", 0.0)
        if st < cutoff:
            break
        tot_down += s.get("down_b", 0.0)
        tot_up += s.get("up_b", 0.0)
        for pname, (pd_b, pu_b) in s.get("procs", {}).items():
            if pname not in app_bytes:
                app_bytes[pname] = {"down": 0.0, "up": 0.0}
            app_bytes[pname]["down"] += pd_b
            app_bytes[pname]["up"] += pu_b

    return tot_down, tot_up, app_bytes


def find_nearest_sample(
    point_history: Iterable[Dict[str, Any]],
    down_points: Sequence[Tuple[float, float]],
    up_points: Sequence[Tuple[float, float]],
    target_t: float,
) -> Optional[Dict[str, Any]]:
    """Tìm mẫu lưu lượng gần nhất với mốc thời gian target_t bằng tìm kiếm nhị phân O(log N)."""
    source = list(point_history)
    if not source:
        if down_points:
            pts = list(down_points)
            best_pt = min(pts, key=lambda p: abs(p[0] - target_t))
            return {
                "timestamp": best_pt[0],
                "down_kb": best_pt[1],
                "up_kb": 0.0,
                "top_procs": [],
            }
        return None

    low = 0
    high = len(source) - 1
    best_item = source[0]
    min_diff = abs(source[0]["timestamp"] - target_t)

    while low <= high:
        mid = (low + high) // 2
        item = source[mid]
        t = item["timestamp"]
        diff = abs(t - target_t)
        if diff < min_diff:
            min_diff = diff
            best_item = item

        if t < target_t:
            low = mid + 1
        else:
            high = mid - 1

    if min_diff <= 60.0:
        return best_item
    return None


# =============================================================================
# 5. PHÂN LOẠI WAN / LAN, KHU VỰC MẠNG & GIAO THỨC (NETWORK CLASSIFICATION)
# =============================================================================

PRIVATE_IP_PREFIXES = (
    "10.",
    "192.168.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.2",
    "172.30.",
    "172.31.",
    "fe80:",
)


def is_private_or_loopback_ip(ip_str: str) -> bool:
    """Kiểm tra một địa chỉ IP là mạng nội bộ LAN hoặc Loopback."""
    if not ip_str:
        return True
    s = str(ip_str).strip().lower()
    if s.startswith("127.") or s == "::1":
        return True
    return s.startswith(PRIVATE_IP_PREFIXES)


def get_scope_factor(filter_scope: str) -> float:
    """Hệ số lọc lưu lượng theo phạm vi mạng (all / wan / lan)."""
    if filter_scope == "wan":
        return 0.98
    if filter_scope == "lan":
        return 0.05
    return 1.0


def classify_protocol_by_port(port_num: int) -> str:
    """Phân loại tên giao thức hiển thị dựa trên số cổng TCP/UDP."""
    p = int(port_num or 0)
    if p == 443:
        return "HTTPS (TCP/443)"
    if p in (80, 8080):
        return f"HTTP (TCP/{p})"
    if p == 53:
        return "DNS (UDP/TCP 53)"
    if p in (445, 139, 137):
        return f"SMB / NetBIOS ({p})"
    if p == 3306:
        return "MySQL Database (3306)"
    if p in (9988, 9999):
        return f"PBL4 Control Channel ({p})"
    if p == 123:
        return "NTP Time Sync (123)"
    if p > 0:
        return f"Application Port ({p})"
    return "Other TCP / UDP"


def classify_network_region(ip_str: str) -> str:
    """Phân loại khu vực / phạm vi mạng dựa trên địa chỉ IP từ xa."""
    s = str(ip_str).strip()
    if s.startswith("127.") or s == "::1":
        return "Localhost / Loopback (127.0.0.1)"
    if s.startswith(PRIVATE_IP_PREFIXES):
        return "Mạng Nội Bộ LAN (Private IP)"
    if s.startswith(("142.250.", "172.217.", "216.58.", "8.8.")):
        return "Google Cloud / DNS"
    if s.startswith(("1.1.1.", "1.0.0.", "104.16.", "104.17.", "104.18.", "172.64.", "172.67.")):
        return "Cloudflare CDN / Edge"
    if s.startswith(("20.", "40.", "52.", "13.")):
        return "Microsoft Azure / Cloud"
    return "Internet Quốc Tế (Public WAN)"


def aggregate_connections_usage(
    active_conns: Iterable[Any],
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], int]:
    """
    Thống kê số lượng kết nối theo Host, Giao thức và Khu vực mạng từ danh sách kết nối.
    Trả về: (host_counts, proto_counts, region_counts, total_conns)
    """
    host_counts: Dict[str, int] = {}
    proto_counts: Dict[str, int] = {}
    region_counts: Dict[str, int] = {}

    for conn in active_conns:
        r_ip = getattr(conn, "remote_ip", None) or (conn.raddr.ip if getattr(conn, "raddr", None) else None)
        if not r_ip:
            continue
        r_port = getattr(conn, "remote_port", None) or (conn.raddr.port if getattr(conn, "raddr", None) else None)
        host_str = f"{r_ip}:{r_port}" if r_port else str(r_ip)
        host_counts[host_str] = host_counts.get(host_str, 0) + 1

        proto_name = classify_protocol_by_port(int(r_port or 0))
        proto_counts[proto_name] = proto_counts.get(proto_name, 0) + 1

        region_name = classify_network_region(str(r_ip))
        region_counts[region_name] = region_counts.get(region_name, 0) + 1

    total_conns = max(sum(host_counts.values()), 1)
    return host_counts, proto_counts, region_counts, total_conns
