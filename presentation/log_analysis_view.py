"""
Giao diện Phân tích Nhật ký Hoạt động & Chính sách Mạng (Log Analysis View - Presentation Layer):
- Hiển thị bảng lịch sử thực thi chính sách an ninh (policies.log).
- Hiển thị nhật ký kết nối socket (connections.log).
- Hỗ trợ lọc nhanh theo từ khóa và mở thư mục nhật ký.
- Tuân thủ kiến trúc 3 lớp: chỉ nhận dữ liệu đã phân tích từ tầng Application.
"""

from typing import Callable, Dict, List, Optional
from PyQt6 import QtWidgets, QtCore, QtGui
from utils.safe_print import safe_print as print


class LogAnalysisView(QtWidgets.QWidget):
    """Màn hình phân tích nhật ký bảo mật và kết nối mạng."""

    def __init__(
        self,
        fetch_policy_logs: Optional[Callable[[int], List[Dict]]] = None,
        fetch_connection_logs: Optional[Callable[[int], str]] = None,
        get_logs_dir: Optional[Callable[[], str]] = None,
        open_logs_folder_cb: Optional[Callable[[], bool]] = None,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self._fetch_policy_logs = fetch_policy_logs
        self._fetch_connection_logs = fetch_connection_logs
        self._get_logs_dir = get_logs_dir
        self._open_logs_folder_cb = open_logs_folder_cb
        self.raw_policy_logs: List[Dict] = []
        self._init_ui()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 1. Header Toolbar
        header_layout = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("📜 Nhật Ký Hoạt Động & Chính Sách Mạng (Log Analysis)")
        title.setStyleSheet("font-size: 16px; font-weight: 800; color: #0F172A; border: none; background: transparent;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self.btn_open_folder = QtWidgets.QPushButton("📂 Mở Thư Mục Logs")
        self.btn_open_folder.setFixedHeight(28)
        self.btn_open_folder.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.setStyleSheet(
            "background-color: #FFFFFF; color: #475569; font-weight: 600; font-size: 11px; "
            "padding: 2px 12px; border: 1px solid #CBD5E1; border-radius: 5px;"
        )
        self.btn_open_folder.clicked.connect(self.open_logs_folder)
        header_layout.addWidget(self.btn_open_folder)

        self.btn_refresh = QtWidgets.QPushButton("🔄 Làm Mới")
        self.btn_refresh.setFixedHeight(28)
        self.btn_refresh.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setStyleSheet(
            "background-color: #2563EB; color: #FFFFFF; font-weight: bold; font-size: 11px; "
            "padding: 2px 14px; border-radius: 5px;"
        )
        self.btn_refresh.clicked.connect(self.refresh_logs)
        header_layout.addWidget(self.btn_refresh)

        layout.addLayout(header_layout)

        # 2. Subtitle / info banner
        info_label = QtWidgets.QLabel(
            "Hệ thống ghi nhận toàn bộ thao tác bảo mật (chặn, bỏ chặn, bóp băng thông) "
            "và kết nối mạng trực tiếp vào file log cục bộ (logs/policies.log & logs/connections.log)."
        )
        info_label.setStyleSheet("font-size: 11px; color: #64748B; background: transparent;")
        layout.addWidget(info_label)

        # 3. Filter Box
        filter_layout = QtWidgets.QHBoxLayout()
        self.txt_policy_filter = QtWidgets.QLineEdit()
        self.txt_policy_filter.setPlaceholderText(
            "🔍 Tìm kiếm theo tiến trình, hành động (BLOCK, UNBLOCK, APPLY, REMOVE)..."
        )
        self.txt_policy_filter.setFixedHeight(28)
        self.txt_policy_filter.setStyleSheet(
            "font-size: 12px; padding: 2px 8px; border: 1px solid #CBD5E1; border-radius: 5px;"
        )
        self.txt_policy_filter.textChanged.connect(self.filter_policy_logs)
        filter_layout.addWidget(self.txt_policy_filter)
        layout.addLayout(filter_layout)

        # 4. Tabs: Policies Log & Connections Log
        self.log_tabs = QtWidgets.QTabWidget()
        self.log_tabs.setStyleSheet("""
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

        # Tab 1: Policies Table
        tab_pol = QtWidgets.QWidget()
        pol_layout = QtWidgets.QVBoxLayout(tab_pol)
        pol_layout.setContentsMargins(8, 8, 8, 8)

        self.policy_log_table = QtWidgets.QTableWidget()
        self.policy_log_table.setColumnCount(4)
        self.policy_log_table.setHorizontalHeaderLabels(
            ["Thời Gian", "Hành Động", "Tiến Trình & Chi Tiết", "Trạng Thái"]
        )
        self.policy_log_table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.policy_log_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.policy_log_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.policy_log_table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.policy_log_table.verticalHeader().setVisible(False)
        pol_layout.addWidget(self.policy_log_table)
        self.log_tabs.addTab(tab_pol, "🛡️ Nhật Ký Chính Sách (policies.log)")

        # Tab 2: Connections Raw Log
        tab_conn = QtWidgets.QWidget()
        conn_layout = QtWidgets.QVBoxLayout(tab_conn)
        conn_layout.setContentsMargins(8, 8, 8, 8)
        self.txt_conn_log = QtWidgets.QPlainTextEdit()
        self.txt_conn_log.setReadOnly(True)
        self.txt_conn_log.setStyleSheet(
            "font-family: 'Consolas', 'Courier New', monospace; font-size: 11px; "
            "background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px; color: #1E293B;"
        )
        conn_layout.addWidget(self.txt_conn_log)
        self.log_tabs.addTab(tab_conn, "🌐 Nhật Ký Kết Nối Mạng (connections.log)")

        layout.addWidget(self.log_tabs)

    def open_logs_folder(self):
        """Ủy quyền mở thư mục logs xuống tầng Application."""
        try:
            if self._open_logs_folder_cb:
                self._open_logs_folder_cb()
        except Exception as e:
            print(f"[UI] Không thể mở thư mục logs: {e}")

    def refresh_logs(self):
        """Tải dữ liệu nhật ký mới nhất thông qua callback tầng Application."""
        try:
            if self._fetch_policy_logs:
                self.raw_policy_logs = self._fetch_policy_logs(200)
            else:
                self.raw_policy_logs = []
            self.render_policy_table(self.raw_policy_logs)

            if self._fetch_connection_logs:
                conn_text = self._fetch_connection_logs(200)
                self.txt_conn_log.setPlainText(conn_text)
            else:
                self.txt_conn_log.setPlainText("Chưa có nhật ký kết nối nào được ghi nhận.")
        except Exception as e:
            print(f"[UI] Lỗi khi tải nhật ký: {e}")

    def render_policy_table(self, logs: List[Dict]):
        """Hiển thị danh sách log chính sách đã phân tích lên bảng."""
        self.policy_log_table.setRowCount(len(logs))
        for row_idx, item in enumerate(logs):
            ts = item.get("timestamp", "")
            action = item.get("action", "POLICY")
            details = item.get("details", item.get("raw", ""))
            status = item.get("status", "SUCCESS")

            ts_item = QtWidgets.QTableWidgetItem(ts)
            ts_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            act_item = QtWidgets.QTableWidgetItem(action)
            act_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            act_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            if "BLOCK" in action:
                act_item.setForeground(QtGui.QBrush(QtGui.QColor("#DC2626")))
            elif "UNBLOCK" in action or "REMOVE" in action:
                act_item.setForeground(QtGui.QBrush(QtGui.QColor("#16A34A")))
            else:
                act_item.setForeground(QtGui.QBrush(QtGui.QColor("#2563EB")))

            det_item = QtWidgets.QTableWidgetItem(details)
            status_item = QtWidgets.QTableWidgetItem(status)
            status_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            if "SUCCESS" in status:
                status_item.setForeground(QtGui.QBrush(QtGui.QColor("#16A34A")))

            self.policy_log_table.setItem(row_idx, 0, ts_item)
            self.policy_log_table.setItem(row_idx, 1, act_item)
            self.policy_log_table.setItem(row_idx, 2, det_item)
            self.policy_log_table.setItem(row_idx, 3, status_item)

    def filter_policy_logs(self, query: str):
        query = query.strip().lower()
        if not self.raw_policy_logs:
            return
        if not query:
            self.render_policy_table(self.raw_policy_logs)
            return
        filtered = [item for item in self.raw_policy_logs if query in item.get("raw", "").lower()]
        self.render_policy_table(filtered)
