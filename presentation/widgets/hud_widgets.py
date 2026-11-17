"""
Các Widget HUD & Thanh Kiểm tra Điểm Lưu lượng (GlassWire HUD & Point Inspection Widgets):
- GlassWireArcGauge: Đồng hồ bán nguyệt hiển thị dung lượng tổng.
- GlassWireWanLanWidget: Thanh phân bổ lưu lượng WAN / LAN.
- GlassWireAppHoverPopup: Bảng popup danh sách ứng dụng khi rê chuột.
- GlassWireAppTriggerPill: Pill kích hoạt popup ứng dụng.
- GlassWirePointInspectionBar: Thanh ghim điểm thời gian trên biểu đồ.
"""

import time
from PyQt6 import QtWidgets, QtCore, QtGui
from application.traffic_analytics import format_speed


class GlassWireArcGauge(QtWidgets.QWidget):
    """Đồng hồ bán nguyệt phong cách GlassWire hiển thị dung lượng tại tâm và vòng cung vàng."""

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

        pen_bg = QtGui.QPen(
            QtGui.QColor("#E2E8F0"), 5.5, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap
        )
        painter.setPen(pen_bg)
        painter.drawArc(rect, 0 * 16, 180 * 16)

        pen_fg = QtGui.QPen(
            QtGui.QColor("#F59E0B"), 6.0, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap
        )
        painter.setPen(pen_fg)
        span = int((self.percent / 100.0) * 180 * 16)
        painter.drawArc(rect, 180 * 16, -span)

        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#F59E0B"))
        painter.drawEllipse(QtCore.QPointF(70, 8), 3.5, 3.5)

        painter.setPen(QtGui.QColor("#0F172A"))
        font_total = QtGui.QFont("Segoe UI", 11, QtGui.QFont.Weight.Bold)
        painter.setFont(font_total)
        painter.drawText(QtCore.QRectF(0, 36, 140, 24), QtCore.Qt.AlignmentFlag.AlignCenter, self.total_text)


class GlassWireWanLanWidget(QtWidgets.QWidget):
    """Thanh phân tách WAN & LAN phong cách GlassWire với dải màu vàng và đầu mút hồng."""

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

        painter.setPen(QtGui.QColor("#334155"))
        painter.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        painter.drawText(
            QtCore.QRectF(0, 4, 115, 18),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            self.wan_str,
        )

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

        painter.setPen(QtGui.QColor("#64748B"))
        painter.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        painter.drawText(
            QtCore.QRectF(0, 28, 115, 18),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            self.lan_str,
        )

        if bar_w > 20:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#F59E0B"))
            painter.drawRoundedRect(QtCore.QRectF(bar_x, 34, 12, 6), 3, 3)


class GlassWireAppHoverPopup(QtWidgets.QFrame):
    """Bảng popup xổ xuống khi rê chuột vào pill 'App + X more'."""

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

        div = QtWidgets.QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background-color: #E2E8F0; border: none;")
        c_layout.addWidget(div)

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
        return format_speed(speed_kb) if speed_kb > 0 else "0 B/s"


class GlassWireAppTriggerPill(QtWidgets.QWidget):
    """Pill hiển thị '📁 App + X more' trên thanh inspection bar."""

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
        self.lbl_text.setStyleSheet("font-size: 11px; font-weight: bold; color: #2563EB;")
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
    """Thanh hiển thị thông số chi tiết khi dừng/ghim tại một điểm trên biểu đồ."""

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

        self.lbl_down = QtWidgets.QLabel("↓ 0.0 B/s")
        self.lbl_down.setStyleSheet("font-size: 12px; font-weight: 800; color: #D97706;")
        layout.addWidget(self.lbl_down)

        self.lbl_up = QtWidgets.QLabel("↑ 0.0 B/s")
        self.lbl_up.setStyleSheet("font-size: 12px; font-weight: 800; color: #DC2626;")
        layout.addWidget(self.lbl_up)

        sep1 = QtWidgets.QFrame()
        sep1.setFixedSize(1, 16)
        sep1.setStyleSheet("background-color: #CBD5E1;")
        layout.addWidget(sep1)

        self.popup = GlassWireAppHoverPopup(icon_manager)
        self.app_pill = GlassWireAppTriggerPill(self.popup, self)
        layout.addWidget(self.app_pill)

        layout.addStretch()

        self.lbl_time = QtWidgets.QLabel("")
        self.lbl_time.setStyleSheet("font-size: 11px; font-weight: 600; color: #64748B;")
        layout.addWidget(self.lbl_time)

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
        if hasattr(self, "popup"):
            self.popup.hide()
        super().hide()
