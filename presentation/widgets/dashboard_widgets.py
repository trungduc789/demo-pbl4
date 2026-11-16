import time
from collections import deque
from typing import Dict, Optional, Tuple
from PyQt6 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg

from application.monitor_service import MonitorService
from config.settings import UPDATE_INTERVAL
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
        "tick_step": 10.0,       # Vạch mốc mỗi 10 giây
    },
    300.0: {
        "name": "5 Minutes",
        "sigma": 10.0,          # Độ lệch chuẩn 10s tạo dải cát vàng cuộn 25-35s chuẩn GlassWire
        "radius_seconds": 30,
        "n_dense": 380,
        "tick_step": 40.0,       # Vạch mốc mỗi 40s (ví dụ: 7:14:40PM tới 7:15:20PM theo đúng yêu cầu)
    },
    600.0: {
        "name": "10 Minutes",
        "sigma": 20.0,          # Sóng thoải rộng toàn cảnh 10 phút (dải sóng 45-60s)
        "radius_seconds": 55,
        "n_dense": 450,
        "tick_step": 60.0,       # Vạch mốc mỗi 60 giây (nối gần lại)
    },
    3600.0: {
        "name": "1 Hour",
        "sigma": 40.0,          # Khung nhìn 1 tiếng
        "radius_seconds": 120,
        "n_dense": 600,
        "tick_step": 300.0,      # Vạch mốc mỗi 5 phút
    },
    86400.0: {
        "name": "24 Hours",
        "sigma": 1.2,           # Độ mịn Gaussian phù hợp với 480 bucket 3 phút của toàn cảnh 24 giờ
        "radius_seconds": 3,     # Bán kính 3 bucket
        "n_dense": 800,
        "tick_step": 7200.0,     # Vạch mốc mỗi 2 giờ
    }
}


