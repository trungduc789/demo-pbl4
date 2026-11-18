"""
Facade Phân tích Lưu lượng & Toán học Đồ thị (Traffic Analytics - Application Layer):
Tổng hợp và re-export các hàm thuần túy từ:
1. `application.wave_chart_math`: Cấu hình thang thời gian, Hysteresis trục Y, nội suy sóng GlassWire PCHIP, tổng hợp cửa sổ trượt.
2. `application.network_classifier`: Định dạng đơn vị dung lượng/tốc độ, phân loại IP WAN/LAN, Giao thức và Vùng mạng.
"""

from application.wave_chart_math import (
    TIME_SCALE_CONFIGS,
    CEILING_TIERS_KB,
    select_y_ceiling_tier,
    generate_glasswire_wave,
    compute_window_traffic,
    find_nearest_sample,
)
from application.network_classifier import (
    PRIVATE_IP_PREFIXES,
    format_bytes,
    format_speed,
    format_ceiling_label,
    is_private_or_loopback_ip,
    get_scope_factor,
    classify_protocol_by_port,
    classify_network_region,
    aggregate_connections_usage,
)

__all__ = [
    "TIME_SCALE_CONFIGS",
    "CEILING_TIERS_KB",
    "PRIVATE_IP_PREFIXES",
    "format_bytes",
    "format_speed",
    "format_ceiling_label",
    "select_y_ceiling_tier",
    "generate_glasswire_wave",
    "compute_window_traffic",
    "find_nearest_sample",
    "is_private_or_loopback_ip",
    "get_scope_factor",
    "classify_protocol_by_port",
    "classify_network_region",
    "aggregate_connections_usage",
]
