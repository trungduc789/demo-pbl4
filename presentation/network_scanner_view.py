"""
Giao diện Quét Kết nối Mạng (Network Scanner View - Presentation Layer):
- Hiển thị danh sách kết nối Socket TCP theo thời gian thực (PID, Tiến trình, Giao thức, Địa chỉ Local/Remote, Trạng thái).
- Tuân thủ kiến trúc 3 lớp: nhận dữ liệu đã làm giàu (enriched) từ tầng Application.
"""

from typing import Callable, Dict, List, Optional
from PyQt6 import QtWidgets, QtCore, QtGui
from utils.safe_print import safe_print as print


class NetworkScannerView(QtWidgets.QWidget):
    """Màn hình Network Scanner hiển thị bảng kết nối Socket TCP."""

    def __init__(
        self,
        fetch_connections: Optional[Callable[[], List[Dict]]] = None,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self._fetch_connections = fetch_connections
        self._init_ui()

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("🕸️ Network Scanner - Danh Sách Kết Nối Socket TCP")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #0F172A;")
        header.addWidget(title)

        header.addStretch()
        self.btn_refresh = QtWidgets.QPushButton("🔄 Làm Mới Ngay")
        self.btn_refresh.clicked.connect(self.refresh_connections)
        header.addWidget(self.btn_refresh)
        layout.addLayout(header)

        self.conn_table = QtWidgets.QTableWidget()
        self.conn_table.setColumnCount(6)
        self.conn_table.setHorizontalHeaderLabels(
            ["PID", "Tiến Trình", "Giao Thức", "Địa Chỉ Local", "Địa Chỉ Remote", "Trạng Thái"]
        )
        self.conn_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.conn_table.verticalHeader().setVisible(False)
        layout.addWidget(self.conn_table)

    def refresh_connections(self):
        """Làm mới danh sách kết nối TCP thông qua callback tầng Application."""
        try:
            conns = self._fetch_connections() if self._fetch_connections else []
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
