"""
Toán học Đồ thị Sóng GlassWire, Chống Rung Thang Đo & Tổng Hợp Khung Thời Gian (Wave Chart Math - Application Layer):
- Cấu hình thang thời gian (TIME_SCALE_CONFIGS) và các bậc trần trục Y (CEILING_TIERS_KB).
- Thuật toán chống rung nhảy trục Y (Hysteresis Hold-Down Timer: select_y_ceiling_tier).
- Nội suy sóng GlassWire siêu mượt (Gaussian Smoothing + Monotonic Cubic Hermite PCHIP: generate_glasswire_wave).
- Tổng hợp lưu lượng theo khung thời gian trượt (compute_window_traffic) & tìm kiếm mẫu nhị phân O(log N) (find_nearest_sample).
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import numpy as np


TIME_SCALE_CONFIGS: Dict[float, Dict[str, Any]] = {
    30.0: {
        "name": "30 Seconds",
        "label": "30 seconds",
        "sigma": 1.8,
        "radius_seconds": 5,
        "n_dense": 220,
        "dense_points": 700,
        "tick_step": 5.0,
        "step": 10,
        "fmt": "%H:%M:%S",
    },
    60.0: {
        "name": "1 Minute",
        "label": "1 minute",
        "sigma": 3.5,
        "radius_seconds": 10,
        "n_dense": 280,
        "dense_points": 750,
        "tick_step": 10.0,
        "step": 15,
        "fmt": "%H:%M:%S",
    },
    300.0: {
        "name": "5 Minutes",
        "label": "5 minutes",
        "sigma": 10.0,
        "radius_seconds": 30,
        "n_dense": 380,
        "dense_points": 850,
        "tick_step": 40.0,
        "step": 60,
        "fmt": "%H:%M",
    },
    600.0: {
        "name": "10 Minutes",
        "label": "10 minutes",
        "sigma": 20.0,
        "radius_seconds": 55,
        "n_dense": 450,
        "dense_points": 900,
        "tick_step": 60.0,
        "step": 120,
        "fmt": "%H:%M",
    },
    3600.0: {
        "name": "1 Hour",
        "label": "1 hour",
        "sigma": 40.0,
        "radius_seconds": 120,
        "n_dense": 600,
        "dense_points": 950,
        "tick_step": 300.0,
        "step": 600,
        "fmt": "%H:%M",
    },
    86400.0: {
        "name": "24 Hours",
        "label": "24 hours",
        "sigma": 1.2,
        "radius_seconds": 3,
        "n_dense": 800,
        "dense_points": 1000,
        "tick_step": 7200.0,
        "step": 14400,
        "fmt": "%H:%M",
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
    1. Lọc Gauss làm mềm các xung raw theo tham số riêng biệt của từng mốc thời gian.
    2. Nội suy Hermite khối (Cubic Hermite PCHIP) trực tiếp lên lưới x_dense dày mịn.
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