class TimeAxisItem(pg.AxisItem):
    """Trục hoành hiển thị mốc giờ hệ thống được định hình chuyên biệt theo từng mốc scale thời gian (hỗ trợ cả Live và Lịch sử ngày)"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_window = 300.0

    def set_window_duration(self, duration: float):
        self.current_window = duration
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        span = max(1.0, maxVal - minVal)
        if span > 600.0:
            if span <= 1800.0:
                step = 180.0      # 3 phút
            elif span <= 3600.0:
                step = 300.0      # 5 phút (cho khung 1 Hour)
            elif span <= 7200.0:
                step = 600.0      # 10 phút
            elif span <= 21600.0:
                step = 1800.0     # 30 phút
            elif span <= 43200.0:
                step = 3600.0     # 1 giờ
            elif span <= 86400.0:
                step = 7200.0     # 2 giờ
            else:
                step = 14400.0    # 4 giờ
        else:
            cfg = TIME_SCALE_CONFIGS.get(self.current_window, TIME_SCALE_CONFIGS[300.0])
            step = cfg["tick_step"]

        start = (int(minVal) // int(step)) * int(step)
        ticks = []
        curr = start
        while curr <= maxVal + step:
            if curr >= minVal - step:
                ticks.append(curr)
            curr += step
            if len(ticks) > 30:
                break
        return [(step, ticks)]

    def tickStrings(self, values, scale, spacing):
        span = self.current_window
        if values and len(values) >= 2:
            span = max(span, values[-1] - values[0])

        res = []
        for val in values:
            try:
                lt = time.localtime(val)
                hr = lt.tm_hour % 12
                hr = 12 if hr == 0 else hr
                am_pm = "PM" if lt.tm_hour >= 12 else "AM"
                # Khi khung >= 1 giờ (3600s), hiển thị giờ và phút.
                # Khi khung < 1 giờ (30s, 1m, 5m, 10m): luôn hiển thị đầy đủ cả giây (ví dụ: 7:14:40 PM -> 7:15:20 PM)
                if span >= 3600.0:
                    res.append(f"{hr}:{lt.tm_min:02d} {am_pm}")
                else:
                    res.append(f"{hr}:{lt.tm_min:02d}:{lt.tm_sec:02d} {am_pm}")
            except Exception:
                res.append("")
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


class GlassWireAppHoverPopup(QtWidgets.QFrame):
    """
    Bảng popup xổ xuống khi rê chuột vào pill 'App + X more' (chuẩn như Hình 2).
    Hiển thị tiêu đề App, Down, Up và danh sách tất cả các ứng dụng có lưu lượng tại thời điểm đó.
    """
    def __init__(self, icon_manager, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.ToolTip | QtCore.Qt.WindowType.FramelessWindowHint)
        self.icon_manager = icon_manager
        self.setObjectName("glasswireAppPopup")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(340)

        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(16)
        shadow.setColor(QtGui.QColor(15, 23, 42, 60))
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        container = QtWidgets.QFrame()
        container.setObjectName("popupContainer")
        container.setStyleSheet("""
            QFrame#popupContainer {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
            }
        """)
        c_layout = QtWidgets.QVBoxLayout(container)
        c_layout.setContentsMargins(8, 6, 8, 6)
        c_layout.setSpacing(4)

        # Header: App | ↓ | ↑
        header = QtWidgets.QWidget()
        h_layout = QtWidgets.QHBoxLayout(header)
        h_layout.setContentsMargins(4, 2, 4, 4)
        h_layout.setSpacing(6)

        lbl_app = QtWidgets.QLabel("Ứng dụng truyền nhận lúc đó")
        lbl_app.setStyleSheet("font-size: 11px; font-weight: bold; color: #475569;")
        h_layout.addWidget(lbl_app, stretch=1)

        lbl_d = QtWidgets.QLabel("↓")
        lbl_d.setStyleSheet("font-size: 13px; font-weight: 800; color: #D97706;")
        lbl_d.setFixedWidth(70)
        lbl_d.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        h_layout.addWidget(lbl_d)

        lbl_u = QtWidgets.QLabel("↑")
        lbl_u.setStyleSheet("font-size: 13px; font-weight: 800; color: #DC2626;")
        lbl_u.setFixedWidth(70)
        lbl_u.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        h_layout.addWidget(lbl_u)

        c_layout.addWidget(header)

        # Divider
        div = QtWidgets.QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background-color: #E2E8F0; border: none;")
        c_layout.addWidget(div)

        # Table
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(3)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: transparent;
                border: none;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 2px 4px;
                border-bottom: 1px solid #F1F5F9;
            }
            QScrollBar:vertical {
                border: none;
                background-color: #F8FAFC;
                width: 6px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background-color: #CBD5E1;
                border-radius: 3px;
                min-height: 20px;
            }
        """)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 70)

        c_layout.addWidget(self.table)
        layout.addWidget(container)

    def set_app_data(self, proc_list: list):
        """Nạp danh sách tất cả ứng dụng tại thời điểm đó."""
        self.table.setRowCount(len(proc_list))
        for idx, p in enumerate(proc_list):
            name = p.get("name", "Unknown")
            name_item = QtWidgets.QTableWidgetItem(f"  {name}")
            name_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            name_item.setForeground(QtGui.QBrush(QtGui.QColor("#0F172A")))

            icon = self.icon_manager.get_icon(p.get("exe", ""), name)
            name_item.setIcon(icon)
            self.table.setItem(idx, 0, name_item)

            d_val = p.get("down_kb", 0.0)
            u_val = p.get("up_kb", 0.0)
            d_str = self._format_speed_short(d_val)
            u_str = self._format_speed_short(u_val)

            d_item = QtWidgets.QTableWidgetItem(d_str)
            d_item.setFont(QtGui.QFont("Segoe UI", 9))
            d_item.setForeground(QtGui.QBrush(QtGui.QColor("#334155")))
            d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(idx, 1, d_item)

            u_item = QtWidgets.QTableWidgetItem(u_str)
            u_item.setFont(QtGui.QFont("Segoe UI", 9))
            u_item.setForeground(QtGui.QBrush(QtGui.QColor("#334155")))
            u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(idx, 2, u_item)

        row_h = 24
        tot_h = min(220, max(30, len(proc_list) * row_h + 8))
        self.table.setFixedHeight(tot_h)
        self.setFixedHeight(tot_h + 46)

    def _format_speed_short(self, speed_kb: float) -> str:
        if speed_kb < 1.0:
            return f"{speed_kb * 1024.0:,.0f} B/s"
        elif speed_kb < 1024.0:
            return f"{speed_kb:,.1f} KB/s"
        else:
            return f"{speed_kb / 1024.0:,.1f} MB/s"


