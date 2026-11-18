"""
Định dạng Đơn vị Lưu lượng & Phân loại Địa chỉ IP / Giao thức / Vùng mạng (Network Classifier - Application Layer):
- Định dạng dung lượng (format_bytes), tốc độ (format_speed) và nhãn trần trục Y (format_ceiling_label).
- Nhận diện IP LAN / Loopback (is_private_or_loopback_ip) và hệ số lọc phạm vi (get_scope_factor).
- Phân loại giao thức theo số cổng (classify_protocol_by_port) và vùng mạng theo IP (classify_network_region).
- Thống kê kết nối đang mở theo Host, Giao thức và Vùng mạng (aggregate_connections_usage).
"""

from typing import Any, Dict, Iterable, Tuple


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
