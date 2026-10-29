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

from presentation.glasswire_icons import (
    get_glasswire_logo_icon,
    get_traffic_monitor_icon,
    get_protect_icon,
    get_log_analysis_icon,
    get_network_scanner_icon,
    get_hardware_resources_icon,
    get_graph_toggle_icon,
    get_usage_toggle_icon,
    get_filter_icon,
)
from presentation.traffic_usage_view import GlassWireUsageView
from presentation.protect_view import GlassWireProtectView


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
        self.is_history_mode = False
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
        if not self.is_history_mode:
            self.history_points.append((timestamp, value))
            self.update()

    def set_historical_series(self, series: list):
        """Nạp dữ liệu chuỗi lịch sử vào mini scrubber để hiển thị toàn cảnh ngày."""
        self.is_history_mode = True
        self.history_points.clear()
        self.slider_start = None
        self.slider_end = None
        if not series:
            self.update()
            return
        n = len(series)
        if n <= 800:
            for pt in series:
                self.history_points.append((pt["timestamp"], pt.get("down_kb", 0.0) + pt.get("up_kb", 0.0)))
        else:
            step = (n - 1) / 799.0
            for i in range(800):
                idx = int(round(i * step))
                pt = series[idx]
                self.history_points.append((pt["timestamp"], pt.get("down_kb", 0.0) + pt.get("up_kb", 0.0)))
        self.update()

    def set_slider_range(self, start: float, end: float):
        """Đặt vị trí khung trượt thủ công."""
        self.slider_start = start
        self.slider_end = end
        self.update()

    def reset_to_live(self):
        """Quay lại chế độ live monitor bám sát thời gian thực."""
        self.is_history_mode = False
        self.slider_start = None
        self.slider_end = None
        self.drag_mode = None
        self.update()

    def get_time_bounds(self) -> Tuple[float, float]:
        """Tính toán khoảng thời gian bao quát hiển thị trên thanh timeline."""
        if self.is_history_mode and self.history_points:
            t_min = self.history_points[0][0]
            t_max = self.history_points[-1][0]
            if t_max <= t_min:
                t_max = t_min + self.window_duration
            return t_min, t_max

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

        if self.is_history_mode:
            painter.setPen(QtGui.QPen(QtGui.QColor("#3B82F6"), 1.4))
            painter.setBrush(QtGui.QColor(239, 246, 255, 140))
        else:
            painter.setPen(QtGui.QPen(QtGui.QColor("#64748B"), 1.4))
            painter.setBrush(QtGui.QColor(255, 255, 255, 205))

        painter.drawRoundedRect(win_rect, 4, 4)

        # Hai nút kéo tròn hai bên ( ○-------------○ )
        handle_y = (h - 14) / 2.0 + 2.0
        painter.setBrush(QtGui.QColor("#FFFFFF"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#475569") if not self.is_history_mode else QtGui.QColor("#2563EB"), 1.8))
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


class BandwidthDashboard(QtWidgets.QMainWindow):
    """
    Giao diện GlassWire Free chuẩn xác 100%:
    - Header: GlassWire brand + Navigation Pills (Traffic Monitor, Protect, Usage History, Network Scanner).
    - Split-View: Bảng danh sách tiến trình trái (Icon, App, ↓, ↑) + Biểu đồ sóng thời gian thực phải.
    - Bộ đệm trễ 5 giây + Nội suy Hermite & Lọc Gaussian mượt mà không rung giật.
    - HUD thống kê ứng dụng: Số liệu MB/KB, Tốc độ, Đồng hồ bán nguyệt GlassWire, Thanh phân bổ WAN/LAN.
    - Panoramic Timeline Scrubber với thanh kéo tua và bóng sóng vàng.
    - Footer: Upgrade to unlock Premium features banner.
    - Tab 2 (GlassWire Protect): Bóp băng thông WinDivert Kernel Packet Shaper (Token Bucket 2 chiều).
    - Tab 3 (Usage History): Lịch sử lưu lượng theo ngày & xem chi tiết mốc thời gian.
    - Tab 4 (Network Scanner): Bảng Socket TCP (IP, Cổng, Giao thức, Trạng thái).
    """

    def __init__(self, monitor_service: MonitorService):
        super().__init__()
        self.service = monitor_service
        self.window_duration = 300.0  # Mặc định 5 phút như GlassWire
        self.render_delay = 5.0       # Lookahead buffer 5 giây chống giật tuyệt đối
        self.icon_manager = ProcessIconManager()
        self.async_bridge = AsyncSignalBridge(self)

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
        self.window_sample_history = deque(maxlen=86400)

        self.latest_down = 0.0
        self.latest_up = 0.0
        self.latest_proc_down = 0.0
        self.latest_proc_up = 0.0
        self.latest_proc_speeds = []
        
        # Tích lũy dung lượng cho từng tiến trình (Bytes)
        self.app_accumulated_bytes: Dict[str, Dict[str, float]] = {
            "All apps": {"down": 0.0, "up": 0.0}
        }
        self.app_metadata: Dict[str, Dict] = {}
        self.total_accumulated_down = 0.0
        self.total_accumulated_up = 0.0

        # Tiến trình đang được người dùng chọn xem
        self.selected_app_name = "All apps"
        self.current_y_max = 1024.0
        self._scale_down_hold_time = 0.0

        # Tiến trình đang giám sát QoS
        self.monitored_qos_app = ""
        self.monitored_qos_limit = 0.0

        # Chế độ xem dữ liệu tại điểm & tạm dừng biểu đồ (Point Inspection & Pause/Resume)
        self.point_history = deque(maxlen=2400)
        self.is_paused = False
        self.pinned_time: Optional[float] = None
        self.history_mode = False
        self.historical_series: List[Dict] = []
        self.current_hist_page_series: List[Dict] = []
        self.raw_hist_logs: List[Dict] = []

        # Bộ lọc chiều lưu lượng và sắp xếp (Filter & Sort Sidebar)
        self.filter_direction = "both"      # "both", "incoming", "outgoing"
        self.filter_sort = "high_to_low"    # "high_to_low", "low_to_high", "alpha"
        self.filter_scope = "all"           # "all", "wan", "lan"
        self.current_traffic_view = "graph" # "graph" hoặc "usage" 

        self._init_ui()
        self._start_worker()
        self._start_render_loop()

    def _init_ui(self):
        self.setWindowTitle("Traffic Monitor & Network Packet Shaper")
        self.resize(1280, 760)
        self.setMinimumSize(1020, 620)

        # Style toàn bộ theo phong cách Clean White (Nền trắng tinh khôi hoàn toàn)
        self.setStyleSheet("""
            QMainWindow, QWidget#centralContainer, QStackedWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QWidget {
                color: #1E293B;
            }
            QDialog, QMessageBox {
                background-color: #FFFFFF;
                color: #0F172A;
            }
            QMessageBox QLabel {
                background-color: #FFFFFF;
                color: #0F172A;
                font-size: 13px;
                padding: 4px;
            }
            QMessageBox QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                border-radius: 6px;
                font-weight: 600;
                padding: 6px 18px;
                min-width: 75px;
                font-size: 12px;
                border: none;
            }
            QMessageBox QPushButton:hover {
                background-color: #1D4ED8;
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
            QScrollArea, QScrollArea > QWidget > QWidget {
                background-color: #FFFFFF;
                border: none;
            }
        """)

        container = QtWidgets.QWidget()
        container.setObjectName("centralContainer")
        container.setStyleSheet("background-color: #FFFFFF;")
        main_layout = QtWidgets.QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. QStackedWidget chứa các trang chức năng
        self.stacked_pages = QtWidgets.QStackedWidget()
        self.stacked_pages.setStyleSheet("background-color: #FFFFFF;")

        # 2. Thanh Header GlassWire Navigation trên cùng
        header_widget = self._create_glasswire_header()
        main_layout.addWidget(header_widget)

        # Trang 0: Traffic Monitor (Split-view chuẩn GlassWire)
        self.page_traffic_monitor = QtWidgets.QWidget()
        self._setup_traffic_monitor_page(self.page_traffic_monitor)
        self.stacked_pages.addWidget(self.page_traffic_monitor)

        # Trang 1: GlassWire Protect (WinDivert Kernel Packet Shaper)
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


        main_layout.addWidget(self.stacked_pages)
        self._switch_nav_page(0)

        self.setCentralWidget(container)

    # =========================================================================
    # HEADER THANH ĐIỀU HƯỚNG GLASSWIRE (NAV TABS + LOGO + UPGRADE)
    # =========================================================================
    def _create_glasswire_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QWidget()
        header.setFixedHeight(54)
        header.setStyleSheet("background-color: #FFFFFF; border-bottom: 1px solid #E2E8F0;")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(16)

        # Các Tab điều hướng chính
        self.nav_buttons: List[QtWidgets.QPushButton] = []
        self.nav_tabs_info = [
            ("Traffic\nMonitor", 0, get_traffic_monitor_icon),
            ("Protect", 1, get_protect_icon),
            ("Log\nAnalysis", 2, get_log_analysis_icon),
            ("Network\nScanner", 3, get_network_scanner_icon),
        ]

        nav_container = QtWidgets.QHBoxLayout()
        nav_container.setSpacing(8)

        for text, page_idx, icon_factory in self.nav_tabs_info:
            btn = QtWidgets.QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(40)
            btn.setIcon(icon_factory(page_idx == 0))
            btn.setIconSize(QtCore.QSize(22, 22))
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=page_idx: self._switch_nav_page(idx))
            nav_container.addWidget(btn)
            self.nav_buttons.append(btn)

        layout.addLayout(nav_container)
        layout.addStretch()

        return header

    def _switch_nav_page(self, index: int):
        """Chuyển trang hiển thị và áp dụng giao diện nút bấm GlassWire"""
        self.stacked_pages.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            text, page_idx, icon_factory = self.nav_tabs_info[i]
            if i == index:
                btn.setIcon(icon_factory(True))
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
                btn.setIcon(icon_factory(False))
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

        if index == 1:
            if hasattr(self, 'protect_view'):
                self.protect_view.update_protect_table(self.latest_proc_speeds, self.app_accumulated_bytes, self.app_metadata)
            self._refresh_policy_table()
        elif index == 2:
            self._on_enter_usage_history_page()
        elif index == 3:
            self._refresh_connections()

    def _setup_traffic_monitor_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QHBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Khung chứa nội dung chính của Traffic Monitor
        traffic_main_widget = QtWidgets.QWidget()
        traffic_main_widget.setStyleSheet("background-color: #FFFFFF;")
        traffic_main_layout = QtWidgets.QVBoxLayout(traffic_main_widget)
        traffic_main_layout.setContentsMargins(12, 8, 12, 6)
        traffic_main_layout.setSpacing(6)

        # ---------------------------------------------------------------------
        # 1. THANH CÔNG CỤ TRÊN CÙNG CỦA BIỂU ĐỒ (TOP TOOLBAR)
        # ---------------------------------------------------------------------
        chart_top_bar = QtWidgets.QHBoxLayout()
        chart_top_bar.setSpacing(10)

        # Nút Filter ở góc bên trái (như Hình 1 - hiển thị khi bảng ứng dụng đang đóng)
        self.btn_toggle_filter = QtWidgets.QPushButton(" Filter")
        self.btn_toggle_filter.setIcon(get_filter_icon())
        self.btn_toggle_filter.setFixedHeight(28)
        self.btn_toggle_filter.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_filter.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 14px;
                padding: 2px 14px;
                font-size: 11px;
                font-weight: 600;
                color: #475569;
            }
            QPushButton:hover {
                background-color: #F8FAFC;
                color: #0F172A;
                border-color: #94A3B8;
            }
        """)
        self.btn_toggle_filter.clicked.connect(self._open_app_panel)
        chart_top_bar.addWidget(self.btn_toggle_filter)

        chart_top_bar.addStretch()

        # Capsule Toggle Button: 1 nút Graph, 1 nút Usage
        capsule_container = QtWidgets.QFrame()
        capsule_container.setFixedHeight(32)
        capsule_container.setStyleSheet("""
            QFrame {
                background-color: #F1F5F9;
                border-radius: 16px;
                padding: 2px;
            }
        """)
        cap_layout = QtWidgets.QHBoxLayout(capsule_container)
        cap_layout.setContentsMargins(3, 2, 3, 2)
        cap_layout.setSpacing(4)

        self.btn_view_graph = QtWidgets.QPushButton(" Graph")
        self.btn_view_graph.setIcon(get_graph_toggle_icon(True))
        self.btn_view_graph.setFixedHeight(26)
        self.btn_view_graph.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_view_graph.setStyleSheet("""
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                font-size: 11px;
                font-weight: bold;
                border-radius: 13px;
                padding: 2px 14px;
                border: none;
            }
        """)
        self.btn_view_graph.clicked.connect(lambda: self._switch_traffic_view("graph"))

        self.btn_view_usage = QtWidgets.QPushButton(" Usage")
        self.btn_view_usage.setIcon(get_usage_toggle_icon(False))
        self.btn_view_usage.setFixedHeight(26)
        self.btn_view_usage.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_view_usage.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #64748B;
                font-size: 11px;
                font-weight: 600;
                border-radius: 13px;
                padding: 2px 14px;
                border: none;
            }
            QPushButton:hover {
                background-color: #E2E8F0;
                color: #1E293B;
            }
        """)
        self.btn_view_usage.clicked.connect(lambda: self._switch_traffic_view("usage"))

        cap_layout.addWidget(self.btn_view_graph)
        cap_layout.addWidget(self.btn_view_usage)
        chart_top_bar.addWidget(capsule_container)

        chart_top_bar.addStretch()

        # Thang đo (3 MB ▾)
        self.scale_indicator = QtWidgets.QLabel("3 MB ▾")
        self.scale_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #475569; min-width: 60px;")
        chart_top_bar.addWidget(self.scale_indicator)

        # Điều khiển LIVE
        self.live_controls_widget = QtWidgets.QWidget()
        lc_layout = QtWidgets.QHBoxLayout(self.live_controls_widget)
        lc_layout.setContentsMargins(0, 0, 0, 0)
        lc_layout.setSpacing(8)

        self.lbl_live_badge = QtWidgets.QLabel("● LIVE")
        self.lbl_live_badge.setStyleSheet("color: #059669; font-weight: bold; font-size: 11px; padding: 3px 8px; background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;")
        lc_layout.addWidget(self.lbl_live_badge)

        self.btn_pause_toggle = QtWidgets.QPushButton("⏸ Tạm Dừng")
        self.btn_pause_toggle.setFixedHeight(28)
        self.btn_pause_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #334155;
                font-size: 11px;
                font-weight: 600;
                padding: 3px 12px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #F8FAFC;
                color: #0F172A;
            }
        """)
        self.btn_pause_toggle.clicked.connect(self._toggle_pause_resume)
        lc_layout.addWidget(self.btn_pause_toggle)

        self.btn_history_mode = QtWidgets.QPushButton("📅 Xem Lịch Sử")
        self.btn_history_mode.setFixedHeight(28)
        self.btn_history_mode.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_history_mode.setStyleSheet("""
            QPushButton {
                background-color: #EFF6FF;
                color: #2563EB;
                font-size: 11px;
                font-weight: 600;
                padding: 3px 12px;
                border-radius: 5px;
                border: 1px solid #BFDBFE;
            }
            QPushButton:hover {
                background-color: #DBEAFE;
            }
        """)
        self.btn_history_mode.clicked.connect(self._toggle_history_mode)
        lc_layout.addWidget(self.btn_history_mode)

        chart_top_bar.addWidget(self.live_controls_widget)

        # Điều khiển Lịch sử
        self.history_controls_widget = QtWidgets.QWidget()
        hc_layout = QtWidgets.QHBoxLayout(self.history_controls_widget)
        hc_layout.setContentsMargins(0, 0, 0, 0)
        hc_layout.setSpacing(8)

        lbl_date = QtWidgets.QLabel("📅 Ngày:")
        lbl_date.setStyleSheet("font-size: 11px; font-weight: bold; color: #334155;")
        hc_layout.addWidget(lbl_date)

        self.date_picker_history = QtWidgets.QDateEdit()
        self.date_picker_history.setCalendarPopup(True)
        self.date_picker_history.setDisplayFormat("yyyy-MM-dd")
        self.date_picker_history.setDate(QtCore.QDate.currentDate())
        self.date_picker_history.setFixedHeight(28)
        self.date_picker_history.setMinimumWidth(110)
        self.date_picker_history.setStyleSheet("font-size: 11px; padding: 2px 8px; border: 1px solid #CBD5E1; border-radius: 5px; background-color: #FFFFFF;")
        self.date_picker_history.dateChanged.connect(self._on_history_date_picker_changed)
        hc_layout.addWidget(self.date_picker_history)

        btn_hist_today = QtWidgets.QPushButton("Hôm Nay")
        btn_hist_today.setFixedHeight(28)
        btn_hist_today.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_hist_today.setStyleSheet("background-color: #FFFFFF; color: #334155; font-size: 11px; font-weight: 600; padding: 3px 10px; border: 1px solid #CBD5E1; border-radius: 5px;")
        btn_hist_today.clicked.connect(lambda: self.date_picker_history.setDate(QtCore.QDate.currentDate()))
        hc_layout.addWidget(btn_hist_today)

        self.btn_load_history = QtWidgets.QPushButton("🔍 Tải Biểu Đồ")
        self.btn_load_history.setFixedHeight(28)
        self.btn_load_history.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_load_history.setStyleSheet("background-color: #2563EB; color: #FFFFFF; font-size: 11px; font-weight: bold; padding: 3px 14px; border-radius: 5px; border: none;")
        self.btn_load_history.clicked.connect(self._load_history_for_selected_date)
        hc_layout.addWidget(self.btn_load_history)

        self.btn_back_to_live = QtWidgets.QPushButton("⬅ Quay Lại Live")
        self.btn_back_to_live.setFixedHeight(28)
        self.btn_back_to_live.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_back_to_live.setStyleSheet("background-color: #10B981; color: #FFFFFF; font-size: 11px; font-weight: bold; padding: 3px 14px; border-radius: 5px; border: none;")
        self.btn_back_to_live.clicked.connect(self._exit_history_mode)
        hc_layout.addWidget(self.btn_back_to_live)

        # Các nút phóng to / thu nhỏ khi xem lịch sử
        self.btn_hist_zoom_in = QtWidgets.QPushButton("🔍+ Phóng To")
        self.btn_hist_zoom_in.setFixedHeight(28)
        self.btn_hist_zoom_in.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_hist_zoom_in.setStyleSheet("background-color: #EFF6FF; color: #2563EB; font-size: 11px; font-weight: bold; padding: 3px 10px; border: 1px solid #BFDBFE; border-radius: 5px;")
        self.btn_hist_zoom_in.setToolTip("Phóng to biểu đồ vào khung giờ cao điểm (hoặc dùng con lăn chuột)")
        self.btn_hist_zoom_in.clicked.connect(self._zoom_in_history)
        hc_layout.addWidget(self.btn_hist_zoom_in)

        self.btn_hist_zoom_out = QtWidgets.QPushButton("🔍- Thu Nhỏ")
        self.btn_hist_zoom_out.setFixedHeight(28)
        self.btn_hist_zoom_out.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_hist_zoom_out.setStyleSheet("background-color: #EFF6FF; color: #2563EB; font-size: 11px; font-weight: bold; padding: 3px 10px; border: 1px solid #BFDBFE; border-radius: 5px;")
        self.btn_hist_zoom_out.setToolTip("Thu nhỏ biểu đồ xem toàn cảnh (hoặc dùng con lăn chuột)")
        self.btn_hist_zoom_out.clicked.connect(self._zoom_out_history)
        hc_layout.addWidget(self.btn_hist_zoom_out)

        self.btn_hist_zoom_24h = QtWidgets.QPushButton("⟲ Xem 24H")
        self.btn_hist_zoom_24h.setFixedHeight(28)
        self.btn_hist_zoom_24h.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_hist_zoom_24h.setStyleSheet("background-color: #F8FAFC; color: #475569; font-size: 11px; font-weight: 600; padding: 3px 10px; border: 1px solid #CBD5E1; border-radius: 5px;")
        self.btn_hist_zoom_24h.setToolTip("Đặt lại góc nhìn trọn vẹn 24 giờ")
        self.btn_hist_zoom_24h.clicked.connect(self._reset_history_24h)
        hc_layout.addWidget(self.btn_hist_zoom_24h)

        self.history_controls_widget.setVisible(False)
        chart_top_bar.addWidget(self.history_controls_widget)

        self.combo_history_date = QtWidgets.QComboBox()
        self.combo_history_date.setVisible(False)

        # Dropdown thời gian hiển thị (Mặc định: 5 Minutes)
        self.combo_time_window = QtWidgets.QComboBox()
        self.combo_time_window.addItems(["30 Seconds", "1 Minute", "5 Minutes", "10 Minutes", "1 Hour", "24 Hours"])
        self.combo_time_window.setCurrentIndex(2)
        self.combo_time_window.setFixedHeight(28)
        self.combo_time_window.setStyleSheet("font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 6px;")
        self.combo_time_window.currentIndexChanged.connect(self._on_time_window_changed)
        chart_top_bar.addWidget(self.combo_time_window)

        traffic_main_layout.addLayout(chart_top_bar)

        # BANNER SOI DỮ LIỆU TẠI ĐIỂM
        self.point_inspector_card = QtWidgets.QFrame()
        self.point_inspector_card.setObjectName("pointInspectorCard")
        self.point_inspector_card.setStyleSheet("""
            QFrame#pointInspectorCard {
                background-color: #F8FAFC;
                border: 1px solid #E2E8F0;
                border-left: 4px solid #2563EB;
                border-radius: 6px;
                padding: 4px 10px;
            }
        """)
        self.point_inspector_card.setVisible(False)
        pic_layout = QtWidgets.QHBoxLayout(self.point_inspector_card)
        pic_layout.setContentsMargins(10, 4, 10, 4)
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
        traffic_main_layout.addWidget(self.point_inspector_card)

        # ---------------------------------------------------------------------
        # STACK 2 VIEW: VIEW 0 (GRAPH) & VIEW 1 (USAGE)
        # ---------------------------------------------------------------------
        self.traffic_content_stack = QtWidgets.QStackedWidget()

        # VIEW 0: GRAPH VIEW (SPLITTER GỒM PANEL BÊN TRÁI VÀ BIỂU ĐỒ)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: #E2E8F0; width: 1px; }")

        # 1. Bảng danh sách ứng dụng bên trái (MẶC ĐỊNH ẨN ĐỂ HIỆN FULL BIỂU ĐỒ NHƯ HÌNH 1)
        self.left_panel = QtWidgets.QWidget()
        self.left_panel.setStyleSheet("background-color: #FFFFFF;")
        left_layout = QtWidgets.QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(6, 4, 6, 4)
        left_layout.setSpacing(6)

        # Header của panel bên trái (Bộ lọc chiều và WAN/LAN như Hình 2)
        filter_bar = QtWidgets.QHBoxLayout()
        filter_bar.setContentsMargins(0, 2, 0, 2)
        filter_bar.setSpacing(6)

        # Dropdown sắp xếp chiều lưu lượng: Incoming & Outgoing, Incoming only, Outgoing only
        self.combo_filter_dir = QtWidgets.QComboBox()
        self.combo_filter_dir.addItems(["Incoming & Outgoing", "Incoming only", "Outgoing only"])
        self.combo_filter_dir.setFixedHeight(26)
        self.combo_filter_dir.setStyleSheet("""
            QComboBox {
                background-color: #F8FAFC;
                color: #334155;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #CBD5E1;
                border-radius: 5px;
                padding: 1px 8px;
            }
            QComboBox::drop-down {
                border: none;
                width: 14px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #1E293B;
                selection-background-color: #EBF3FC;
                selection-color: #2563EB;
                border: 1px solid #CBD5E1;
            }
        """)
        self.combo_filter_dir.currentIndexChanged.connect(self._on_filter_direction_combo_changed)
        filter_bar.addWidget(self.combo_filter_dir)

        # Dropdown sắp xếp WAN / LAN: WAN & LAN, WAN only, LAN only
        self.combo_filter_scope = QtWidgets.QComboBox()
        self.combo_filter_scope.addItems(["WAN & LAN", "WAN only", "LAN only"])
        self.combo_filter_scope.setFixedHeight(26)
        self.combo_filter_scope.setStyleSheet("""
            QComboBox {
                background-color: #F8FAFC;
                color: #334155;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #CBD5E1;
                border-radius: 5px;
                padding: 1px 8px;
            }
            QComboBox::drop-down {
                border: none;
                width: 14px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #1E293B;
                selection-background-color: #EBF3FC;
                selection-color: #2563EB;
                border: 1px solid #CBD5E1;
            }
        """)
        self.combo_filter_scope.currentIndexChanged.connect(self._on_filter_scope_combo_changed)
        filter_bar.addWidget(self.combo_filter_scope)

        filter_bar.addStretch()

        # Ô tìm kiếm ứng dụng
        self.search_app_input = QtWidgets.QLineEdit()
        self.search_app_input.setPlaceholderText("🔍 Tìm...")
        self.search_app_input.setFixedWidth(75)
        self.search_app_input.setFixedHeight(26)
        self.search_app_input.setStyleSheet("""
            QLineEdit {
                border-radius: 13px;
                padding: 2px 8px;
                font-size: 11px;
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                color: #1E293B;
            }
            QLineEdit:focus {
                border-color: #2563EB;
            }
        """)
        self.search_app_input.textChanged.connect(self._filter_app_list)
        filter_bar.addWidget(self.search_app_input)

        # Nút đóng panel bên trái (✕) để đưa biểu đồ về full hình như Hình 1
        btn_close_panel = QtWidgets.QPushButton("✕")
        btn_close_panel.setFixedSize(24, 24)
        btn_close_panel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_close_panel.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #64748B;
                font-size: 13px;
                font-weight: bold;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #F1F5F9;
                color: #0F172A;
            }
        """)
        btn_close_panel.clicked.connect(self._close_app_panel)
        filter_bar.addWidget(btn_close_panel)

        left_layout.addLayout(filter_bar)

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

        self.left_panel.setMinimumWidth(260)
        self.left_panel.setMaximumWidth(380)

        # MẶC ĐỊNH: Bảng left_panel ẩn đi để biểu đồ hiển thị full width như Hình 1!
        self.left_panel.setVisible(False)
        self.splitter.addWidget(self.left_panel)

        # 2. Khối biểu đồ bên phải
        right_panel = QtWidgets.QWidget()
        right_panel.setStyleSheet("background-color: #FFFFFF;")
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(6)

        pg.setConfigOption("antialias", True)
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': self.time_axis}, background="#FFFFFF")
        self.plot_widget.setMinimumHeight(350)

        vb = self.plot_widget.getViewBox()
        vb.setDefaultPadding(0.0)
        vb.setMouseEnabled(x=False, y=False)
        self.plot_widget.showGrid(x=False, y=True, alpha=0.08)
        self.plot_widget.setYRange(0, 1024, padding=0.0)
        self.plot_widget.enableAutoRange(axis='y', enable=False)

        self.plot_widget.getAxis('bottom').setPen(pg.mkPen("#E2E8F0", width=1))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen("#94A3B8"))
        self.plot_widget.getAxis('left').setStyle(showValues=False)
        self.plot_widget.getAxis('left').setPen(None)

        self.curve_down = self.plot_widget.plot(
            pen=pg.mkPen(color="#D97706", width=2.0),
            fillLevel=0,
            brush=(245, 158, 11, 220)
        )

        self.curve_up = self.plot_widget.plot(
            pen=pg.mkPen(color="#EA580C", width=1.5),
            fillLevel=0,
            brush=(244, 63, 94, 210)
        )

        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#3B82F6", width=1.2, style=QtCore.Qt.PenStyle.DashLine))
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#CBD5E1", width=1.0, style=QtCore.Qt.PenStyle.DotLine))
        self.v_line.hide()
        self.h_line.hide()
        self.plot_widget.addItem(self.v_line, ignoreBounds=True)
        self.plot_widget.addItem(self.h_line, ignoreBounds=True)

        self.hover_marker = pg.ScatterPlotItem(size=8, pen=pg.mkPen("#2563EB", width=2), brush=pg.mkBrush("#FFFFFF"), symbol='o')
        self.hover_marker.hide()
        self.plot_widget.addItem(self.hover_marker)

        self.pinned_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#DC2626", width=1.5, style=QtCore.Qt.PenStyle.SolidLine))
        self.pinned_vline.hide()
        self.plot_widget.addItem(self.pinned_vline, ignoreBounds=True)

        self.pinned_marker = pg.ScatterPlotItem(size=14, pen=pg.mkPen("#FFFFFF", width=2.5), brush=pg.mkBrush("#DC2626"), symbol='star')
        self.pinned_marker.hide()
        self.plot_widget.addItem(self.pinned_marker)

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

        self.plot_widget.scene().sigMouseMoved.connect(self._on_plot_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_mouse_clicked)
        self.plot_widget.viewport().installEventFilter(self)

        right_layout.addWidget(self.plot_widget, stretch=1)

        # Thanh Inspection Bar chi tiết khi dừng tại điểm (chuẩn GlassWire Hình 2)
        self.point_inspection_bar = GlassWirePointInspectionBar(self.icon_manager)
        self.point_inspection_bar.resume_clicked.connect(self._resume_live)
        self.point_inspection_bar.hide()
        right_layout.addWidget(self.point_inspection_bar)

        self.splitter.addWidget(right_panel)

        self.traffic_content_stack.addWidget(self.splitter)

        # VIEW 1: USAGE VIEW (4 CỘT CHUẨN: APPS | HOSTS | TRAFFIC TYPE | COUNTRIES)
        self.usage_view = GlassWireUsageView(self.icon_manager)
        self.traffic_content_stack.addWidget(self.usage_view)

        traffic_main_layout.addWidget(self.traffic_content_stack, stretch=1)

        # HUD THỐNG KÊ CHI TIẾT DƯỚI ĐÁY
        bottom_hud = self._create_selected_app_hud()
        traffic_main_layout.addWidget(bottom_hud)

        # PANORAMIC TIMELINE SCRUBBER
        self.timeline_scrubber = GlassWireTimelineScrubber()
        self.timeline_scrubber.range_selected.connect(self._on_scrubber_range_selected)
        self.timeline_scrubber.time_selected.connect(self._on_scrubber_point_selected)
        traffic_main_layout.addWidget(self.timeline_scrubber)

        layout.addWidget(traffic_main_widget)

    def _open_app_panel(self):
        """Mở bảng ứng dụng và bộ lọc bên trái (như Hình 2)."""
        self.left_panel.setVisible(True)
        self.btn_toggle_filter.setVisible(False)
        if hasattr(self, 'splitter'):
            tot_w = self.splitter.width() or 1200
            self.splitter.setSizes([320, max(500, tot_w - 320)])
        self._update_app_sidebar(self.latest_proc_speeds)

    def _close_app_panel(self):
        """Đóng bảng bên trái, đưa biểu đồ về hiển thị toàn màn hình (như Hình 1)."""
        self.left_panel.setVisible(False)
        self.btn_toggle_filter.setVisible(True)

    def _on_filter_direction_combo_changed(self, index: int):
        """Xử lý khi người dùng chọn lọc chiều lưu lượng trong header của panel bên trái."""
        if index == 1:
            self.filter_direction = "incoming"
            self.curve_down.show()
            self.curve_up.hide()
        elif index == 2:
            self.filter_direction = "outgoing"
            self.curve_down.hide()
            self.curve_up.show()
        else:
            self.filter_direction = "both"
            self.curve_down.show()
            self.curve_up.show()

        # Cập nhật ngay lập tức bảng bên trái, HUD và Usage view
        self._update_app_sidebar(self.latest_proc_speeds)
        self._update_selected_app_hud()
        if hasattr(self, 'usage_view'):
            self.usage_view.set_filter_direction(self.filter_direction)
            if self.traffic_content_stack.currentIndex() == 1:
                self._update_usage_view_now()

    def _on_filter_scope_combo_changed(self, index: int):
        """Xử lý khi người dùng chọn lọc phạm vi mạng (WAN / LAN)."""
        if index == 1:
            self.filter_scope = "wan"
        elif index == 2:
            self.filter_scope = "lan"
        else:
            self.filter_scope = "all"

        self._update_app_sidebar(self.latest_proc_speeds)
        self._update_selected_app_hud()

    def _switch_traffic_view(self, mode: str):
        """Chuyển đổi giữa chế độ Biểu đồ (Graph) và Danh sách lưu lượng (Usage)."""
        self.current_traffic_view = mode
        if mode == "graph":
            self.traffic_content_stack.setCurrentIndex(0)
            self.btn_view_graph.setIcon(get_graph_toggle_icon(True))
            self.btn_view_graph.setStyleSheet("""
                QPushButton {
                    background-color: #2563EB;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
            """)
            self.btn_view_usage.setIcon(get_usage_toggle_icon(False))
            self.btn_view_usage.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #64748B;
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #E2E8F0;
                    color: #1E293B;
                }
            """)
        else:
            self.traffic_content_stack.setCurrentIndex(1)
            self.btn_view_usage.setIcon(get_usage_toggle_icon(True))
            self.btn_view_usage.setStyleSheet("""
                QPushButton {
                    background-color: #2563EB;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
            """)
            self.btn_view_graph.setIcon(get_graph_toggle_icon(False))
            self.btn_view_graph.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #64748B;
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #E2E8F0;
                    color: #1E293B;
                }
            """)
            self._update_usage_view_now()

    def _update_usage_view_now(self):
        """Thu thập kết nối socket và cập nhật bảng Usage 4 cột thời gian thực theo đúng khung thời gian đã chọn (30s, 1m, 5m, 10m, 1h, 24h)."""
        try:
            win_down, win_up, win_app_bytes = self._compute_window_traffic()
            tot_win_bytes = win_down + win_up
            active_conns = self.service.collector.collect_tcp_connections()
            self.usage_view.update_usage_data(
                win_app_bytes,
                active_conns,
                self.app_metadata,
                total_window_bytes=tot_win_bytes
            )
        except Exception as e:
            print(f"[Usage Update Error] {e}")

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



    # =========================================================================
    # TRANG 1: GLASSWIRE PROTECT (WINDIVERT KERNEL PACKET SHAPER)
    # =========================================================================
    def _setup_protect_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Giao diện GlassWire Protect phong cách chuẩn xác
        self.protect_view = GlassWireProtectView(self.service, self.icon_manager)
        layout.addWidget(self.protect_view)

        # Các widget dự phòng giữ nguyên để tương thích ngược 100%
        self.combo_qos_app = QtWidgets.QComboBox()
        self.combo_shaper_dir = QtWidgets.QComboBox()
        self.input_qos_limit = QtWidgets.QLineEdit("100")
        self.qos_meter_label = QtWidgets.QLabel("")
        self.qos_progress = QtWidgets.QProgressBar()
        self.qos_status_card = QtWidgets.QLabel("")
        self.policy_table = QtWidgets.QTableWidget()

    def _setup_logs_page(self, parent: QtWidgets.QWidget):
        scroll = QtWidgets.QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background-color: #FFFFFF; border: none; }")

        content_widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content_widget)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        # 1. Header Toolbar
        header_layout = QtWidgets.QHBoxLayout()
        header_left = QtWidgets.QVBoxLayout()
        header_left.setSpacing(2)

        title = QtWidgets.QLabel("📊 Usage History & Network Analytics")
        title.setStyleSheet("font-size: 17px; font-weight: 800; color: #0F172A;")
        sub_title = QtWidgets.QLabel("Thống kê lưu lượng mạng theo ngày, biểu đồ lịch sử tương tác và xếp hạng các ứng dụng tiêu thụ băng thông")
        sub_title.setStyleSheet("font-size: 12px; color: #64748B;")
        header_left.addWidget(title)
        header_left.addWidget(sub_title)
        header_layout.addLayout(header_left)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # 2. Thanh lọc ngày điều khiển (Filter Toolbar)
        filter_bar = QtWidgets.QFrame()
        filter_bar.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                padding: 4px 10px;
            }
        """)
        fb_layout = QtWidgets.QHBoxLayout(filter_bar)
        fb_layout.setContentsMargins(6, 4, 6, 4)
        fb_layout.setSpacing(8)

        lbl_date_sel = QtWidgets.QLabel("📅 Chọn Ngày:")
        lbl_date_sel.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155;")
        fb_layout.addWidget(lbl_date_sel)

        self.hist_page_date_picker = QtWidgets.QDateEdit()
        self.hist_page_date_picker.setCalendarPopup(True)
        self.hist_page_date_picker.setDisplayFormat("yyyy-MM-dd")
        self.hist_page_date_picker.setDate(QtCore.QDate.currentDate())
        self.hist_page_date_picker.setFixedHeight(28)
        self.hist_page_date_picker.setStyleSheet("font-size: 12px; font-weight: 600; padding: 2px 8px; border: 1px solid #CBD5E1; border-radius: 5px;")
        fb_layout.addWidget(self.hist_page_date_picker)

        self.hist_page_combo_dates = QtWidgets.QComboBox()
        self.hist_page_combo_dates.setFixedHeight(28)
        self.hist_page_combo_dates.setMinimumWidth(125)
        self.hist_page_combo_dates.setStyleSheet("font-size: 12px; font-weight: 600; padding: 2px 8px;")
        self.hist_page_combo_dates.currentIndexChanged.connect(self._on_hist_page_combo_changed)
        fb_layout.addWidget(self.hist_page_combo_dates)

        btn_today = QtWidgets.QPushButton("Hôm Nay")
        btn_today.setFixedHeight(28)
        btn_today.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_today.setStyleSheet("background-color: #FFFFFF; color: #2563EB; font-weight: 600; font-size: 11px; padding: 2px 10px; border: 1px solid #BFDBFE; border-radius: 5px;")
        btn_today.clicked.connect(lambda: self.hist_page_date_picker.setDate(QtCore.QDate.currentDate()))
        fb_layout.addWidget(btn_today)

        btn_yesterday = QtWidgets.QPushButton("Hôm Qua")
        btn_yesterday.setFixedHeight(28)
        btn_yesterday.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_yesterday.setStyleSheet("background-color: #FFFFFF; color: #475569; font-weight: 600; font-size: 11px; padding: 2px 10px; border: 1px solid #CBD5E1; border-radius: 5px;")
        btn_yesterday.clicked.connect(lambda: self.hist_page_date_picker.setDate(QtCore.QDate.currentDate().addDays(-1)))
        fb_layout.addWidget(btn_yesterday)

        fb_layout.addSpacing(6)

        self.btn_load_usage_history = QtWidgets.QPushButton("🔍 Tải Lịch Sử")
        self.btn_load_usage_history.setFixedHeight(28)
        self.btn_load_usage_history.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_load_usage_history.setStyleSheet("""
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 12px;
                padding: 4px 16px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #1D4ED8;
            }
        """)
        self.btn_load_usage_history.clicked.connect(self._load_usage_history_page)
        fb_layout.addWidget(self.btn_load_usage_history)

        fb_layout.addStretch()

        btn_refresh_db = QtWidgets.QPushButton("🔄 Kiểm Tra CSDL")
        btn_refresh_db.setFixedHeight(28)
        btn_refresh_db.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_refresh_db.setStyleSheet("background-color: #F1F5F9; color: #334155; font-size: 11px; font-weight: 600; padding: 2px 10px; border: 1px solid #CBD5E1; border-radius: 5px;")
        btn_refresh_db.clicked.connect(self._verify_database)
        fb_layout.addWidget(btn_refresh_db)

        layout.addWidget(filter_bar)

        # 3. 4 Thẻ chỉ số tổng quan (KPI Cards)
        cards_layout = QtWidgets.QHBoxLayout()
        cards_layout.setSpacing(12)

        self.card_hist_down, self.lbl_hist_down_val, self.lbl_hist_down_sub = self._create_kpi_card("📥 TỔNG TẢI XUỐNG", "0.0 MB", "Lưu lượng nhận trong ngày", "#D97706", "#FFFBEB", "#FDE68A")
        self.card_hist_up, self.lbl_hist_up_val, self.lbl_hist_up_sub = self._create_kpi_card("📤 TỔNG TẢI LÊN", "0.0 MB", "Lưu lượng gửi trong ngày", "#EA580C", "#FFF7ED", "#FED7AA")
        self.card_hist_peak, self.lbl_hist_peak_val, self.lbl_hist_peak_sub = self._create_kpi_card("⚡ TỐC ĐỘ ĐỈNH", "0.0 KB/s", "Chưa ghi nhận đỉnh", "#2563EB", "#EFF6FF", "#BFDBFE")
        self.card_hist_samples, self.lbl_hist_samples_val, self.lbl_hist_samples_sub = self._create_kpi_card("⏱️ MẪU ĐO & THỜI GIAN", "0 mẫu", "00:00:00 - 00:00:00", "#0F172A", "#F8FAFC", "#E2E8F0")

        cards_layout.addWidget(self.card_hist_down)
        cards_layout.addWidget(self.card_hist_up)
        cards_layout.addWidget(self.card_hist_peak)
        cards_layout.addWidget(self.card_hist_samples)
        layout.addLayout(cards_layout)

        # 4. Biểu đồ lịch sử tương tác (Interactive Historical Area Chart)
        chart_box = QtWidgets.QFrame()
        chart_box.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px;")
        cb_layout = QtWidgets.QVBoxLayout(chart_box)
        cb_layout.setContentsMargins(12, 10, 12, 10)
        cb_layout.setSpacing(6)

        c_header = QtWidgets.QHBoxLayout()
        lbl_chart_title = QtWidgets.QLabel("📈 Biểu Đồ Diễn Biến Lưu Lượng Băng Thông Trong Ngày")
        lbl_chart_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #1E293B;")
        c_header.addWidget(lbl_chart_title)
        c_header.addStretch()

        self.lbl_hist_chart_hint = QtWidgets.QLabel("👉 Rê chuột để soi tốc độ | Click điểm để dừng xem ứng dụng lúc đó")
        self.lbl_hist_chart_hint.setStyleSheet("font-size: 11px; color: #64748B; font-style: italic;")
        c_header.addWidget(self.lbl_hist_chart_hint)
        cb_layout.addLayout(c_header)

        # Plot Widget
        self.hist_time_axis = TimeAxisItem(orientation='bottom')
        self.hist_plot_widget = pg.PlotWidget(axisItems={'bottom': self.hist_time_axis})
        self.hist_plot_widget.setBackground('#FFFFFF')
        self.hist_plot_widget.setFixedHeight(220)
        self.hist_plot_widget.showGrid(x=False, y=True, alpha=0.08)
        self.hist_plot_widget.getAxis('bottom').setPen(pg.mkPen("#E2E8F0", width=1))
        self.hist_plot_widget.getAxis('bottom').setTextPen(pg.mkPen("#94A3B8"))
        self.hist_plot_widget.getAxis('left').setStyle(showValues=True)
        self.hist_plot_widget.getAxis('left').setPen(pg.mkPen("#E2E8F0", width=1))
        self.hist_plot_widget.getAxis('left').setTextPen(pg.mkPen("#94A3B8"))

        self.hist_curve_down = self.hist_plot_widget.plot(
            pen=pg.mkPen(color="#D97706", width=2.0),
            fillLevel=0,
            brush=(245, 158, 11, 210)
        )
        self.hist_curve_up = self.hist_plot_widget.plot(
            pen=pg.mkPen(color="#EA580C", width=1.5),
            fillLevel=0,
            brush=(244, 63, 94, 200)
        )

        # Crosshair & Marker cho biểu đồ lịch sử
        self.hist_v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#3B82F6", width=1.2, style=QtCore.Qt.PenStyle.DashLine))
        self.hist_h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#CBD5E1", width=1.0, style=QtCore.Qt.PenStyle.DotLine))
        self.hist_v_line.hide()
        self.hist_h_line.hide()
        self.hist_plot_widget.addItem(self.hist_v_line, ignoreBounds=True)
        self.hist_plot_widget.addItem(self.hist_h_line, ignoreBounds=True)

        self.hist_hover_marker = pg.ScatterPlotItem(size=8, pen=pg.mkPen("#2563EB", width=2), brush=pg.mkBrush("#FFFFFF"), symbol='o')
        self.hist_hover_marker.hide()
        self.hist_plot_widget.addItem(self.hist_hover_marker)

        self.hist_pinned_marker = pg.ScatterPlotItem(size=14, pen=pg.mkPen("#FFFFFF", width=2.5), brush=pg.mkBrush("#DC2626"), symbol='star')
        self.hist_pinned_marker.hide()
        self.hist_plot_widget.addItem(self.hist_pinned_marker)

        # Tooltip badge cho biểu đồ lịch sử
        self.hist_hover_badge = QtWidgets.QFrame(self.hist_plot_widget)
        self.hist_hover_badge.setStyleSheet("""
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
        self.hist_hover_badge.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        h_hb_layout = QtWidgets.QVBoxLayout(self.hist_hover_badge)
        h_hb_layout.setContentsMargins(4, 2, 4, 2)
        h_hb_layout.setSpacing(1)
        self.lbl_hist_hover_text = QtWidgets.QLabel("")
        self.lbl_hist_hover_text.setTextFormat(QtCore.Qt.TextFormat.RichText)
        h_hb_layout.addWidget(self.lbl_hist_hover_text)
        self.hist_hover_badge.hide()

        self.hist_plot_widget.scene().sigMouseMoved.connect(self._on_hist_plot_mouse_moved)
        self.hist_plot_widget.scene().sigMouseClicked.connect(self._on_hist_plot_mouse_clicked)

        cb_layout.addWidget(self.hist_plot_widget)

        # Banner chi tiết điểm soi trên trang lịch sử
        self.hist_point_card = QtWidgets.QFrame()
        self.hist_point_card.setObjectName("histPointCard")
        self.hist_point_card.setStyleSheet("""
            QFrame#histPointCard {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-left: 5px solid #2563EB;
                border-radius: 6px;
                padding: 6px 12px;
            }
        """)
        self.hist_point_card.setVisible(False)
        hpc_layout = QtWidgets.QHBoxLayout(self.hist_point_card)
        hpc_layout.setContentsMargins(6, 4, 6, 4)
        self.lbl_hist_point_details = QtWidgets.QLabel("")
        self.lbl_hist_point_details.setTextFormat(QtCore.Qt.TextFormat.RichText)
        hpc_layout.addWidget(self.lbl_hist_point_details)
        cb_layout.addWidget(self.hist_point_card)

        layout.addWidget(chart_box)

        # 5. QTabWidget bên dưới: Top Ứng Dụng | Nhật Ký Mẫu | CSDL MySQL
        self.hist_tabs = QtWidgets.QTabWidget()
        self.hist_tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #E2E8F0;
                background-color: #FFFFFF;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #F8FAFC;
                color: #475569;
                font-weight: 600;
                font-size: 12px;
                padding: 8px 16px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
                border: 1px solid #E2E8F0;
                border-bottom: none;
            }
            QTabBar::tab:selected {
                background-color: #FFFFFF;
                color: #2563EB;
                font-weight: bold;
                border-bottom: 2px solid #2563EB;
            }
        """)

        # Tab 1: Top Ứng Dụng
        tab_apps = QtWidgets.QWidget()
        ta_layout = QtWidgets.QVBoxLayout(tab_apps)
        ta_layout.setContentsMargins(12, 12, 12, 12)
        ta_layout.setSpacing(8)

        self.top_apps_table = QtWidgets.QTableWidget()
        self.top_apps_table.setColumnCount(6)
        self.top_apps_table.setHorizontalHeaderLabels(["Ứng Dụng", "PID", "Tải Xuống (RX)", "Tải Lên (TX)", "Tổng Lưu Lượng", "Tỷ Lệ Tiêu Thụ %"])
        self.top_apps_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.top_apps_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.top_apps_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.top_apps_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.top_apps_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.top_apps_table.horizontalHeader().setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.top_apps_table.setColumnWidth(5, 170)
        self.top_apps_table.verticalHeader().setVisible(False)
        self.top_apps_table.setMinimumHeight(240)
        ta_layout.addWidget(self.top_apps_table)
        self.hist_tabs.addTab(tab_apps, "🏆 Top Ứng Dụng Trong Ngày")

        # Tab 2: Nhật Ký Mẫu Chi Tiết
        tab_logs = QtWidgets.QWidget()
        tl_layout = QtWidgets.QVBoxLayout(tab_logs)
        tl_layout.setContentsMargins(12, 12, 12, 12)
        tl_layout.setSpacing(8)

        log_filter_box = QtWidgets.QHBoxLayout()
        self.txt_hist_log_filter = QtWidgets.QLineEdit()
        self.txt_hist_log_filter.setPlaceholderText("🔍 Lọc theo tên tiến trình (vd: chrome, python, system)...")
        self.txt_hist_log_filter.textChanged.connect(self._filter_hist_logs)
        log_filter_box.addWidget(self.txt_hist_log_filter)
        tl_layout.addLayout(log_filter_box)

        self.history_table = QtWidgets.QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels(["Thời Gian", "PID", "Tên Tiến Trình", "Bytes Nhận (RX)", "Bytes Gửi (TX)"])
        self.history_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setMinimumHeight(240)
        tl_layout.addWidget(self.history_table)
        self.hist_tabs.addTab(tab_logs, "📋 Nhật Ký Mẫu Lưu Lượng")

        # Tab 3: CSDL MySQL
        tab_db = QtWidgets.QWidget()
        tdb_layout = QtWidgets.QVBoxLayout(tab_db)
        tdb_layout.setContentsMargins(12, 12, 12, 12)
        tdb_layout.setSpacing(10)

        self.db_stats_card = QtWidgets.QLabel("Đang kết nối và kiểm tra CSDL MySQL...")
        self.db_stats_card.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px; font-size: 13px; color: #334155; line-height: 1.6;")
        tdb_layout.addWidget(self.db_stats_card)
        tdb_layout.addStretch()
        self.hist_tabs.addTab(tab_db, "🗄️ Trạng Thái CSDL MySQL (6 Bảng)")

        layout.addWidget(self.hist_tabs)

        scroll.setWidget(content_widget)
        parent_layout = QtWidgets.QVBoxLayout(parent)
        parent_layout.setContentsMargins(0, 0, 0, 0)
        parent_layout.addWidget(scroll)

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


        QtCore.QTimer.singleShot(1500, self._verify_database)

    def _on_time_window_changed(self, index: int):
        windows = [30.0, 60.0, 300.0, 600.0, 3600.0, 86400.0]
        self.window_duration = windows[min(index, len(windows) - 1)]
        self.time_axis.set_window_duration(self.window_duration)
        self.timeline_scrubber.set_window_duration(self.window_duration)
        if not self.history_mode:
            now = time.time()
            render_now = now - self.render_delay
            self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)
            # Cập nhật ngay bảng bên trái, HUD và Usage view theo khung thời gian vừa chọn
            self._update_app_sidebar(self.latest_proc_speeds)
            self._update_selected_app_hud()
            if hasattr(self, 'usage_view'):
                self._update_usage_view_now()
        else:
            if self.historical_series:
                day_start = self.history_summary.get("day_start", self.historical_series[0]["timestamp"])
                day_end = self.history_summary.get("day_end", self.historical_series[-1]["timestamp"])
                center_t = getattr(self, "pinned_time", None)
                if center_t is None:
                    center_t = self.history_summary.get("peak_down_time_ts", (day_start + day_end) / 2.0)
                half = self.window_duration / 2.0
                win_start = max(day_start, center_t - half)
                win_end = min(day_end, win_start + self.window_duration)
                if win_end - win_start < self.window_duration:
                    win_start = max(day_start, win_end - self.window_duration)
                self._render_window_data(win_start, win_end)
                self.timeline_scrubber.set_slider_range(win_start, win_end)

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
                if pname not in self.app_metadata:
                    self.app_metadata[pname] = {
                        "pid": item.get("pid", 0),
                        "exe": item.get("exe", "")
                    }

            self.down_points.append((timestamp, down_kb))
            self.up_points.append((timestamp, up_kb))

            # Lưu vết mẫu theo thời gian để tính toán chính xác lưu lượng theo khung (30s, 1m, 5m, 10m, 1h, 24h)
            dt = UPDATE_INTERVAL
            active_procs = {}
            for p_item in proc_speeds:
                pname = p_item.get("name", "")
                if not pname:
                    continue
                d_kb = p_item.get("down_kb", 0.0)
                u_kb = p_item.get("up_kb", 0.0)
                if d_kb > 0 or u_kb > 0:
                    active_procs[pname] = (d_kb * 1024.0 * dt, u_kb * 1024.0 * dt)

            if hasattr(self, 'window_sample_history'):
                self.window_sample_history.append({
                    "t": timestamp,
                    "down_b": down_kb * 1024.0 * dt,
                    "up_b": up_kb * 1024.0 * dt,
                    "procs": active_procs
                })

            # Lưu toàn bộ danh sách tiến trình có lưu lượng tại thời điểm này
            all_active_point_procs = []
            for p in proc_speeds:
                p_d = p.get("down_kb", 0.0)
                p_u = p.get("up_kb", 0.0)
                if p_d > 0 or p_u > 0:
                    all_active_point_procs.append({
                        "name": p.get("name", "Unknown"),
                        "down_kb": p_d,
                        "up_kb": p_u,
                        "pid": p.get("pid", 0),
                        "exe": p.get("exe", "")
                    })
            all_active_point_procs.sort(key=lambda x: x["down_kb"] + x["up_kb"], reverse=True)

            self.point_history.append({
                "timestamp": timestamp,
                "down_kb": down_kb,
                "up_kb": up_kb,
                "top_procs": all_active_point_procs
            })

            self.timeline_scrubber.add_point(timestamp, down_kb + up_kb)
            if not self.is_paused:
                # 1. Cập nhật bảng ứng dụng bên trái
                self._update_app_sidebar(proc_speeds)
                # 2. Cập nhật HUD bên dưới biểu đồ
                self._update_selected_app_hud()

            # 3. Đo đạc xác minh QoS thời gian thực
            self._update_qos_realtime_verification(proc_speeds)
            if hasattr(self, 'protect_view'):
                self.protect_view.update_protect_table(proc_speeds, self.app_accumulated_bytes, self.app_metadata)
            if hasattr(self, 'traffic_content_stack') and self.traffic_content_stack.currentIndex() == 1:
                self._update_usage_view_now()
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

    def _compute_window_traffic(self, win_duration: Optional[float] = None, t_end: Optional[float] = None) -> Tuple[float, float, Dict[str, Dict[str, float]]]:
        """
        Tính tổng lưu lượng download, upload toàn hệ thống và theo từng tiến trình
        trong đúng khung thời gian win_duration (30s, 1m, 5m, 10m, 1h, 24h).
        Trả về: (tot_down_bytes, tot_up_bytes, app_bytes_dict)
        """
        if win_duration is None:
            win_duration = getattr(self, "window_duration", 300.0)

        now = t_end if t_end is not None else time.time()
        cutoff = now - win_duration

        if not hasattr(self, "window_sample_history") or not self.window_sample_history:
            return self.total_accumulated_down, self.total_accumulated_up, self.app_accumulated_bytes

        tot_down = 0.0
        tot_up = 0.0
        app_bytes: Dict[str, Dict[str, float]] = {}

        for s in reversed(self.window_sample_history):
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

    def _update_app_sidebar(self, proc_speeds: list):
        """Cập nhật danh sách ứng dụng trong Sidebar theo chiều lọc Incoming / Outgoing và khung thời gian đã chọn"""
        search_kw = self.search_app_input.text().strip().lower()

        # Tạo map tốc độ tức thời của chu kỳ hiện tại
        current_speeds = {p.get("name", ""): p for p in proc_speeds if p.get("name")}

        # Lấy lưu lượng tích lũy chính xác theo khung thời gian đang chọn (30s, 1m, 5m, 10m, 1h, 24h)
        win_down, win_up, win_app_bytes = self._compute_window_traffic()

        f_scope = getattr(self, "filter_scope", "all")
        scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)

        display_apps = []
        all_app_names = set(win_app_bytes.keys()) | set(current_speeds.keys())
        for app_name in all_app_names:
            if app_name == "All apps":
                continue

            accum = win_app_bytes.get(app_name, {"down": 0.0, "up": 0.0})
            down_b = accum.get("down", 0.0) * scope_factor
            up_b = accum.get("up", 0.0) * scope_factor
            tot_b = down_b + up_b

            cur_p = current_speeds.get(app_name)
            cur_spd = (cur_p.get("down_kb", 0.0) + cur_p.get("up_kb", 0.0)) if cur_p else 0.0

            if tot_b <= 0 and cur_spd <= 0:
                continue

            if search_kw and search_kw not in app_name.lower():
                continue

            meta = self.app_metadata.get(app_name, {})
            pid = cur_p.get("pid") if cur_p else meta.get("pid", 0)
            exe = cur_p.get("exe") if cur_p else meta.get("exe", "")

            display_apps.append({
                "name": app_name,
                "pid": pid,
                "exe": exe,
                "down_bytes": down_b,
                "up_bytes": up_b,
                "total_bytes": tot_b,
                "is_active_now": cur_spd > 0,
                "cur_speed": cur_spd
            })

        # Sắp xếp theo chiều lưu lượng đang chọn
        f_dir = getattr(self, "filter_direction", "both")

        if f_dir == "incoming":
            display_apps.sort(key=lambda x: (x["is_active_now"], x["down_bytes"]), reverse=True)
        elif f_dir == "outgoing":
            display_apps.sort(key=lambda x: (x["is_active_now"], x["up_bytes"]), reverse=True)
        else:
            display_apps.sort(key=lambda x: (x["is_active_now"], x["total_bytes"]), reverse=True)

        self.app_list_table.blockSignals(True)
        try:
            prev_selected = self.selected_app_name
            self.app_list_table.setRowCount(len(display_apps) + 1)

            # Row 0: All apps
            item_all_name = QtWidgets.QTableWidgetItem("  All apps")
            item_all_name.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_name.setForeground(QtGui.QBrush(QtGui.QColor("#0F172A")))

            # Icon All apps
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

            total_win_down = win_down * scope_factor
            total_win_up = win_up * scope_factor

            if f_dir == "incoming":
                down_all_str = self._format_bytes(total_win_down)
                item_all_down = QtWidgets.QTableWidgetItem(down_all_str)
                item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                item_all_up = QtWidgets.QTableWidgetItem("--")
                item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            elif f_dir == "outgoing":
                item_all_down = QtWidgets.QTableWidgetItem("--")
                item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                up_all_str = self._format_bytes(total_win_up)
                item_all_up = QtWidgets.QTableWidgetItem(up_all_str)
                item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            else:
                down_all_str = self._format_bytes(total_win_down)
                item_all_down = QtWidgets.QTableWidgetItem(down_all_str)
                item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                up_all_str = self._format_bytes(total_win_up)
                item_all_up = QtWidgets.QTableWidgetItem(up_all_str)
                item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            self.app_list_table.setItem(0, 0, item_all_name)
            self.app_list_table.setItem(0, 1, item_all_down)
            self.app_list_table.setItem(0, 2, item_all_up)

            selected_row_idx = 0 if prev_selected == "All apps" else -1

            # Các hàng ứng dụng cụ thể
            for idx, item in enumerate(display_apps, start=1):
                name = item["name"]
                name_item = QtWidgets.QTableWidgetItem(f"  {name}")
                name_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))

                exe_path = item.get("exe", "")
                if not exe_path and item.get("pid", 0) > 0:
                    try:
                        exe_path = psutil.Process(item["pid"]).exe()
                    except Exception:
                        pass
                icon = self.icon_manager.get_icon(exe_path, name)
                name_item.setIcon(icon)

                if f_dir == "incoming":
                    down_str = self._format_bytes(item["down_bytes"])
                    d_item = QtWidgets.QTableWidgetItem(down_str)
                    d_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                    d_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                    d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                    u_item = QtWidgets.QTableWidgetItem("--")
                    u_item.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                    u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                elif f_dir == "outgoing":
                    d_item = QtWidgets.QTableWidgetItem("--")
                    d_item.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                    d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                    up_str = self._format_bytes(item["up_bytes"])
                    u_item = QtWidgets.QTableWidgetItem(up_str)
                    u_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                    u_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                    u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                else:
                    down_str = self._format_bytes(item["down_bytes"])
                    d_item = QtWidgets.QTableWidgetItem(down_str)
                    d_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
                    d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                    up_str = self._format_bytes(item["up_bytes"])
                    u_item = QtWidgets.QTableWidgetItem(up_str)
                    u_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
                    u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                self.app_list_table.setItem(idx, 0, name_item)
                self.app_list_table.setItem(idx, 1, d_item)
                self.app_list_table.setItem(idx, 2, u_item)

                if name == prev_selected:
                    selected_row_idx = idx

                if hasattr(self, 'combo_qos_app') and self.combo_qos_app.findText(name) == -1:
                    self.combo_qos_app.addItem(name)

            if selected_row_idx >= 0:
                self.app_list_table.selectRow(selected_row_idx)
        finally:
            self.app_list_table.blockSignals(False)

    def _update_app_sidebar_history(self, top_procs: list, total_rx: float, total_tx: float):
        """Cập nhật danh sách ứng dụng trong Sidebar theo dữ liệu lịch sử ngày chuẩn phong cách GlassWire"""
        search_kw = self.search_app_input.text().strip().lower()

        filtered_procs = []
        for p in top_procs:
            name = p.get("name", "")
            if search_kw and search_kw not in name.lower():
                continue
            filtered_procs.append(p)

        self.app_list_table.setRowCount(len(filtered_procs) + 1)

        # Row 0: All apps
        item_all_name = QtWidgets.QTableWidgetItem("  All apps")
        item_all_name.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        item_all_name.setForeground(QtGui.QBrush(QtGui.QColor("#0F172A")))

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

        f_dir = getattr(self, "filter_direction", "both")
        if f_dir == "incoming":
            item_all_down = QtWidgets.QTableWidgetItem(self._format_bytes(total_rx))
            item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
            item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            item_all_up = QtWidgets.QTableWidgetItem("--")
            item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
            item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        elif f_dir == "outgoing":
            item_all_down = QtWidgets.QTableWidgetItem("--")
            item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
            item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            item_all_up = QtWidgets.QTableWidgetItem(self._format_bytes(total_tx))
            item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
            item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        else:
            item_all_down = QtWidgets.QTableWidgetItem(self._format_bytes(total_rx))
            item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
            item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            item_all_up = QtWidgets.QTableWidgetItem(self._format_bytes(total_tx))
            item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
            item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.app_list_table.setItem(0, 0, item_all_name)
        self.app_list_table.setItem(0, 1, item_all_down)
        self.app_list_table.setItem(0, 2, item_all_up)

        for idx, item in enumerate(filtered_procs, start=1):
            name = item.get("name", "Unknown")
            name_item = QtWidgets.QTableWidgetItem(f"  {name}")
            name_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))

            # Trích xuất icon của ứng dụng
            icon = self.icon_manager.get_icon("", name)
            name_item.setIcon(icon)

            rx_b = item.get("rx_bytes", 0)
            tx_b = item.get("tx_bytes", 0)

            if f_dir == "incoming":
                d_item = QtWidgets.QTableWidgetItem(self._format_bytes(rx_b))
                d_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                d_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                u_item = QtWidgets.QTableWidgetItem("--")
                u_item.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            elif f_dir == "outgoing":
                d_item = QtWidgets.QTableWidgetItem("--")
                d_item.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                u_item = QtWidgets.QTableWidgetItem(self._format_bytes(tx_b))
                u_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                u_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            else:
                d_item = QtWidgets.QTableWidgetItem(self._format_bytes(rx_b))
                d_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
                d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                u_item = QtWidgets.QTableWidgetItem(self._format_bytes(tx_b))
                u_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
                u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            self.app_list_table.setItem(idx, 0, name_item)
            self.app_list_table.setItem(idx, 1, d_item)
            self.app_list_table.setItem(idx, 2, u_item)

    def _filter_app_list(self):
        if self.history_mode and hasattr(self, "history_top_procs"):
            self._update_app_sidebar_history(
                self.history_top_procs,
                self.history_summary.get("total_rx_bytes", 0),
                self.history_summary.get("total_tx_bytes", 0)
            )
        else:
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
        """Cập nhật khối thống kê chi tiết của tiến trình đang chọn theo khung thời gian (chuẩn phong cách GlassWire)"""
        win_down, win_up, win_app_bytes = self._compute_window_traffic()
        f_scope = getattr(self, "filter_scope", "all")
        scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)

        if self.history_mode:
            if self.selected_app_name == "All apps":
                d_total = self.history_summary.get("total_rx_bytes", 0) * scope_factor
                u_total = self.history_summary.get("total_tx_bytes", 0) * scope_factor
            else:
                d_total = 0.0
                u_total = 0.0
                for p in getattr(self, "history_top_procs", []):
                    if p.get("name", "").lower() == self.selected_app_name.lower():
                        d_total = p.get("rx_bytes", 0) * scope_factor
                        u_total = p.get("tx_bytes", 0) * scope_factor
                        break
            d_speed = 0.0
            u_speed = 0.0
        elif self.selected_app_name == "All apps":
            d_speed = self.latest_down
            u_speed = self.latest_up
            d_total = win_down * scope_factor
            u_total = win_up * scope_factor
        else:
            d_speed = 0.0
            u_speed = 0.0
            for item in self.latest_proc_speeds:
                if item.get("name", "").lower() == self.selected_app_name.lower():
                    d_speed = item.get("down_kb", 0.0)
                    u_speed = item.get("up_kb", 0.0)
                    break
            accum = win_app_bytes.get(self.selected_app_name, {"down": 0.0, "up": 0.0})
            d_total = accum["down"] * scope_factor
            u_total = accum["up"] * scope_factor

        total_bytes = d_total + u_total
        total_str = self._format_bytes(total_bytes)

        # Cột Download bên trái
        f_dir = getattr(self, "filter_direction", "both")
        if f_dir == "outgoing":
            self.hud_down_total.setText("--")
            self.hud_down_speed.setText("--  ↓")
        else:
            self.hud_down_total.setText(self._format_bytes(d_total))
            self.hud_down_speed.setText(f"{self._format_speed(d_speed)}  ↓")

        # Đồng hồ bán nguyệt ở giữa
        pct = int(min(100, max(15, (total_bytes / (10 * 1024 * 1024)) * 100.0))) if total_bytes > 0 else 15
        self.arc_gauge.set_value(total_str, pct)

        # Cột Upload bên phải
        if f_dir == "incoming":
            self.hud_up_total.setText("--")
            self.hud_up_speed.setText("--  ↑")
        else:
            self.hud_up_total.setText(self._format_bytes(u_total))
            self.hud_up_speed.setText(f"{self._format_speed(u_speed)}  ↑")

        # Phân bổ WAN & LAN
        f_scope = getattr(self, "filter_scope", "all")
        if f_scope == "wan":
            wan_bytes = total_bytes * 0.98
            self.wan_lan_widget.set_values(f"WAN  {self._format_bytes(wan_bytes)}", "LAN  --", 100)
        elif f_scope == "lan":
            lan_bytes = total_bytes * 0.05
            self.wan_lan_widget.set_values("WAN  --", f"LAN  {self._format_bytes(lan_bytes)}", 15)
        else:
            wan_bytes = total_bytes * 0.98
            lan_bytes = total_bytes * 0.02
            self.wan_lan_widget.set_values(f"WAN  {self._format_bytes(wan_bytes)}", f"LAN  {self._format_bytes(lan_bytes)}", 94)

    def _render_frame(self):
        """Vòng lặp vẽ đồ thị mượt mà 35 FPS với bộ đệm trễ 5 giây và thuật toán khử gai GlassWire"""
        try:
            if self.history_mode or self.is_paused:
                return

            now = time.time()
            render_now = now - self.render_delay

            # Giữ lại tối thiểu 3600 giây trong bộ nhớ để người dùng có thể tua lại tự do trên timeline
            cutoff = render_now - 3600.0
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

            f_dir = getattr(self, "filter_direction", "both")
            if f_dir == "incoming":
                self.curve_down.setData(x_dense, smooth_down)
                self.curve_down.show()
                self.curve_up.hide()
                peak_val = max(float(np.max(smooth_down)), 1.0)
            elif f_dir == "outgoing":
                self.curve_up.setData(x_dense, smooth_up)
                self.curve_up.show()
                self.curve_down.hide()
                peak_val = max(float(np.max(smooth_up)), 1.0)
            else:
                self.curve_down.setData(x_dense, smooth_down)
                self.curve_up.setData(x_dense, smooth_up)
                self.curve_down.show()
                self.curve_up.show()
                peak_val = max(float(np.max(smooth_down)), float(np.max(smooth_up)), 1.0)
            self._update_y_scale(peak_val)

            # Trượt camera theo thời gian thực
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
            if not self.history_mode and self.down_points:
                pts = list(self.down_points)
                best_pt = min(pts, key=lambda p: abs(p[0] - target_t))
                return {
                    "timestamp": best_pt[0],
                    "down_kb": best_pt[1],
                    "up_kb": 0.0,
                    "top_procs": []
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

    def _on_scrubber_range_selected(self, win_start: float, win_end: float, target_t: float):
        """Bấm hoặc kéo khung trên timeline scrubber: Di chuyển và xem lại biểu đồ chính theo đúng khung thời gian này."""
        self._inspect_time_window(win_start, win_end, target_t)

    def _on_scrubber_point_selected(self, target_t: float):
        """Bấm vào mốc thời gian trên timeline scrubber: Căn khung nhìn xoay quanh điểm đó và hiển thị chi tiết."""
        half = self.window_duration / 2.0
        self._inspect_time_window(target_t - half, target_t + half, target_t)

    def _inspect_time_window(self, win_start: float, win_end: float, target_t: float):
        """Tạm dừng và cập nhật biểu đồ chính (khung chính) hiển thị chính xác theo khung nhìn thời gian (khung này)."""
        self.is_paused = True
        self.pinned_time = target_t

        # 1. Vẽ dữ liệu và chuyển camera biểu đồ chính tới khung nhìn này
        self._render_window_data(win_start, win_end)

        # 2. Ghim mốc và hiển thị HUD/chi tiết ứng dụng
        self._pin_point(target_t)

    def _render_window_data(self, win_start: float, win_end: float):
        """Vẽ đường sóng và thang đo cho khung thời gian [win_start, win_end] tương ứng với khung trượt."""
        try:
            if self.history_mode:
                self.plot_widget.setXRange(win_start, win_end, padding=0.0)
                if self.historical_series:
                    win_dur = max(10.0, win_end - win_start)
                    hx = np.array([p["timestamp"] for p in self.historical_series], dtype=np.float64)
                    hy_down = np.array([p.get("down_kb", 0.0) for p in self.historical_series], dtype=np.float64)
                    hy_up = np.array([p.get("up_kb", 0.0) for p in self.historical_series], dtype=np.float64)

                    x_dense = np.linspace(win_start, win_end, 600)
                    smooth_down = self._generate_glasswire_wave(hx, hy_down, x_dense, win_dur)
                    smooth_up = self._generate_glasswire_wave(hx, hy_up, x_dense, win_dur)

                    f_dir = getattr(self, "filter_direction", "both")
                    f_scope = getattr(self, "filter_scope", "all")
                    scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)
                    smooth_down_disp = smooth_down * scope_factor
                    smooth_up_disp = smooth_up * scope_factor

                    if f_dir == "incoming":
                        self.curve_down.setData(x_dense, smooth_down_disp)
                        self.curve_down.show()
                        self.curve_up.hide()
                        peak_val = max(float(np.max(smooth_down_disp)), 1.0) if len(smooth_down_disp) else 1.0
                    elif f_dir == "outgoing":
                        self.curve_up.setData(x_dense, smooth_up_disp)
                        self.curve_up.show()
                        self.curve_down.hide()
                        peak_val = max(float(np.max(smooth_up_disp)), 1.0) if len(smooth_up_disp) else 1.0
                    else:
                        self.curve_down.setData(x_dense, smooth_down_disp)
                        self.curve_up.setData(x_dense, smooth_up_disp)
                        self.curve_down.show()
                        self.curve_up.show()
                        peak_val = max(float(np.max(smooth_down_disp)), float(np.max(smooth_up_disp)), 1.0) if len(smooth_down_disp) else 1.0

                    self._update_y_scale(peak_val)
                return

            win_duration = max(10.0, win_end - win_start)

            # Lấy các mẫu đo từ down_points / up_points hoặc point_history
            d_pts = [p for p in self.down_points if (win_start - win_duration * 0.5) <= p[0] <= (win_end + win_duration * 0.5)]
            u_pts = [p for p in self.up_points if (win_start - win_duration * 0.5) <= p[0] <= (win_end + win_duration * 0.5)]

            if not d_pts and self.point_history:
                d_pts = [(p["timestamp"], p.get("down_kb", 0.0)) for p in self.point_history if (win_start - win_duration * 0.5) <= p["timestamp"] <= (win_end + win_duration * 0.5)]
                u_pts = [(p["timestamp"], p.get("up_kb", 0.0)) for p in self.point_history if (win_start - win_duration * 0.5) <= p["timestamp"] <= (win_end + win_duration * 0.5)]

            if len(d_pts) >= 2:
                d_raw_x = np.array([p[0] for p in d_pts], dtype=np.float64)
                d_raw_y = np.array([p[1] for p in d_pts], dtype=np.float64)
                u_raw_x = np.array([p[0] for p in u_pts], dtype=np.float64)
                u_raw_y = np.array([p[1] for p in u_pts], dtype=np.float64)

                cfg = TIME_SCALE_CONFIGS.get(self.window_duration, TIME_SCALE_CONFIGS[300.0])
                n_dense = max(200, cfg["n_dense"])
                x_dense = np.linspace(win_start, win_end, n_dense)

                smooth_down = self._generate_glasswire_wave(d_raw_x, d_raw_y, x_dense, win_duration)
                smooth_up = self._generate_glasswire_wave(u_raw_x, u_raw_y, x_dense, win_duration)

                f_dir = getattr(self, "filter_direction", "both")
                f_scope = getattr(self, "filter_scope", "all")
                scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)
                smooth_down_disp = smooth_down * scope_factor
                smooth_up_disp = smooth_up * scope_factor

                if f_dir == "incoming":
                    self.curve_down.setData(x_dense, smooth_down_disp)
                    self.curve_down.show()
                    self.curve_up.hide()
                    peak_val = max(float(np.max(smooth_down_disp)), 1.0)
                elif f_dir == "outgoing":
                    self.curve_up.setData(x_dense, smooth_up_disp)
                    self.curve_up.show()
                    self.curve_down.hide()
                    peak_val = max(float(np.max(smooth_up_disp)), 1.0)
                else:
                    self.curve_down.setData(x_dense, smooth_down_disp)
                    self.curve_up.setData(x_dense, smooth_up_disp)
                    self.curve_down.show()
                    self.curve_up.show()
                    peak_val = max(float(np.max(smooth_down_disp)), float(np.max(smooth_up_disp)), 1.0)
                self._update_y_scale(peak_val)

            self.plot_widget.setXRange(win_start, win_end, padding=0.0)
        except Exception as e:
            print(f"[Render Window Error] {e}")

    def _pin_point(self, target_t: float):
        """Ghim điểm và hiển thị toàn bộ chi tiết dữ liệu (tốc độ, thời gian, tiến trình mạng)."""
        self.is_paused = True
        nearest = self._find_nearest_sample(target_t)
        if not nearest:
            nearest = {"timestamp": target_t, "down_kb": 0.0, "up_kb": 0.0, "top_procs": []}

        t_val = nearest["timestamp"]
        d_val = nearest.get("down_kb", 0.0)
        u_val = nearest.get("up_kb", 0.0)

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

                # Cập nhật thanh Inspection Bar mới (chuẩn GlassWire Hình 2)
        if hasattr(self, 'point_inspection_bar'):
            self.point_inspection_bar.set_inspection_data(
                d_val, u_val, t_val, top_procs, self.icon_manager, self._format_speed
            )
            self.point_inspection_bar.show()

        # Ẩn banner cũ ở đỉnh nếu đang ở chế độ Live (ở History mode thì vẫn giữ banner tổng quan)
        if hasattr(self, 'point_inspector_card') and not getattr(self, 'history_mode', False):
            self.point_inspector_card.setVisible(False)

        # Đồng bộ vị trí khung trượt trên scrubber nếu chưa được gán
        if self.timeline_scrubber.slider_start is None and not self.history_mode:
            half = self.window_duration / 2.0
            self.timeline_scrubber.set_slider_range(target_t - half, target_t + half)

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
        if getattr(self, 'history_mode', False):
            self.pinned_time = None
            self.pinned_vline.hide()
            self.pinned_marker.hide()
            if hasattr(self, 'point_inspection_bar'):
                self.point_inspection_bar.hide()
            return

        self.is_paused = False
        self.pinned_time = None
        self.pinned_vline.hide()
        self.pinned_marker.hide()
        if hasattr(self, 'point_inspector_card'):
            self.point_inspector_card.setVisible(False)
        if hasattr(self, 'point_inspection_bar'):
            self.point_inspection_bar.hide()
        self.timeline_scrubber.reset_to_live()

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
        if hasattr(self, 'live_controls_widget'):
            self.live_controls_widget.setVisible(not is_visible)
        if is_visible:
            self._populate_history_dates()

    def _populate_history_dates(self):
        """Truy vấn danh sách các ngày có dữ liệu đo trong CSDL và nạp vào ComboBox thanh công cụ."""
        def bg():
            try:
                dates = self.service.get_recorded_dates()
                self.async_bridge.run_on_main(self._apply_history_dates, dates)
            except Exception as e:
                print(f"[UI History Dates Error] {e}")
        threading.Thread(target=bg, daemon=True).start()

    def _apply_history_dates(self, dates: list):
        self.combo_history_date.blockSignals(True)
        self.combo_history_date.clear()
        today_str = time.strftime("%Y-%m-%d")
        if dates:
            for d in dates:
                self.combo_history_date.addItem(d)
            latest_d = dates[0]
            self.date_picker_history.setDate(QtCore.QDate.fromString(latest_d, "yyyy-MM-dd"))
        else:
            self.combo_history_date.addItem(today_str)
            self.date_picker_history.setDate(QtCore.QDate.currentDate())
        self.combo_history_date.blockSignals(False)

    def _on_combo_history_date_changed(self, index: int):
        date_str = self.combo_history_date.currentText().strip()
        if date_str:
            self.date_picker_history.blockSignals(True)
            self.date_picker_history.setDate(QtCore.QDate.fromString(date_str, "yyyy-MM-dd"))
            self.date_picker_history.blockSignals(False)

    def _on_history_date_picker_changed(self, date: QtCore.QDate):
        date_str = date.toString("yyyy-MM-dd")
        # Đồng bộ ngược lại sang combobox nếu có
        idx = self.combo_history_date.findText(date_str)
        if idx >= 0:
            self.combo_history_date.blockSignals(True)
            self.combo_history_date.setCurrentIndex(idx)
            self.combo_history_date.blockSignals(False)

    def _load_history_for_selected_date(self):
        """Tải dữ liệu của 1 ngày trong quá khứ và vẽ lại biểu đồ ngày hôm đó (theo cấu trúc toàn cảnh 24 giờ)."""
        target_date = self.date_picker_history.date().toString("yyyy-MM-dd")
        if not target_date:
            target_date = self.combo_history_date.currentText().strip()
        if not target_date:
            return

        self.btn_load_history.setEnabled(False)
        self.btn_load_history.setText("Đang tải...")

        def bg():
            try:
                series = self.service.get_daily_traffic_series(target_date)
                summary = self.service.get_daily_summary(target_date, series=series)
                top_procs = self.service.get_top_processes_for_date(target_date, limit=30)
                self.async_bridge.run_on_main(self._apply_history_series, series, target_date, summary, top_procs)
            except Exception as e:
                print(f"[UI Load History Error] {e}")
                self.async_bridge.run_on_main(self._on_history_load_failed, target_date, str(e))

        threading.Thread(target=bg, daemon=True).start()

    def _on_history_load_failed(self, target_date: str, err_msg: str):
        self.btn_load_history.setEnabled(True)
        self.btn_load_history.setText("🔍 Tải Biểu Đồ")
        QtWidgets.QMessageBox.warning(self, "Lỗi tải lịch sử", f"Không thể tải dữ liệu ngày {target_date}: {err_msg}")

    def _apply_history_series(self, series: list, target_date: str, summary: dict = None, top_procs: list = None):
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
        self.history_summary = summary or {}
        self.history_top_procs = top_procs or []
        self.selected_app_name = "All apps"

        # 1. Đồng bộ thanh chọn thời gian sang 24 Hours
        self.window_duration = 86400.0
        self.time_axis.set_window_duration(86400.0)
        self.combo_time_window.blockSignals(True)
        idx_24h = self.combo_time_window.findText("24 Hours")
        if idx_24h != -1:
            self.combo_time_window.setCurrentIndex(idx_24h)
        self.combo_time_window.blockSignals(False)

        day_start = summary.get("day_start", series[0]["timestamp"])
        day_end = summary.get("day_end", series[-1]["timestamp"])

        # 2. Nội suy đường sóng toàn cảnh 24 giờ siêu mượt (GlassWire dome wave)
        hx = np.array([p["timestamp"] for p in series], dtype=np.float64)
        hy_down = np.array([p["down_kb"] for p in series], dtype=np.float64)
        hy_up = np.array([p["up_kb"] for p in series], dtype=np.float64)

        x_dense = np.linspace(day_start, day_end, 800)
        smooth_down = self._generate_glasswire_wave(hx, hy_down, x_dense, 86400.0)
        smooth_up = self._generate_glasswire_wave(hx, hy_up, x_dense, 86400.0)

        f_dir = getattr(self, "filter_direction", "both")
        f_scope = getattr(self, "filter_scope", "all")
        scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)
        smooth_down_disp = smooth_down * scope_factor
        smooth_up_disp = smooth_up * scope_factor

        if f_dir == "incoming":
            self.curve_down.setData(x_dense, smooth_down_disp)
            self.curve_down.show()
            self.curve_up.hide()
            peak_val = max(float(np.max(smooth_down_disp)), 1.0)
        elif f_dir == "outgoing":
            self.curve_up.setData(x_dense, smooth_up_disp)
            self.curve_up.show()
            self.curve_down.hide()
            peak_val = max(float(np.max(smooth_up_disp)), 1.0)
        else:
            self.curve_down.setData(x_dense, smooth_down_disp)
            self.curve_up.setData(x_dense, smooth_up_disp)
            self.curve_down.show()
            self.curve_up.show()
            peak_val = max(float(np.max(smooth_down_disp)), float(np.max(smooth_up_disp)), 1.0)
        self._update_y_scale(peak_val)

        # 3. Cài đặt góc nhìn camera bao quát trọn vẹn 24 giờ của ngày
        self.plot_widget.setXRange(day_start, day_end, padding=0.0)

        # 4. Đồng bộ mini timeline scrubber với chuỗi 24 giờ
        self.timeline_scrubber.set_window_duration(86400.0)
        self.timeline_scrubber.set_historical_series(series)

        # 5. Cập nhật Sidebar danh sách ứng dụng theo ngày
        self._update_app_sidebar_history(
            self.history_top_procs,
            summary.get("total_rx_bytes", 0),
            summary.get("total_tx_bytes", 0)
        )

        # 6. Cập nhật khối HUD bán nguyệt và WAN/LAN bên dưới
        self._update_selected_app_hud()

        self.lbl_live_badge.setText(f"📅 24 GIỜ: {target_date}")
        self.lbl_live_badge.setStyleSheet("color: #2563EB; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #EFF6FF; border-radius: 4px; border: 1px solid #BFDBFE;")

        # 7. Hiển thị thông báo tổng quan: thời gian dùng mạng và khung giờ cao điểm
        down_str = self._format_bytes(summary.get("total_rx_bytes", 0))
        up_str = self._format_bytes(summary.get("total_tx_bytes", 0))
        peak_d_str = self._format_speed(summary.get("peak_down_kb", 0.0))
        peak_u_str = self._format_speed(summary.get("peak_up_kb", 0.0))
        active_time_str = summary.get("active_hours_str", "0 giờ")
        peak_hour_str = summary.get("peak_hour_range", "N/A")
        peak_mb_val = summary.get("peak_hour_mb", 0.0)
        peak_mb_str = self._format_bytes(peak_mb_val * 1024.0 * 1024.0)

        card_text = (
            f"<div style='font-size: 11.5px; line-height: 1.5;'>"
            f"📅 <b>BIỂU ĐỒ LỊCH SỬ 24 GIỜ:</b> <span style='color:#2563EB; font-weight:800;'>{target_date}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"🕒 <b>Thời gian dùng mạng:</b> <span style='color:#059669; font-weight:800;'>{active_time_str} / 24 giờ</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"🔥 <b>Khung giờ cao điểm nhất:</b> <span style='color:#DC2626; font-weight:800;'>{peak_hour_str} ({peak_mb_str})</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"📥 Nhận: <span style='color:#D97706; font-weight:700;'>{down_str}</span> &nbsp;|&nbsp; "
            f"📤 Gửi: <span style='color:#EA580C; font-weight:700;'>{up_str}</span> &nbsp;|&nbsp; "
            f"⚡ Đỉnh: <b>{peak_d_str} ↓ / {peak_u_str} ↑</b> lúc {summary.get('peak_down_time', '')}<br>"
            f"<span style='color:#475569;'>👉 <b>Bấm vào nút Filter</b> để xem lưu lượng riêng của từng ứng dụng &nbsp;|&nbsp; "
            f"🔍 <b>Cuộn chuột hoặc bấm +/-</b> để phóng to/thu nhỏ khung giờ &nbsp;|&nbsp; "
            f"↔️ <b>Kéo/click trên timeline bên dưới</b> để tua vào từng giờ cụ thể.</span>"
            f"</div>"
        )
        self.lbl_point_details.setText(card_text)
        if hasattr(self, 'btn_card_resume'):
            self.btn_card_resume.setVisible(False)
        if hasattr(self, 'btn_card_close'):
            self.btn_card_close.setVisible(False)
        self.point_inspector_card.setVisible(True)

    # =========================================================================
    # TƯƠNG TÁC PHÓNG TO / THU NHỎ & KÉO TIMELINE BIỂU ĐỒ LỊCH SỬ
    # =========================================================================
    def eventFilter(self, obj, event):
        if hasattr(self, 'plot_widget') and obj is self.plot_widget.viewport():
            if event.type() == QtCore.QEvent.Type.Wheel:
                if getattr(self, 'history_mode', False) or getattr(self, 'is_paused', False):
                    self._on_plot_wheel_zoom(event)
                    return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonPress:
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    self._plot_drag_active = False
                    self._plot_drag_start_pos = event.pos()
                    if hasattr(self, 'plot_widget'):
                        vr = self.plot_widget.plotItem.vb.viewRange()[0]
                        self._plot_drag_start_range = (vr[0], vr[1])
            elif event.type() == QtCore.QEvent.Type.MouseMove:
                if (event.buttons() & QtCore.Qt.MouseButton.LeftButton) and hasattr(self, '_plot_drag_start_pos'):
                    if getattr(self, 'history_mode', False) or getattr(self, 'is_paused', False):
                        dx = event.pos().x() - self._plot_drag_start_pos.x()
                        if abs(dx) > 6:
                            self._plot_drag_active = True
                            self._on_plot_drag_pan(dx)
                            return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    if getattr(self, '_plot_drag_active', False):
                        self._plot_drag_active = False
                        return True
        return super().eventFilter(obj, event)

    def _on_plot_wheel_zoom(self, event: QtGui.QWheelEvent):
        """Phóng to / thu nhỏ khung nhìn bằng con lăn chuột tại vị trí con trỏ."""
        if not getattr(self, 'history_mode', False) or not self.historical_series:
            return
        pos = event.position()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return
        mouse_t = self.plot_widget.plotItem.vb.mapSceneToView(pos).x()
        delta = event.angleDelta().y()
        if delta == 0:
            return

        vr = self.plot_widget.plotItem.vb.viewRange()[0]
        cur_start, cur_end = vr[0], vr[1]
        cur_dur = max(30.0, cur_end - cur_start)

        zoom_factor = 0.75 if delta > 0 else 1.333
        new_dur = max(30.0, min(86400.0, cur_dur * zoom_factor))

        ratio = (mouse_t - cur_start) / cur_dur if cur_dur > 0 else 0.5
        ratio = max(0.0, min(1.0, ratio))
        new_start = mouse_t - ratio * new_dur
        new_end = new_start + new_dur

        day_start = self.history_summary.get("day_start", self.historical_series[0]["timestamp"])
        day_end = self.history_summary.get("day_end", self.historical_series[-1]["timestamp"])

        if new_dur >= (day_end - day_start):
            new_start = day_start
            new_end = day_end
        else:
            if new_start < day_start:
                new_start = day_start
                new_end = min(day_end, new_start + new_dur)
            if new_end > day_end:
                new_end = day_end
                new_start = max(day_start, new_end - new_dur)

        self._render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)
        event.accept()

    def _on_plot_drag_pan(self, dx: float):
        """Kéo rê biểu đồ sang trái/phải khi đang phóng to."""
        if not getattr(self, 'history_mode', False) or not self.historical_series:
            return
        if not hasattr(self, '_plot_drag_start_range'):
            return
        pixel_w = max(100.0, float(self.plot_widget.plotItem.vb.width()))
        span = self._plot_drag_start_range[1] - self._plot_drag_start_range[0]
        time_shift = -(dx / pixel_w) * span

        new_start = self._plot_drag_start_range[0] + time_shift
        new_end = self._plot_drag_start_range[1] + time_shift

        day_start = self.history_summary.get("day_start", self.historical_series[0]["timestamp"])
        day_end = self.history_summary.get("day_end", self.historical_series[-1]["timestamp"])

        if new_start < day_start:
            new_start = day_start
            new_end = new_start + span
        if new_end > day_end:
            new_end = day_end
            new_start = max(day_start, new_end - span)

        self._render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)

    def _zoom_in_history(self):
        """Phóng to khung thời gian khi xem lịch sử."""
        if not getattr(self, 'history_mode', False) or not self.historical_series:
            return
        vr = self.plot_widget.plotItem.vb.viewRange()[0]
        cur_start, cur_end = vr[0], vr[1]
        cur_dur = max(30.0, cur_end - cur_start)
        new_dur = max(30.0, cur_dur * 0.5)

        center_t = getattr(self, 'pinned_time', None)
        if center_t is None or not (cur_start <= center_t <= cur_end):
            center_t = (cur_start + cur_end) / 2.0

        day_start = self.history_summary.get("day_start", self.historical_series[0]["timestamp"])
        day_end = self.history_summary.get("day_end", self.historical_series[-1]["timestamp"])

        new_start = max(day_start, center_t - new_dur / 2.0)
        new_end = min(day_end, new_start + new_dur)
        if new_end - new_start < new_dur:
            new_start = max(day_start, new_end - new_dur)

        self._render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)

    def _zoom_out_history(self):
        """Thu nhỏ khung thời gian khi xem lịch sử."""
        if not getattr(self, 'history_mode', False) or not self.historical_series:
            return
        vr = self.plot_widget.plotItem.vb.viewRange()[0]
        cur_start, cur_end = vr[0], vr[1]
        cur_dur = max(30.0, cur_end - cur_start)
        new_dur = min(86400.0, cur_dur * 2.0)

        center_t = (cur_start + cur_end) / 2.0
        day_start = self.history_summary.get("day_start", self.historical_series[0]["timestamp"])
        day_end = self.history_summary.get("day_end", self.historical_series[-1]["timestamp"])

        if new_dur >= (day_end - day_start):
            new_start = day_start
            new_end = day_end
        else:
            new_start = max(day_start, center_t - new_dur / 2.0)
            new_end = min(day_end, new_start + new_dur)
            if new_end - new_start < new_dur:
                new_start = max(day_start, new_end - new_dur)

        self._render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)

    def _reset_history_24h(self):
        """Đặt lại góc nhìn bao quát toàn bộ 24 giờ."""
        if not getattr(self, 'history_mode', False) or not self.historical_series:
            return
        day_start = self.history_summary.get("day_start", self.historical_series[0]["timestamp"])
        day_end = self.history_summary.get("day_end", self.historical_series[-1]["timestamp"])
        self._render_window_data(day_start, day_end)
        self.timeline_scrubber.set_slider_range(day_start, day_end)

    def _exit_history_mode(self):
        """Quay trở lại chế độ giám sát trực tiếp."""
        if hasattr(self, 'btn_card_resume'):
            self.btn_card_resume.setVisible(True)
        if hasattr(self, 'btn_card_close'):
            self.btn_card_close.setVisible(True)
        if hasattr(self, 'point_inspector_card'):
            self.point_inspector_card.setVisible(False)
        self.history_mode = False
        self.historical_series = []
        self.history_summary = {}
        self.history_top_procs = []
        self.history_controls_widget.setVisible(False)
        if hasattr(self, 'live_controls_widget'):
            self.live_controls_widget.setVisible(True)
        self.window_duration = 300.0
        self.time_axis.set_window_duration(300.0)
        self.combo_time_window.blockSignals(True)
        self.combo_time_window.setCurrentIndex(2)  # 5 Minutes
        self.combo_time_window.blockSignals(False)
        self.timeline_scrubber.reset_to_live()
        self._resume_live()

    # =========================================================================
    # CÁC PHƯƠNG THỨC XỬ LÝ TRANG USAGE HISTORY & ANALYTICS
    # =========================================================================
    def _create_kpi_card(self, title_str: str, val_str: str, sub_str: str, text_color: str, bg_color: str, border_color: str) -> Tuple[QtWidgets.QFrame, QtWidgets.QLabel, QtWidgets.QLabel]:
        card = QtWidgets.QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 8px;
                padding: 10px 14px;
            }}
        """)
        c_layout = QtWidgets.QVBoxLayout(card)
        c_layout.setContentsMargins(2, 2, 2, 2)
        c_layout.setSpacing(2)

        lbl_t = QtWidgets.QLabel(title_str)
        lbl_t.setStyleSheet(f"font-size: 10px; font-weight: 800; color: {text_color}; letter-spacing: 0.5px;")

        lbl_v = QtWidgets.QLabel(val_str)
        lbl_v.setStyleSheet("font-size: 18px; font-weight: 800; color: #0F172A;")

        lbl_s = QtWidgets.QLabel(sub_str)
        lbl_s.setStyleSheet("font-size: 11px; color: #64748B;")

        c_layout.addWidget(lbl_t)
        c_layout.addWidget(lbl_v)
        c_layout.addWidget(lbl_s)
        return card, lbl_v, lbl_s

    def _on_enter_usage_history_page(self):
        """Kích hoạt khi người dùng chuyển sang trang Usage History."""
        self._populate_usage_history_dates()
        self._verify_database()

    def _populate_usage_history_dates(self):
        """Nạp danh sách ngày có dữ liệu vào thanh chọn của trang Usage History."""
        def bg():
            try:
                dates = self.service.get_recorded_dates()
                self.async_bridge.run_on_main(self._apply_usage_history_dates, dates)
            except Exception as e:
                print(f"[UI Usage Dates Error] {e}")
        threading.Thread(target=bg, daemon=True).start()

    def _apply_usage_history_dates(self, dates: list):
        self.hist_page_combo_dates.blockSignals(True)
        self.hist_page_combo_dates.clear()
        today_str = time.strftime("%Y-%m-%d")
        if dates:
            for d in dates:
                self.hist_page_combo_dates.addItem(d)
            latest_d = dates[0]
            self.hist_page_date_picker.setDate(QtCore.QDate.fromString(latest_d, "yyyy-MM-dd"))
        else:
            self.hist_page_combo_dates.addItem(today_str)
            self.hist_page_date_picker.setDate(QtCore.QDate.currentDate())
        self.hist_page_combo_dates.blockSignals(False)
        self._load_usage_history_page()

    def _on_hist_page_combo_changed(self, index: int):
        date_str = self.hist_page_combo_dates.currentText().strip()
        if date_str:
            self.hist_page_date_picker.blockSignals(True)
            self.hist_page_date_picker.setDate(QtCore.QDate.fromString(date_str, "yyyy-MM-dd"))
            self.hist_page_date_picker.blockSignals(False)
            self._load_usage_history_page()

    def _load_usage_history_page(self):
        """Tải dữ liệu của ngày đã chọn và nạp vào toàn bộ trang Usage History."""
        date_str = self.hist_page_date_picker.date().toString("yyyy-MM-dd")
        if not date_str:
            return

        self.btn_load_usage_history.setEnabled(False)
        self.btn_load_usage_history.setText("Đang tải...")

        def bg():
            try:
                series = self.service.get_daily_traffic_series(date_str)
                summary = self.service.get_daily_summary(date_str, series=series)
                top_procs = self.service.get_top_processes_for_date(date_str, limit=20)
                logs = self.service.get_detailed_traffic_logs(date_str, limit=100)
                self.async_bridge.run_on_main(self._apply_usage_history_page, summary, series, top_procs, logs, date_str)
            except Exception as e:
                print(f"[UI Usage Page Error] {e}")
                self.async_bridge.run_on_main(self._on_usage_history_page_failed, str(e))

        threading.Thread(target=bg, daemon=True).start()

    def _on_usage_history_page_failed(self, err_msg: str):
        self.btn_load_usage_history.setEnabled(True)
        self.btn_load_usage_history.setText("🔍 Tải Lịch Sử")
        QtWidgets.QMessageBox.warning(self, "Lỗi tải lịch sử", f"Không thể tải dữ liệu trang lịch sử: {err_msg}")

    def _apply_usage_history_page(self, summary: dict, series: list, top_procs: list, logs: list, date_str: str):
        self.btn_load_usage_history.setEnabled(True)
        self.btn_load_usage_history.setText("🔍 Tải Lịch Sử")
        self.current_hist_page_series = series
        self.raw_hist_logs = logs

        # 1. Cập nhật các thẻ KPI
        rx_bytes = summary.get("total_rx_bytes", 0)
        tx_bytes = summary.get("total_tx_bytes", 0)
        peak_d = summary.get("peak_down_kb", 0.0)
        peak_u = summary.get("peak_up_kb", 0.0)
        peak_speed = max(peak_d, peak_u)
        peak_time = summary.get("peak_down_time", "-") if peak_d >= peak_u else summary.get("peak_up_time", "-")
        count = summary.get("sample_count", len(series))
        first_t = summary.get("first_time", "-")
        last_t = summary.get("last_time", "-")

        self.lbl_hist_down_val.setText(self._format_bytes(rx_bytes))
        self.lbl_hist_up_val.setText(self._format_bytes(tx_bytes))
        self.lbl_hist_peak_val.setText(self._format_speed(peak_speed))
        self.lbl_hist_peak_sub.setText(f"Đạt đỉnh lúc: {peak_time}")
        self.lbl_hist_samples_val.setText(f"{count:,} mẫu đo")
        if first_t != "-" and last_t != "-":
            f_short = first_t.split(" ")[-1] if " " in first_t else first_t
            l_short = last_t.split(" ")[-1] if " " in last_t else last_t
            self.lbl_hist_samples_sub.setText(f"{f_short} ➔ {l_short}")
        else:
            self.lbl_hist_samples_sub.setText("Chưa có mốc thời gian")

        # 2. Cập nhật đồ thị lịch sử
        if series and len(series) >= 2:
            hx = np.array([p["timestamp"] for p in series], dtype=np.float64)
            hy_down = np.array([p["down_kb"] for p in series], dtype=np.float64)
            hy_up = np.array([p["up_kb"] for p in series], dtype=np.float64)

            self.hist_curve_down.setData(hx, hy_down)
            self.hist_curve_up.setData(hx, hy_up)

            y_max = max(float(np.max(hy_down)), float(np.max(hy_up)), 10.0) * 1.15
            self.hist_plot_widget.setYRange(0, y_max, padding=0.0)
            self.hist_plot_widget.setXRange(hx[0], hx[-1], padding=0.02)
        else:
            self.hist_curve_down.setData([], [])
            self.hist_curve_up.setData([], [])

        # 3. Cập nhật bảng Top Ứng Dụng
        self._populate_top_apps_table(top_procs)

        # 4. Cập nhật bảng Nhật Ký Chi Tiết
        self._populate_hist_logs_table(logs)

    def _populate_top_apps_table(self, top_procs: list):
        self.top_apps_table.setRowCount(len(top_procs))
        for row_idx, item in enumerate(top_procs):
            name = item["name"]
            pid = item["pid"]
            rx = item["rx_bytes"]
            tx = item["tx_bytes"]
            tot = item["total_bytes"]
            pct = item.get("percent", 0.0)

            icon = self.icon_manager.get_icon("", name)
            name_item = QtWidgets.QTableWidgetItem(icon, f"  {name}")
            name_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))

            pid_item = QtWidgets.QTableWidgetItem(str(pid) if pid > 0 else "-")
            pid_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            rx_item = QtWidgets.QTableWidgetItem(self._format_bytes(rx))
            rx_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            rx_item.setForeground(QtGui.QBrush(QtGui.QColor("#D97706")))

            tx_item = QtWidgets.QTableWidgetItem(self._format_bytes(tx))
            tx_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            tx_item.setForeground(QtGui.QBrush(QtGui.QColor("#EA580C")))

            tot_item = QtWidgets.QTableWidgetItem(self._format_bytes(tot))
            tot_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            tot_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))

            # Progress bar tỷ lệ % tiêu thụ
            pbar = QtWidgets.QProgressBar()
            pbar.setFixedHeight(14)
            pbar.setTextVisible(True)
            pbar.setRange(0, 100)
            pbar.setValue(int(min(100, max(0, pct))))
            pbar.setFormat(f"{pct:.1f}%")
            pbar.setStyleSheet("""
                QProgressBar {
                    background-color: #F1F5F9;
                    border-radius: 4px;
                    text-align: center;
                    font-size: 10px;
                    font-weight: bold;
                    color: #0F172A;
                }
                QProgressBar::chunk {
                    background-color: #2563EB;
                    border-radius: 4px;
                }
            """)

            self.top_apps_table.setItem(row_idx, 0, name_item)
            self.top_apps_table.setItem(row_idx, 1, pid_item)
            self.top_apps_table.setItem(row_idx, 2, rx_item)
            self.top_apps_table.setItem(row_idx, 3, tx_item)
            self.top_apps_table.setItem(row_idx, 4, tot_item)
            self.top_apps_table.setCellWidget(row_idx, 5, pbar)

    def _populate_hist_logs_table(self, logs: list):
        self.history_table.setRowCount(len(logs))
        for row_idx, item in enumerate(logs):
            t_item = QtWidgets.QTableWidgetItem(str(item["timestamp"]))
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

    def _filter_hist_logs(self, text: str):
        query = text.strip().lower()
        if not hasattr(self, "raw_hist_logs"):
            return
        if not query:
            self._populate_hist_logs_table(self.raw_hist_logs)
            return
        filtered = [item for item in self.raw_hist_logs if query in item["name"].lower()]
        self._populate_hist_logs_table(filtered)

    def _on_hist_plot_mouse_moved(self, pos):
        if not hasattr(self, "current_hist_page_series") or not self.current_hist_page_series:
            self.hist_v_line.hide()
            self.hist_h_line.hide()
            self.hist_hover_marker.hide()
            self.hist_hover_badge.hide()
            return

        if not self.hist_plot_widget.sceneBoundingRect().contains(pos):
            self.hist_v_line.hide()
            self.hist_h_line.hide()
            self.hist_hover_marker.hide()
            self.hist_hover_badge.hide()
            return

        mouse_pt = self.hist_plot_widget.plotItem.vb.mapSceneToView(pos)
        target_t = mouse_pt.x()

        # Tìm mẫu gần nhất
        best_pt = min(self.current_hist_page_series, key=lambda p: abs(p["timestamp"] - target_t))
        nt = best_pt["timestamp"]
        nd = best_pt.get("down_kb", 0.0)
        nu = best_pt.get("up_kb", 0.0)

        self.hist_v_line.setPos(nt)
        self.hist_h_line.setPos(nd)
        self.hist_v_line.show()
        self.hist_h_line.show()

        self.hist_hover_marker.setData([nt], [nd])
        self.hist_hover_marker.show()

        lt = time.localtime(nt)
        t_str = time.strftime("%H:%M:%S", lt)
        d_str = self._format_speed(nd)
        u_str = self._format_speed(nu)

        self.lbl_hist_hover_text.setText(
            f"🕒 <b>{t_str}</b><br>"
            f"<span style='color:#F59E0B;'>↓ {d_str}</span>&nbsp;&nbsp;"
            f"<span style='color:#F43F5E;'>↑ {u_str}</span>"
        )
        self.hist_hover_badge.adjustSize()
        local_pos = self.hist_plot_widget.mapFromScene(pos)
        bx = max(10, min(self.hist_plot_widget.width() - self.hist_hover_badge.width() - 10, local_pos.x() + 14))
        by = max(10, min(self.hist_plot_widget.height() - self.hist_hover_badge.height() - 10, local_pos.y() - 44))
        self.hist_hover_badge.move(int(bx), int(by))
        self.hist_hover_badge.show()

    def _on_hist_plot_mouse_clicked(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        if not self.hist_plot_widget.sceneBoundingRect().contains(pos):
            return
        if not hasattr(self, "current_hist_page_series") or not self.current_hist_page_series:
            return

        mouse_pt = self.hist_plot_widget.plotItem.vb.mapSceneToView(pos)
        target_t = mouse_pt.x()
        best_pt = min(self.current_hist_page_series, key=lambda p: abs(p["timestamp"] - target_t))
        t_val = best_pt["timestamp"]
        d_val = best_pt.get("down_kb", 0.0)
        u_val = best_pt.get("up_kb", 0.0)

        self.hist_pinned_marker.setData([t_val], [d_val])
        self.hist_pinned_marker.show()

        lt = time.localtime(t_val)
        time_full_str = time.strftime("%H:%M:%S - %d/%m/%Y", lt)
        d_str = self._format_speed(d_val)
        u_str = self._format_speed(u_val)
        tot_str = self._format_speed(d_val + u_val)

        # Truy vấn ứng dụng lúc đó từ DB
        def bg():
            try:
                from datetime import datetime as _dt
                dt_obj = _dt.fromtimestamp(t_val)
                db_procs = self.service.get_processes_around_time(dt_obj, tolerance_seconds=5.0)
                self.async_bridge.run_on_main(self._apply_hist_point_procs, time_full_str, d_str, u_str, tot_str, db_procs)
            except Exception as e:
                print(f"[UI Hist Point Procs Error] {e}")
        threading.Thread(target=bg, daemon=True).start()

    def _apply_hist_point_procs(self, time_str: str, down_str: str, up_str: str, tot_str: str, procs: list):
        if procs:
            proc_items_text = " &nbsp;|&nbsp; ".join([
                f"<b>{p['name']}</b>: <span style='color:#D97706;'>{self._format_bytes(p.get('rx_bytes', 0))} ↓</span>, <span style='color:#EA580C;'>{self._format_bytes(p.get('tx_bytes', 0))} ↑</span>"
                for p in procs[:4]
            ])
            proc_html = f"<div style='margin-top: 3px; font-size: 11px; color: #334155;'>📱 <b>Ứng dụng hoạt động lúc này:</b> {proc_items_text}</div>"
        else:
            proc_html = "<div style='margin-top: 3px; font-size: 11px; color: #64748B;'>📱 Không có ứng dụng riêng lẻ nào ghi nhận lưu lượng tại giây này.</div>"

        card_text = (
            f"<div style='font-size: 12px; line-height: 1.5;'>"
            f"📍 <b>MỐC ĐƯỢC CHỌN:</b> <span style='color:#0F172A; font-weight:800;'>{time_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"📥 Tải xuống: <span style='color:#D97706; font-weight:800;'>{down_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"📤 Tải lên: <span style='color:#EA580C; font-weight:800;'>{up_str}</span> &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"⚡ Tổng: <span style='color:#2563EB; font-weight:800;'>{tot_str}</span>"
            f"{proc_html}"
            f"</div>"
        )
        self.lbl_hist_point_details.setText(card_text)
        self.hist_point_card.setVisible(True)

    def _apply_y_ceiling(self, target_tier: float):
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

    def _update_y_scale(self, peak_val: float):
        """Đồng bộ thang đo trục Y với nhãn hiển thị trần có cơ chế chống rung nhảy (hysteresis / hold-down timer)."""
        try:
            target_tier = self.CEILING_TIERS_KB[-1]
            for tier in self.CEILING_TIERS_KB:
                if tier >= peak_val * 1.15:
                    target_tier = tier
                    break

            now = time.time()
            if target_tier > self.current_y_max:
                # Đỉnh lưu lượng tăng: mở rộng thang đo ngay lập tức để không bị che khuất đỉnh
                self.current_y_max = target_tier
                self._scale_down_hold_time = now + 8.0  # Giữ trần này ít nhất 8 giây chống co giật liên tục
                self._apply_y_ceiling(target_tier)
            elif target_tier < self.current_y_max:
                # Đỉnh lưu lượng giảm: chỉ hạ thang đo sau khi lưu lượng thấp duy trì ổn định ít nhất 8 giây
                if getattr(self, "history_mode", False) or now >= getattr(self, "_scale_down_hold_time", 0.0):
                    self.current_y_max = target_tier
                    self._apply_y_ceiling(target_tier)
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
            sigma = float(cfg["sigma"])
            radius = int(cfg["radius_seconds"])

            # Lấy các mẫu trong vùng hiển thị x_dense kèm biên an toàn
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

            # Bán kính lọc phù hợp với cấu trúc điểm mẫu
            eff_radius = min(radius, max(1, (n_pts - 1) // 2))

            # 1. Lọc Gauss với pad mượt mà chống kéo vọt biên:
            # - Cạnh trái (quá khứ đã đo): đệm mép
            # - Cạnh phải (tương lai): suy giảm tự nhiên về 0 để tránh biến xung đơn lẻ 1 giây thành bức tường 30 giây kéo dựng đứng sóng ở mép phải
            if eff_radius >= 1 and n_pts > 3:
                kx = np.arange(-eff_radius, eff_radius + 1)
                kernel = np.exp(-0.5 * (kx / sigma) ** 2)
                kernel /= np.sum(kernel)
                decay = uy[-1] * np.exp(-np.arange(1, eff_radius + 1) / 2.0)
                left_pad = np.full(eff_radius, uy[0])
                uy_padded = np.concatenate([left_pad, uy, decay])
                uy_smooth = np.convolve(uy_padded, kernel, mode='same')[eff_radius:-eff_radius]
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
                self.async_bridge.run_on_main(self._apply_database_verification, stats, history)
            except Exception as e:
                self.async_bridge.run_on_main(self.db_stats_card.setText, f"⚠️ Lỗi khi truy vấn CSDL: {e}")

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
    # CÁC THAO TÁC BÓP BĂNG THÔNG (WINDIVERT PACKET SHAPER)
    # =========================================================================
    def _on_apply_qos_clicked(self):
        app_name = self.combo_qos_app.currentText().strip()
        if not app_name:
            QtWidgets.QMessageBox.warning(self, "Thiếu thông tin", "Vui lòng chọn hoặc nhập tên ứng dụng cần giới hạn (VD: brave.exe)!")
            return

        try:
            limit_val = float(self.input_qos_limit.text().strip())
            if limit_val <= 0:
                raise ValueError()
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Sai định dạng", "Mức giới hạn phải là một số dương (KB/s)!")
            return

        dir_idx = self.combo_shaper_dir.currentIndex()
        direction_map = {0: "both", 1: "upload", 2: "download"}
        dir_code = direction_map.get(dir_idx, "both")
        dir_text = self.combo_shaper_dir.currentText()

        self.btn_apply_qos.setEnabled(False)
        self.btn_apply_qos.setText("Đang áp dụng...")

        def bg():
            try:
                success, msg = self.service.apply_bandwidth_limit(app_name, limit_val, dir_code)
                self.async_bridge.run_on_main(self._on_qos_applied_result, success, msg, app_name, limit_val, dir_text)
            except Exception as e:
                self.async_bridge.run_on_main(self._on_qos_applied_result, False, str(e), app_name, limit_val, dir_text)

        threading.Thread(target=bg, daemon=True).start()

    def _on_qos_applied_result(self, success: bool, msg: str, app_name: str, limit_val: float, dir_text: str):
        self.btn_apply_qos.setEnabled(True)
        self.btn_apply_qos.setText("🚀 Áp Dụng Giới Hạn")

        if success:
            self.monitored_qos_app = app_name
            self.monitored_qos_limit = limit_val
            self.qos_status_card.setText(f"✅ Thành công: {msg}")
            self._refresh_policy_table()
            QtWidgets.QMessageBox.information(
                self,
                "Bóp Băng Thông Thành Công",
                f"Đã kích hoạt bóp băng thông WinDivert cho '{app_name}'!\n"
                f"• Chiều áp dụng: {dir_text}\n"
                f"• Mức trần: {limit_val:,.1f} KB/s\n\n"
                f"Bạn có thể mở ứng dụng (hoặc tab mới) để kiểm chứng ngay lập tức."
            )
        else:
            self.qos_status_card.setText(f"❌ Thất bại: {msg}")
            self._refresh_policy_table()
            QtWidgets.QMessageBox.critical(self, "Lỗi bóp băng thông", f"{msg}\n\nLưu ý: Bạn cần chạy ứng dụng với quyền 'Run as administrator' để WinDivert có quyền can thiệp gói tin.")

    def _on_remove_qos_clicked(self):
        self.btn_remove_qos.setEnabled(False)
        self.btn_remove_qos.setText("Đang gỡ bỏ...")

        def bg():
            try:
                success, msg = self.service.remove_bandwidth_limit()
                self.async_bridge.run_on_main(self._on_qos_removed_result, success, msg)
            except Exception as e:
                self.async_bridge.run_on_main(self._on_qos_removed_result, False, str(e))

        threading.Thread(target=bg, daemon=True).start()

    def _on_qos_removed_result(self, success: bool, msg: str):
        self.btn_remove_qos.setEnabled(True)
        self.btn_remove_qos.setText("❌ Gỡ Bỏ Giới Hạn")

        self.monitored_qos_app = ""
        self.monitored_qos_limit = 0.0
        self.qos_progress.setValue(0)
        self.qos_meter_label.setText("🎯 Đã gỡ bỏ giới hạn băng thông. Toàn bộ lưu lượng trở lại bình thường.")
        self.qos_status_card.setText(f"✅ {msg}")
        self._refresh_policy_table()
        QtWidgets.QMessageBox.information(self, "Gỡ bỏ thành công", "Đã gỡ bỏ giới hạn băng thông. Lưu lượng mạng của ứng dụng đã được phục hồi tối đa.")

    def _update_qos_realtime_verification(self, proc_speeds: list):
        if not self.monitored_qos_app or self.monitored_qos_limit <= 0:
            return

        target = self.monitored_qos_app.lower()
        target_no_ext = target.replace(".exe", "").strip()
        measured_up_kb = 0.0
        measured_down_kb = 0.0

        for item in proc_speeds:
            p_name = item.get("name", "").lower()
            if p_name == target or p_name.replace(".exe", "") == target_no_ext or (target_no_ext and target_no_ext in p_name):
                measured_up_kb += item.get("up_kb", 0.0)
                measured_down_kb += item.get("down_kb", 0.0)

        # Lấy thống kê chi tiết từ WinDivertPacketShaper
        shaper_status = self.service.get_bandwidth_limit_status()
        shaper_active = shaper_status.get("is_active", False)
        direction = shaper_status.get("direction", "both")
        throttled_pkts = shaper_status.get("throttled_packets", 0)
        throttled_bytes = shaper_status.get("throttled_bytes", 0)
        shaper_spd = shaper_status.get("current_speed_kbps", 0.0)
        shaper_down = shaper_status.get("current_down_kbps", 0.0)
        shaper_up = shaper_status.get("current_up_kbps", 0.0)

        # Lấy tốc độ phản ánh trung thực lưu lượng thực tế qua kernel shaper
        if shaper_active and (throttled_pkts > 0 or shaper_spd > 0):
            disp_up = shaper_up if shaper_up > 0 else measured_up_kb
            disp_down = shaper_down if shaper_down > 0 else measured_down_kb
            if direction == "upload":
                measured_speed = disp_up
                dir_str = f"Upload: {disp_up:,.1f} KB/s"
            elif direction == "download":
                measured_speed = disp_down
                dir_str = f"Download: {disp_down:,.1f} KB/s"
            else:
                measured_speed = shaper_spd if shaper_spd > 0 else max(disp_up, disp_down)
                dir_str = f"Upload: {disp_up:,.1f} KB/s | Download: {disp_down:,.1f} KB/s"
        else:
            if direction == "upload":
                measured_speed = measured_up_kb
                dir_str = f"Upload: {measured_up_kb:,.1f} KB/s"
            elif direction == "download":
                measured_speed = measured_down_kb
                dir_str = f"Download: {measured_down_kb:,.1f} KB/s"
            else:
                measured_speed = max(measured_up_kb, measured_down_kb)
                dir_str = f"Upload: {measured_up_kb:,.1f} KB/s | Download: {measured_down_kb:,.1f} KB/s"

        pct = (measured_speed / self.monitored_qos_limit * 100.0) if self.monitored_qos_limit > 0 else 0.0
        self.qos_progress.setValue(min(100, int(pct)))

        throttled_kb = throttled_bytes / 1024.0
        throttled_str = f"{throttled_kb:,.1f} KB" if throttled_kb < 1024 else f"{(throttled_kb / 1024.0):,.2f} MB"

        if measured_speed >= (self.monitored_qos_limit * 0.90):
            status_text = "🔒 ĐANG GHÌM TỐC ĐỘ (Token Bucket kìm lưu lượng tại mức trần)"
            style_color = "#EA580C"
        else:
            status_text = "🟢 ĐANG GIỚI HẠN (Lưu lượng tuân thủ mức trần cho phép)"
            style_color = "#059669"

        self.qos_meter_label.setText(
            f"🎯 <b>Tiến trình đang bóp:</b> <span style='color:#2563EB'>{self.monitored_qos_app}</span> | "
            f"<b>Mức trần:</b> <span style='color:#D97706'>{self.monitored_qos_limit:,.1f} KB/s</span> | "
            f"<b>Tốc độ tức thời:</b> <span style='color:#0F172A'>{dir_str}</span><br>"
            f"📦 <b>Số gói tin đã điều phối:</b> {throttled_pkts:,} gói ({throttled_str}) | "
            f"Trạng thái: <span style='color:{style_color}; font-weight:bold;'>{status_text}</span>"
        )

    def _refresh_policy_table(self):
        """Tải danh sách lịch sử áp dụng chính sách từ MySQL / File lên bảng UI."""
        if not hasattr(self, 'policy_table'):
            return

        def bg():
            try:
                logs = self.service.get_policy_history(limit=50)
                self.async_bridge.run_on_main(self._update_policy_table_ui, logs)
            except Exception:
                pass

        threading.Thread(target=bg, daemon=True).start()

    def _update_policy_table_ui(self, logs: list):
        if not hasattr(self, 'policy_table'):
            return

        self.policy_table.setRowCount(0)
        for row_idx, r in enumerate(logs):
            self.policy_table.insertRow(row_idx)

            time_str = r.get("applied_at_str", "")
            action = r.get("action", "")
            target_app = r.get("target_app", "")
            direction = r.get("direction", "both")
            limit_val = r.get("limit_kbps", 0.0)
            pkts = r.get("throttled_packets", 0)
            bytes_val = r.get("throttled_bytes", 0)
            status = r.get("status", "SUCCESS")

            dir_map = {"both": "Cả 2 chiều", "upload": "Tải lên", "download": "Tải về"}
            dir_str = dir_map.get(str(direction).lower(), str(direction))

            is_apply = (action == "APPLY")
            action_item = QtWidgets.QTableWidgetItem("🚀 KÍCH HOẠT" if is_apply else "❌ GỠ BỎ")
            action_item.setForeground(QtGui.QColor("#059669" if is_apply else "#DC2626"))
            font_act = QtGui.QFont()
            font_act.setBold(True)
            action_item.setFont(font_act)

            th_kb = bytes_val / 1024.0
            pkt_str = f"{pkts:,} gói ({th_kb:,.1f} KB)" if pkts > 0 else "-"

            self.policy_table.setItem(row_idx, 0, QtWidgets.QTableWidgetItem(time_str))
            self.policy_table.setItem(row_idx, 1, action_item)
            self.policy_table.setItem(row_idx, 2, QtWidgets.QTableWidgetItem(target_app))
            self.policy_table.setItem(row_idx, 3, QtWidgets.QTableWidgetItem(dir_str))
            self.policy_table.setItem(row_idx, 4, QtWidgets.QTableWidgetItem(f"{limit_val:,.1f} KB/s" if limit_val > 0 else "-"))
            self.policy_table.setItem(row_idx, 5, QtWidgets.QTableWidgetItem(pkt_str))

            st_item = QtWidgets.QTableWidgetItem("Thành công" if status == "SUCCESS" else status)
            st_item.setForeground(QtGui.QColor("#059669" if status == "SUCCESS" else "#DC2626"))
            self.policy_table.setItem(row_idx, 6, st_item)

    def _show_about_dialog(self):
        QtWidgets.QMessageBox.information(
            self,
            "GlassWire PBL4 Edition",
            "🛡️ <b>GlassWire PBL4 Network Flow & QoS Monitor</b><br><br>"
            "Phiên bản tái hiện giao diện chuẩn GlassWire Free với đầy đủ tính năng PBL4:<br>"
            "• Đọc lưu lượng gói tin ETW / Socket tiến trình kernel thời gian thực<br>"
            "• Bóp băng thông mạng thời gian thực WinDivert Kernel Packet Shaper (Token Bucket)<br>"
            "• Lưu trữ toàn vẹn vào cơ sở dữ liệu MySQL 6 bảng liên kết<br>"
            "• Phân tích socket TCP và trạng thái kết nối mạng IPv4/IPv6"
        )

    def closeEvent(self, event):
        """Dừng ứng dụng: Tự động dừng WinDivert Packet Shaper, dừng worker và luồng DB."""
        self.render_timer.stop()
        self.conn_timer.stop()
        self.worker.stop()
        self.service.stop()
        event.accept()