class GlassWireAppTriggerPill(QtWidgets.QWidget):
    """
    Pill hiển thị '📁 App + X more' trên thanh inspection bar.
    Khi rê chuột vào sẽ hiển thị GlassWireAppHoverPopup bên dưới.
    """
    def __init__(self, popup: GlassWireAppHoverPopup, parent=None):
        super().__init__(parent)
        self.popup = popup
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.hide_timer = QtCore.QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(220)
        self.hide_timer.timeout.connect(self._check_and_hide_popup)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 8, 2)
        layout.setSpacing(6)

        self.lbl_icon = QtWidgets.QLabel()
        self.lbl_icon.setFixedSize(16, 16)
        layout.addWidget(self.lbl_icon)

        self.lbl_text = QtWidgets.QLabel("Không có ứng dụng nào")
        self.lbl_text.setStyleSheet("""
            font-size: 11px;
            font-weight: bold;
            color: #2563EB;
        """)
        layout.addWidget(self.lbl_text)

        self.setStyleSheet("""
            GlassWireAppTriggerPill {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 5px;
            }
            GlassWireAppTriggerPill:hover {
                background-color: #EFF6FF;
                border-color: #93C5FD;
            }
        """)

    def set_app_summary(self, top_procs: list, icon_manager):
        if not top_procs:
            self.lbl_icon.clear()
            self.lbl_text.setText("Không có ứng dụng nào truyền nhận")
            self.lbl_text.setStyleSheet("font-size: 11px; font-weight: normal; color: #64748B;")
            self.popup.set_app_data([])
            return

        self.popup.set_app_data(top_procs)
        first_app = top_procs[0]
        first_name = first_app.get("name", "Unknown")
        first_icon = icon_manager.get_icon(first_app.get("exe", ""), first_name)
        self.lbl_icon.setPixmap(first_icon.pixmap(16, 16))

        count = len(top_procs)
        if count > 1:
            more_cnt = count - 1
            self.lbl_text.setText(f"{first_name}  +{more_cnt} more")
        else:
            self.lbl_text.setText(first_name)
        self.lbl_text.setStyleSheet("font-size: 11px; font-weight: bold; color: #2563EB;")

    def enterEvent(self, event):
        self.hide_timer.stop()
        if self.popup.table.rowCount() > 0:
            pt = self.mapToGlobal(QtCore.QPoint(0, self.height() + 3))
            # Kiểm tra nếu tràn màn hình thì bật lên phía trên
            screen_geom = QtGui.QGuiApplication.primaryScreen().geometry()
            if pt.y() + self.popup.height() > screen_geom.bottom() - 10:
                pt.setY(self.mapToGlobal(QtCore.QPoint(0, 0)).y() - self.popup.height() - 3)
            self.popup.move(pt)
            self.popup.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hide_timer.start()
        super().leaveEvent(event)

    def _check_and_hide_popup(self):
        if not self.underMouse() and not self.popup.underMouse():
            self.popup.hide()
        elif self.popup.underMouse():
            self.hide_timer.start(250)


