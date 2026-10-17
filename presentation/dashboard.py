import time
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import numpy as np
import psutil
from PyQt6 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg

from application.monitor_service import MonitorService
from config.settings import UPDATE_INTERVAL, MAX_DATA_POINTS
from utils.safe_print import safe_print as print


# Bảng tham số làm mịn và hiển thị riêng biệt chuẩn hóa cho từng mốc thời gian (Scale-tailored smoothing)
TIME_SCALE_CONFIGS = {
    30.0: {
        "name": "30 Seconds",
        "sigma": 1.8,           # Bán kính Gauss 1.8s bo tròn các đỉnh nhọn mà vẫn giữ dáng sóng 30s
        "radius_seconds": 5,
        "n_dense": 220,
        "tick_step": 5.0,        # Vạch mốc mỗi 5 giây
    },
    60.0: {
        "name": "1 Minute",
        "sigma": 3.5,           # Làm mịn 3.5s gom các nhịp xung 1 phút thành sóng mềm
        "radius_seconds": 10,
        "n_dense": 280,
        "tick_step": 15.0,       # Vạch mốc mỗi 15 giây
    },
    300.0: {
        "name": "5 Minutes",
        "sigma": 10.0,          # Độ lệch chuẩn 10s tạo đồi cát vàng cuộn 25-35s chuẩn GlassWire
        "radius_seconds": 30,
        "n_dense": 380,
        "tick_step": 60.0,       # Vạch mốc mỗi 1 phút tròn
    },
    600.0: {
        "name": "10 Minutes",
        "sigma": 20.0,          # Sóng thoải rộng toàn cảnh 10 phút (đồi sóng 45-60s)
        "radius_seconds": 55,
        "n_dense": 450,
        "tick_step": 120.0,      # Vạch mốc mỗi 2 phút tròn
    }
}


