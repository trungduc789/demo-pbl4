"""
Cửa sổ Quản lý & Trạng thái Máy Con (Client Status & Configuration Window - Presentation Layer):
- Tab 1: Giám sát lưu lượng trực tiếp (Đồ thị sóng Bezier 60s, Card tốc độ Rx/Tx, CPU/RAM, Top tiến trình).
- Tab 2: Chính sách an ninh hiện hành & Nhật ký kiểm toán thao tác từ xa (Audit Log).
- Tab 3: Chẩn đoán kết nối mạng & đo độ trễ tới Admin Server và Internet Gateway.
- Tab 4: Cấu hình kết nối Server & Thông tin định danh phần cứng máy con.
"""

import os
import sys
import json
import time
from typing import Optional, List, Dict, Any
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QHeaderView, QFrame, QMessageBox, QApplication, QTextEdit,
    QProgressBar
)


def get_client_config_path() -> str:
    """Xác định đường dẫn tệp client_config.json linh hoạt theo môi trường thực thi."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "client_config.json")


CONFIG_PATH = get_client_config_path()


def format_bytes(num_bytes: float) -> str:
    """Định dạng số byte thành chuỗi KB, MB, GB dễ đọc."""
    if num_bytes < 1024:
        return f"{num_bytes:.0f} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_speed(kbps: float) -> str:
    """Định dạng tốc độ truyền nhận mạng (KB/s, MB/s)."""
    if kbps < 1.0:
        return f"{kbps * 1024:.0f} B/s"
    elif kbps < 1024:
        return f"{kbps:.1f} KB/s"
    else:
        return f"{kbps / 1024:.2f} MB/s"


# =============================================================================
# WIDGET ĐỒ THỊ SÓNG LƯU LƯỢNG CAO CẤP (SMOOTH BEZIER TRAFFIC SPARKLINE)
# =============================================================================
class TrafficSparklineWidget(QWidget):
    """
    Widget đồ thị sóng thời gian thực 60 giây cho Tải xuống & Tải lên:
    - Làm mịn bằng đường cong Bezier (Cubic Spline) giúp sóng uốn lượn tự nhiên,
      loại bỏ hoàn toàn gai nhọn gãy khúc.
    - Tô chuyển sắc Gradient mờ dần từ đỉnh sóng xuống đường cơ sở.
    - Thang đo trục Y tự động co dãn êm dịu (Damped Dynamic Scale).
    - Hiển thị đầy đủ mốc thời gian (60s, 30s, Live) và tốc độ đỉnh.
    """

    def __init__(self, max_points: int = 60, parent=None):
        super().__init__(parent)
        self.max_points = max_points
        self.down_points: List[float] = [0.0] * max_points
        self.up_points: List[float] = [0.0] * max_points
        self.smooth_max: float = 20.0
        self.setFixedHeight(135)
        self.setStyleSheet("""
            QWidget {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
        """)

    def add_sample(self, down_kb: float, up_kb: float):
        """Thêm một điểm dữ liệu mới vào chuỗi thời gian."""
        self.down_points.pop(0)
        self.down_points.append(max(0.0, float(down_kb)))
        self.up_points.pop(0)
        self.up_points.append(max(0.0, float(up_kb)))

        # Thang đo tối đa làm mịn để đồ thị không bị giật
        curr_max = max(max(self.down_points), max(self.up_points), 10.0)
        self.smooth_max = self.smooth_max * 0.8 + curr_max * 0.2
        self.update()

    def _build_smooth_path(self, points: List[float], max_val: float, w: float, bottom_y: float) -> QtGui.QPainterPath:
        """Tạo đường dẫn Bezier mượt mà đi qua các điểm dữ liệu."""
        path = QtGui.QPainterPath()
        n = len(points)
        if n < 2:
            return path

        step = w / (n - 1)
        avail_h = bottom_y - 20

        # Điểm đầu tiên
        first_y = bottom_y - (points[0] / max_val) * avail_h
        path.moveTo(0, first_y)

        # Nối các điểm bằng cubicTo
        for i in range(1, n):
            x_prev = (i - 1) * step
            y_prev = bottom_y - (points[i - 1] / max_val) * avail_h
            x_curr = i * step
            y_curr = bottom_y - (points[i] / max_val) * avail_h

            cx = (x_prev + x_curr) / 2.0
            path.cubicTo(cx, y_prev, cx, y_curr, x_curr, y_curr)

        return path

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        bottom_y = h - 22  # Dành 22px dưới cùng cho trục thời gian

        # 1. Nền trắng bo góc
        bg_path = QtGui.QPainterPath()
        bg_path.addRoundedRect(QtCore.QRectF(0, 0, w, h), 8, 8)
        painter.fillPath(bg_path, QtGui.QColor("#FFFFFF"))

        # 2. Đường lưới ngang mờ
        painter.setPen(QtGui.QPen(QtGui.QColor("#F8FAFC"), 1, QtCore.Qt.PenStyle.DashLine))
        grid_steps = 3
        for g in range(1, grid_steps + 1):
            yg = int(bottom_y * g / (grid_steps + 1))
            painter.drawLine(0, yg, w, yg)

        max_v = max(self.smooth_max, 10.0)

        # 3. Vẽ vùng sóng Tải xuống (Download - Amber #F59E0B)
        path_down = self._build_smooth_path(self.down_points, max_v, w, bottom_y)
        if not path_down.isEmpty():
            path_down_fill = QtGui.QPainterPath(path_down)
            path_down_fill.lineTo(w, bottom_y)
            path_down_fill.lineTo(0, bottom_y)
            path_down_fill.closeSubpath()

            grad_down = QtGui.QLinearGradient(0, 10, 0, bottom_y)
            grad_down.setColorAt(0.0, QtGui.QColor(245, 158, 11, 95))
            grad_down.setColorAt(0.5, QtGui.QColor(245, 158, 11, 35))
            grad_down.setColorAt(1.0, QtGui.QColor(245, 158, 11, 5))
            painter.fillPath(path_down_fill, grad_down)

            painter.setPen(QtGui.QPen(QtGui.QColor("#F59E0B"), 2.0))
            painter.drawPath(path_down)

        # 4. Vẽ đường sóng Tải lên (Upload - Rose #E11D48)
        path_up = self._build_smooth_path(self.up_points, max_v, w, bottom_y)
        if not path_up.isEmpty():
            path_up_fill = QtGui.QPainterPath(path_up)
            path_up_fill.lineTo(w, bottom_y)
            path_up_fill.lineTo(0, bottom_y)
            path_up_fill.closeSubpath()

            grad_up = QtGui.QLinearGradient(0, 10, 0, bottom_y)
            grad_up.setColorAt(0.0, QtGui.QColor(225, 29, 72, 70))
            grad_up.setColorAt(1.0, QtGui.QColor(225, 29, 72, 5))
            painter.fillPath(path_up_fill, grad_up)

            painter.setPen(QtGui.QPen(QtGui.QColor("#E11D48"), 1.8))
            painter.drawPath(path_up)

        # 5. Đường cơ sở (Baseline)
        painter.setPen(QtGui.QPen(QtGui.QColor("#E2E8F0"), 1))
        painter.drawLine(0, int(bottom_y), w, int(bottom_y))

        # 6. Trục thời gian và nhãn đỉnh tốc độ
        painter.setFont(QtGui.QFont("Segoe UI", 8))
        painter.setPen(QtGui.QColor("#94A3B8"))

        painter.drawText(8, h - 6, "60s trước")
        painter.drawText(w // 2 - 12, h - 6, "30s")
        painter.drawText(w - 76, h - 6, "Hiện tại (Live)")

        peak_val = max(max(self.down_points), max(self.up_points))
        peak_text = f"Đỉnh: {format_speed(peak_val)}"
        painter.drawText(w - 110, 16, peak_text)


# =============================================================================
# CỬA SỔ CHÍNH: CLIENT STATUS WINDOW (4 TABS)
# =============================================================================
class ClientStatusWindow(QMainWindow):
    """Cửa sổ điều khiển và giám sát trung tâm của Agent máy con."""

    def __init__(self, agent_service, parent=None):
        super().__init__(parent)
        self.agent = agent_service
        self._init_ui()

        # Timer định kỳ cập nhật trạng thái giao diện (chu kỳ 1 giây)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self._refresh_ui)
        self.refresh_timer.start()

    def _init_ui(self):
        self.setWindowTitle("Client Agent - Network Monitor & Protect")
        self.resize(740, 580)
        self.setMinimumSize(660, 500)
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QTabWidget::pane {
                border: 1px solid #E2E8F0;
                background-color: #FFFFFF;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #F8FAFC;
                color: #64748B;
                padding: 9px 20px;
                font-size: 12px;
                font-weight: 600;
                border: 1px solid #E2E8F0;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                background-color: #FFFFFF;
                color: #2563EB;
                font-weight: bold;
                border-top: 2px solid #2563EB;
            }
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 600;
                border-radius: 6px;
                padding: 6px 16px;
                border: none;
            }
            QPushButton:hover {
                background-color: #1D4ED8;
            }
            QPushButton:disabled {
                background-color: #94A3B8;
                color: #F1F5F9;
            }
            QLineEdit, QSpinBox {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 12px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #2563EB;
                background-color: #FFFFFF;
            }
            QTableWidget {
                border: 1px solid #E2E8F0;
                border-radius: 6px;
                background-color: #FFFFFF;
                gridline-color: #F1F5F9;
                font-size: 12px;
            }
            QHeaderView::section {
                background-color: #F8FAFC;
                color: #475569;
                font-weight: bold;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #E2E8F0;
            }
        """)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # 1. Header Banner
        header = QHBoxLayout()
        header_left = QVBoxLayout()
        header_left.setSpacing(2)

        title_box = QHBoxLayout()
        title_box.setSpacing(8)
        lbl_title = QLabel("Client Network Agent")
        lbl_title.setStyleSheet("font-size: 16px; font-weight: 800; color: #0F172A;")
        lbl_ver = QLabel(f"v{self.agent.registration_data.agent_version}")
        lbl_ver.setStyleSheet("""
            background-color: #EFF6FF;
            color: #1D4ED8;
            font-size: 11px;
            font-weight: bold;
            padding: 2px 6px;
            border-radius: 4px;
            border: 1px solid #BFDBFE;
        """)
        title_box.addWidget(lbl_title)
        title_box.addWidget(lbl_ver)
        title_box.addStretch()

        sub = QLabel("Tiến trình giám sát và tiếp nhận chính sách an ninh mạng tập trung")
        sub.setStyleSheet("font-size: 11px; color: #64748B;")
        header_left.addLayout(title_box)
        header_left.addWidget(sub)
        header.addLayout(header_left)
        header.addStretch()

        # Connection Badge
        self.lbl_conn_badge = QLabel("● Đang kết nối...")
        self.lbl_conn_badge.setStyleSheet("""
            background-color: #F1F5F9;
            color: #64748B;
            font-size: 11px;
            font-weight: bold;
            padding: 5px 12px;
            border-radius: 12px;
        """)
        header.addWidget(self.lbl_conn_badge)
        layout.addLayout(header)

        # 2. Tab Widget (4 Tabs)
        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._create_live_tab(), "Giám Sát Trực Tiếp")
        self.tab_widget.addTab(self._create_policy_tab(), "Chính Sách & Nhật Ký")
        self.tab_widget.addTab(self._create_diagnostics_tab(), "Chẩn Đoán Mạng")
        self.tab_widget.addTab(self._create_settings_tab(), "Cấu Hình & Thiết Bị")
        layout.addWidget(self.tab_widget)

        self.setCentralWidget(central)

    # =========================================================================
    # TAB 1: GIÁM SÁT TRỰC TIẾP (LIVE MONITOR - DEFAULT TAB)
    # =========================================================================
    def _create_live_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(12)

        # Top Speed Cards
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_down = self._create_metric_card("TỐC ĐỘ TẢI XUỐNG (RX)", "0 B/s", "#D97706")
        self.card_up = self._create_metric_card("TỐC ĐỘ TẢI LÊN (TX)", "0 B/s", "#E11D48")
        cards.addWidget(self.card_down)
        cards.addWidget(self.card_up)
        l.addLayout(cards)

        # Sparkline Graph & System Metrics Row
        mid_row = QHBoxLayout()
        mid_row.setSpacing(12)

        # Sparkline Box (Left - 60s)
        spark_frame = QFrame()
        spark_frame.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 10px;")
        sl = QVBoxLayout(spark_frame)
        sl.setContentsMargins(10, 8, 10, 8)
        sl.setSpacing(8)

        sl_header = QHBoxLayout()
        sl_title = QLabel("Diễn Biến Lưu Lượng 60 Giây")
        sl_title.setStyleSheet("font-size: 11px; font-weight: bold; color: #334155;")
        sl_legend = QLabel("Vàng: Tải xuống  |  Đỏ: Tải lên")
        sl_legend.setStyleSheet("font-size: 10px; color: #64748B; font-weight: 600;")
        sl_header.addWidget(sl_title)
        sl_header.addStretch()
        sl_header.addWidget(sl_legend)
        sl.addLayout(sl_header)

        self.sparkline = TrafficSparklineWidget(max_points=60)
        sl.addWidget(self.sparkline)
        mid_row.addWidget(spark_frame, stretch=3)

        # Hardware Metrics Box (Right) - Cân đối cùng chiều cao
        metrics_frame = QFrame()
        metrics_frame.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px;")
        ml = QVBoxLayout(metrics_frame)
        ml.setContentsMargins(12, 10, 12, 10)
        ml.setSpacing(8)

        # CPU Metric
        cpu_row = QHBoxLayout()
        lbl_cpu_t = QLabel("CPU Mạng:")
        lbl_cpu_t.setStyleSheet("font-size: 11px; font-weight: bold; color: #475569;")
        self.lbl_cpu = QLabel("0.0%")
        self.lbl_cpu.setStyleSheet("font-size: 11px; font-weight: bold; color: #2563EB;")
        cpu_row.addWidget(lbl_cpu_t)
        cpu_row.addStretch()
        cpu_row.addWidget(self.lbl_cpu)
        ml.addLayout(cpu_row)

        self.bar_cpu = QProgressBar()
        self.bar_cpu.setRange(0, 100)
        self.bar_cpu.setValue(0)
        self.bar_cpu.setTextVisible(False)
        self.bar_cpu.setFixedHeight(6)
        self.bar_cpu.setStyleSheet("""
            QProgressBar {
                background-color: #E2E8F0;
                border-radius: 3px;
                border: none;
            }
            QProgressBar::chunk {
                background-color: #2563EB;
                border-radius: 3px;
            }
        """)
        ml.addWidget(self.bar_cpu)

        # RAM Metric
        ram_row = QHBoxLayout()
        lbl_ram_t = QLabel("RAM Hệ Thống:")
        lbl_ram_t.setStyleSheet("font-size: 11px; font-weight: bold; color: #475569;")
        self.lbl_ram = QLabel("0.0%")
        self.lbl_ram.setStyleSheet("font-size: 11px; font-weight: bold; color: #4F46E5;")
        ram_row.addWidget(lbl_ram_t)
        ram_row.addStretch()
        ram_row.addWidget(self.lbl_ram)
        ml.addLayout(ram_row)

        self.bar_ram = QProgressBar()
        self.bar_ram.setRange(0, 100)
        self.bar_ram.setValue(0)
        self.bar_ram.setTextVisible(False)
        self.bar_ram.setFixedHeight(6)
        self.bar_ram.setStyleSheet("""
            QProgressBar {
                background-color: #E2E8F0;
                border-radius: 3px;
                border: none;
            }
            QProgressBar::chunk {
                background-color: #4F46E5;
                border-radius: 3px;
            }
        """)
        ml.addWidget(self.bar_ram)

        # Cumulative totals
        ml.addSpacing(4)
        div_line = QFrame()
        div_line.setFrameShape(QFrame.Shape.HLine)
        div_line.setStyleSheet("color: #E2E8F0;")
        ml.addWidget(div_line)

        self.lbl_total_down = QLabel("Tổng tải xuống: 0 B")
        self.lbl_total_down.setStyleSheet("font-size: 11px; color: #334155; font-weight: 600;")
        self.lbl_total_up = QLabel("Tổng tải lên: 0 B")
        self.lbl_total_up.setStyleSheet("font-size: 11px; color: #334155; font-weight: 600;")
        ml.addWidget(self.lbl_total_down)
        ml.addWidget(self.lbl_total_up)
        ml.addStretch()

        mid_row.addWidget(metrics_frame, stretch=2)
        l.addLayout(mid_row)

        # Top Active Processes Table
        lbl_tbl_title = QLabel("Tiến Trình Đang Sử Dụng Băng Thông:")
        lbl_tbl_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155; margin-top: 2px;")
        l.addWidget(lbl_tbl_title)

        self.tbl_live_procs = QTableWidget()
        self.tbl_live_procs.setColumnCount(4)
        self.tbl_live_procs.setHorizontalHeaderLabels([
            "Ứng Dụng", "PID", "Tốc Độ Tải Xuống (Rx)", "Tốc Độ Tải Lên (Tx)"
        ])
        self.tbl_live_procs.verticalHeader().setVisible(False)
        self.tbl_live_procs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        l.addWidget(self.tbl_live_procs, stretch=1)

        return tab

    def _create_metric_card(self, title: str, value: str, color: str) -> QFrame:
        """Tạo thẻ hiển thị chỉ số tốc độ nổi bật."""
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid {color};
                border-radius: 8px;
                padding: 8px 14px;
            }}
        """)
        l = QVBoxLayout(card)
        l.setSpacing(2)
        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B;")
        lbl_v = QLabel(value)
        lbl_v.setStyleSheet(f"font-size: 19px; font-weight: 800; color: {color};")
        card.lbl_val = lbl_v
        l.addWidget(lbl_t)
        l.addWidget(lbl_v)
        return card

    # =========================================================================
    # TAB 2: CHÍNH SÁCH AN NINH & NHẬT KÝ (SECURITY & AUDIT)
    # =========================================================================
    def _create_policy_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(10)

        # Banner thông báo chế độ chỉ xem
        banner = QFrame()
        banner.setStyleSheet("background-color: #FEF3C7; border: 1px solid #FCD34D; border-radius: 8px; padding: 8px 12px;")
        b_lay = QHBoxLayout(banner)
        b_lay.setContentsMargins(4, 2, 4, 2)
        lbl_txt = QLabel("Chế độ Chỉ Xem: Chính sách an ninh (Firewall & QoS) được quản trị tập trung từ xa bởi Admin Server.")
        lbl_txt.setStyleSheet("font-size: 11px; color: #92400E; font-weight: 600;")
        lbl_txt.setWordWrap(True)
        b_lay.addWidget(lbl_txt)
        l.addWidget(banner)

        # Section 1: Active Enforcement Policies Table
        lbl_sec1 = QLabel("Chính Sách Đang Thực Thi Trên Máy Này:")
        lbl_sec1.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155;")
        l.addWidget(lbl_sec1)

        self.tbl_policies = QTableWidget()
        self.tbl_policies.setColumnCount(5)
        self.tbl_policies.setHorizontalHeaderLabels([
            "Ứng Dụng", "Hành Động Can Thiệp", "Hạn Mức Băng Thông", "Chiều Áp Dụng", "Thời Điểm"
        ])
        self.tbl_policies.verticalHeader().setVisible(False)
        self.tbl_policies.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        l.addWidget(self.tbl_policies, stretch=1)

        # Section 2: Admin Action & Audit Log Table
        lbl_sec2 = QLabel("Nhật Ký Lệnh Điều Khiển Từ Admin (Audit Log):")
        lbl_sec2.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155; margin-top: 4px;")
        l.addWidget(lbl_sec2)

        self.tbl_audit = QTableWidget()
        self.tbl_audit.setColumnCount(5)
        self.tbl_audit.setHorizontalHeaderLabels([
            "Thời Điểm", "Nguồn Gửi", "Lệnh / Thao Tác", "Đối Tượng", "Chi Tiết Tác Động"
        ])
        self.tbl_audit.verticalHeader().setVisible(False)
        self.tbl_audit.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tbl_audit.horizontalHeader().resizeSection(0, 80)
        self.tbl_audit.horizontalHeader().resizeSection(1, 95)
        self.tbl_audit.horizontalHeader().resizeSection(2, 110)
        self.tbl_audit.horizontalHeader().resizeSection(3, 110)
        self.tbl_audit.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        l.addWidget(self.tbl_audit, stretch=1)

        return tab

    # =========================================================================
    # TAB 3: CHẨN ĐOÁN MẠNG & PING (NETWORK DIAGNOSTICS)
    # =========================================================================
    def _create_diagnostics_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(12)

        # Latency Cards Row
        cards = QHBoxLayout()
        cards.setSpacing(12)

        # Card Server Latency
        self.card_srv_lat = QFrame()
        self.card_srv_lat.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid #10B981;
                border-radius: 8px;
                padding: 10px 14px;
            }
        """)
        csl = QVBoxLayout(self.card_srv_lat)
        csl.setSpacing(2)
        lbl_csl_t = QLabel("ĐỘ TRỄ TỚI ADMIN SERVER")
        lbl_csl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B;")
        self.lbl_srv_lat_val = QLabel("-- ms")
        self.lbl_srv_lat_val.setStyleSheet("font-size: 18px; font-weight: 800; color: #059669;")
        self.lbl_srv_status = QLabel("Kiểm tra định kỳ")
        self.lbl_srv_status.setStyleSheet("font-size: 10px; color: #64748B;")
        csl.addWidget(lbl_csl_t)
        csl.addWidget(self.lbl_srv_lat_val)
        csl.addWidget(self.lbl_srv_status)
        cards.addWidget(self.card_srv_lat)

        # Card Internet Latency
        self.card_net_lat = QFrame()
        self.card_net_lat.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid #2563EB;
                border-radius: 8px;
                padding: 10px 14px;
            }
        """)
        cnl = QVBoxLayout(self.card_net_lat)
        cnl.setSpacing(2)
        lbl_cnl_t = QLabel("ĐỘ TRỄ INTERNET (DNS 8.8.8.8)")
        lbl_cnl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B;")
        self.lbl_net_lat_val = QLabel("-- ms")
        self.lbl_net_lat_val.setStyleSheet("font-size: 18px; font-weight: 800; color: #2563EB;")
        self.lbl_net_status = QLabel("Kiểm tra định kỳ")
        self.lbl_net_status.setStyleSheet("font-size: 10px; color: #64748B;")
        cnl.addWidget(lbl_cnl_t)
        cnl.addWidget(self.lbl_net_lat_val)
        cnl.addWidget(self.lbl_net_status)
        cards.addWidget(self.card_net_lat)

        l.addLayout(cards)

        # Active Network Adapter Details
        nic_frame = QFrame()
        nic_frame.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px;")
        nl = QVBoxLayout(nic_frame)
        nl.setSpacing(6)

        nl_title = QLabel("Thông Số Card Mạng Đang Sử Dụng (Active Interface):")
        nl_title.setStyleSheet("font-size: 11px; font-weight: bold; color: #334155;")
        nl.addWidget(nl_title)

        nic_grid = QHBoxLayout()
        self.lbl_nic_name = QLabel("Tên: Đang phát hiện...")
        self.lbl_nic_speed = QLabel("Tốc độ: -- Mbps")
        self.lbl_nic_type = QLabel("Loại: --")
        for lbl in (self.lbl_nic_name, self.lbl_nic_speed, self.lbl_nic_type):
            lbl.setStyleSheet("font-size: 11px; color: #475569;")
            nic_grid.addWidget(lbl)
        nl.addLayout(nic_grid)
        l.addWidget(nic_frame)

        # Quick Diagnostic Section
        diag_header = QHBoxLayout()
        lbl_diag = QLabel("Nhật Ký Kiểm Tra Chẩn Đoán:")
        lbl_diag.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155;")
        diag_header.addWidget(lbl_diag)
        diag_header.addStretch()

        self.btn_run_diag = QPushButton("Chạy Kiểm Tra Ngay")
        self.btn_run_diag.setFixedHeight(30)
        self.btn_run_diag.clicked.connect(self._on_run_diagnostic)
        diag_header.addWidget(self.btn_run_diag)
        l.addLayout(diag_header)

        # Console Nhật ký chẩn đoán dạng Light Card có độ tương phản cao, chống lỗi stylesheet
        self.txt_diag_log = QTextEdit()
        self.txt_diag_log.setReadOnly(True)
        self.txt_diag_log.setStyleSheet("""
            QTextEdit {
                background-color: #F8FAFC;
                color: #0F172A;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 10px;
                line-height: 1.5;
            }
        """)
        self.txt_diag_log.setHtml(
            "<div style='color: #64748B;'>[Hệ Thống] Nhấn nút <b>'Chạy Kiểm Tra Ngay'</b> ở trên để bắt đầu quy trình chẩn đoán kết nối mạng...</div>"
        )
        l.addWidget(self.txt_diag_log, stretch=1)

        # Cập nhật thông số card mạng ban đầu
        self._update_nic_info()

        return tab

    def _update_nic_info(self):
        """Lấy thông số card mạng từ tầng nghiệp vụ."""
        try:
            nic = self.agent.get_active_nic_info()
            self.lbl_nic_name.setText(f"Tên: {nic.get('name', 'N/A')}")
            self.lbl_nic_speed.setText(f"Tốc độ: {nic.get('speed_mbps', 0)} Mbps")
            self.lbl_nic_type.setText(f"Loại: {nic.get('type', 'N/A')}")
        except Exception:
            pass

    def _on_run_diagnostic(self):
        """Xử lý sự kiện bấm nút chạy chẩn đoán."""
        self.btn_run_diag.setEnabled(False)
        self.btn_run_diag.setText("Đang kiểm tra...")
        self.txt_diag_log.clear()
        self.txt_diag_log.append("<div style='color: #2563EB; font-weight: bold;'>[Bắt đầu] Đang kiểm tra hệ thống mạng và đo lường độ trễ...</div>")
        QApplication.processEvents()

        try:
            results = self.agent.run_full_diagnostics()
            self.txt_diag_log.clear()
            for line in results:
                if "✓" in line:
                    html_line = f"<div style='color: #059669; font-weight: 600; margin-bottom: 3px;'>{line}</div>"
                elif "⚠" in line:
                    html_line = f"<div style='color: #D97706; font-weight: 600; margin-bottom: 3px;'>{line}</div>"
                elif "ℹ" in line:
                    html_line = f"<div style='color: #2563EB; margin-bottom: 3px;'>{line}</div>"
                else:
                    html_line = f"<div style='color: #0F172A; margin-bottom: 3px;'>{line}</div>"
                self.txt_diag_log.append(html_line)
        except Exception as e:
            self.txt_diag_log.append(f"<div style='color: #DC2626;'>Lỗi khi chạy chẩn đoán: {e}</div>")
        finally:
            self.btn_run_diag.setEnabled(True)
            self.btn_run_diag.setText("Chạy Kiểm Tra Ngay")
            self._update_nic_info()

    # =========================================================================
    # TAB 4: CẤU HÌNH & THIẾT BỊ (SYSTEM & SETTINGS - MERGED)
    # =========================================================================
    def _create_settings_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(12)

        # Group 1: Server Connection Settings
        grp_conn = QFrame()
        grp_conn.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px;")
        cl = QVBoxLayout(grp_conn)
        cl.setSpacing(8)

        lbl_c_title = QLabel("Cài Đặt Kết Nối Máy Chủ Quản Trị (Admin Server):")
        lbl_c_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #0F172A;")
        cl.addWidget(lbl_c_title)

        f_row = QHBoxLayout()
        f_row.setSpacing(12)

        lbl_ip = QLabel("Địa chỉ IP Server:")
        lbl_ip.setStyleSheet("font-size: 12px; font-weight: 600; color: #475569;")
        self.txt_server_ip = QLineEdit(self.agent.server_host)

        lbl_port = QLabel("Cổng TCP:")
        lbl_port.setStyleSheet("font-size: 12px; font-weight: 600; color: #475569;")
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(self.agent.server_port)

        f_row.addWidget(lbl_ip)
        f_row.addWidget(self.txt_server_ip, stretch=2)
        f_row.addWidget(lbl_port)
        f_row.addWidget(self.spin_port, stretch=1)
        cl.addLayout(f_row)

        self.chk_toast = QCheckBox("Hiển thị thông báo góc màn hình khi Admin áp dụng chính sách")
        self.chk_toast.setChecked(getattr(self.agent, "show_toast_notifications", True))
        cl.addWidget(self.chk_toast)

        self.chk_autostart = QCheckBox("Tự động khởi động cùng Windows (Chạy ngầm dưới khay hệ thống System Tray)")
        self.chk_autostart.setChecked(self.agent.is_windows_autostart())
        cl.addWidget(self.chk_autostart)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_save = QPushButton("Lưu Cấu Hình & Kết Nối Lại")
        btn_save.setFixedHeight(32)
        btn_save.clicked.connect(self._on_save_settings)
        btn_row.addWidget(btn_save)
        cl.addLayout(btn_row)

        l.addWidget(grp_conn)

        # Group 2: Device Identification
        grp_dev = QFrame()
        grp_dev.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px;")
        dl = QVBoxLayout(grp_dev)
        dl.setSpacing(6)

        lbl_d_title = QLabel("Thông Tin Định Danh Thiết Bị (Device Specifications):")
        lbl_d_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #0F172A;")
        dl.addWidget(lbl_d_title)

        reg = self.agent.registration_data
        specs = [
            ("Tên máy (Hostname):", reg.hostname),
            ("Device UUID:", reg.device_uuid),
            ("Địa chỉ MAC:", reg.mac_address),
            ("Địa chỉ IP LAN:", reg.ip_address),
            ("Hệ điều hành:", f"{reg.os_name} {reg.os_version}"),
            ("Phiên bản Agent:", reg.agent_version),
        ]

        for label_text, val_text in specs:
            row = QHBoxLayout()
            lbl_k = QLabel(label_text)
            lbl_k.setStyleSheet("font-size: 11px; font-weight: bold; color: #64748B; min-width: 130px;")
            lbl_v = QLabel(val_text)
            lbl_v.setStyleSheet("font-size: 11px; color: #0F172A;")
            row.addWidget(lbl_k)
            row.addWidget(lbl_v, stretch=1)
            dl.addLayout(row)

        btn_copy_row = QHBoxLayout()
        btn_copy = QPushButton("Sao Chép Device UUID")
        btn_copy.setFixedWidth(160)
        btn_copy.setFixedHeight(28)
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(reg.device_uuid))
        btn_copy_row.addWidget(btn_copy)
        btn_copy_row.addStretch()
        dl.addLayout(btn_copy_row)

        l.addWidget(grp_dev)
        l.addStretch()
        return tab

    def _on_save_settings(self):
        """Lưu thiết lập vào client_config.json và cập nhật tiến trình."""
        new_host = self.txt_server_ip.text().strip()
        new_port = self.spin_port.value()
        if not new_host:
            QMessageBox.warning(self, "Lỗi", "Địa chỉ IP máy chủ không được để trống!")
            return

        enable_autostart = self.chk_autostart.isChecked()
        self.agent.set_windows_autostart(enable_autostart)

        cfg = {
            "server_host": new_host,
            "server_port": new_port,
            "enable_etw": self.agent.enable_etw,
            "show_toast_notifications": self.chk_toast.isChecked(),
            "auto_start_windows": enable_autostart,
        }
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"[ClientWindow] Lỗi ghi config: {e}")

        self.agent.show_toast_notifications = self.chk_toast.isChecked()
        self.agent.update_server_target(new_host, new_port)
        QMessageBox.information(
            self, "Thành Công", f"Đã lưu cấu hình và đang kết nối lại tới {new_host}:{new_port}!"
        )

    # =========================================================================
    # CẬP NHẬT GIAO DIỆN ĐỊNH KỲ (REFRESH LOOP)
    # =========================================================================
    def _refresh_ui(self):
        """Cập nhật dữ liệu thời gian thực cho cả 4 tab."""
        # 1. Trạng thái kết nối
        is_conn = self.agent.net_client.is_connected()
        if is_conn:
            self.lbl_conn_badge.setText(f"● Đã kết nối: {self.agent.server_host}:{self.agent.server_port}")
            self.lbl_conn_badge.setStyleSheet("""
                background-color: #ECFDF5;
                color: #059669;
                font-size: 11px;
                font-weight: bold;
                padding: 4px 12px;
                border-radius: 12px;
                border: 1px solid #A7F3D0;
            """)
        else:
            self.lbl_conn_badge.setText(f"● Đang ngắt kết nối ({self.agent.server_host}:{self.agent.server_port})")
            self.lbl_conn_badge.setStyleSheet("""
                background-color: #F1F5F9;
                color: #64748B;
                font-size: 11px;
                font-weight: bold;
                padding: 4px 12px;
                border-radius: 12px;
            """)

        # 2. Cập nhật Tab 1: Số liệu Live & Sparkline
        down_kb = self.agent.latest_down_kbps
        up_kb = self.agent.latest_up_kbps
        self.card_down.lbl_val.setText(format_speed(down_kb))
        self.card_up.lbl_val.setText(format_speed(up_kb))
        self.sparkline.add_sample(down_kb, up_kb)

        cpu_val = self.agent.latest_cpu_pct
        ram_val = self.agent.latest_ram_pct
        self.lbl_cpu.setText(f"{cpu_val:.1f}%")
        self.bar_cpu.setValue(int(cpu_val))

        self.lbl_ram.setText(f"{ram_val:.1f}%")
        self.bar_ram.setValue(int(ram_val))

        self.lbl_total_down.setText(f"Tổng tải xuống: {format_bytes(self.agent.total_rx_bytes)}")
        self.lbl_total_up.setText(f"Tổng tải lên: {format_bytes(self.agent.total_tx_bytes)}")

        # Bảng top tiến trình mạng
        top_procs = getattr(self.agent, "latest_top_procs", [])
        self.tbl_live_procs.setRowCount(len(top_procs))
        for r, p in enumerate(top_procs):
            item_name = QTableWidgetItem(str(getattr(p, "name", "Unknown")))
            item_pid = QTableWidgetItem(str(getattr(p, "pid", 0)))
            item_pid.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            item_down = QTableWidgetItem(format_speed(getattr(p, "down_kbps", 0.0)))
            item_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            item_up = QTableWidgetItem(format_speed(getattr(p, "up_kbps", 0.0)))
            item_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            self.tbl_live_procs.setItem(r, 0, item_name)
            self.tbl_live_procs.setItem(r, 1, item_pid)
            self.tbl_live_procs.setItem(r, 2, item_down)
            self.tbl_live_procs.setItem(r, 3, item_up)

        # 3. Cập nhật Tab 2: Chính sách hiện hành & Nhật ký kiểm toán
        policies = list(self.agent.active_policies.values())
        self.tbl_policies.setRowCount(len(policies))
        for r, pol in enumerate(policies):
            act = pol.get("action", "")
            act_label = "CHẶN TOÀN BỘ (BLOCK)" if act == "BLOCK" else "BÓP BĂNG THÔNG (QoS)"
            limit_str = f"{pol.get('limit_kbps', 0):.1f} KB/s" if act == "THROTTLE" else "0 KB/s"
            t_str = time.strftime("%H:%M:%S", time.localtime(pol.get("time", time.time())))

            self.tbl_policies.setItem(r, 0, QTableWidgetItem(pol.get("app", "")))
            self.tbl_policies.setItem(r, 1, QTableWidgetItem(act_label))
            self.tbl_policies.setItem(r, 2, QTableWidgetItem(limit_str))
            self.tbl_policies.setItem(r, 3, QTableWidgetItem(pol.get("direction", "both")))
            self.tbl_policies.setItem(r, 4, QTableWidgetItem(t_str))

        # Bảng Nhật ký kiểm toán (Audit Logs)
        audit_logs = self.agent.get_audit_logs()
        self.tbl_audit.setRowCount(min(len(audit_logs), 50))
        for r, entry in enumerate(audit_logs[:50]):
            self.tbl_audit.setItem(r, 0, QTableWidgetItem(entry.get("time_str", "")))
            self.tbl_audit.setItem(r, 1, QTableWidgetItem(entry.get("source", "")))
            self.tbl_audit.setItem(r, 2, QTableWidgetItem(entry.get("action", "")))
            self.tbl_audit.setItem(r, 3, QTableWidgetItem(entry.get("target", "")))
            self.tbl_audit.setItem(r, 4, QTableWidgetItem(entry.get("details", "")))

        # 4. Cập nhật Tab 3: Thẻ độ trễ (nếu đang ở tab 2)
        if self.tab_widget.currentIndex() == 2:
            # Server Latency
            srv_lat, srv_ok, _ = self.agent.check_server_latency()
            if srv_ok and srv_lat is not None:
                self.lbl_srv_lat_val.setText(f"{srv_lat:.1f} ms")
                self.lbl_srv_status.setText("● Kết nối TCP ổn định")
                self.lbl_srv_status.setStyleSheet("font-size: 10px; color: #059669; font-weight: bold;")
            else:
                self.lbl_srv_lat_val.setText("-- ms")
                self.lbl_srv_status.setText("● Không thể kết nối")
                self.lbl_srv_status.setStyleSheet("font-size: 10px; color: #DC2626; font-weight: bold;")

            # Internet Latency
            net_lat, net_ok, _ = self.agent.check_internet_latency()
            if net_ok and net_lat is not None:
                self.lbl_net_lat_val.setText(f"{net_lat:.1f} ms")
                self.lbl_net_status.setText("● Internet sẵn sàng")
                self.lbl_net_status.setStyleSheet("font-size: 10px; color: #2563EB; font-weight: bold;")
            else:
                self.lbl_net_lat_val.setText("-- ms")
                self.lbl_net_status.setText("● Không phản hồi mạng ngoài")
                self.lbl_net_status.setStyleSheet("font-size: 10px; color: #D97706; font-weight: bold;")