class GlassWirePointInspectionBar(QtWidgets.QFrame):
    """
    Thanh hiển thị thông số chi tiết khi dừng/ghim tại một điểm trên biểu đồ (chuẩn Hình 2).
    Gồm: [↓ Downloads]  [↑ Uploads]  [📁 App + X more]  ...  [Thời gian dừng]  [✕ Tiếp tục]
    """
    resume_clicked = QtCore.pyqtSignal()

    def __init__(self, icon_manager, parent=None):
        super().__init__(parent)
        self.setObjectName("pointInspectionBar")
        self.setFixedHeight(34)
        self.setStyleSheet("""
            QFrame#pointInspectionBar {
                background-color: #FFFFFF;
                border-top: 1px solid #E2E8F0;
                border-bottom: 1px solid #E2E8F0;
            }
        """)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(12)

        # 1. Download speed with amber arrow
        self.lbl_down = QtWidgets.QLabel("↓ 0.0 B/s")
        self.lbl_down.setStyleSheet("font-size: 12px; font-weight: 800; color: #D97706;")
        layout.addWidget(self.lbl_down)

        # 2. Upload speed with red arrow
        self.lbl_up = QtWidgets.QLabel("↑ 0.0 B/s")
        self.lbl_up.setStyleSheet("font-size: 12px; font-weight: 800; color: #DC2626;")
        layout.addWidget(self.lbl_up)

        # Separator
        sep1 = QtWidgets.QFrame()
        sep1.setFixedSize(1, 16)
        sep1.setStyleSheet("background-color: #CBD5E1;")
        layout.addWidget(sep1)

        # 3. App pill trigger button with hover popup
        self.popup = GlassWireAppHoverPopup(icon_manager)
        self.app_pill = GlassWireAppTriggerPill(self.popup, self)
        layout.addWidget(self.app_pill)

        layout.addStretch()

        # 4. Thời gian đang dừng là bao nhiêu
        self.lbl_time = QtWidgets.QLabel("")
        self.lbl_time.setStyleSheet("font-size: 11px; font-weight: 600; color: #64748B;")
        layout.addWidget(self.lbl_time)

        # 5. Nút tiếp tục live / bỏ ghim
        self.btn_resume = QtWidgets.QPushButton("▶ Tiếp Tục")
        self.btn_resume.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_resume.setFixedHeight(22)
        self.btn_resume.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-size: 10px;
                font-weight: bold;
                padding: 2px 8px;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        self.btn_resume.clicked.connect(self.resume_clicked.emit)
        layout.addWidget(self.btn_resume)

        self.btn_close = QtWidgets.QPushButton("✕")
        self.btn_close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_close.setFixedSize(20, 20)
        self.btn_close.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #64748B;
                font-size: 11px;
                font-weight: bold;
                border: none;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #F1F5F9;
                color: #0F172A;
            }
        """)
        self.btn_close.clicked.connect(self.resume_clicked.emit)
        layout.addWidget(self.btn_close)

    def set_inspection_data(self, down_val: float, up_val: float, t_val: float, top_procs: list, icon_manager, format_func):
        self.lbl_down.setText(f"↓ {format_func(down_val)}")
        self.lbl_up.setText(f"↑ {format_func(up_val)}")

        lt = time.localtime(t_val)
        time_str = time.strftime("%H:%M:%S  -  %d/%m/%Y", lt)
        self.lbl_time.setText(f"Thời điểm dừng: {time_str}")

        self.app_pill.set_app_summary(top_procs, icon_manager)

    def hide(self):
        if hasattr(self, 'popup'):
            self.popup.hide()
        super().hide()


class GlassWireTimelineScrubber(QtWidgets.QWidget):
    """Thanh toàn cảnh mini-timeline kéo tua phong cách GlassWire với bóng sóng vàng và nút kéo tròn"""
    range_selected = QtCore.pyqtSignal(float, float, float)  # (win_start, win_end, target_t)
    time_selected = QtCore.pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.history_points = deque(maxlen=2400)
        self.window_duration = 300.0
        self.slider_start: Optional[float] = None
        self.slider_end: Optional[float] = None
        self.drag_mode: Optional[str] = None
        self.drag_start_x: float = 0.0
        self.drag_start_win: Tuple[float, float] = (0.0, 0.0)
        self.drag_offset_x: float = 0.0

    def set_window_duration(self, duration: float):
        self.window_duration = duration
        if self.slider_start is not None and self.slider_end is not None:
            mid = (self.slider_start + self.slider_end) / 2.0
            self.slider_start = mid - duration / 2.0
            self.slider_end = mid + duration / 2.0
        self.update()

    def add_point(self, timestamp: float, value: float):
        self.history_points.append((timestamp, value))
        self.update()

    def set_slider_range(self, start: float, end: float):
        """Đặt vị trí khung trượt thủ công."""
        self.slider_start = start
        self.slider_end = end
        self.update()

    def reset_to_live(self):
        """Quay lại chế độ live monitor bám sát thời gian thực."""
        self.slider_start = None
        self.slider_end = None
        self.drag_mode = None
        self.update()

    def get_time_bounds(self) -> Tuple[float, float]:
        """Tính toán khoảng thời gian bao quát hiển thị trên thanh timeline."""
        now = time.time()
        if len(self.history_points) > 1:
            t_max = max(now, self.history_points[-1][0])
            first_t = self.history_points[0][0]
            span = max(600.0, self.window_duration * 1.5, t_max - first_t)
            t_min = t_max - span
        else:
            t_max = now
            t_min = now - max(600.0, self.window_duration * 1.5)
        return t_min, t_max

    def get_slider_window(self, t_min: float, t_max: float) -> Tuple[float, float]:
        """Lấy khoảng thời gian của khung trượt hiện tại (win_start, win_end)."""
        if self.slider_start is not None and self.slider_end is not None:
            s = max(t_min, min(t_max, self.slider_start))
            e = max(t_min, min(t_max, self.slider_end))
            if e <= s:
                e = min(t_max, s + self.window_duration)
            return s, e
        # Mặc định ở chế độ live: bám sát mép phải (thời gian mới nhất)
        e = t_max
        s = max(t_min, t_max - self.window_duration)
        return s, e

    def time_to_x(self, t: float, t_min: float, t_max: float, w: float) -> float:
        if t_max <= t_min:
            return 0.0
        pct = max(0.0, min(1.0, (t - t_min) / (t_max - t_min)))
        return pct * w

    def x_to_time(self, x: float, t_min: float, t_max: float, w: float) -> float:
        pct = max(0.0, min(1.0, x / max(1.0, float(w))))
        return t_min + pct * (t_max - t_min)

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        w = max(1, self.width())
        x = event.position().x()
        t_min, t_max = self.get_time_bounds()
        s, e = self.get_slider_window(t_min, t_max)
        x1 = self.time_to_x(s, t_min, t_max, w)
        x2 = self.time_to_x(e, t_min, t_max, w)
        box_w = max(36.0, x2 - x1)
        x2 = x1 + box_w

        if abs(x - x1) <= 9:
            self.drag_mode = "left_handle"
            self.drag_start_x = x
            self.drag_start_win = (s, e)
        elif abs(x - x2) <= 9:
            self.drag_mode = "right_handle"
            self.drag_start_x = x
            self.drag_start_win = (s, e)
        elif x1 <= x <= x2:
            self.drag_mode = "move_box"
            self.drag_start_x = x
            self.drag_offset_x = x - x1
            self.drag_start_win = (s, e)
        else:
            # Nhấp chuột ra ngoài khung: Di chuyển tâm khung trượt tới điểm vừa nhấp
            clicked_t = self.x_to_time(x, t_min, t_max, w)
            half = (e - s) / 2.0
            new_s = clicked_t - half
            new_e = clicked_t + half
            if new_s < t_min:
                new_s = t_min
                new_e = min(t_max, t_min + (e - s))
            if new_e > t_max:
                new_e = t_max
                new_s = max(t_min, t_max - (e - s))

            self.slider_start = new_s
            self.slider_end = new_e
            self.drag_mode = "move_box"
            self.drag_start_x = x
            self.drag_offset_x = (x2 - x1) / 2.0
            self.drag_start_win = (new_s, new_e)
            self.update()
            self.range_selected.emit(new_s, new_e, clicked_t)
            self.time_selected.emit(clicked_t)

    def mouseMoveEvent(self, event):
        w = max(1, self.width())
        x = event.position().x()
        t_min, t_max = self.get_time_bounds()
        s, e = self.get_slider_window(t_min, t_max)
        x1 = self.time_to_x(s, t_min, t_max, w)
        x2 = self.time_to_x(e, t_min, t_max, w)
        box_w = max(36.0, x2 - x1)
        x2 = x1 + box_w

        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            win_dur = self.drag_start_win[1] - self.drag_start_win[0]
            if win_dur <= 0.0:
                win_dur = self.window_duration

            if self.drag_mode == "move_box":
                target_x1 = x - getattr(self, "drag_offset_x", box_w / 2.0)
                new_s = self.x_to_time(target_x1, t_min, t_max, w)
                new_e = new_s + win_dur
                if new_s < t_min:
                    new_s = t_min
                    new_e = min(t_max, t_min + win_dur)
                if new_e > t_max:
                    new_e = t_max
                    new_s = max(t_min, t_max - win_dur)
                self.slider_start = new_s
                self.slider_end = new_e
                self.update()
                target_t = (new_s + new_e) / 2.0
                self.range_selected.emit(new_s, new_e, target_t)
                self.time_selected.emit(target_t)

            elif self.drag_mode == "left_handle":
                new_s = self.x_to_time(x, t_min, t_max, w)
                new_s = max(t_min, min(e - 10.0, new_s))
                self.slider_start = new_s
                self.window_duration = max(10.0, e - new_s)
                self.update()
                self.range_selected.emit(new_s, e, new_s)
                self.time_selected.emit(new_s)

            elif self.drag_mode == "right_handle":
                new_e = self.x_to_time(x, t_min, t_max, w)
                new_e = min(t_max, max(s + 10.0, new_e))
                self.slider_end = new_e
                self.window_duration = max(10.0, new_e - s)
                self.update()
                self.range_selected.emit(s, new_e, new_e)
                self.time_selected.emit(new_e)

            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        else:
            if abs(x - x1) <= 9 or abs(x - x2) <= 9:
                self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
            elif x1 <= x <= x2:
                self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        self.drag_mode = None
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # Nền dải timeline
        painter.fillRect(0, 0, w, h, QtGui.QColor("#FFFFFF"))
        painter.setPen(QtGui.QColor("#E2E8F0"))
        painter.drawLine(0, 0, w, 0)
        painter.drawLine(0, h - 1, w, h - 1)

        t_min, t_max = self.get_time_bounds()

        # Bóng sóng vàng mini phản ánh chính xác theo mốc thời gian
        if len(self.history_points) > 3:
            max_v = max([p[1] for p in self.history_points] + [1.0])
            path = QtGui.QPainterPath()
            path.moveTo(0, h - 12)
            first_x = self.time_to_x(self.history_points[0][0], t_min, t_max, w)
            path.lineTo(first_x, h - 12)

            for t, v in self.history_points:
                norm_y = (v / max_v) * (h - 16)
                cur_x = self.time_to_x(t, t_min, t_max, w)
                cur_y = (h - 12) - norm_y
                path.lineTo(cur_x, cur_y)

            last_x = self.time_to_x(self.history_points[-1][0], t_min, t_max, w)
            path.lineTo(last_x, h - 12)
            path.lineTo(w, h - 12)
            path.closeSubpath()

            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(245, 158, 11, 140))
            painter.drawPath(path)

            painter.setPen(QtGui.QPen(QtGui.QColor("#F59E0B"), 1.0))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        # Khung quan sát hiện tại (View Slider phong cách GlassWire)
        s, e = self.get_slider_window(t_min, t_max)
        x1 = self.time_to_x(s, t_min, t_max, w)
        x2 = self.time_to_x(e, t_min, t_max, w)
        box_w = max(36.0, x2 - x1)
        if x1 + box_w > w:
            x1 = max(0.0, w - box_w)
            x2 = w
        else:
            x2 = x1 + box_w

        win_rect = QtCore.QRectF(x1, 2, box_w, h - 14)

        painter.setPen(QtGui.QPen(QtGui.QColor("#64748B"), 1.4))
        painter.setBrush(QtGui.QColor(255, 255, 255, 205))

        painter.drawRoundedRect(win_rect, 4, 4)

        # Hai nút kéo tròn hai bên ( ○-------------○ )
        handle_y = (h - 14) / 2.0 + 2.0
        painter.setBrush(QtGui.QColor("#FFFFFF"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#475569"), 1.8))
        painter.drawEllipse(QtCore.QPointF(x1, handle_y), 4.5, 4.5)
        painter.drawEllipse(QtCore.QPointF(x2, handle_y), 4.5, 4.5)

        # Mốc thời gian dưới đáy timeline
        painter.setPen(QtGui.QColor("#94A3B8"))
        painter.setFont(QtGui.QFont("Segoe UI", 7))
        for i in range(6):
            cur_t = t_min + (i / 5.0) * (t_max - t_min)
            lt = time.localtime(cur_t)
            hr = lt.tm_hour % 12
            hr = 12 if hr == 0 else hr
            am_pm = "PM" if lt.tm_hour >= 12 else "AM"
            if (t_max - t_min) >= 43200.0:
                lbl_time = f"{hr}:00 {am_pm}"
            else:
                lbl_time = f"{hr}:{lt.tm_min:02d}:{lt.tm_sec:02d} {am_pm}"
            lbl_x = int(i * (w / 5.3)) + 6
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


class AsyncSignalBridge(QtCore.QObject):
    """Cầu nối tín hiệu luồng ngầm an toàn tuyệt đối 100% về luồng giao diện chính PyQt6."""
    sig_callback = QtCore.pyqtSignal(object, tuple, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sig_callback.connect(self._on_callback)

    def _on_callback(self, fn, args, kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as e:
            print(f"[AsyncBridge Callback Error] {e}")

    def run_on_main(self, fn, *args, **kwargs):
        self.sig_callback.emit(fn, args, kwargs)


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
