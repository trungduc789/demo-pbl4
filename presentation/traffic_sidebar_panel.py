"""
Bảng Danh sách Ứng dụng & Bộ lọc Bên trái của trang Traffic Monitor (TrafficSidebarPanel):
- Quản lý bộ lọc chiều lưu lượng (Incoming & Outgoing / Incoming only / Outgoing only).
- Quản lý bộ lọc phạm vi mạng (WAN & LAN / WAN only / LAN only).
- Ô tìm kiếm nhanh tiến trình và bảng danh sách ứng dụng thời gian thực.
- Giao tiếp với trang cha hoàn toàn qua Qt Signals.
"""

from typing import Callable, Dict, List, Optional
from PyQt6 import QtWidgets, QtCore, QtGui
from application.traffic_analytics import format_bytes, get_scope_factor


class TrafficSidebarPanel(QtWidgets.QWidget):
    """Panel bên trái hiển thị danh sách ứng dụng và bộ lọc của trang Traffic Monitor."""

    filter_direction_changed = QtCore.pyqtSignal(int)
    filter_scope_changed = QtCore.pyqtSignal(int)
    search_changed = QtCore.pyqtSignal()
    close_requested = QtCore.pyqtSignal()
    app_selected = QtCore.pyqtSignal(str)
    app_double_clicked = QtCore.pyqtSignal(str)

    def __init__(self, icon_manager, resolve_exe_cb: Optional[Callable[[int], str]] = None, parent=None):
        super().__init__(parent)
        self.icon_manager = icon_manager
        self._resolve_exe_cb = resolve_exe_cb
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet("background-color: #FFFFFF;")
        left_layout = QtWidgets.QVBoxLayout(self)
        left_layout.setContentsMargins(6, 4, 6, 4)
        left_layout.setSpacing(6)

        filter_bar = QtWidgets.QHBoxLayout()
        filter_bar.setContentsMargins(0, 2, 0, 2)
        filter_bar.setSpacing(6)

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
        self.combo_filter_dir.currentIndexChanged.connect(self.filter_direction_changed.emit)
        filter_bar.addWidget(self.combo_filter_dir)

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
        self.combo_filter_scope.currentIndexChanged.connect(self.filter_scope_changed.emit)
        filter_bar.addWidget(self.combo_filter_scope)

        filter_bar.addStretch()

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
        self.search_app_input.textChanged.connect(lambda _t: self.search_changed.emit())
        filter_bar.addWidget(self.search_app_input)

        self.btn_close_panel = QtWidgets.QPushButton("✕")
        self.btn_close_panel.setFixedSize(24, 24)
        self.btn_close_panel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_close_panel.setStyleSheet("""
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
        self.btn_close_panel.clicked.connect(self.close_requested.emit)
        filter_bar.addWidget(self.btn_close_panel)

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
        self.app_list_table.itemSelectionChanged.connect(self._handle_selection_changed)
        self.app_list_table.doubleClicked.connect(self._handle_double_clicked)
        left_layout.addWidget(self.app_list_table)

        self.setMinimumWidth(260)
        self.setMaximumWidth(380)
        self.setVisible(False)

    def _handle_selection_changed(self):
        selected_rows = self.app_list_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row == 0:
            app_name = "All apps"
        else:
            item = self.app_list_table.item(row, 0)
            app_name = item.text().strip() if item else "All apps"
        self.app_selected.emit(app_name)

    def _handle_double_clicked(self, item):
        row = item.row()
        if row > 0:
            cell = self.app_list_table.item(row, 0)
            if cell:
                self.app_double_clicked.emit(cell.text().strip())

    def update_sidebar_table(
        self,
        proc_speeds: list,
        win_down: float,
        win_up: float,
        win_app_bytes: Dict[str, Dict[str, float]],
        app_metadata: Dict[str, Dict],
        filter_direction: str,
        filter_scope: str,
        selected_app_name: str,
    ):
        """Render bảng danh sách ứng dụng theo bộ lọc chiều và phạm vi mạng."""
        search_kw = self.search_app_input.text().strip().lower()
        current_speeds = {p.get("name", ""): p for p in proc_speeds if p.get("name")}
        scope_factor = get_scope_factor(filter_scope)

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

            meta = app_metadata.get(app_name, {})
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
                "cur_speed": cur_spd,
            })

        if filter_direction == "incoming":
            display_apps.sort(key=lambda x: (x["is_active_now"], x["down_bytes"]), reverse=True)
        elif filter_direction == "outgoing":
            display_apps.sort(key=lambda x: (x["is_active_now"], x["up_bytes"]), reverse=True)
        else:
            display_apps.sort(key=lambda x: (x["is_active_now"], x["total_bytes"]), reverse=True)

        self.app_list_table.blockSignals(True)
        try:
            self.app_list_table.setRowCount(len(display_apps) + 1)

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

            total_win_down = win_down * scope_factor
            total_win_up = win_up * scope_factor

            if filter_direction == "incoming":
                item_all_down = self._make_cell(format_bytes(total_win_down), "#1E293B", bold=True)
                item_all_up = self._make_cell("--", "#94A3B8", bold=True)
            elif filter_direction == "outgoing":
                item_all_down = self._make_cell("--", "#94A3B8", bold=True)
                item_all_up = self._make_cell(format_bytes(total_win_up), "#1E293B", bold=True)
            else:
                item_all_down = self._make_cell(format_bytes(total_win_down), "#1E293B", bold=True)
                item_all_up = self._make_cell(format_bytes(total_win_up), "#1E293B", bold=True)

            self.app_list_table.setItem(0, 0, item_all_name)
            self.app_list_table.setItem(0, 1, item_all_down)
            self.app_list_table.setItem(0, 2, item_all_up)

            selected_row_idx = 0 if selected_app_name == "All apps" else -1

            for idx, item in enumerate(display_apps, start=1):
                name = item["name"]
                name_item = QtWidgets.QTableWidgetItem(f"  {name}")
                name_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))

                exe_path = item.get("exe", "")
                if not exe_path and item.get("pid", 0) > 0 and self._resolve_exe_cb:
                    exe_path = self._resolve_exe_cb(item["pid"])
                icon = self.icon_manager.get_icon(exe_path, name)
                name_item.setIcon(icon)

                if filter_direction == "incoming":
                    d_item = self._make_cell(format_bytes(item["down_bytes"]), "#1E293B", bold=True)
                    u_item = self._make_cell("--", "#94A3B8", bold=False)
                elif filter_direction == "outgoing":
                    d_item = self._make_cell("--", "#94A3B8", bold=False)
                    u_item = self._make_cell(format_bytes(item["up_bytes"]), "#1E293B", bold=True)
                else:
                    d_item = self._make_cell(format_bytes(item["down_bytes"]), "#475569", bold=False)
                    u_item = self._make_cell(format_bytes(item["up_bytes"]), "#475569", bold=False)

                self.app_list_table.setItem(idx, 0, name_item)
                self.app_list_table.setItem(idx, 1, d_item)
                self.app_list_table.setItem(idx, 2, u_item)

                if name == selected_app_name:
                    selected_row_idx = idx

            if selected_row_idx >= 0:
                self.app_list_table.selectRow(selected_row_idx)
        finally:
            self.app_list_table.blockSignals(False)

    @staticmethod
    def _make_cell(text: str, color_hex: str, bold: bool = False) -> QtWidgets.QTableWidgetItem:
        cell = QtWidgets.QTableWidgetItem(text)
        cell.setForeground(QtGui.QBrush(QtGui.QColor(color_hex)))
        if bold:
            cell.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return cell
