"""
Mô-đun tạo icon SVG chuẩn phong cách GlassWire (Vector Icons sắc nét cho PyQt6).
Bao gồm:
- Logo GlassWire (vòng tròn gradient đa sắc)
- Tab Traffic Monitor (vòm sóng waveform đặc trưng của GlassWire)
- Tab GlassWire Protect (chiếc khiên bảo mật)
- Tab Log Analysis (nhật ký & thông báo 9+)
- Tab Network Scanner (sơ đồ kết nối mạng 5 nốt)
- Tab Hardware Resources (chip vi xử lý CPU)
- Capsule Toggle: Graph view & Usage view
- Filter icon (3 thanh trượt equalizer)
- Firewall toggle, Lock, Shield, v.v.
"""

from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtCore import QByteArray, QSize, Qt


def _render_svg_to_icon(svg_str: str, width: int = 24, height: int = 24) -> QIcon:
    """Chuyển đổi chuỗi SVG thành QIcon vector sắc nét."""
    renderer = QSvgRenderer(QByteArray(svg_str.encode("utf-8")))
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


def get_glasswire_logo_icon() -> QIcon:
    """Logo chính thức GlassWire: Vòng tròn màu chuyển sắc đặc trưng."""
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
      <defs>
        <linearGradient id="gw_grad1" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#00C9FF" />
          <stop offset="100%" stop-color="#92FE9D" />
        </linearGradient>
        <linearGradient id="gw_grad2" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#FC466B" />
          <stop offset="100%" stop-color="#3F5EFB" />
        </linearGradient>
        <linearGradient id="gw_grad3" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#FDBB2D" />
          <stop offset="100%" stop-color="#22C1C3" />
        </linearGradient>
      </defs>
      <!-- Vòng tròn đa sắc GlassWire -->
      <path d="M24 4 C35.04 4 44 12.96 44 24 C44 29.5 41.76 34.5 38.14 38.14 L30 30 C31.8 28.5 33 26.4 33 24 C33 19.03 28.97 15 24 15 C21.6 15 19.5 16.2 18 18 L9.86 9.86 C13.5 6.24 18.5 4 24 4 Z" fill="url(#gw_grad1)" />
      <path d="M44 24 C44 35.04 35.04 44 24 44 C18.5 44 13.5 41.76 9.86 38.14 L18 30 C19.5 31.8 21.6 33 24 33 C28.97 33 33 28.97 33 24 Z" fill="url(#gw_grad2)" />
      <path d="M24 44 C12.96 44 4 35.04 4 24 C4 18.5 6.24 13.5 9.86 9.86 L18 18 C16.2 19.5 15 21.6 15 24 C15 28.97 19.03 33 24 33 Z" fill="url(#gw_grad3)" />
    </svg>
    """
    return _render_svg_to_icon(svg, 32, 32)


def get_traffic_monitor_icon(active: bool = False) -> QIcon:
    """Icon Traffic Monitor chuẩn GlassWire: Vòm sóng xanh dương thanh lịch."""
    color = "#2563EB" if active else "#64748B"
    wave_color = "#FFFFFF" if active else "#94A3B8"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="28" height="28">
      <!-- Vòm núi / sóng radar GlassWire -->
      <path d="M14 3 C7.9 3 3 7.9 3 14 C3 18.5 5.8 22.3 9.7 23.9 L10.8 19.2 C8.4 18.1 6.8 15.6 6.8 12.8 C6.8 8.8 10 5.6 14 5.6 C18 5.6 21.2 8.8 21.2 12.8 C21.2 15.6 19.6 18.1 17.2 19.2 L18.3 23.9 C22.2 22.3 25 18.5 25 14 C25 7.9 20.1 3 14 3 Z" fill="{color}" />
      <!-- Sóng tín hiệu bên trong -->
      <path d="M6 15 C8.5 11 11.5 18 14 13 C16.5 8 19.5 16 22 13" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    """
    return _render_svg_to_icon(svg, 24, 24)


def get_protect_icon(active: bool = False) -> QIcon:
    """Icon GlassWire Protect: Chiếc khiên bảo vệ."""
    color = "#2563EB" if active else "#64748B"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
      <path fill="{color}" d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 15l-4-4 1.41-1.41L10 13.17l6.59-6.59L18 8l-8 8z"/>
    </svg>
    """
    return _render_svg_to_icon(svg, 22, 22)


def get_log_analysis_icon(active: bool = False) -> QIcon:
    """Icon Log Analysis: Đồng hồ nhật ký kèm huy hiệu đỏ 9+ chuẩn mực."""
    color = "#2563EB" if active else "#64748B"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="28" height="28">
      <!-- Vòng tròn nhật ký -->
      <circle cx="12" cy="14" r="9" fill="none" stroke="{color}" stroke-width="2.2" />
      <polyline points="12,9 12,14 15,16" fill="none" stroke="{color}" stroke-width="2.2" stroke-linecap="round" />
      <!-- Huy hiệu thông báo đỏ 9+ -->
      <circle cx="21" cy="7" r="6" fill="#EF4444" />
      <text x="21" y="9.8" font-size="7" font-family="Segoe UI, sans-serif" font-weight="bold" fill="#FFFFFF" text-anchor="middle">9+</text>
    </svg>
    """
    return _render_svg_to_icon(svg, 24, 24)


