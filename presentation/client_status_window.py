"""
Cửa sổ Quản lý & Trạng thái Máy Con (Client Status & Configuration Window - Presentation Layer):
- Tab 1: Giám sát lưu lượng trực tiếp (Đồ thị sóng Bezier 60s, Card tốc độ Rx/Tx, CPU/RAM, Top tiến trình kèm nút Tự vệ cục bộ).
- Tab 2: Chính sách an ninh 2 lớp (🔒 Admin Enforced & 🔓 Local Self-Protect) + Nhật ký kiểm toán (Audit Log).
- Tab 3: Chẩn đoán kết nối mạng & đo độ trễ tới Admin Server và Internet Gateway.
- Tab 4: Cấu hình kết nối Server & Thông tin định danh phần cứng máy con.
"""

import os
import sys
import time
from typing import Optional, List, Dict, Any
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QHeaderView, QFrame, QMessageBox, QApplication, QTextEdit,
    QProgressBar, QInputDialog, QAbstractItemView
)


from application.traffic_analytics import format_bytes, format_speed
from presentation.widgets.sparkline_widget import TrafficSparklineWidget


def get_client_config_path() -> str:
    """Xác định đường dẫn tệp client_config.json linh hoạt theo môi trường thực thi."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "client_config.json")


CONFIG_PATH = get_client_config_path()


# =============================================================================
# CỬA SỔ CHÍNH: CLIENT STATUS WINDOW (4 TABS + 2-TIER SECURITY CONTROL)
# =============================================================================
class ClientStatusWindow(QMainWindow):
    """Cửa sổ điều khiển và giám sát trung tâm của Agent máy con."""

    def __init__(self, agent_service, parent=None):
        super().__init__(parent)
        self.agent = agent_service
        self._live_row_order: List[str] = []
        self._live_row_map: Dict[str, int] = {}
        self._live_action_buttons: Dict[str, QPushButton] = {}
        self._last_policies_snapshot: Optional[List[tuple]] = None

        self._init_ui()

        # Timer định kỳ cập nhật trạng thái giao diện (chu kỳ 1 giây)
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self._refresh_ui)
        self.refresh_timer.start()

    def _init_ui(self):
        self.setWindowTitle("Client Agent - Network Monitor & Protect")
        self.resize(780, 610)
        self.setMinimumSize(680, 520)
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QLabel, QCheckBox {
                border: none;
                background: transparent;
                padding: 0px;
            }
            QTabWidget::pane {
                border: none;
                border-top: 1px solid #E2E8F0;
                background-color: #FFFFFF;
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

        header_left.addLayout(title_box)
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
        spark_frame.setObjectName("sparkFrame")
        spark_frame.setStyleSheet("""
            QFrame#sparkFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
        """)
        sl = QVBoxLayout(spark_frame)
        sl.setContentsMargins(12, 10, 12, 10)
        sl.setSpacing(6)

        sl_header = QHBoxLayout()
        sl_title = QLabel("Diễn Biến Lưu Lượng 60 Giây")
        sl_title.setStyleSheet("font-size: 11px; font-weight: bold; color: #334155; border: none; background: transparent;")
        sl_legend = QLabel("Vàng: Tải xuống  |  Đỏ: Tải lên")
        sl_legend.setStyleSheet("font-size: 10px; color: #64748B; font-weight: 600; border: none; background: transparent;")
        sl_header.addWidget(sl_title)
        sl_header.addStretch()
        sl_header.addWidget(sl_legend)
        sl.addLayout(sl_header)

        self.sparkline = TrafficSparklineWidget(max_points=60)
        sl.addWidget(self.sparkline)
        mid_row.addWidget(spark_frame, stretch=3)

        # Hardware Metrics Box (Right) - Cân đối cùng chiều cao
        metrics_frame = QFrame()
        metrics_frame.setObjectName("metricsFrame")
        metrics_frame.setStyleSheet("""
            QFrame#metricsFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
        """)
        ml = QVBoxLayout(metrics_frame)
        ml.setContentsMargins(14, 12, 14, 12)
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
        lbl_tbl_title = QLabel("Tiến Trình Đang Sử Dụng Băng Thông (Hỗ Trợ Tự Vệ Cục Bộ):")
        lbl_tbl_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155; margin-top: 2px;")
        l.addWidget(lbl_tbl_title)

        self.tbl_live_procs = QTableWidget()
        self.tbl_live_procs.setColumnCount(5)
        self.tbl_live_procs.setHorizontalHeaderLabels([
            "Ứng Dụng", "PID", "Tốc Độ Tải Xuống (Rx)", "Tốc Độ Tải Lên (Tx)", "Bảo Vệ / Trạng Thái"
        ])
        self.tbl_live_procs.verticalHeader().setVisible(False)
        self.tbl_live_procs.verticalHeader().setDefaultSectionSize(34)
        self.tbl_live_procs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_live_procs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.tbl_live_procs.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tbl_live_procs.setColumnWidth(1, 75)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tbl_live_procs.setColumnWidth(2, 140)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tbl_live_procs.setColumnWidth(3, 140)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tbl_live_procs.setColumnWidth(4, 155)
        l.addWidget(self.tbl_live_procs, stretch=1)

        return tab

    def _create_metric_card(self, title: str, value: str, color: str) -> QFrame:
        """Tạo thẻ hiển thị chỉ số tốc độ nổi bật."""
        card = QFrame()
        card.setObjectName("metricCard")
        card.setStyleSheet(f"""
            QFrame#metricCard {{
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid {color};
                border-radius: 8px;
            }}
        """)
        l = QVBoxLayout(card)
        l.setContentsMargins(14, 10, 14, 10)
        l.setSpacing(2)
        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B; border: none; background: transparent;")
        lbl_v = QLabel(value)
        lbl_v.setStyleSheet(f"font-size: 19px; font-weight: 800; color: {color}; border: none; background: transparent;")
        card.lbl_val = lbl_v
        l.addWidget(lbl_t)
        l.addWidget(lbl_v)
        return card

    # =========================================================================
    # TAB 2: CHÍNH SÁCH AN NINH 2 LỚP & NHẬT KÝ (2-TIER SECURITY & AUDIT)
    # =========================================================================
    def _create_policy_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(10)

        # Thanh công cụ tự vệ cục bộ cho người dùng máy con
        ctrl_bar = QHBoxLayout()
        lbl_sec1 = QLabel("Chính Sách Đang Thực Thi Trên Máy Này:")
        lbl_sec1.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155;")
        ctrl_bar.addWidget(lbl_sec1)
        ctrl_bar.addStretch()

        self.btn_local_block = QPushButton("🚫 Tự Chặn Ứng Dụng")
        self.btn_local_block.setFixedHeight(26)
        self.btn_local_block.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_local_block.setStyleSheet("""
            QPushButton {
                background-color: #FEF2F2;
                color: #DC2626;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #FECACA;
                border-radius: 5px;
                padding: 2px 10px;
            }
            QPushButton:hover { background-color: #FEE2E2; }
        """)
        self.btn_local_block.clicked.connect(self._on_prompt_local_block)
        ctrl_bar.addWidget(self.btn_local_block)

        self.btn_local_qos = QPushButton("⚡ Tự Giới Hạn (QoS)")
        self.btn_local_qos.setFixedHeight(26)
        self.btn_local_qos.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_local_qos.setStyleSheet("""
            QPushButton {
                background-color: #FFFBEB;
                color: #D97706;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #FDE68A;
                border-radius: 5px;
                padding: 2px 10px;
            }
            QPushButton:hover { background-color: #FEF3C7; }
        """)
        self.btn_local_qos.clicked.connect(self._on_prompt_local_qos)
        ctrl_bar.addWidget(self.btn_local_qos)

        self.btn_clear_local = QPushButton("🔓 Gỡ Lệnh Cục Bộ")
        self.btn_clear_local.setFixedHeight(26)
        self.btn_clear_local.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_clear_local.setStyleSheet("""
            QPushButton {
                background-color: #EFF6FF;
                color: #2563EB;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #BFDBFE;
                border-radius: 5px;
                padding: 2px 10px;
            }
            QPushButton:hover { background-color: #DBEAFE; }
        """)
        self.btn_clear_local.clicked.connect(self._on_clear_all_local_policies)
        ctrl_bar.addWidget(self.btn_clear_local)

        l.addLayout(ctrl_bar)

        # Bảng chính sách 6 cột phân biệt rõ Nguồn Cấp & Quyền Gỡ Bỏ
        self.tbl_policies = QTableWidget()
        self.tbl_policies.setColumnCount(6)
        self.tbl_policies.setHorizontalHeaderLabels([
            "Ứng Dụng", "Nguồn Cấp", "Hành Động", "Hạn Mức", "Thời Điểm", "Thao Tác"
        ])
        self.tbl_policies.verticalHeader().setVisible(False)
        self.tbl_policies.verticalHeader().setDefaultSectionSize(36)
        self.tbl_policies.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        pol_header = self.tbl_policies.horizontalHeader()
        pol_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        pol_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tbl_policies.setColumnWidth(1, 135)
        pol_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tbl_policies.setColumnWidth(2, 145)
        pol_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tbl_policies.setColumnWidth(3, 95)
        pol_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tbl_policies.setColumnWidth(4, 85)
        pol_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.tbl_policies.setColumnWidth(5, 135)
        l.addWidget(self.tbl_policies, stretch=1)

        # Section 2: Admin & Local Action Audit Log Table
        lbl_sec2 = QLabel("Nhật Ký Kiểm Toán An Ninh (Admin & Local Audit Log):")
        lbl_sec2.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155; margin-top: 4px;")
        l.addWidget(lbl_sec2)

        self.tbl_audit = QTableWidget()
        self.tbl_audit.setColumnCount(5)
        self.tbl_audit.setHorizontalHeaderLabels([
            "Thời Điểm", "Nguồn Gửi", "Lệnh / Thao Tác", "Đối Tượng", "Chi Tiết Tác Động"
        ])
        self.tbl_audit.verticalHeader().setVisible(False)
        self.tbl_audit.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_audit.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tbl_audit.horizontalHeader().resizeSection(0, 80)
        self.tbl_audit.horizontalHeader().resizeSection(1, 95)
        self.tbl_audit.horizontalHeader().resizeSection(2, 110)
        self.tbl_audit.horizontalHeader().resizeSection(3, 110)
        self.tbl_audit.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        l.addWidget(self.tbl_audit, stretch=1)

        return tab

    # =========================================================================
    # XỬ LÝ TỰ VỆ CỤC BỘ (LOCAL SELF-PROTECTION HANDLERS)
    # =========================================================================
    def _get_candidate_app_names(self) -> List[str]:
        """Lấy danh sách tên các tiến trình đang hoạt động để gợi ý cho người dùng chọn."""
        names = []
        for p in getattr(self.agent, "latest_top_procs", []):
            n = str(getattr(p, "name", "")).strip()
            if n and n not in names:
                names.append(n)
        for n in ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "discord.exe", "steam.exe", "Zalo.exe"]:
            if n not in names:
                names.append(n)
        return names

    def _on_prompt_local_block(self):
        """Hộp thoại cho phép người dùng máy con tự chặn một ứng dụng cục bộ."""
        candidates = self._get_candidate_app_names()
        app_name, ok = QInputDialog.getItem(
            self,
            "Tự Chặn Ứng Dụng Cục Bộ",
            "Chọn hoặc nhập tên tiến trình (.exe) muốn chặn trên máy này:",
            candidates,
            0,
            True,
        )
        if not ok or not app_name.strip():
            return
        self._execute_local_block(app_name.strip())

    def _on_prompt_local_qos(self):
        """Hộp thoại cho phép người dùng máy con tự giới hạn băng thông (QoS) cục bộ."""
        candidates = self._get_candidate_app_names()
        app_name, ok = QInputDialog.getItem(
            self,
            "Tự Giới Hạn Băng Thông (QoS)",
            "Chọn hoặc nhập tên tiến trình (.exe) cần giới hạn tốc độ:",
            candidates,
            0,
            True,
        )
        if not ok or not app_name.strip():
            return
        app_name = app_name.strip()

        if hasattr(self.agent, "is_locked_by_admin") and self.agent.is_locked_by_admin(app_name):
            QMessageBox.warning(
                self,
                "🔒 Bị Khóa Bởi Admin Server",
                f"Ứng dụng '{app_name}' đang chịu chính sách bắt buộc từ Admin Server.\n"
                "Máy con không được phép thay đổi hoặc ghi đè chính sách của Quản trị viên!",
            )
            return

        limit_kbps, ok_lim = QInputDialog.getDouble(
            self,
            "Nhập Hạn Mức Tốc Độ",
            f"Nhập mức trần băng thông tối đa cho '{app_name}' (KB/s):",
            150.0,
            1.0,
            102400.0,
            1,
        )
        if not ok_lim:
            return

        if hasattr(self.agent, "apply_local_policy"):
            ok_exec, msg = self.agent.apply_local_policy(app_name, action="THROTTLE", limit_kbps=limit_kbps)
            if not ok_exec:
                QMessageBox.warning(self, "Không Thể Áp Dụng", msg)
            self._last_policies_snapshot = None
            self._refresh_ui()

    def _execute_local_block(self, app_name: str):
        """Thực thi chặn cục bộ cho `app_name`."""
        if not hasattr(self.agent, "apply_local_policy"):
            return
        ok, msg = self.agent.apply_local_policy(app_name, action="BLOCK")
        if not ok:
            QMessageBox.warning(self, "Không Thể Chặn", msg)
        self._last_policies_snapshot = None
        self._refresh_ui()

    def _execute_local_unblock(self, app_name: str):
        """Thực thi gỡ bỏ chính sách cục bộ (hoặc báo lỗi nếu bị Admin khóa)."""
        if not hasattr(self.agent, "remove_local_policy"):
            return
        ok, msg = self.agent.remove_local_policy(app_name)
        if not ok:
            QMessageBox.warning(self, "🔒 Bị Khóa Bởi Admin Server", msg)
        self._last_policies_snapshot = None
        self._refresh_ui()

    def _on_clear_all_local_policies(self):
        """Gỡ toàn bộ các lệnh cục bộ, giữ nguyên các lệnh của Admin."""
        if not hasattr(self.agent, "clear_all_local_policies"):
            return
        removed, kept_admin = self.agent.clear_all_local_policies()
        self._last_policies_snapshot = None
        self._refresh_ui()
        if kept_admin > 0:
            QMessageBox.information(
                self,
                "Đã Gỡ Chính Sách Cục Bộ",
                f"Đã gỡ {removed} chính sách cục bộ.\n"
                f"🔒 Giữ nguyên {kept_admin} chính sách bắt buộc từ Admin Server.",
            )

    def _on_live_proc_action_clicked(self, app_name: str):
        """Xử lý khi người dùng bấm nút hành động ngay trên bảng Tiến trình (Tab 1)."""
        pol = self.agent.get_policy_for_app(app_name) if hasattr(self.agent, "get_policy_for_app") else None
        if pol:
            is_admin_locked = pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN")
            if is_admin_locked:
                # Gọi remove_local_policy để tự động ghi nhận DENIED vào Audit Log và báo cảnh báo
                self._execute_local_unblock(app_name)
            else:
                self._execute_local_unblock(app_name)
        else:
            self._execute_local_block(app_name)

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
        self.card_srv_lat.setObjectName("cardSrvLat")
        self.card_srv_lat.setStyleSheet("""
            QFrame#cardSrvLat {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid #10B981;
                border-radius: 8px;
            }
        """)
        csl = QVBoxLayout(self.card_srv_lat)
        csl.setContentsMargins(14, 10, 14, 10)
        csl.setSpacing(2)
        lbl_csl_t = QLabel("ĐỘ TRỄ TỚI ADMIN SERVER")
        lbl_csl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B; border: none; background: transparent;")
        self.lbl_srv_lat_val = QLabel("-- ms")
        self.lbl_srv_lat_val.setStyleSheet("font-size: 18px; font-weight: 800; color: #059669; border: none; background: transparent;")
        csl.addWidget(lbl_csl_t)
        csl.addWidget(self.lbl_srv_lat_val)
        cards.addWidget(self.card_srv_lat)

        # Card Internet Latency
        self.card_net_lat = QFrame()
        self.card_net_lat.setObjectName("cardNetLat")
        self.card_net_lat.setStyleSheet("""
            QFrame#cardNetLat {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid #2563EB;
                border-radius: 8px;
            }
        """)
        cnl = QVBoxLayout(self.card_net_lat)
        cnl.setContentsMargins(14, 10, 14, 10)
        cnl.setSpacing(2)
        lbl_cnl_t = QLabel("ĐỘ TRỄ INTERNET (DNS 8.8.8.8)")
        lbl_cnl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B; border: none; background: transparent;")
        self.lbl_net_lat_val = QLabel("-- ms")
        self.lbl_net_lat_val.setStyleSheet("font-size: 18px; font-weight: 800; color: #2563EB; border: none; background: transparent;")
        cnl.addWidget(lbl_cnl_t)
        cnl.addWidget(self.lbl_net_lat_val)
        cards.addWidget(self.card_net_lat)

        l.addLayout(cards)

        # Active Network Adapter Details
        nic_frame = QFrame()
        nic_frame.setObjectName("nicFrame")
        nic_frame.setStyleSheet("""
            QFrame#nicFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
        """)
        nl = QVBoxLayout(nic_frame)
        nl.setContentsMargins(14, 10, 14, 10)
        nl.setSpacing(6)

        nl_title = QLabel("Thông Số Card Mạng Đang Sử Dụng (Active Interface):")
        nl_title.setStyleSheet("font-size: 11px; font-weight: bold; color: #334155; border: none; background: transparent;")
        nl.addWidget(nl_title)

        nic_grid = QHBoxLayout()
        self.lbl_nic_name = QLabel("Tên: Đang phát hiện...")
        self.lbl_nic_speed = QLabel("Tốc độ: -- Mbps")
        self.lbl_nic_type = QLabel("Loại: --")
        for lbl in (self.lbl_nic_name, self.lbl_nic_speed, self.lbl_nic_type):
            lbl.setStyleSheet("font-size: 11px; color: #475569; border: none; background: transparent;")
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
        l.addWidget(self.txt_diag_log, stretch=1)

        # Cập nhật thông số card mạng ban đầu
        self._update_nic_info()

        return tab

    def _update_nic_info(self):
        """Lấy thông số card mạng từ tầng nghiệp vụ."""
        try:
            if hasattr(self.agent, "get_active_nic_info"):
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
        grp_conn.setObjectName("grpConn")
        grp_conn.setStyleSheet("""
            QFrame#grpConn {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
        """)
        cl = QVBoxLayout(grp_conn)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(10)

        lbl_c_title = QLabel("Cài Đặt Kết Nối Máy Chủ Quản Trị (Admin Server):")
        lbl_c_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #0F172A; border: none; background: transparent;")
        cl.addWidget(lbl_c_title)

        f_row = QHBoxLayout()
        f_row.setSpacing(12)

        lbl_ip = QLabel("Địa chỉ IP Server:")
        lbl_ip.setStyleSheet("font-size: 12px; font-weight: 600; color: #475569; border: none; background: transparent;")
        self.txt_server_ip = QLineEdit(self.agent.server_host)

        lbl_port = QLabel("Cổng TCP:")
        lbl_port.setStyleSheet("font-size: 12px; font-weight: 600; color: #475569; border: none; background: transparent;")
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
        autostart_fn = getattr(self.agent, "is_windows_autostart", None)
        self.chk_autostart.setChecked(bool(autostart_fn()) if callable(autostart_fn) else False)
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
        grp_dev.setObjectName("grpDev")
        grp_dev.setStyleSheet("""
            QFrame#grpDev {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }
        """)
        dl = QVBoxLayout(grp_dev)
        dl.setContentsMargins(14, 12, 14, 12)
        dl.setSpacing(6)

        lbl_d_title = QLabel("Thông Tin Định Danh Thiết Bị (Device Specifications):")
        lbl_d_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #0F172A; border: none; background: transparent;")
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
            lbl_k.setStyleSheet("font-size: 11px; font-weight: bold; color: #64748B; min-width: 130px; border: none; background: transparent;")
            lbl_v = QLabel(val_text)
            lbl_v.setStyleSheet("font-size: 11px; color: #0F172A; border: none; background: transparent;")
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
        """Ủy quyền lưu thiết lập cấu hình và cập nhật kết nối sang tầng Application (ClientAgentService)."""
        new_host = self.txt_server_ip.text().strip()
        new_port = self.spin_port.value()
        if not new_host:
            QMessageBox.warning(self, "Lỗi", "Địa chỉ IP máy chủ không được để trống!")
            return

        enable_autostart = self.chk_autostart.isChecked()
        show_toast = self.chk_toast.isChecked()

        if hasattr(self.agent, "save_client_config"):
            self.agent.save_client_config(
                server_host=new_host,
                server_port=new_port,
                show_toast=show_toast,
                auto_start=enable_autostart,
                config_path=CONFIG_PATH,
            )
        else:
            if hasattr(self.agent, "set_windows_autostart"):
                self.agent.set_windows_autostart(enable_autostart)
            self.agent.show_toast_notifications = show_toast
            self.agent.update_server_target(new_host, new_port)

        QMessageBox.information(
            self, "Thành Công", f"Đã lưu cấu hình và đang kết nối lại tới {new_host}:{new_port}!"
        )

    # =========================================================================
    # CẬP NHẬT GIAO DIỆN ĐỊNH KỲ (REFRESH LOOP)
    # =========================================================================
    def _style_live_action_button(self, btn: QPushButton, app_name: str):
        """Cập nhật giao diện nút Bảo Vệ trên bảng tiến trình theo trạng thái chính sách 2 lớp."""
        pol = self.agent.get_policy_for_app(app_name) if hasattr(self.agent, "get_policy_for_app") else None
        if pol:
            is_admin = pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN")
            act = pol.get("action", "BLOCK")
            if is_admin:
                label = "🔒 Admin Chặn" if act == "BLOCK" else f"🔒 Admin QoS ({pol.get('limit_kbps', 0):.0f}K)"
                btn.setText(label)
                btn.setToolTip("Chính sách bắt buộc từ Admin Server - Không thể tự gỡ tại máy con")
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #FEF2F2;
                        color: #991B1B;
                        font-size: 11px;
                        font-weight: bold;
                        border: 1px solid #FCA5A5;
                        border-radius: 5px;
                        padding: 2px 8px;
                    }
                    QPushButton:hover { background-color: #FEE2E2; }
                """)
            else:
                label = "🔓 Bỏ Chặn (Local)" if act == "BLOCK" else "🔓 Gỡ QoS (Local)"
                btn.setText(label)
                btn.setToolTip("Chính sách tự vệ cục bộ - Nhấn để gỡ bỏ")
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #EFF6FF;
                        color: #1D4ED8;
                        font-size: 11px;
                        font-weight: bold;
                        border: 1px solid #93C5FD;
                        border-radius: 5px;
                        padding: 2px 8px;
                    }
                    QPushButton:hover { background-color: #DBEAFE; }
                """)
        else:
            btn.setText("🛡️ Tự Chặn")
            btn.setToolTip(f"Chặn cục bộ kết nối mạng của '{app_name}'")
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #F8FAFC;
                    color: #475569;
                    font-size: 11px;
                    font-weight: 600;
                    border: 1px solid #CBD5E1;
                    border-radius: 5px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #FEF2F2;
                    color: #DC2626;
                    border-color: #FECACA;
                }
            """)

    def _update_live_procs_table(self, top_procs: list):
        """Cập nhật bảng tiến trình Tab 1 giữ nguyên vị trí dòng để không bị nhảy loạn khi bấm nút."""
        proc_map: Dict[str, Any] = {}
        for p in top_procs:
            name = str(getattr(p, "name", "Unknown"))
            if name and name not in proc_map:
                proc_map[name] = p

        # Cập nhật các dòng đã có
        for row, name in enumerate(self._live_row_order):
            p = proc_map.get(name)
            down_val = getattr(p, "down_kbps", 0.0) if p else 0.0
            up_val = getattr(p, "up_kbps", 0.0) if p else 0.0
            pid_val = getattr(p, "pid", None) if p else None

            if pid_val is not None:
                it_pid = self.tbl_live_procs.item(row, 1)
                if it_pid:
                    it_pid.setText(str(pid_val))

            it_down = self.tbl_live_procs.item(row, 2)
            if it_down:
                it_down.setText(format_speed(down_val))

            it_up = self.tbl_live_procs.item(row, 3)
            if it_up:
                it_up.setText(format_speed(up_val))

            btn = self._live_action_buttons.get(name)
            if btn:
                self._style_live_action_button(btn, name)

        # Thêm dòng mới vào cuối bảng nếu chưa có
        for name, p in proc_map.items():
            if name not in self._live_row_map:
                row = self.tbl_live_procs.rowCount()
                self.tbl_live_procs.insertRow(row)
                self._live_row_order.append(name)
                self._live_row_map[name] = row

                item_name = QTableWidgetItem(name)
                item_name.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_pid = QTableWidgetItem(str(getattr(p, "pid", 0)))
                item_pid.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                item_down = QTableWidgetItem(format_speed(getattr(p, "down_kbps", 0.0)))
                item_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                item_up = QTableWidgetItem(format_speed(getattr(p, "up_kbps", 0.0)))
                item_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                self.tbl_live_procs.setItem(row, 0, item_name)
                self.tbl_live_procs.setItem(row, 1, item_pid)
                self.tbl_live_procs.setItem(row, 2, item_down)
                self.tbl_live_procs.setItem(row, 3, item_up)

                btn_act = QPushButton()
                btn_act.setFixedHeight(24)
                btn_act.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                self._style_live_action_button(btn_act, name)
                btn_act.clicked.connect(lambda _checked=False, n=name: self._on_live_proc_action_clicked(n))

                box = QWidget()
                lay = QHBoxLayout(box)
                lay.setContentsMargins(4, 2, 4, 2)
                lay.addWidget(btn_act)
                self.tbl_live_procs.setCellWidget(row, 4, box)
                self._live_action_buttons[name] = btn_act

    def _refresh_policies_table(self):
        """Cập nhật bảng chính sách Tab 2 (chỉ vẽ lại khi danh sách chính sách thực sự thay đổi)."""
        policies = list(self.agent.active_policies.values())
        snapshot = [
            (
                pol.get("app", ""),
                pol.get("source", "ADMIN"),
                bool(pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN")),
                pol.get("action", ""),
                float(pol.get("limit_kbps", 0.0)),
                pol.get("direction", "both"),
                int(pol.get("time", 0)),
            )
            for pol in policies
        ]
        if snapshot == self._last_policies_snapshot:
            return
        self._last_policies_snapshot = snapshot

        self.tbl_policies.setRowCount(len(policies))
        for r, pol in enumerate(policies):
            app_name = pol.get("app", "")
            is_admin = pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN")
            act = pol.get("action", "")
            act_label = "CHẶN TOÀN BỘ (BLOCK)" if act == "BLOCK" else "BÓP BĂNG THÔNG (QoS)"
            limit_str = f"{pol.get('limit_kbps', 0):.1f} KB/s" if act == "THROTTLE" else "0 KB/s"
            t_str = time.strftime("%H:%M:%S", time.localtime(pol.get("time", time.time())))

            it_app = QTableWidgetItem(app_name)
            it_app.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))

            src_text = "🔒 Admin Server" if is_admin else "🔓 Cục Bộ (Máy Con)"
            it_src = QTableWidgetItem(src_text)
            it_src.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            it_src.setForeground(QtGui.QBrush(QtGui.QColor("#DC2626" if is_admin else "#2563EB")))

            self.tbl_policies.setItem(r, 0, it_app)
            self.tbl_policies.setItem(r, 1, it_src)
            self.tbl_policies.setItem(r, 2, QTableWidgetItem(act_label))
            self.tbl_policies.setItem(r, 3, QTableWidgetItem(limit_str))
            self.tbl_policies.setItem(r, 4, QTableWidgetItem(t_str))

            btn_op = QPushButton()
            btn_op.setFixedHeight(24)
            btn_op.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            if is_admin:
                btn_op.setText("🔒 Khóa Bởi Admin")
                btn_op.setStyleSheet("""
                    QPushButton {
                        background-color: #FEF2F2;
                        color: #991B1B;
                        font-size: 11px;
                        font-weight: bold;
                        border: 1px solid #FCA5A5;
                        border-radius: 5px;
                        padding: 2px 8px;
                    }
                    QPushButton:hover { background-color: #FEE2E2; }
                """)
            else:
                btn_op.setText("🔓 Gỡ Bỏ")
                btn_op.setStyleSheet("""
                    QPushButton {
                        background-color: #EFF6FF;
                        color: #1D4ED8;
                        font-size: 11px;
                        font-weight: bold;
                        border: 1px solid #93C5FD;
                        border-radius: 5px;
                        padding: 2px 8px;
                    }
                    QPushButton:hover { background-color: #DBEAFE; }
                """)
            btn_op.clicked.connect(lambda _checked=False, n=app_name: self._execute_local_unblock(n))

            box_op = QWidget()
            lay_op = QHBoxLayout(box_op)
            lay_op.setContentsMargins(4, 2, 4, 2)
            lay_op.addWidget(btn_op)
            self.tbl_policies.setCellWidget(r, 5, box_op)

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
        down_kb = getattr(self.agent, "latest_down_kbps", 0.0)
        up_kb = getattr(self.agent, "latest_up_kbps", 0.0)
        self.card_down.lbl_val.setText(format_speed(down_kb))
        self.card_up.lbl_val.setText(format_speed(up_kb))
        self.sparkline.add_sample(down_kb, up_kb)

        cpu_val = getattr(self.agent, "latest_cpu_pct", 0.0)
        ram_val = getattr(self.agent, "latest_ram_pct", 0.0)
        self.lbl_cpu.setText(f"{cpu_val:.1f}%")
        self.bar_cpu.setValue(int(cpu_val))

        self.lbl_ram.setText(f"{ram_val:.1f}%")
        self.bar_ram.setValue(int(ram_val))

        self.lbl_total_down.setText(f"Tổng tải xuống: {format_bytes(self.agent.total_rx_bytes)}")
        self.lbl_total_up.setText(f"Tổng tải lên: {format_bytes(self.agent.total_tx_bytes)}")

        # Bảng top tiến trình mạng (Cố định dòng + hỗ trợ nút Tự vệ cục bộ)
        top_procs = getattr(self.agent, "latest_top_procs", [])
        self._update_live_procs_table(top_procs)

        # 3. Cập nhật Tab 2: Chính sách 2 lớp & Nhật ký kiểm toán
        self._refresh_policies_table()

        # Bảng Nhật ký kiểm toán (Audit Logs)
        audit_fn = getattr(self.agent, "get_audit_logs", None)
        audit_logs = audit_fn() if callable(audit_fn) else []
        self.tbl_audit.setRowCount(min(len(audit_logs), 50))
        for r, entry in enumerate(audit_logs[:50]):
            self.tbl_audit.setItem(r, 0, QTableWidgetItem(entry.get("time_str", "")))
            self.tbl_audit.setItem(r, 1, QTableWidgetItem(entry.get("source", "")))
            self.tbl_audit.setItem(r, 2, QTableWidgetItem(entry.get("action", "")))
            self.tbl_audit.setItem(r, 3, QTableWidgetItem(entry.get("target", "")))
            self.tbl_audit.setItem(r, 4, QTableWidgetItem(entry.get("details", "")))

        # 4. Cập nhật Tab 3: Thẻ độ trễ (đo bất đồng bộ trên luồng nền, không block GUI)
        if self.tab_widget.currentIndex() == 2 and hasattr(self.agent, "check_server_latency"):
            if hasattr(self.agent, "trigger_async_latency_check"):
                self.agent.trigger_async_latency_check()
                srv_ok = getattr(self.agent, "cached_server_ok", False)
                srv_lat = getattr(self.agent, "cached_server_latency", None)
                net_ok = getattr(self.agent, "cached_internet_ok", False)
                net_lat = getattr(self.agent, "cached_internet_latency", None)
            else:
                srv_lat, srv_ok, _ = self.agent.check_server_latency()
                net_lat, net_ok, _ = self.agent.check_internet_latency()

            if srv_ok and srv_lat is not None:
                self.lbl_srv_lat_val.setText(f"{srv_lat:.1f} ms")
            else:
                self.lbl_srv_lat_val.setText("-- ms")

            if net_ok and net_lat is not None:
                self.lbl_net_lat_val.setText(f"{net_lat:.1f} ms")
            else:
                self.lbl_net_lat_val.setText("-- ms")