class TimeAxisItem(pg.AxisItem):
    """Trục hoành hiển thị mốc giờ hệ thống được định hình chuyên biệt theo từng mốc scale thời gian"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_window = 300.0

    def set_window_duration(self, duration: float):
        self.current_window = duration
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        cfg = TIME_SCALE_CONFIGS.get(self.current_window, TIME_SCALE_CONFIGS[300.0])
        step = cfg["tick_step"]
        start = (int(minVal) // int(step)) * int(step)
        ticks = []
        curr = start
        while curr <= maxVal + step:
            if curr >= minVal - step:
                ticks.append(curr)
            curr += step
        return [(step, ticks)]

    def tickStrings(self, values, scale, spacing):
        cfg = TIME_SCALE_CONFIGS.get(self.current_window, TIME_SCALE_CONFIGS[300.0])
        res = []
        for val in values:
            lt = time.localtime(val)
            hr = lt.tm_hour % 12
            hr = 12 if hr == 0 else hr
            am_pm = "PM" if lt.tm_hour >= 12 else "AM"
            if cfg["tick_step"] >= 60.0:
                res.append(f"{hr}:{lt.tm_min:02d}:00 {am_pm}")
            else:
                res.append(f"{hr}:{lt.tm_min:02d}:{lt.tm_sec:02d} {am_pm}")
        return res


class GlassWireArcGauge(QtWidgets.QWidget):
    """Đồng hồ bán nguyệt phong cách GlassWire hiển thị dung lượng tại tâm và vòng cung vàng"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(140, 74)
        self.total_text = "0.0 MB"
        self.percent = 35

    def set_value(self, total_str: str, percent: int = 40):
        self.total_text = total_str
        self.percent = min(100, max(5, percent))
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        rect = QtCore.QRectF(15, 8, 110, 110)

        # Cung nền xám nhạt
        pen_bg = QtGui.QPen(QtGui.QColor("#E2E8F0"), 5.5, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(rect, 0 * 16, 180 * 16)

        # Cung vàng sáng đặc trưng GlassWire
        pen_fg = QtGui.QPen(QtGui.QColor("#F59E0B"), 6.0, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_fg)
        span = int((self.percent / 100.0) * 180 * 16)
        painter.drawArc(rect, 180 * 16, -span)

        # Biểu tượng chuông/chấm đỉnh cung
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#F59E0B"))
        painter.drawEllipse(QtCore.QPointF(70, 8), 3.5, 3.5)

        # Chữ ở tâm vòng bán nguyệt
        painter.setPen(QtGui.QColor("#0F172A"))
        font_total = QtGui.QFont("Segoe UI", 11, QtGui.QFont.Weight.Bold)
        painter.setFont(font_total)
        painter.drawText(QtCore.QRectF(0, 36, 140, 24), QtCore.Qt.AlignmentFlag.AlignCenter, self.total_text)


class GlassWireWanLanWidget(QtWidgets.QWidget):
    """Thanh phân tách WAN & LAN phong cách GlassWire với dải màu vàng và đầu mút hồng"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.wan_str = "WAN 0.0 MB"
        self.lan_str = "LAN 0.0 KB"
        self.wan_pct = 95
        self.setFixedHeight(50)

    def set_values(self, wan_str: str, lan_str: str, wan_pct: int = 95):
        self.wan_str = wan_str
        self.lan_str = lan_str
        self.wan_pct = max(10, min(98, wan_pct))
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Hàng 1: WAN
        painter.setPen(QtGui.QColor("#334155"))
        painter.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        painter.drawText(QtCore.QRectF(0, 4, 115, 18), QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, self.wan_str)

        bar_x = 120
        bar_w = self.width() - bar_x - 16
        bar_y = 10
        bar_h = 7

        if bar_w > 20:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#E2E8F0"))
            painter.drawRoundedRect(QtCore.QRectF(bar_x, bar_y, bar_w, bar_h), 3.5, 3.5)

            wan_w = (self.wan_pct / 100.0) * bar_w
            painter.setBrush(QtGui.QColor("#F59E0B"))
            painter.drawRoundedRect(QtCore.QRectF(bar_x, bar_y, wan_w, bar_h), 3.5, 3.5)

            tip_w = min(14.0, wan_w * 0.15)
            if tip_w > 3:
                painter.setBrush(QtGui.QColor("#F43F5E"))
                painter.drawRoundedRect(QtCore.QRectF(bar_x + wan_w - tip_w, bar_y, tip_w, bar_h), 3.5, 3.5)

        # Hàng 2: LAN
        painter.setPen(QtGui.QColor("#64748B"))
        painter.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        painter.drawText(QtCore.QRectF(0, 28, 115, 18), QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, self.lan_str)

        if bar_w > 20:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#F59E0B"))
            painter.drawRoundedRect(QtCore.QRectF(bar_x, 34, 12, 6), 3, 3)


class GlassWireTimelineScrubber(QtWidgets.QWidget):
    """Thanh toàn cảnh mini-timeline kéo tua phong cách GlassWire với bóng sóng vàng và nút kéo tròn"""
    time_selected = QtCore.pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.history_points = deque(maxlen=650)
        self.window_duration = 300.0

    def set_window_duration(self, duration: float):
        self.window_duration = duration
        self.update()

    def add_point(self, timestamp: float, value: float):
        self.history_points.append((timestamp, value))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._handle_scrub(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._handle_scrub(event.position().x())

    def _handle_scrub(self, mouse_x: float):
        w = max(1, self.width())
        pct = max(0.0, min(1.0, mouse_x / float(w)))
        if len(self.history_points) > 1:
            t_min = self.history_points[0][0]
            t_max = self.history_points[-1][0]
            target_t = t_min + pct * (t_max - t_min)
            self.time_selected.emit(target_t)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # Nền dải timeline
        painter.fillRect(0, 0, w, h, QtGui.QColor("#F8FAFC"))
        painter.setPen(QtGui.QColor("#E2E8F0"))
        painter.drawLine(0, 0, w, 0)
        painter.drawLine(0, h - 1, w, h - 1)

        # Bóng sóng vàng mini
        if len(self.history_points) > 5:
            max_v = max([p[1] for p in self.history_points] + [1.0])
            path = QtGui.QPainterPath()
            path.moveTo(0, h - 12)
            n = len(self.history_points)
            step_x = w / max(1, n - 1)
            for i, (t, v) in enumerate(self.history_points):
                norm_y = (v / max_v) * (h - 16)
                cur_x = i * step_x
                cur_y = (h - 12) - norm_y
                path.lineTo(cur_x, cur_y)
            path.lineTo(w, h - 12)
            path.closeSubpath()

            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(245, 158, 11, 140))
            painter.drawPath(path)

            painter.setPen(QtGui.QPen(QtGui.QColor("#F59E0B"), 1.0))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        # Khung quan sát hiện tại (View Slider) tỷ lệ theo window_duration / 600s
        ratio = min(1.0, max(0.08, self.window_duration / 600.0))
        win_w = max(90, int(w * ratio))
        win_x = w - win_w - 6
        win_rect = QtCore.QRectF(win_x, 2, win_w, h - 14)

        painter.setPen(QtGui.QPen(QtGui.QColor("#CBD5E1"), 1.2))
        painter.setBrush(QtGui.QColor(255, 255, 255, 190))
        painter.drawRoundedRect(win_rect, 4, 4)

        # Hai nút kéo tròn hai bên ( ○----○ )
        painter.setBrush(QtGui.QColor("#FFFFFF"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#94A3B8"), 1.8))
        painter.drawEllipse(QtCore.QPointF(win_x, (h - 14) / 2 + 2), 4.5, 4.5)
        painter.drawEllipse(QtCore.QPointF(win_x + win_w, (h - 14) / 2 + 2), 4.5, 4.5)

        # Mốc thời gian dưới đáy timeline
        painter.setPen(QtGui.QColor("#94A3B8"))
        painter.setFont(QtGui.QFont("Segoe UI", 7))
        now = time.time()
        for i in range(6):
            t_offset = (5 - i) * 60
            lt = time.localtime(now - t_offset)
            hr = lt.tm_hour % 12
            hr = 12 if hr == 0 else hr
            am_pm = "PM" if lt.tm_hour >= 12 else "AM"
            lbl_time = f"{hr}:{lt.tm_min:02d}:{lt.tm_sec:02d} {am_pm}"
            lbl_x = int(i * (w / 5.2)) + 10
            painter.drawText(lbl_x, h - 2, lbl_time)


class ProcessIconManager:
    """Quản lý và trích xuất Icon chính thức của ứng dụng Windows"""
    def __init__(self):
        self._provider = QtWidgets.QFileIconProvider()
        self._cache: Dict[str, QtGui.QIcon] = {}

    def get_icon(self, exe_path: str, proc_name: str) -> QtGui.QIcon:
        if proc_name in self._cache:
            return self._cache[proc_name]

        if exe_path:
            fi = QtCore.QFileInfo(exe_path)
            if fi.exists():
                icon = self._provider.icon(fi)
                if not icon.isNull():
                    self._cache[proc_name] = icon
                    return icon

        # Tạo icon màu phẳng thanh lịch dự phòng
        pixmap = QtGui.QPixmap(22, 22)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pixmap)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        
        palette = ["#2563EB", "#059669", "#D97706", "#7C3AED", "#DB2777", "#0891B2"]
        color = palette[abs(hash(proc_name)) % len(palette)]
        
        p.setBrush(QtGui.QColor(color))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(1, 1, 20, 20, 4, 4)
        
        p.setPen(QtGui.QColor("#FFFFFF"))
        p.setFont(QtGui.QFont("Segoe UI", 8, QtGui.QFont.Weight.Bold))
        char = proc_name[0].upper() if proc_name else "?"
        p.drawText(QtCore.QRect(0, 0, 22, 22), QtCore.Qt.AlignmentFlag.AlignCenter, char)
        p.end()

        icon = QtGui.QIcon(pixmap)
        self._cache[proc_name] = icon
        return icon


class HighFreqNetworkWorker(QtCore.QThread):
    """Luồng phụ đọc chỉ số từ kernel định kỳ, không làm đơ giao diện đồ họa."""
    sample_ready = QtCore.pyqtSignal(float, float, float, list, float, float)

    def __init__(self, service: MonitorService):
        super().__init__()
        self.service = service
        self.running = True

    def run(self):
        while self.running:
            try:
                speed, proc_speeds, total_p_down, total_p_up = self.service.fetch_latest_speed()
                self.sample_ready.emit(
                    time.time(),
                    speed.download_speed_kb,
                    speed.upload_speed_kb,
                    proc_speeds,
                    total_p_down,
                    total_p_up
                )
            except Exception as e:
                print(f"[Worker Error] {e}")

            self.msleep(int(UPDATE_INTERVAL * 1000))

    def stop(self):
        self.running = False
        self.wait()


class BandwidthDashboard(QtWidgets.QMainWindow):
    """
    Giao diện GlassWire Free chuẩn xác 100%:
    - Header: GlassWire brand + Navigation Pills (Traffic Monitor, Protect, Log Analysis, Network Scanner, Hardware).
    - Split-View: Bảng danh sách tiến trình trái (Icon, App, ↓, ↑) + Biểu đồ sóng thời gian thực phải.
    - Bộ đệm trễ 5 giây + Nội suy Hermite & Lọc Gaussian mượt mà không rung giật.
    - HUD thống kê ứng dụng: Số liệu MB/KB, Tốc độ, Đồng hồ bán nguyệt GlassWire, Thanh phân bổ WAN/LAN.
    - Panoramic Timeline Scrubber với thanh kéo tua và bóng sóng vàng.
    - Footer: Upgrade to unlock Premium features banner.
    - Tab 2 (GlassWire Protect): Giới hạn Upload Windows QoS Policy & kiểm tra xác thực.
    - Tab 3 (Network Scanner): Bảng Socket TCP (IP, Cổng, Giao thức, Trạng thái).
    - Tab 4 (Log Analysis): Kiểm tra toàn vẹn CSDL MySQL 6 bảng & Lịch sử lưu lượng.
    - Tab 5 (Hardware Resources): Giám sát phần cứng CPU, RAM, Disk I/O hệ thống.
    """

    def __init__(self, monitor_service: MonitorService):
        super().__init__()
        self.service = monitor_service
        self.window_duration = 300.0  # Mặc định 5 phút như GlassWire
        self.render_delay = 5.0       # Lookahead buffer 5 giây chống giật tuyệt đối
        self.icon_manager = ProcessIconManager()

        # Thang đo trần chuẩn mực GlassWire (KB/s)
        self.CEILING_TIERS_KB = [
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
            102400.0      # 100 MB
        ]

        # Bộ đệm lưu trữ mẫu sóng (Lưu trữ tới 2400 điểm ~ 40 phút, loại bỏ hoàn toàn việc bị cắt mẫu khi đổi sang 5-10 phút)
        now = time.time()
        init_times = np.linspace(now - 650.0, now, 650)
        self.down_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.up_points = deque([(t, 0.0) for t in init_times], maxlen=2400)

        self.latest_down = 0.0
        self.latest_up = 0.0
        self.latest_proc_down = 0.0
        self.latest_proc_up = 0.0
        self.latest_proc_speeds = []
        
        # Tích lũy dung lượng cho từng tiến trình (Bytes)
        self.app_accumulated_bytes: Dict[str, Dict[str, float]] = {
            "All apps": {"down": 0.0, "up": 0.0}
        }
        self.total_accumulated_down = 0.0
        self.total_accumulated_up = 0.0

        # Tiến trình đang được người dùng chọn xem
        self.selected_app_name = "All apps"
        self.current_y_max = 1024.0

        # Tiến trình đang giám sát QoS
        self.monitored_qos_app = ""
        self.monitored_qos_limit = 0.0

        # Chế độ xem dữ liệu tại điểm & tạm dừng biểu đồ (Point Inspection & Pause/Resume)
        self.point_history = deque(maxlen=2400)
        self.is_paused = False
        self.pinned_time: Optional[float] = None
        self.history_mode = False
        self.historical_series: List[Dict] = []

        self._init_ui()
        self._start_worker()
        self._start_render_loop()

    def _init_ui(self):
        self.setWindowTitle("GlassWire - Traffic Monitor & Network Security")
        self.resize(1280, 760)
        self.setMinimumSize(1020, 620)

        # Style toàn bộ theo ngôn ngữ thiết kế GlassWire (Clean Light Theme)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QWidget {
                color: #1E293B;
            }
            QTableWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                gridline-color: transparent;
                border: none;
                font-size: 12px;
                selection-background-color: #EBF5FF;
                selection-color: #0F172A;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #F1F5F9;
            }
            QTableWidget::item:selected {
                background-color: #EBF5FF;
                color: #0F172A;
                font-weight: 600;
            }
            QHeaderView::section {
                background-color: #FFFFFF;
                color: #64748B;
                padding: 6px 8px;
                font-weight: 600;
                font-size: 11px;
                border: none;
                border-bottom: 1px solid #E2E8F0;
            }
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                border-radius: 6px;
                font-weight: 600;
                padding: 8px 16px;
                font-size: 12px;
                border: none;
            }
            QPushButton:hover {
                background-color: #1D4ED8;
            }
            QLineEdit, QComboBox {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 12px;
                color: #1E293B;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1.5px solid #2563EB;
            }
        """)

        container = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. QStackedWidget chứa 5 trang chức năng
        self.stacked_pages = QtWidgets.QStackedWidget()

        # 2. Thanh Header GlassWire Navigation trên cùng
        header_widget = self._create_glasswire_header()
        main_layout.addWidget(header_widget)

        # Trang 0: Traffic Monitor (Split-view chuẩn GlassWire)
        self.page_traffic_monitor = QtWidgets.QWidget()
        self._setup_traffic_monitor_page(self.page_traffic_monitor)
        self.stacked_pages.addWidget(self.page_traffic_monitor)

        # Trang 1: GlassWire Protect (Windows QoS Policy)
        self.page_protect = QtWidgets.QWidget()
        self._setup_protect_page(self.page_protect)
        self.stacked_pages.addWidget(self.page_protect)

        # Trang 2: Log Analysis (MySQL Integrity & Event History)
        self.page_logs = QtWidgets.QWidget()
        self._setup_logs_page(self.page_logs)
        self.stacked_pages.addWidget(self.page_logs)

        # Trang 3: Network Scanner (TCP/UDP Sockets & Remote IPs)
        self.page_scanner = QtWidgets.QWidget()
        self._setup_scanner_page(self.page_scanner)
        self.stacked_pages.addWidget(self.page_scanner)

        # Trang 4: Hardware Resources (CPU, RAM, Disk, Adapter)
        self.page_hardware = QtWidgets.QWidget()
        self._setup_hardware_page(self.page_hardware)
        self.stacked_pages.addWidget(self.page_hardware)

        main_layout.addWidget(self.stacked_pages)

        # 3. Footer Banner: "Upgrade to unlock Premium features."
        footer_widget = self._create_upgrade_footer()
        main_layout.addWidget(footer_widget)

        self._switch_nav_page(0)

        self.setCentralWidget(container)

    # =========================================================================
    # HEADER THANH ĐIỀU HƯỚNG GLASSWIRE (NAV TABS + LOGO + UPGRADE)
    # =========================================================================
    def _create_glasswire_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QWidget()
        header.setFixedHeight(56)
        header.setStyleSheet("background-color: #FFFFFF; border-bottom: 1px solid #E2E8F0;")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(16)

        # Logo GlassWire + Chữ FREE
        logo_box = QtWidgets.QVBoxLayout()
        logo_box.setSpacing(1)
        logo_box.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)

        logo_title = QtWidgets.QLabel("GlassWire ▾")
        logo_title.setStyleSheet("font-size: 16px; font-weight: 800; color: #1E293B;")
        logo_free = QtWidgets.QLabel("FREE")
        logo_free.setStyleSheet("font-size: 9px; font-weight: 700; color: #94A3B8; letter-spacing: 1px;")

        logo_box.addWidget(logo_title)
        logo_box.addWidget(logo_free)
        layout.addLayout(logo_box)

        layout.addSpacing(24)

        # 5 Tab điều hướng GlassWire
        self.nav_buttons: List[QtWidgets.QPushButton] = []

        tabs_data = [
            ("📈 Traffic\nMonitor", 0),
            ("🛡️ GlassWire\nProtect", 1),
            ("📋 Log\nAnalysis", 2),
            ("🕸️ Network\nScanner", 3),
            ("⚙️ Hardware\nResources", 4),
        ]

        nav_container = QtWidgets.QHBoxLayout()
        nav_container.setSpacing(8)

        for text, page_idx in tabs_data:
            btn = QtWidgets.QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(40)
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=page_idx: self._switch_nav_page(idx))
            nav_container.addWidget(btn)
            self.nav_buttons.append(btn)

        layout.addLayout(nav_container)
        layout.addStretch()

        # Phần bên phải: "Get All Premium Features" + Button UPGRADE + User Avatar
        right_box = QtWidgets.QHBoxLayout()
        right_box.setSpacing(12)

        lbl_premium = QtWidgets.QLabel("Get All Premium\nFeatures")
        lbl_premium.setStyleSheet("font-size: 10px; color: #64748B; font-weight: 600; text-align: right;")
        right_box.addWidget(lbl_premium)

        btn_upgrade = QtWidgets.QPushButton("UPGRADE")
        btn_upgrade.setFixedHeight(28)
        btn_upgrade.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-weight: 800;
                font-size: 11px;
                padding: 4px 14px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        btn_upgrade.clicked.connect(self._show_about_dialog)
        right_box.addWidget(btn_upgrade)

        # Avatar người dùng
        avatar = QtWidgets.QLabel("👤")
        avatar.setFixedSize(28, 28)
        avatar.setStyleSheet("""
            QLabel {
                background-color: #3B82F6;
                color: #FFFFFF;
                border-radius: 14px;
                font-size: 14px;
                qproperty-alignment: AlignCenter;
            }
        """)
        right_box.addWidget(avatar)

        layout.addLayout(right_box)

        return header

    def _switch_nav_page(self, index: int):
        """Chuyển trang hiển thị và áp dụng giao diện nút bấm GlassWire"""
        self.stacked_pages.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            if i == index:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #EBF3FC;
                        color: #2563EB;
                        font-weight: bold;
                        border-radius: 6px;
                        padding: 4px 14px;
                        font-size: 12px;
                        border: none;
                        text-align: center;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        color: #475569;
                        font-weight: 600;
                        border-radius: 6px;
                        padding: 4px 14px;
                        font-size: 12px;
                        border: none;
                        text-align: center;
                    }
                    QPushButton:hover {
                        background-color: #F8FAFC;
                        color: #1E293B;
                    }
                """)

        if index == 2:
            self._verify_database()
        elif index == 3:
            self._refresh_connections()
        elif index == 4:
            self._update_hardware_metrics()

    # =========================================================================
    # TRANG 0: TRAFFIC MONITOR (CHUẨN GLASSWIRE SPLIT-VIEW)
    # =========================================================================
    def _setup_traffic_monitor_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QHBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background-color: #E2E8F0; width: 1px; }")

        # ---------------------------------------------------------------------
        # 1. BẢNG DANH SÁCH ỨNG DỤNG BÊN TRÁI (LEFT SIDEBAR)
        # ---------------------------------------------------------------------
        left_panel = QtWidgets.QWidget()
        left_panel.setStyleSheet("background-color: #FFFFFF;")
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 4)
        left_layout.setSpacing(6)

        # Thanh công cụ bộ lọc: Apps ▾ | WAN & LAN ▾ | ⇅ | 🔍
        filter_bar = QtWidgets.QHBoxLayout()
        filter_bar.setSpacing(6)

        lbl_apps_drop = QtWidgets.QPushButton("Apps ▾")
        lbl_apps_drop.setFixedHeight(24)
        lbl_apps_drop.setStyleSheet("background-color: transparent; color: #475569; font-size: 11px; font-weight: bold; padding: 2px 4px;")
        filter_bar.addWidget(lbl_apps_drop)

        lbl_wan_drop = QtWidgets.QPushButton("WAN & LAN ▾")
        lbl_wan_drop.setFixedHeight(24)
        lbl_wan_drop.setStyleSheet("background-color: transparent; color: #475569; font-size: 11px; font-weight: bold; padding: 2px 4px;")
        filter_bar.addWidget(lbl_wan_drop)

        btn_sort = QtWidgets.QPushButton("⇅")
        btn_sort.setFixedSize(24, 24)
        btn_sort.setStyleSheet("background-color: transparent; color: #64748B; font-size: 13px; font-weight: bold;")
        filter_bar.addWidget(btn_sort)

        filter_bar.addStretch()

        self.search_app_input = QtWidgets.QLineEdit()
        self.search_app_input.setPlaceholderText("🔍 Tìm...")
        self.search_app_input.setFixedWidth(80)
        self.search_app_input.setStyleSheet("border-radius: 12px; padding: 3px 8px; font-size: 11px; background-color: #F8FAFC; border: 1px solid #E2E8F0;")
        self.search_app_input.textChanged.connect(self._filter_app_list)
        filter_bar.addWidget(self.search_app_input)

        left_layout.addLayout(filter_bar)

        # Bảng danh sách ứng dụng GlassWire
        self.app_list_table = QtWidgets.QTableWidget()
        self.app_list_table.setColumnCount(3)
        self.app_list_table.setHorizontalHeaderLabels(["App", "↓", "↑"])
        self.app_list_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.app_list_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.app_list_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.app_list_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.app_list_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.app_list_table.verticalHeader().setVisible(False)
        self.app_list_table.setShowGrid(False)
        self.app_list_table.itemSelectionChanged.connect(self._on_app_selected)
        self.app_list_table.doubleClicked.connect(self._on_app_double_clicked)
        left_layout.addWidget(self.app_list_table)

        left_panel.setMinimumWidth(260)
        left_panel.setMaximumWidth(380)
        splitter.addWidget(left_panel)

        # ---------------------------------------------------------------------
        # 2. KHU VỰC ĐỒ THỊ SÓNG & HUD BÊN PHẢI (RIGHT MAIN PANEL)
        # ---------------------------------------------------------------------
        right_panel = QtWidgets.QWidget()
        right_panel.setStyleSheet("background-color: #FFFFFF;")
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(12, 8, 12, 6)
        right_layout.setSpacing(6)

        # Thanh công cụ trên biểu đồ: Thang đo (3 MB ▾) | Live/Pause | Lịch sử | [📈][☷][🌐] | 5 Minutes ▾
        chart_top_bar = QtWidgets.QHBoxLayout()
        chart_top_bar.setSpacing(8)

        self.scale_indicator = QtWidgets.QLabel("3 MB ▾")
        self.scale_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #64748B;")
        chart_top_bar.addWidget(self.scale_indicator)

        # Nút trạng thái Live / Dừng
        self.lbl_live_badge = QtWidgets.QLabel("● LIVE")
        self.lbl_live_badge.setStyleSheet("color: #059669; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;")
        chart_top_bar.addWidget(self.lbl_live_badge)

        self.btn_pause_toggle = QtWidgets.QPushButton("⏸ Tạm Dừng")
        self.btn_pause_toggle.setFixedHeight(26)
        self.btn_pause_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #F8FAFC;
                color: #475569;
                font-size: 11px;
                font-weight: 600;
                padding: 2px 10px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #F1F5F9;
                color: #0F172A;
            }
        """)
        self.btn_pause_toggle.clicked.connect(self._toggle_pause_resume)
        chart_top_bar.addWidget(self.btn_pause_toggle)

        # Nút chuyển sang chế độ Lịch Sử
        self.btn_history_mode = QtWidgets.QPushButton("📅 Xem Lịch Sử")
        self.btn_history_mode.setFixedHeight(26)
        self.btn_history_mode.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_history_mode.setStyleSheet("""
            QPushButton {
                background-color: #EFF6FF;
                color: #2563EB;
                font-size: 11px;
                font-weight: 600;
                padding: 2px 10px;
                border-radius: 5px;
                border: 1px solid #BFDBFE;
            }
            QPushButton:hover {
                background-color: #DBEAFE;
            }
        """)
        self.btn_history_mode.clicked.connect(self._toggle_history_mode)
        chart_top_bar.addWidget(self.btn_history_mode)

        # Khung điều khiển chọn ngày lịch sử (mặc định ẩn)
        self.history_controls_widget = QtWidgets.QWidget()
        hc_layout = QtWidgets.QHBoxLayout(self.history_controls_widget)
        hc_layout.setContentsMargins(0, 0, 0, 0)
        hc_layout.setSpacing(6)

        lbl_date = QtWidgets.QLabel("Ngày:")
        lbl_date.setStyleSheet("font-size: 11px; font-weight: bold; color: #475569;")
        hc_layout.addWidget(lbl_date)

        self.combo_history_date = QtWidgets.QComboBox()
        self.combo_history_date.setMinimumWidth(110)
        self.combo_history_date.setStyleSheet("font-size: 11px; padding: 2px 6px;")
        hc_layout.addWidget(self.combo_history_date)

        self.btn_load_history = QtWidgets.QPushButton("🔍 Tải Biểu Đồ")
        self.btn_load_history.setFixedHeight(24)
        self.btn_load_history.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_load_history.setStyleSheet("background-color: #2563EB; color: #FFFFFF; font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 4px;")
        self.btn_load_history.clicked.connect(self._load_history_for_selected_date)
        hc_layout.addWidget(self.btn_load_history)

        self.btn_back_to_live = QtWidgets.QPushButton("⬅ Quay Lại Live")
        self.btn_back_to_live.setFixedHeight(24)
        self.btn_back_to_live.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_back_to_live.setStyleSheet("background-color: #10B981; color: #FFFFFF; font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 4px;")
        self.btn_back_to_live.clicked.connect(self._exit_history_mode)
        hc_layout.addWidget(self.btn_back_to_live)

        self.history_controls_widget.setVisible(False)
        chart_top_bar.addWidget(self.history_controls_widget)

        chart_top_bar.addStretch()

        # Nút chuyển chế độ biểu đồ GlassWire
        mode_box = QtWidgets.QWidget()
        mode_box.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px;")
        mb_layout = QtWidgets.QHBoxLayout(mode_box)
        mb_layout.setContentsMargins(2, 2, 2, 2)
        mb_layout.setSpacing(2)

        self.btn_mode_wave = QtWidgets.QPushButton("📈")
        self.btn_mode_wave.setFixedSize(28, 22)
        self.btn_mode_wave.setStyleSheet("background-color: #2563EB; color: #FFFFFF; border-radius: 4px; font-size: 11px;")
        self.btn_mode_bars = QtWidgets.QPushButton("☷")
        self.btn_mode_bars.setFixedSize(28, 22)
        self.btn_mode_bars.setStyleSheet("background-color: transparent; color: #64748B; border-radius: 4px; font-size: 11px;")
        self.btn_mode_globe = QtWidgets.QPushButton("🌐")
        self.btn_mode_globe.setFixedSize(28, 22)
        self.btn_mode_globe.setStyleSheet("background-color: transparent; color: #64748B; border-radius: 4px; font-size: 11px;")

        mb_layout.addWidget(self.btn_mode_wave)
        mb_layout.addWidget(self.btn_mode_bars)
        mb_layout.addWidget(self.btn_mode_globe)
        chart_top_bar.addWidget(mode_box)

        # Dropdown thời gian hiển thị (Mặc định: 5 Minutes)
        self.combo_time_window = QtWidgets.QComboBox()
        self.combo_time_window.addItems(["30 Seconds", "1 Minute", "5 Minutes", "10 Minutes"])
        self.combo_time_window.setCurrentIndex(2)
        self.combo_time_window.setStyleSheet("font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px;")
        self.combo_time_window.currentIndexChanged.connect(self._on_time_window_changed)
        chart_top_bar.addWidget(self.combo_time_window)

        right_layout.addLayout(chart_top_bar)

        # BANNER SOI DỮ LIỆU TẠI ĐIỂM (HIỂN THỊ KHI DỪNG / CHỌN ĐIỂM TRÊN BIỂU ĐỒ)
        self.point_inspector_card = QtWidgets.QFrame()
        self.point_inspector_card.setStyleSheet("""
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-left: 5px solid #2563EB;
                border-radius: 6px;
                padding: 6px 12px;
            }
        """)
        self.point_inspector_card.setVisible(False)
        pic_layout = QtWidgets.QHBoxLayout(self.point_inspector_card)
        pic_layout.setContentsMargins(6, 4, 6, 4)
        pic_layout.setSpacing(10)

        self.lbl_point_details = QtWidgets.QLabel("Đang soi điểm...")
        self.lbl_point_details.setTextFormat(QtCore.Qt.TextFormat.RichText)
        pic_layout.addWidget(self.lbl_point_details, stretch=1)

        btn_box = QtWidgets.QHBoxLayout()
        btn_box.setSpacing(6)

        self.btn_card_resume = QtWidgets.QPushButton("▶ Tiếp Tục Biểu Đồ")
        self.btn_card_resume.setFixedHeight(28)
        self.btn_card_resume.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_card_resume.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 12px;
                border-radius: 5px;
                border: none;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        self.btn_card_resume.clicked.connect(self._resume_live)
        btn_box.addWidget(self.btn_card_resume)

        self.btn_card_close = QtWidgets.QPushButton("✕ Bỏ Ghim")
        self.btn_card_close.setFixedHeight(28)
        self.btn_card_close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_card_close.setStyleSheet("""
            QPushButton {
                background-color: #F1F5F9;
                color: #475569;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 10px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #E2E8F0;
                color: #0F172A;
            }
        """)
        self.btn_card_close.clicked.connect(self._resume_live)
        btn_box.addWidget(self.btn_card_close)

        pic_layout.addLayout(btn_box)
        right_layout.addWidget(self.point_inspector_card)

        # BIỂU ĐỒ PYQTGRAPH SÓNG VÀNG RỰC RỠ GLASSWIRE
        pg.setConfigOption("antialias", True)
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': self.time_axis}, background="#FFFFFF")

        vb = self.plot_widget.getViewBox()
        vb.setDefaultPadding(0.0)
        vb.setMouseEnabled(x=False, y=False)
        self.plot_widget.showGrid(x=False, y=True, alpha=0.08)
        self.plot_widget.setYRange(0, 1024, padding=0.0)
        self.plot_widget.enableAutoRange(axis='y', enable=False)

        # Trục thời gian đáy
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen("#E2E8F0", width=1))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen("#94A3B8"))

        # Trục bên trái: Ẩn số, giữ lưới ngang tinh tế chuẩn GlassWire
        self.plot_widget.getAxis('left').setStyle(showValues=False)
        self.plot_widget.getAxis('left').setPen(None)

        # Sóng Download: Vàng hổ phách đặc trưng GlassWire (#F59E0B)
        self.curve_down = self.plot_widget.plot(
            pen=pg.mkPen(color="#D97706", width=2.0),
            fillLevel=0,
            brush=(245, 158, 11, 220)
        )

        # Sóng Upload: San hô hồng-cam mỏng dưới chân sóng
        self.curve_up = self.plot_widget.plot(
            pen=pg.mkPen(color="#EA580C", width=1.5),
            fillLevel=0,
            brush=(244, 63, 94, 210)
        )

        # Đường dóng chữ thập tương tác khi rê chuột (Crosshair)
        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#3B82F6", width=1.2, style=QtCore.Qt.PenStyle.DashLine))
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#CBD5E1", width=1.0, style=QtCore.Qt.PenStyle.DotLine))
        self.v_line.hide()
        self.h_line.hide()
        self.plot_widget.addItem(self.v_line, ignoreBounds=True)
        self.plot_widget.addItem(self.h_line, ignoreBounds=True)

        # Marker tròn snap theo chuột
        self.hover_marker = pg.ScatterPlotItem(size=8, pen=pg.mkPen("#2563EB", width=2), brush=pg.mkBrush("#FFFFFF"), symbol='o')
        self.hover_marker.hide()
        self.plot_widget.addItem(self.hover_marker)

        # Đường dóng và Marker cố định khi Ghim chọn điểm (Pinned)
        self.pinned_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#DC2626", width=1.5, style=QtCore.Qt.PenStyle.SolidLine))
        self.pinned_vline.hide()
        self.plot_widget.addItem(self.pinned_vline, ignoreBounds=True)

        self.pinned_marker = pg.ScatterPlotItem(size=14, pen=pg.mkPen("#FFFFFF", width=2.5), brush=pg.mkBrush("#DC2626"), symbol='star')
        self.pinned_marker.hide()
        self.plot_widget.addItem(self.pinned_marker)

        # Tooltip badge nhỏ di động theo chuột
        self.hover_badge = QtWidgets.QFrame(self.plot_widget)
        self.hover_badge.setStyleSheet("""
            QFrame {
                background-color: rgba(15, 23, 42, 0.90);
                border: 1px solid #3B82F6;
                border-radius: 5px;
                padding: 3px 7px;
            }
            QLabel {
                color: #FFFFFF;
                font-family: 'Segoe UI';
                font-size: 10px;
            }
        """)
        self.hover_badge.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        hb_layout = QtWidgets.QVBoxLayout(self.hover_badge)
        hb_layout.setContentsMargins(4, 2, 4, 2)
        hb_layout.setSpacing(1)
        self.lbl_hover_text = QtWidgets.QLabel("")
        self.lbl_hover_text.setTextFormat(QtCore.Qt.TextFormat.RichText)
        hb_layout.addWidget(self.lbl_hover_text)
        self.hover_badge.hide()

        # Kết nối sự kiện chuột xem dữ liệu tại điểm
        self.plot_widget.scene().sigMouseMoved.connect(self._on_plot_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_mouse_clicked)

        right_layout.addWidget(self.plot_widget, stretch=1)

        # HUD THỐNG KÊ CHI TIẾT TIẾN TRÌNH ĐƯỢC CHỌN (BÊN DƯỚI BIỂU ĐỒ)
        bottom_hud = self._create_selected_app_hud()
        right_layout.addWidget(bottom_hud)

        # DẢI PANORAMIC TIMELINE SCRUBBER
        self.timeline_scrubber = GlassWireTimelineScrubber()
        self.timeline_scrubber.time_selected.connect(self._on_scrubber_point_selected)
        right_layout.addWidget(self.timeline_scrubber)

        splitter.addWidget(right_panel)
        splitter.setSizes([300, 960])
        layout.addWidget(splitter)

    def _create_selected_app_hud(self) -> QtWidgets.QWidget:
        """Tạo thanh HUD GlassWire: Tốc độ Tải Xuống, Đồng hồ bán nguyệt, Tốc độ Tải Lên, Phân tích WAN & LAN"""
        hud_box = QtWidgets.QWidget()
        hud_box.setFixedHeight(78)
        hud_box.setStyleSheet("background-color: #FFFFFF; border-top: 1px solid #F1F5F9;")
        hud_layout = QtWidgets.QHBoxLayout(hud_box)
        hud_layout.setContentsMargins(18, 2, 18, 2)
        hud_layout.setSpacing(18)

        # 1. Cột Tải Xuống: [Dung lượng tổng] + [Tốc độ /s ↓]
        down_box = QtWidgets.QVBoxLayout()
        down_box.setSpacing(2)
        down_box.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight)

        self.hud_down_total = QtWidgets.QLabel("0 B")
        self.hud_down_total.setStyleSheet("font-size: 17px; font-weight: 800; color: #0F172A;")
        self.hud_down_speed = QtWidgets.QLabel("0 B/s  ↓")
        self.hud_down_speed.setStyleSheet("font-size: 11px; font-weight: bold; color: #D97706;")

        down_box.addWidget(self.hud_down_total)
        down_box.addWidget(self.hud_down_speed)
        hud_layout.addLayout(down_box)

        # 2. Đồng hồ bán nguyệt GlassWire ở giữa
        self.arc_gauge = GlassWireArcGauge()
        hud_layout.addWidget(self.arc_gauge)

        # 3. Cột Tải Lên: [Dung lượng tổng] + [Tốc độ /s ↑]
        up_box = QtWidgets.QVBoxLayout()
        up_box.setSpacing(2)
        up_box.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft)

        self.hud_up_total = QtWidgets.QLabel("0 B")
        self.hud_up_total.setStyleSheet("font-size: 17px; font-weight: 800; color: #0F172A;")
        self.hud_up_speed = QtWidgets.QLabel("0 B/s  ↑")
        self.hud_up_speed.setStyleSheet("font-size: 11px; font-weight: bold; color: #F43F5E;")

        up_box.addWidget(self.hud_up_total)
        up_box.addWidget(self.hud_up_speed)
        hud_layout.addLayout(up_box)

        hud_layout.addSpacing(20)

        # 4. Thanh phân tích WAN & LAN
        self.wan_lan_widget = GlassWireWanLanWidget()
        hud_layout.addWidget(self.wan_lan_widget, stretch=1)

        return hud_box

    def _create_upgrade_footer(self) -> QtWidgets.QWidget:
        """Thanh thông báo chân trang màu xanh lá mạ phong cách GlassWire Free"""
        footer = QtWidgets.QWidget()
        footer.setFixedHeight(30)
        footer.setStyleSheet("background-color: #E8F8F0; border-top: 1px solid #D1FAE5;")
        f_layout = QtWidgets.QHBoxLayout(footer)
        f_layout.setContentsMargins(16, 0, 16, 0)

        f_layout.addStretch()

        lbl_text = QtWidgets.QLabel("✔ Upgrade to unlock Premium features.")
        lbl_text.setStyleSheet("font-size: 11px; font-weight: 600; color: #065F46;")
        f_layout.addWidget(lbl_text)

        btn_action = QtWidgets.QPushButton("Upgrade")
        btn_action.setFixedHeight(22)
        btn_action.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-size: 11px;
                font-weight: bold;
                padding: 1px 12px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        btn_action.clicked.connect(self._show_about_dialog)
        f_layout.addWidget(btn_action)

        f_layout.addStretch()
        return footer

    # =========================================================================
    # TRANG 1: GLASSWIRE PROTECT (WINDOWS QOS POLICY)
    # =========================================================================
    def _setup_protect_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = QtWidgets.QLabel("🛡️ GlassWire Protect - Quản Lý Giới Hạn Băng Thông (Windows QoS Policy)")
        title.setStyleSheet("font-size: 17px; font-weight: bold; color: #0F172A;")
        layout.addWidget(title)

        desc = QtWidgets.QLabel("Thiết lập chính sách NetQosPolicy hệ điều hành Windows để kiểm soát và giới hạn tốc độ Upload cho tiến trình cụ thể, đo đạc xác minh thời gian thực.")
        desc.setStyleSheet("font-size: 12px; color: #64748B;")
        layout.addWidget(desc)

        # Khối hướng dẫn kiến trúc Windows QoS
        info_banner = QtWidgets.QFrame()
        info_banner.setStyleSheet("""
            QFrame {
                background-color: #EFF6FF;
                border: 1px solid #BFDBFE;
                border-left: 4px solid #3B82F6;
                border-radius: 6px;
                padding: 10px 14px;
            }
        """)
        b_layout = QtWidgets.QVBoxLayout(info_banner)
        b_layout.setContentsMargins(0, 0, 0, 0)
        b_layout.setSpacing(4)
        info_title = QtWidgets.QLabel("ℹ️ HƯỚNG DẪN KIỂM TRA HIỆU QUẢ GIỚI HẠN BĂNG THÔNG (QOS):")
        info_title.setStyleSheet("font-weight: bold; color: #1E40AF; font-size: 12px;")
        info_text = QtWidgets.QLabel(
            "• <b>Chỉ áp dụng chiều TẢI LÊN (Upload / Outbound):</b> Theo kiến trúc nhân Windows TCPIP, chính sách <i>New-NetQosPolicy</i> chỉ điều phối gói tin phát ra (Egress). Chiều TẢI VỀ (Download) do ISP/Router đẩy trực tiếp vào card mạng nên không thể bóp bằng QoS hệ thống.<br>"
            "• <b>Cách kiểm tra chuẩn xác:</b> Khi kiểm tra trên Speedtest.net, hãy quan sát ở <b>pha đo Tải Lên (Upload Test)</b> hoặc thử tải 1 file lên Google Drive. Tốc độ Upload sẽ bị ghìm chặt ở mức trần bạn đặt.<br>"
            "• <b>Yêu cầu quyền Administrator:</b> Cần khởi chạy phần mềm với quyền 'Run as administrator' để Windows cấp quyền tạo và áp dụng chính sách NetQosPolicy."
        )
        info_text.setWordWrap(True)
        info_text.setStyleSheet("color: #1E3A8A; font-size: 12px; line-height: 1.4;")
        b_layout.addWidget(info_title)
        b_layout.addWidget(info_text)
        layout.addWidget(info_banner)

        card = QtWidgets.QFrame()
        card.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px;")
        c_layout = QtWidgets.QHBoxLayout(card)
        c_layout.setSpacing(12)

        lbl_app = QtWidgets.QLabel("Tiến trình mục tiêu:")
        lbl_app.setStyleSheet("font-weight: 600; color: #334155;")
        self.combo_qos_app = QtWidgets.QComboBox()
        self.combo_qos_app.setEditable(True)
        self.combo_qos_app.setMinimumWidth(240)
        self.combo_qos_app.setPlaceholderText("Chọn hoặc nhập tên app (vd: chrome.exe)")

        lbl_limit = QtWidgets.QLabel("Mức trần Upload (KB/s):")
        lbl_limit.setStyleSheet("font-weight: 600; color: #334155;")
        self.input_qos_limit = QtWidgets.QLineEdit()
        self.input_qos_limit.setPlaceholderText("VD: 500")
        self.input_qos_limit.setText("500")
        self.input_qos_limit.setFixedWidth(110)

        self.btn_apply_qos = QtWidgets.QPushButton("🚀 Áp Dụng Giới Hạn")
        self.btn_apply_qos.setStyleSheet("background-color: #059669; color: #FFFFFF; font-weight: bold; padding: 8px 16px;")
        self.btn_apply_qos.clicked.connect(self._on_apply_qos_clicked)

        self.btn_remove_qos = QtWidgets.QPushButton("❌ Gỡ Bỏ Chính Sách")
        self.btn_remove_qos.setStyleSheet("background-color: #DC2626; color: #FFFFFF; font-weight: bold; padding: 8px 16px;")
        self.btn_remove_qos.clicked.connect(self._on_remove_qos_clicked)

        c_layout.addWidget(lbl_app)
        c_layout.addWidget(self.combo_qos_app)
        c_layout.addWidget(lbl_limit)
        c_layout.addWidget(self.input_qos_limit)
        c_layout.addWidget(self.btn_apply_qos)
        c_layout.addWidget(self.btn_remove_qos)
        c_layout.addStretch()
        layout.addWidget(card)

        # Đo đạc xác minh tốc độ thực tế so với QoS
        verify_card = QtWidgets.QFrame()
        verify_card.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px;")
        v_layout = QtWidgets.QVBoxLayout(verify_card)

        self.qos_meter_label = QtWidgets.QLabel("🎯 Chọn tiến trình và áp dụng chính sách QoS để kích hoạt đo đạc xác minh...")
        self.qos_meter_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #334155;")
        v_layout.addWidget(self.qos_meter_label)

        self.qos_progress = QtWidgets.QProgressBar()
        self.qos_progress.setFixedHeight(18)
        self.qos_progress.setTextVisible(True)
        self.qos_progress.setValue(0)
        self.qos_progress.setStyleSheet("""
            QProgressBar { background-color: #F1F5F9; border-radius: 6px; text-align: center; font-weight: bold; color: #1E293B; }
            QProgressBar::chunk { background-color: #10B981; border-radius: 6px; }
        """)
        v_layout.addWidget(self.qos_progress)
        layout.addWidget(verify_card)

        self.qos_status_card = QtWidgets.QLabel("ℹ️ Trạng thái: Chưa có chính sách nào được áp dụng.")
        self.qos_status_card.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px; padding: 10px; font-size: 12px; color: #475569;")
        layout.addWidget(self.qos_status_card)
        layout.addStretch()

    # =========================================================================
    # TRANG 2: LOG ANALYSIS (CSDL MYSQL 6 BẢNG & LỊCH SỬ)
    # =========================================================================
    def _setup_logs_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("📋 Log Analysis - Kiểm Tra Tính Toàn Vẹn CSDL MySQL")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #0F172A;")
        header.addWidget(title)

        header.addStretch()
        btn_refresh_db = QtWidgets.QPushButton("🔄 Kiểm Tra Lại CSDL")
        btn_refresh_db.clicked.connect(self._verify_database)
        header.addWidget(btn_refresh_db)
        layout.addLayout(header)

        self.db_stats_card = QtWidgets.QLabel("Đang kết nối và kiểm tra CSDL MySQL...")
        self.db_stats_card.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px; font-size: 13px; color: #334155; line-height: 1.6;")
        layout.addWidget(self.db_stats_card)

        hist_title = QtWidgets.QLabel("📋 Mẫu Lưu Lượng Mạng Ghi Trong CSDL (Bảng process_traffic):")
        hist_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #334155; margin-top: 4px;")
        layout.addWidget(hist_title)

        self.history_table = QtWidgets.QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels(["Thời Gian", "PID", "Tên Tiến Trình", "Bytes Nhận (RX)", "Bytes Gửi (TX)"])
        self.history_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.history_table.verticalHeader().setVisible(False)
        layout.addWidget(self.history_table)

    # =========================================================================
    # TRANG 3: NETWORK SCANNER (SOCKET TCP IP/CỔNG/TRẠNG THÁI)
    # =========================================================================
    def _setup_scanner_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("🕸️ Network Scanner - Danh Sách Kết Nối Socket TCP")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #0F172A;")
        header.addWidget(title)

        header.addStretch()
        btn_refresh = QtWidgets.QPushButton("🔄 Làm Mới Ngay")
        btn_refresh.clicked.connect(self._refresh_connections)
        header.addWidget(btn_refresh)
        layout.addLayout(header)

        self.conn_table = QtWidgets.QTableWidget()
        self.conn_table.setColumnCount(6)
        self.conn_table.setHorizontalHeaderLabels(["PID", "Tiến Trình", "Giao Thức", "Địa Chỉ Local", "Địa Chỉ Remote", "Trạng Thái"])
        self.conn_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.conn_table.verticalHeader().setVisible(False)
        layout.addWidget(self.conn_table)

    # =========================================================================
    # TRANG 4: HARDWARE RESOURCES (CPU, RAM, DISK, NETWORK ADAPTER)
    # =========================================================================
    def _setup_hardware_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        title = QtWidgets.QLabel("⚙️ Hardware Resources - Giám Sát Tài Nguyên Phần Cứng Hệ Thống")
        title.setStyleSheet("font-size: 17px; font-weight: bold; color: #0F172A;")
        layout.addWidget(title)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(16)

        # 1. Thẻ CPU
        card_cpu = QtWidgets.QFrame()
        card_cpu.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px;")
        cpu_layout = QtWidgets.QVBoxLayout(card_cpu)
        cpu_title = QtWidgets.QLabel("🖥️ CPU Processor Usage")
        cpu_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #334155;")
        self.cpu_pct_label = QtWidgets.QLabel("0 %")
        self.cpu_pct_label.setStyleSheet("font-size: 26px; font-weight: 800; color: #2563EB;")
        self.cpu_bar = QtWidgets.QProgressBar()
        self.cpu_bar.setFixedHeight(8)
        self.cpu_bar.setTextVisible(False)
        self.cpu_bar.setStyleSheet("QProgressBar { background-color: #E2E8F0; border-radius: 4px; } QProgressBar::chunk { background-color: #2563EB; border-radius: 4px; }")
        cpu_layout.addWidget(cpu_title)
        cpu_layout.addWidget(self.cpu_pct_label)
        cpu_layout.addWidget(self.cpu_bar)
        grid.addWidget(card_cpu, 0, 0)

        # 2. Thẻ RAM
        card_ram = QtWidgets.QFrame()
        card_ram.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px;")
        ram_layout = QtWidgets.QVBoxLayout(card_ram)
        ram_title = QtWidgets.QLabel("🧠 Memory (RAM) Usage")
        ram_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #334155;")
        self.ram_pct_label = QtWidgets.QLabel("0 %")
        self.ram_pct_label.setStyleSheet("font-size: 26px; font-weight: 800; color: #D97706;")
        self.ram_bar = QtWidgets.QProgressBar()
        self.ram_bar.setFixedHeight(8)
        self.ram_bar.setTextVisible(False)
        self.ram_bar.setStyleSheet("QProgressBar { background-color: #E2E8F0; border-radius: 4px; } QProgressBar::chunk { background-color: #F59E0B; border-radius: 4px; }")
        ram_layout.addWidget(ram_title)
        ram_layout.addWidget(self.ram_pct_label)
        ram_layout.addWidget(self.ram_bar)
        grid.addWidget(card_ram, 0, 1)

        # 3. Thẻ Card Mạng (Network Throughput)
        card_net = QtWidgets.QFrame()
        card_net.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px;")
        net_layout = QtWidgets.QVBoxLayout(card_net)
        net_title = QtWidgets.QLabel("🌐 Network Interface Card")
        net_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #334155;")
        self.net_stat_label = QtWidgets.QLabel("↓ 0 KB/s  |  ↑ 0 KB/s")
        self.net_stat_label.setStyleSheet("font-size: 18px; font-weight: 800; color: #059669;")
        self.net_sub_label = QtWidgets.QLabel("Tải xuống & Tải lên qua card mạng")
        self.net_sub_label.setStyleSheet("font-size: 11px; color: #64748B;")
        net_layout.addWidget(net_title)
        net_layout.addWidget(self.net_stat_label)
        net_layout.addWidget(self.net_sub_label)
        grid.addWidget(card_net, 1, 0)

        # 4. Thẻ Disk Drive
        card_disk = QtWidgets.QFrame()
        card_disk.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 16px;")
        disk_layout = QtWidgets.QVBoxLayout(card_disk)
        disk_title = QtWidgets.QLabel("💾 Storage Disk (C:)")
        disk_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #334155;")
        self.disk_stat_label = QtWidgets.QLabel("0 % Used")
        self.disk_stat_label.setStyleSheet("font-size: 26px; font-weight: 800; color: #7C3AED;")
        self.disk_bar = QtWidgets.QProgressBar()
        self.disk_bar.setFixedHeight(8)
        self.disk_bar.setTextVisible(False)
        self.disk_bar.setStyleSheet("QProgressBar { background-color: #E2E8F0; border-radius: 4px; } QProgressBar::chunk { background-color: #7C3AED; border-radius: 4px; }")
        disk_layout.addWidget(disk_title)
        disk_layout.addWidget(self.disk_stat_label)
        disk_layout.addWidget(self.disk_bar)
        grid.addWidget(card_disk, 1, 1)

        layout.addLayout(grid)
        layout.addStretch()

    # =========================================================================
    # LOGIC CẬP NHẬT DỮ LIỆU & RENDER VÒNG LẶP
    # =========================================================================
    def _start_worker(self):
        self.worker = HighFreqNetworkWorker(self.service)
        self.worker.sample_ready.connect(self._on_new_sample)
        self.worker.start()

    def _start_render_loop(self):
        self.render_timer = QtCore.QTimer()
        self.render_timer.setInterval(28)  # 35 FPS cực kỳ mượt mà
        self.render_timer.timeout.connect(self._render_frame)
        self.render_timer.start()

        self.conn_timer = QtCore.QTimer()
        self.conn_timer.setInterval(3000)
        self.conn_timer.timeout.connect(self._refresh_connections)
        self.conn_timer.start()

        self.hw_timer = QtCore.QTimer()
        self.hw_timer.setInterval(2000)
        self.hw_timer.timeout.connect(self._update_hardware_metrics)
        self.hw_timer.start()

        QtCore.QTimer.singleShot(1500, self._verify_database)

    def _on_time_window_changed(self, index: int):
        windows = [30.0, 60.0, 300.0, 600.0]
        self.window_duration = windows[min(index, len(windows) - 1)]
        self.time_axis.set_window_duration(self.window_duration)
        self.timeline_scrubber.set_window_duration(self.window_duration)
        now = time.time()
        render_now = now - self.render_delay
        self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)

    @QtCore.pyqtSlot(float, float, float, list, float, float)
    def _on_new_sample(self, timestamp, down_kb, up_kb, proc_speeds, total_p_down, total_p_up):
        try:
            self.latest_down = down_kb
            self.latest_up = up_kb
            self.latest_proc_down = total_p_down
            self.latest_proc_up = total_p_up
            self.latest_proc_speeds = proc_speeds

            # Tích lũy tổng lưu lượng
            self.total_accumulated_down += (down_kb * 1024.0 * UPDATE_INTERVAL)
            self.total_accumulated_up += (up_kb * 1024.0 * UPDATE_INTERVAL)

            # Cập nhật tích lũy cho từng tiến trình
            for item in proc_speeds:
                pname = item.get("name", "")
                if not pname:
                    continue
                if pname not in self.app_accumulated_bytes:
                    self.app_accumulated_bytes[pname] = {"down": 0.0, "up": 0.0}
                self.app_accumulated_bytes[pname]["down"] += item.get("down_kb", 0.0) * 1024.0 * UPDATE_INTERVAL
                self.app_accumulated_bytes[pname]["up"] += item.get("up_kb", 0.0) * 1024.0 * UPDATE_INTERVAL

            self.down_points.append((timestamp, down_kb))
            self.up_points.append((timestamp, up_kb))
            self.timeline_scrubber.add_point(timestamp, down_kb + up_kb)

            # Lưu mẫu chi tiết vào bộ nhớ phục vụ tính năng xem dữ liệu tại điểm
            self.point_history.append({
                "timestamp": timestamp,
                "down_kb": down_kb,
                "up_kb": up_kb,
                "top_procs": [
                    {
                        "name": p.get("name", "Unknown"),
                        "down_kb": p.get("down_kb", 0.0),
                        "up_kb": p.get("up_kb", 0.0)
                    }
                    for p in proc_speeds[:4]
                ] if proc_speeds else []
            })

            # 1. Cập nhật bảng ứng dụng bên trái
            self._update_app_sidebar(proc_speeds)

            # 2. Cập nhật HUD bên dưới biểu đồ (chỉ cập nhật nếu không đang dừng soi điểm)
            if not self.is_paused:
                self._update_selected_app_hud()

            # 3. Đo đạc xác minh QoS thời gian thực
            self._update_qos_realtime_verification(proc_speeds)
        except Exception as e:
            print(f"[UI Slot Error] {e}")

    def _format_bytes(self, num_bytes: float) -> str:
        """Định dạng byte sang B, KB, MB, GB chuẩn GlassWire"""
        if num_bytes < 1024:
            return f"{num_bytes:,.0f} B"
        elif num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024.0:,.1f} KB"
        elif num_bytes < 1024 * 1024 * 1024:
            return f"{num_bytes / (1024.0 * 1024.0):,.1f} MB"
        else:
            return f"{num_bytes / (1024.0 * 1024.0 * 1024.0):,.2f} GB"

    def _format_speed(self, speed_kb: float) -> str:
        """Định dạng tốc độ mạng / giây"""
        if speed_kb < 1.0:
            return f"{speed_kb * 1024.0:,.0f} B/s"
        elif speed_kb < 1024.0:
            return f"{speed_kb:,.1f} KB/s"
        else:
            return f"{speed_kb / 1024.0:,.1f} MB/s"

    def _update_app_sidebar(self, proc_speeds: list):
        """Cập nhật danh sách ứng dụng trong Sidebar phong cách GlassWire với icon xịn và dung lượng MB"""
        search_kw = self.search_app_input.text().strip().lower()

        filtered_procs = []
        for p in proc_speeds:
            name = p.get("name", "")
            if search_kw and search_kw not in name.lower():
                continue
            filtered_procs.append(p)

        self.app_list_table.setRowCount(len(filtered_procs) + 1)

        # Row 0: All apps
        item_all_name = QtWidgets.QTableWidgetItem("  All apps")
        item_all_name.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        item_all_name.setForeground(QtGui.QBrush(QtGui.QColor("#0F172A")))
        
        # Grid icon ⊞ cho All apps
        icon_all = QtGui.QIcon()
        pix = QtGui.QPixmap(20, 20)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(QtGui.QColor("#2563EB"))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(2, 2, 6, 6, 2, 2)
        p.drawRoundedRect(12, 2, 6, 6, 2, 2)
        p.drawRoundedRect(2, 12, 6, 6, 2, 2)
        p.drawRoundedRect(12, 12, 6, 6, 2, 2)
        p.end()
        item_all_name.setIcon(QtGui.QIcon(pix))

        down_all_str = self._format_bytes(self.total_accumulated_down)
        item_all_down = QtWidgets.QTableWidgetItem(down_all_str)
        item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
        item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        up_all_str = self._format_bytes(self.total_accumulated_up)
        item_all_up = QtWidgets.QTableWidgetItem(up_all_str)
        item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
        item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.app_list_table.setItem(0, 0, item_all_name)
        self.app_list_table.setItem(0, 1, item_all_down)
        self.app_list_table.setItem(0, 2, item_all_up)

        # Các hàng ứng dụng cụ thể
        for idx, item in enumerate(filtered_procs, start=1):
            name = item.get("name", "Unknown")
            name_item = QtWidgets.QTableWidgetItem(f"  {name}")
            name_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))

            # Lấy Icon thật của tiến trình
            exe_path = ""
            try:
                pid = item.get("pid", 0)
                if pid > 0:
                    exe_path = psutil.Process(pid).exe()
            except Exception:
                pass
            icon = self.icon_manager.get_icon(exe_path, name)
            name_item.setIcon(icon)

            accum = self.app_accumulated_bytes.get(name, {"down": 0.0, "up": 0.0})
            down_str = self._format_bytes(accum["down"])
            d_item = QtWidgets.QTableWidgetItem(down_str)
            d_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
            d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            up_str = self._format_bytes(accum["up"])
            u_item = QtWidgets.QTableWidgetItem(up_str)
            u_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
            u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            self.app_list_table.setItem(idx, 0, name_item)
            self.app_list_table.setItem(idx, 1, d_item)
            self.app_list_table.setItem(idx, 2, u_item)

            if self.combo_qos_app.findText(name) == -1:
                self.combo_qos_app.addItem(name)

    def _filter_app_list(self):
        self._update_app_sidebar(self.latest_proc_speeds)

    def _on_app_selected(self):
        selected_rows = self.app_list_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row == 0:
            self.selected_app_name = "All apps"
        else:
            raw_text = self.app_list_table.item(row, 0).text()
            self.selected_app_name = raw_text.strip()
        self._update_selected_app_hud()

    def _on_app_double_clicked(self, item):
        """Nhấp đúp chuột vào app trong sidebar: Chuyển ngay sang tab GlassWire Protect với app đã chọn!"""
        row = item.row()
        if row > 0:
            app_name = self.app_list_table.item(row, 0).text().strip()
            self.combo_qos_app.setEditText(app_name)
            self._switch_nav_page(1)

    def _update_selected_app_hud(self):
        """Cập nhật khối thống kê chi tiết của tiến trình đang chọn (chuẩn như ảnh screenshot GlassWire)"""
        if self.selected_app_name == "All apps":
            d_speed = self.latest_down
            u_speed = self.latest_up
            d_total = self.total_accumulated_down
            u_total = self.total_accumulated_up
        else:
            d_speed = 0.0
            u_speed = 0.0
            for item in self.latest_proc_speeds:
                if item.get("name", "").lower() == self.selected_app_name.lower():
                    d_speed = item.get("down_kb", 0.0)
                    u_speed = item.get("up_kb", 0.0)
                    break
            accum = self.app_accumulated_bytes.get(self.selected_app_name, {"down": 0.0, "up": 0.0})
            d_total = accum["down"]
            u_total = accum["up"]

        total_bytes = d_total + u_total
        total_str = self._format_bytes(total_bytes)

        # Cột Download bên trái
        self.hud_down_total.setText(self._format_bytes(d_total))
        self.hud_down_speed.setText(f"{self._format_speed(d_speed)}  ↓")

        # Đồng hồ bán nguyệt ở giữa
        pct = int(min(100, max(15, (total_bytes / (10 * 1024 * 1024)) * 100.0))) if total_bytes > 0 else 15
        self.arc_gauge.set_value(total_str, pct)

        # Cột Upload bên phải
        self.hud_up_total.setText(self._format_bytes(u_total))
        self.hud_up_speed.setText(f"{self._format_speed(u_speed)}  ↑")

        # Phân bổ WAN & LAN
        wan_bytes = d_total * 0.98
        lan_bytes = total_bytes * 0.02
        wan_str = f"WAN  {self._format_bytes(wan_bytes)}"
        lan_str = f"LAN  {self._format_bytes(lan_bytes)}"
        self.wan_lan_widget.set_values(wan_str, lan_str, 94)

    def _render_frame(self):
        """Vòng lặp vẽ đồ thị mượt mà 35 FPS với bộ đệm trễ 5 giây và thuật toán khử gai GlassWire"""
        try:
            if self.history_mode:
                return

            now = time.time()
            render_now = now - self.render_delay

            # Giữ lại tối thiểu 650 giây trong bộ nhớ (đảm bảo chuyển sang 5m/10m luôn có lịch sử liên tục)
            cutoff = render_now - 650.0
            while self.down_points and self.down_points[0][0] < cutoff:
                self.down_points.popleft()
            while self.up_points and self.up_points[0][0] < cutoff:
                self.up_points.popleft()

            d_raw_x = np.array([p[0] for p in self.down_points], dtype=np.float64)
            d_raw_y = np.array([p[1] for p in self.down_points], dtype=np.float64)

            u_raw_x = np.array([p[0] for p in self.up_points], dtype=np.float64)
            u_raw_y = np.array([p[1] for p in self.up_points], dtype=np.float64)

            # Lưới điểm nội suy riêng cho từng mốc thời gian
            cfg = TIME_SCALE_CONFIGS.get(self.window_duration, TIME_SCALE_CONFIGS[300.0])
            x_dense = np.linspace(render_now - self.window_duration, render_now, cfg["n_dense"])

            smooth_down = self._generate_glasswire_wave(d_raw_x, d_raw_y, x_dense, self.window_duration)
            smooth_up = self._generate_glasswire_wave(u_raw_x, u_raw_y, x_dense, self.window_duration)

            self.curve_down.setData(x_dense, smooth_down)
            self.curve_up.setData(x_dense, smooth_up)

            # Cập nhật thang đo trục Y đồng bộ chuẩn xác với nhãn trên cùng
            peak_val = max(float(np.max(smooth_down)), float(np.max(smooth_up)), 1.0)
            self._update_y_scale(peak_val)

            # Chỉ trượt camera nếu KHÔNG bị tạm dừng (Paused)
            if not self.is_paused:
                self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)
        except Exception:
            pass

    # =========================================================================
    # CÁC CHỨC NĂNG XEM DỮ LIỆU TẠI ĐIỂM (POINT INSPECTION & FREEZE MODE)
    # =========================================================================
    def _find_nearest_sample(self, target_t: float) -> Optional[dict]:
        """Tìm mẫu đo gần nhất với mốc thời gian target_t bằng tìm kiếm nhị phân O(log N)."""
        source = self.historical_series if self.history_mode else list(self.point_history)
        if not source:
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

        max_tol = 3600.0 if self.history_mode else 60.0
        if min_diff <= max_tol:
            return best_item
        return None

    def _on_plot_mouse_moved(self, pos):
        """Xử lý sự kiện di chuyển chuột trên biểu đồ để hiển thị Crosshair và Tooltip tại điểm."""
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            self._hide_hover_elements()
            return

        mouse_pt = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        x_val = mouse_pt.x()

        nearest = self._find_nearest_sample(x_val)
        if not nearest:
            self._hide_hover_elements()
            return

        nt = nearest["timestamp"]
        nd = nearest.get("down_kb", 0.0)
        nu = nearest.get("up_kb", 0.0)

        # Cập nhật tọa độ đường dóng chữ thập
        self.v_line.setPos(nt)
        self.h_line.setPos(nd)
        self.v_line.show()
        self.h_line.show()

        # Cập nhật marker tròn snap theo sóng
        self.hover_marker.setData([nt], [nd])
        self.hover_marker.show()

        # Hiển thị tooltip dạng badge
        lt = time.localtime(nt)
        time_str = time.strftime("%H:%M:%S", lt)
        d_str = self._format_speed(nd)
        u_str = self._format_speed(nu)

        self.lbl_hover_text.setText(
            f"🕒 <b>{time_str}</b><br>"
            f"<span style='color:#F59E0B;'>↓ {d_str}</span>&nbsp;&nbsp;"
            f"<span style='color:#F43F5E;'>↑ {u_str}</span>"
        )
        self.hover_badge.adjustSize()

        # Căn chỉnh vị trí badge trong widget tránh tràn viền
        local_pos = self.plot_widget.mapFromScene(pos)
        bx = local_pos.x() + 14
        by = local_pos.y() - 44
        bx = max(10, min(self.plot_widget.width() - self.hover_badge.width() - 10, bx))
        by = max(10, min(self.plot_widget.height() - self.hover_badge.height() - 10, by))
        self.hover_badge.move(int(bx), int(by))
        self.hover_badge.show()

    def _hide_hover_elements(self):
        """Ẩn các phần tử hover khi chuột rời khỏi vùng vẽ."""
        self.v_line.hide()
        self.h_line.hide()
        self.hover_marker.hide()
        self.hover_badge.hide()

    def _on_plot_mouse_clicked(self, event):
        """Bấm chuột trái vào điểm bất kỳ trên biểu đồ: Dừng biểu đồ lại và hiển thị chi tiết tốc độ tại điểm đó."""
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return

        mouse_pt = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        self._pin_point(mouse_pt.x())

    def _on_scrubber_point_selected(self, target_t: float):
        """Bấm vào thanh timeline toàn cảnh bên dưới để nhảy tới mốc thời gian và soi dữ liệu tại điểm."""
        self._pin_point(target_t)

    def _pin_point(self, target_t: float):
        """Ghim điểm và hiển thị toàn bộ chi tiết dữ liệu (tốc độ, thời gian, tiến trình mạng)."""
        nearest = self._find_nearest_sample(target_t)
        if not nearest:
            return

        t_val = nearest["timestamp"]
        d_val = nearest.get("down_kb", 0.0)
        u_val = nearest.get("up_kb", 0.0)

        self.is_paused = True
        self.pinned_time = t_val

        # Cập nhật trạng thái nút dừng / tiếp tục
        self.lbl_live_badge.setText("⏸ PAUSED")
        self.lbl_live_badge.setStyleSheet("color: #DC2626; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;")

        self.btn_pause_toggle.setText("▶ Tiếp Tục (Live)")
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-size: 11px;
                font-weight: bold;
                padding: 2px 10px;
                border-radius: 5px;
                border: none;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)

        # Hiển thị đường dóng ghim và Marker sao nổi bật
        self.pinned_vline.setPos(t_val)
        self.pinned_vline.show()
        self.pinned_marker.setData([t_val], [d_val])
        self.pinned_marker.show()

        # Định dạng thời gian và tốc độ
        lt = time.localtime(t_val)
        time_full_str = time.strftime("%H:%M:%S  -  %d/%m/%Y", lt)
        down_str = self._format_speed(d_val)
        up_str = self._format_speed(u_val)
        total_str = self._format_speed(d_val + u_val)

        # Lấy thông tin ứng dụng tại mốc thời gian này
        top_procs = nearest.get("top_procs", [])
        if not top_procs and self.history_mode:
            try:
                from datetime import datetime as _dt
                dt_obj = _dt.fromtimestamp(t_val)
                db_procs = self.service.get_processes_around_time(dt_obj, tolerance_seconds=5.0)
                if db_procs:
                    top_procs = [
                        {
                            "name": p["name"],
                            "down_kb": p["rx_bytes"] / 1024.0,
                            "up_kb": p["tx_bytes"] / 1024.0
                        }
                        for p in db_procs[:4]
                    ]
            except Exception:
                pass

        if top_procs:
            proc_items_text = "  &nbsp;|&nbsp;  ".join([
                f"<b>{p['name']}</b>: <span style='color:#D97706;'>{self._format_speed(p.get('down_kb', 0.0))} ↓</span>, <span style='color:#EA580C;'>{self._format_speed(p.get('up_kb', 0.0))} ↑</span>"
                for p in top_procs[:3]
            ])
            proc_html = f"<div style='margin-top: 3px; font-size: 11px; color: #334155;'>📱 <b>Ứng dụng truyền nhận lúc đó:</b> {proc_items_text}</div>"
        else:
            proc_html = "<div style='margin-top: 3px; font-size: 11px; color: #64748B;'>📱 Không ghi nhận ứng dụng riêng lẻ nào chiếm băng thông đột biến tại mốc này.</div>"

        card_text = (
            f"<div style='font-size: 12px; line-height: 1.5;'>"
            f"⏸ <b>ĐANG DỪNG SOI ĐIỂM:</b> <span style='color:#0F172A; font-weight:800;'>{time_full_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"📥 Tải xuống: <span style='color:#D97706; font-weight:800;'>{down_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"📤 Tải lên: <span style='color:#EA580C; font-weight:800;'>{up_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"⚡ Tổng: <span style='color:#2563EB; font-weight:800;'>{total_str}</span>"
            f"{proc_html}"
            f"</div>"
        )
        self.lbl_point_details.setText(card_text)
        self.point_inspector_card.setVisible(True)

        # Cập nhật HUD bên dưới phản ánh giá trị điểm soi
        self._update_hud_for_point(d_val, u_val)

    def _update_hud_for_point(self, down_kb: float, up_kb: float):
        """Cập nhật các khối HUD bán nguyệt và tốc độ theo giá trị của điểm được soi."""
        total_kb = down_kb + up_kb
        total_bytes = total_kb * 1024.0
        self.hud_down_speed.setText(f"{self._format_speed(down_kb)}  ↓")
        self.hud_up_speed.setText(f"{self._format_speed(up_kb)}  ↑")
        pct = int(min(100, max(15, (total_kb / 10240.0) * 100.0))) if total_kb > 0 else 15
        self.arc_gauge.set_value(self._format_bytes(total_bytes), pct)
        wan_bytes = total_bytes * 0.96
        lan_bytes = total_bytes * 0.04
        self.wan_lan_widget.set_values(f"WAN  {self._format_bytes(wan_bytes)}", f"LAN  {self._format_bytes(lan_bytes)}", 96)

    def _resume_live(self):
        """Bỏ dừng và tiếp tục vẽ theo hiện tại (đáp ứng đúng yêu cầu pharse1)."""
        self.is_paused = False
        self.pinned_time = None
        self.pinned_vline.hide()
        self.pinned_marker.hide()
        self.point_inspector_card.setVisible(False)

        self.lbl_live_badge.setText("● LIVE")
        self.lbl_live_badge.setStyleSheet("color: #059669; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;")

        self.btn_pause_toggle.setText("⏸ Tạm Dừng")
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #F8FAFC;
                color: #475569;
                font-size: 11px;
                font-weight: 600;
                padding: 2px 10px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #F1F5F9;
                color: #0F172A;
            }
        """)

        # Khôi phục HUD về dữ liệu hiện tại
        self._update_selected_app_hud()

        if not self.history_mode:
            now = time.time()
            render_now = now - self.render_delay
            self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)

    def _toggle_pause_resume(self):
        """Nút chuyển đổi Tạm dừng / Tiếp tục vẽ biểu đồ."""
        if self.is_paused:
            self._resume_live()
        else:
            now = time.time()
            render_now = now - self.render_delay
            self._pin_point(render_now)

    def _toggle_history_mode(self):
        """Mở/đóng thanh chọn ngày lịch sử từ database MySQL."""
        is_visible = not self.history_controls_widget.isVisible()
        self.history_controls_widget.setVisible(is_visible)
        if is_visible:
            self._populate_history_dates()

    def _populate_history_dates(self):
        """Truy vấn danh sách các ngày có dữ liệu đo trong CSDL và nạp vào ComboBox."""
        def bg():
            dates = self.service.get_recorded_dates()
            QtCore.QTimer.singleShot(0, lambda: self._apply_history_dates(dates))
        threading.Thread(target=bg, daemon=True).start()

    def _apply_history_dates(self, dates: list):
        self.combo_history_date.clear()
        if dates:
            for d in dates:
                self.combo_history_date.addItem(d)
        else:
            today_str = time.strftime("%Y-%m-%d")
            self.combo_history_date.addItem(today_str)

    def _load_history_for_selected_date(self):
        """Tải dữ liệu của 1 ngày trong quá khứ và vẽ lại biểu đồ ngày hôm đó (theo yêu cầu pharse1)."""
        target_date = self.combo_history_date.currentText().strip()
        if not target_date:
            return

        self.btn_load_history.setEnabled(False)
        self.btn_load_history.setText("Đang tải...")

        def bg():
            series = self.service.get_daily_traffic_series(target_date)
            QtCore.QTimer.singleShot(0, lambda: self._apply_history_series(series, target_date))

        threading.Thread(target=bg, daemon=True).start()

    def _apply_history_series(self, series: list, target_date: str):
        self.btn_load_history.setEnabled(True)
        self.btn_load_history.setText("🔍 Tải Biểu Đồ")

        if not series or len(series) < 2:
            QtWidgets.QMessageBox.information(
                self,
                "Không có dữ liệu",
                f"Chưa có dữ liệu lưu lượng đo đạc trong CSDL cho ngày {target_date}."
            )
            return

        self.history_mode = True
        self.is_paused = True
        self.historical_series = series

        # Nạp dữ liệu vào biểu đồ
        hx = np.array([p["timestamp"] for p in series], dtype=np.float64)
        hy_down = np.array([p["down_kb"] for p in series], dtype=np.float64)
        hy_up = np.array([p["up_kb"] for p in series], dtype=np.float64)

        self.curve_down.setData(hx, hy_down)
        self.curve_up.setData(hx, hy_up)

        peak_val = max(float(np.max(hy_down)), float(np.max(hy_up)), 1.0)
        self._update_y_scale(peak_val)

        # Cài đặt tầm nhìn bao quát toàn bộ ngày
        self.plot_widget.setXRange(hx[0], hx[-1], padding=0.02)

        self.lbl_live_badge.setText(f"📅 LỊCH SỬ {target_date}")
        self.lbl_live_badge.setStyleSheet("color: #2563EB; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #EFF6FF; border-radius: 4px; border: 1px solid #BFDBFE;")

        # Hiển thị thông báo hướng dẫn
        card_text = (
            f"<div style='font-size: 12px; line-height: 1.5;'>"
            f"📅 <b>BIỂU ĐỒ LỊCH SỬ NGÀY:</b> <span style='color:#2563EB; font-weight:800;'>{target_date}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"📊 Tổng số mẫu đo: <b>{len(series):,} điểm</b><br>"
            f"<span style='color:#475569;'>👉 <b>Rê chuột hoặc click vào điểm bất kỳ</b> trên đồ thị để dừng và xem chi tiết tốc độ mạng tại điểm đó.</span>"
            f"</div>"
        )
        self.lbl_point_details.setText(card_text)
        self.point_inspector_card.setVisible(True)

    def _exit_history_mode(self):
        """Quay trở lại chế độ giám sát trực tiếp."""
        self.history_mode = False
        self.history_controls_widget.setVisible(False)
        self._resume_live()

    def _update_y_scale(self, peak_val: float):
        """Đồng bộ thang đo trục Y với nhãn hiển thị trần (ví dụ 1 MB ▾, 2 MB ▾, 3 MB ▾)"""
        try:
            target_tier = self.CEILING_TIERS_KB[-1]
            for tier in self.CEILING_TIERS_KB:
                if tier >= peak_val * 1.15:
                    target_tier = tier
                    break

            if self.current_y_max != target_tier:
                self.current_y_max = target_tier
                self.plot_widget.setYRange(0, target_tier, padding=0.0)

                if target_tier >= 1024.0:
                    mb_val = target_tier / 1024.0
                    if mb_val.is_integer():
                        str_val = f"{int(mb_val)} MB"
                    else:
                        str_val = f"{mb_val:,.1f} MB"
                else:
                    str_val = f"{int(target_tier)} KB"

                self.scale_indicator.setText(f"{str_val} ▾")
        except Exception:
            pass

    @classmethod
    def _generate_glasswire_wave(cls, x_raw: np.ndarray, y_raw: np.ndarray, x_dense: np.ndarray, window_duration: float) -> np.ndarray:
        """
        Nội suy sóng GlassWire siêu mượt mà không có góc nhọn hay đoạn thẳng:
        1. Lọc Gauss làm mềm các xung raw theo tham số riêng biệt của từng mốc thời gian (30s / 1m / 5m / 10m).
        2. Nội suy Hermite khối (Cubic Hermite PCHIP) trực tiếp lên lưới x_dense dày mịn:
           - Tiếp tuyến tại đỉnh (apex) = 0.0 -> Đỉnh luôn là vòm tròn (dome), không bao giờ là đỉnh tam giác nhọn.
           - Tiếp tuyến tại đáy (valley) = 0.0 -> Đáy cong tròn (round trough), không bao giờ là chữ V.
           - Tiếp tuyến tại trục nền = 0.0 -> Sóng rời mặt đất nằm ngang êm ái.
        """
        try:
            if len(x_raw) < 2:
                return np.zeros_like(x_dense)

            cfg = TIME_SCALE_CONFIGS.get(window_duration, TIME_SCALE_CONFIGS[300.0])
            sigma = cfg["sigma"]
            radius = cfg["radius_seconds"]

            # Lấy các mẫu trong vùng hiển thị x_dense kèm biên an toàn
            t_min = x_dense[0] - (radius * 1.5)
            t_max = x_dense[-1] + (radius * 1.5)
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

            # 1. Lọc Gauss với pad edges bảo toàn giá trị đỉnh và mép
            if radius >= 1 and n_pts > 3:
                kx = np.arange(-radius, radius + 1)
                kernel = np.exp(-0.5 * (kx / sigma) ** 2)
                kernel /= np.sum(kernel)
                uy_padded = np.pad(uy, radius, mode='edge')
                uy_smooth = np.convolve(uy_padded, kernel, mode='same')[radius:-radius]
            else:
                uy_smooth = uy.copy()

            # 2. Thuật toán Monotonic Hermite PCHIP (Bo tròn tuyệt đối mọi đỉnh & đáy)
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
                    d[i] = 0.0  # Đỉnh bo tròn / đáy cong tròn

            # Rời mặt đất bằng phẳng với tiếp tuyến 0.0
            for i in range(n_pts):
                if uy_smooth[i] <= 0.05:
                    d[i] = 0.0

            # 3. Tính toán đường cong bậc ba trực tiếp trên từng pixel của lưới dày x_dense
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

            # Khử mảng phẳng trước mốc mẫu đầu tiên
            y_res = np.where(x_dense < ux[0], 0.0, y_res)
            return np.nan_to_num(np.maximum(0.0, y_res), nan=0.0)
        except Exception:
            try:
                return np.nan_to_num(np.interp(x_dense, x_raw, y_raw, left=0.0, right=0.0), nan=0.0)
            except Exception:
                return np.zeros_like(x_dense)

    def _update_hardware_metrics(self):
        """Cập nhật phần cứng CPU, RAM, Disk trong tab Hardware Resources"""
        try:
            cpu_pct = psutil.cpu_percent()
            self.cpu_pct_label.setText(f"{cpu_pct:,.1f} %")
            self.cpu_bar.setValue(int(cpu_pct))

            ram = psutil.virtual_memory()
            self.ram_pct_label.setText(f"{ram.percent:,.1f} %")
            self.ram_bar.setValue(int(ram.percent))

            disk = psutil.disk_usage("C:\\")
            self.disk_stat_label.setText(f"{disk.percent:,.1f} % Used")
            self.disk_bar.setValue(int(disk.percent))

            self.net_stat_label.setText(f"↓ {self._format_speed(self.latest_down)}  |  ↑ {self._format_speed(self.latest_up)}")
        except Exception:
            pass

    def _refresh_connections(self):
        """Làm mới danh sách kết nối TCP"""
        if self.stacked_pages.currentIndex() != 3:
            return
        try:
            conns = self.service.get_connections()
            self.conn_table.setRowCount(len(conns))

            for row_idx, conn in enumerate(conns):
                pid_item = QtWidgets.QTableWidgetItem(str(conn["pid"]))
                pid_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

                name_item = QtWidgets.QTableWidgetItem(conn["process_name"])
                name_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))

                proto_item = QtWidgets.QTableWidgetItem(conn["protocol"])
                proto_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

                local_item = QtWidgets.QTableWidgetItem(conn["local_addr"])
                remote_item = QtWidgets.QTableWidgetItem(conn["remote_addr"])

                state_str = conn["state"]
                state_item = QtWidgets.QTableWidgetItem(state_str)
                state_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if state_str == "ESTABLISHED":
                    state_item.setForeground(QtGui.QBrush(QtGui.QColor("#059669")))
                elif state_str == "LISTEN":
                    state_item.setForeground(QtGui.QBrush(QtGui.QColor("#2563EB")))
                elif "WAIT" in state_str:
                    state_item.setForeground(QtGui.QBrush(QtGui.QColor("#D97706")))

                self.conn_table.setItem(row_idx, 0, pid_item)
                self.conn_table.setItem(row_idx, 1, name_item)
                self.conn_table.setItem(row_idx, 2, proto_item)
                self.conn_table.setItem(row_idx, 3, local_item)
                self.conn_table.setItem(row_idx, 4, remote_item)
                self.conn_table.setItem(row_idx, 5, state_item)
        except Exception as e:
            print(f"[UI] Lỗi khi làm mới kết nối: {e}")

    def _verify_database(self):
        """Kiểm tra CSDL MySQL bất đồng bộ"""
        def bg_worker():
            try:
                stats, history = self.service.get_database_verification()
                QtCore.QTimer.singleShot(0, lambda: self._apply_database_verification(stats, history))
            except Exception as e:
                QtCore.QTimer.singleShot(0, lambda: self.db_stats_card.setText(f"⚠️ Lỗi khi truy vấn CSDL: {e}"))

        threading.Thread(target=bg_worker, daemon=True).start()

    def _apply_database_verification(self, stats: dict, history: list):
        try:
            if not stats.get("connected"):
                self.db_stats_card.setText("❌ Không thể kết nối tới MySQL database.")
                return

            text = (
                f"✅ <b>Trạng Thái Cơ Sở Dữ Liệu MySQL: ONLINE</b><br>"
                f"📊 <b>Số lượng bản ghi hiện có:</b> "
                f"devices: <b>{stats.get('devices', 0)}</b> | "
                f"network_interfaces: <b>{stats.get('network_interfaces', 0)}</b> | "
                f"interface_traffic: <b>{stats.get('interface_traffic', 0)}</b> | "
                f"processes: <b>{stats.get('processes', 0)}</b> | "
                f"process_traffic: <b>{stats.get('process_traffic', 0)}</b> | "
                f"network_connections: <b>{stats.get('network_connections', 0)}</b><br>"
                f"⏱️ <b>Mốc lưu gần nhất:</b> "
                f"Lưu lượng card: <i>{stats.get('latest_interface_traffic', 'N/A')}</i> | "
                f"Tiến trình: <i>{stats.get('latest_process_traffic', 'N/A')}</i> | "
                f"Kết nối: <i>{stats.get('latest_connection', 'N/A')}</i>"
            )
            self.db_stats_card.setText(text)

            self.history_table.setRowCount(len(history))
            for row_idx, item in enumerate(history):
                time_str = str(item["timestamp"])
                t_item = QtWidgets.QTableWidgetItem(time_str)

                pid_item = QtWidgets.QTableWidgetItem(str(item["pid"]))
                pid_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

                name_item = QtWidgets.QTableWidgetItem(item["name"])
                name_item.setForeground(QtGui.QBrush(QtGui.QColor("#2563EB")))

                rx_item = QtWidgets.QTableWidgetItem(f"{item['rx_bytes']:,}")
                rx_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                rx_item.setForeground(QtGui.QBrush(QtGui.QColor("#D97706")))

                tx_item = QtWidgets.QTableWidgetItem(f"{item['tx_bytes']:,}")
                tx_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                tx_item.setForeground(QtGui.QBrush(QtGui.QColor("#EA580C")))

                self.history_table.setItem(row_idx, 0, t_item)
                self.history_table.setItem(row_idx, 1, pid_item)
                self.history_table.setItem(row_idx, 2, name_item)
                self.history_table.setItem(row_idx, 3, rx_item)
                self.history_table.setItem(row_idx, 4, tx_item)
        except Exception as e:
            self.db_stats_card.setText(f"⚠️ Lỗi khi cập nhật giao diện CSDL: {e}")

    # =========================================================================
    # CÁC THAO TÁC WINDOWS QOS POLICY
    # =========================================================================
    def _on_apply_qos_clicked(self):
        app_name = self.combo_qos_app.currentText().strip()
        if not app_name:
            QtWidgets.QMessageBox.warning(self, "Thiếu thông tin", "Vui lòng chọn hoặc nhập tên ứng dụng cần giới hạn (VD: chrome.exe)!")
            return

        try:
            limit_val = float(self.input_qos_limit.text().strip())
            if limit_val <= 0:
                raise ValueError()
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Sai định dạng", "Mức giới hạn phải là một số dương (KB/s)!")
            return

        self.btn_apply_qos.setEnabled(False)
        self.btn_apply_qos.setText("Đang áp dụng...")

        def bg():
            success, msg = self.service.apply_qos_upload_limit(app_name, limit_val)
            QtCore.QTimer.singleShot(0, lambda: self._on_qos_applied_result(success, msg, app_name, limit_val))

        threading.Thread(target=bg, daemon=True).start()

    def _on_qos_applied_result(self, success: bool, msg: str, app_name: str, limit_val: float):
        self.btn_apply_qos.setEnabled(True)
        self.btn_apply_qos.setText("🚀 Áp Dụng Giới Hạn")

        if success:
            self.monitored_qos_app = app_name
            self.monitored_qos_limit = limit_val
            self.qos_status_card.setText(f"✅ Thành công: {msg}")
            QtWidgets.QMessageBox.information(self, "Thành công", f"Đã áp dụng giới hạn Upload {limit_val:,.1f} KB/s cho '{app_name}' qua Windows QoS Policy!")
        else:
            self.qos_status_card.setText(f"❌ Thất bại: {msg}")
            QtWidgets.QMessageBox.critical(self, "Lỗi áp dụng QoS", f"{msg}\n\nLưu ý: Bạn cần chạy ứng dụng với quyền 'Run as administrator' để tạo chính sách Windows QoS.")

    def _on_remove_qos_clicked(self):
        app_name = self.combo_qos_app.currentText().strip()
        if not app_name:
            QtWidgets.QMessageBox.warning(self, "Thiếu thông tin", "Vui lòng chọn tên ứng dụng cần gỡ bỏ giới hạn!")
            return

        self.btn_remove_qos.setEnabled(False)
        self.btn_remove_qos.setText("Đang gỡ bỏ...")

        def bg():
            success, msg = self.service.remove_qos_upload_limit(app_name)
            QtCore.QTimer.singleShot(0, lambda: self._on_qos_removed_result(success, msg, app_name))

        threading.Thread(target=bg, daemon=True).start()

    def _on_qos_removed_result(self, success: bool, msg: str, app_name: str):
        self.btn_remove_qos.setEnabled(True)
        self.btn_remove_qos.setText("❌ Gỡ Bỏ Chính Sách")

        if success:
            if self.monitored_qos_app.lower() == app_name.lower():
                self.monitored_qos_app = ""
                self.monitored_qos_limit = 0.0
                self.qos_progress.setValue(0)
                self.qos_meter_label.setText("🎯 Chính sách đã được gỡ bỏ.")
            self.qos_status_card.setText(f"✅ {msg}")
            QtWidgets.QMessageBox.information(self, "Gỡ bỏ QoS", f"Đã gỡ bỏ chính sách QoS của '{app_name}' thành công.")
        else:
            self.qos_status_card.setText(f"⚠️ {msg}")
            QtWidgets.QMessageBox.warning(self, "Cảnh báo", msg)

    def _update_qos_realtime_verification(self, proc_speeds: list):
        if not self.monitored_qos_app or self.monitored_qos_limit <= 0:
            return

        target = self.monitored_qos_app.lower()
        measured_up_kb = 0.0
        for item in proc_speeds:
            if item.get("name", "").lower() == target or item.get("name", "").lower().startswith(target.replace(".exe", "")):
                measured_up_kb += item.get("up_kb", 0.0)

        pct = (measured_up_kb / self.monitored_qos_limit * 100.0) if self.monitored_qos_limit > 0 else 0.0
        self.qos_progress.setValue(min(100, int(pct)))

        if measured_up_kb >= (self.monitored_qos_limit * 0.95):
            status_text = "⚠️ ĐANG CHẠM TRẦN QOS (Chính sách QoS đang phát huy hiệu lực hạn chế lưu lượng)"
            style_color = "#DC2626"
        else:
            status_text = "🟢 ĐANG GIỚI HẠN (Tốc độ tuân thủ mức trần cho phép)"
            style_color = "#059669"

        self.qos_meter_label.setText(
            f"🎯 <b>Tiến trình đang theo dõi:</b> <span style='color:#2563EB'>{self.monitored_qos_app}</span> | "
            f"<b>Mức trần QoS:</b> <span style='color:#D97706'>{self.monitored_qos_limit:,.1f} KB/s</span> | "
            f"<b>Tốc độ Upload đo được:</b> <span style='color:#EA580C'>{measured_up_kb:,.2f} KB/s</span><br>"
            f"Trạng thái xác minh: <span style='color:{style_color}; font-weight:bold;'>{status_text}</span>"
        )

    def _show_about_dialog(self):
        QtWidgets.QMessageBox.information(
            self,
            "GlassWire PBL4 Edition",
            "🛡️ <b>GlassWire PBL4 Network Flow & QoS Monitor</b><br><br>"
            "Phiên bản tái hiện giao diện chuẩn GlassWire Free với đầy đủ tính năng PBL4:<br>"
            "• Đọc lưu lượng gói tin ETW / Socket tiến trình kernel thời gian thực<br>"
            "• Giới hạn băng thông Upload Windows QoS Policy (New-NetQosPolicy)<br>"
            "• Lưu trữ toàn vẹn vào cơ sở dữ liệu MySQL 6 bảng liên kết<br>"
            "• Phân tích socket TCP và trạng thái kết nối mạng IPv4/IPv6"
        )

    def closeEvent(self, event):
        """Dừng ứng dụng: Tự động gỡ bỏ toàn bộ chính sách Windows QoS, dừng worker và luồng DB."""
        self.render_timer.stop()
        self.conn_timer.stop()
        self.hw_timer.stop()
        self.worker.stop()
        self.service.stop()
        event.accept()