def get_network_scanner_icon(active: bool = False) -> QIcon:
    """Icon Network Scanner: Nốt mạng phân nhánh với số 5 nhỏ."""
    color = "#2563EB" if active else "#64748B"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="28" height="28">
      <line x1="8" y1="14" x2="20" y2="8" stroke="{color}" stroke-width="2" />
      <line x1="8" y1="14" x2="20" y2="20" stroke="{color}" stroke-width="2" />
      <circle cx="8" cy="14" r="3.5" fill="{color}" />
      <circle cx="20" cy="8" r="3" fill="{color}" />
      <circle cx="20" cy="20" r="3" fill="{color}" />
      <!-- Số 5 nhỏ góc trên -->
      <text x="24" y="6" font-size="8" font-family="Segoe UI" font-weight="bold" fill="#64748B">5</text>
    </svg>
    """
    return _render_svg_to_icon(svg, 24, 24)


def get_hardware_resources_icon(active: bool = False) -> QIcon:
    """Icon Hardware Resources: Vi mạch CPU xử lý."""
    color = "#2563EB" if active else "#64748B"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
      <rect x="5" y="5" width="14" height="14" rx="2" fill="none" stroke="{color}" stroke-width="2" />
      <rect x="9" y="9" width="6" height="6" fill="{color}" />
      <!-- Chân pin vi mạch -->
      <line x1="9" y1="2" x2="9" y2="5" stroke="{color}" stroke-width="1.8" />
      <line x1="15" y1="2" x2="15" y2="5" stroke="{color}" stroke-width="1.8" />
      <line x1="9" y1="19" x2="9" y2="22" stroke="{color}" stroke-width="1.8" />
      <line x1="15" y1="19" x2="15" y2="22" stroke="{color}" stroke-width="1.8" />
      <line x1="2" y1="9" x2="5" y2="9" stroke="{color}" stroke-width="1.8" />
      <line x1="2" y1="15" x2="5" y2="15" stroke="{color}" stroke-width="1.8" />
      <line x1="19" y1="9" x2="22" y2="9" stroke="{color}" stroke-width="1.8" />
      <line x1="19" y1="15" x2="22" y2="15" stroke="{color}" stroke-width="1.8" />
    </svg>
    """
    return _render_svg_to_icon(svg, 22, 22)


def get_graph_toggle_icon(active: bool = False) -> QIcon:
    """Icon toggle Graph: Đường sóng dao động."""
    color = "#FFFFFF" if active else "#64748B"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
      <path d="M3 13 C 6 6, 8 18, 12 11 C 16 4, 18 16, 21 11" fill="none" stroke="{color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    """
    return _render_svg_to_icon(svg, 20, 20)


def get_usage_toggle_icon(active: bool = False) -> QIcon:
    """Icon toggle Usage: Bố cục 3 cột chia GlassWire [| | ]."""
    color = "#FFFFFF" if active else "#64748B"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
      <rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="{color}" stroke-width="2" />
      <line x1="9" y1="4" x2="9" y2="20" stroke="{color}" stroke-width="1.8" />
      <line x1="15" y1="4" x2="15" y2="20" stroke="{color}" stroke-width="1.8" />
    </svg>
    """
    return _render_svg_to_icon(svg, 20, 20)


def get_filter_icon() -> QIcon:
    """Icon Filter: 3 thanh trượt equalizer phong cách GlassWire."""
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
      <line x1="4" y1="6" x2="20" y2="6" stroke="#475569" stroke-width="2" stroke-linecap="round" />
      <line x1="4" y1="12" x2="20" y2="12" stroke="#475569" stroke-width="2" stroke-linecap="round" />
      <line x1="4" y1="18" x2="20" y2="18" stroke="#475569" stroke-width="2" stroke-linecap="round" />
      <circle cx="8" cy="6" r="2.5" fill="#2563EB" />
      <circle cx="16" cy="12" r="2.5" fill="#2563EB" />
      <circle cx="10" cy="18" r="2.5" fill="#2563EB" />
    </svg>
    """
    return _render_svg_to_icon(svg, 20, 20)


def get_firewall_shield_icon(status: str = "allowed") -> QIcon:
    """Icon khiên tường lửa Allowed (xanh lá) hoặc Blocked (đỏ cam)."""
    color = "#10B981" if status == "allowed" else "#EF4444"
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
      <path fill="{color}" d="M12 2L4 5v6.09c0 5.05 3.41 9.76 8 10.91 4.59-1.15 8-5.86 8-10.91V5l-8-3z" />
    </svg>
    """
    return _render_svg_to_icon(svg, 18, 18)
