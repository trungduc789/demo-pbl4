"""
Giao diện Bảng Quản trị Đội ngũ Máy con (Admin Fleet View - Presentation Layer):
- Hiển thị các thẻ chỉ số tổng quan toàn mạng (HUD Cards): Tổng máy, Online/Offline, Băng thông tổng.
- Bảng danh sách các máy con theo thời gian thực: Trạng thái, IP, OS, Tốc độ Upload/Download, Chính sách đang áp dụng.
- Tìm kiếm nhanh và lọc theo trạng thái.
- Nút bấm xem chi tiết máy con (Drill-down Remote View) để can thiệp Firewall & QoS.
"""

from typing import Dict, List, Optional, Any
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QAbstractItemView
)


class AdminFleetView(QWidget):
    """Màn hình chính của Admin: Quản lý danh sách toàn bộ các máy trạm trong mạng."""

    # Tín hiệu khi Admin chọn 1 máy để can thiệp: phát ra (device_uuid)
    machine_selected = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_devices: List[Any] = []
        self._current_summary: Dict[str, Any] = {}
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(14)
        self.setStyleSheet("background-color: #F8FAFC;")

        # 1. Header Toolbar
        header_layout = QHBoxLayout()
        header_left = QVBoxLayout()
        header_left.setSpacing(2)

        title = QLabel("🖥️ Quản Trị Hệ Thống Mạng (Fleet Management)")
        title.setStyleSheet("font-size: 19px; font-weight: 800; color: #0F172A;")
        sub_title = QLabel("Giám sát thông số thời gian thực, lưu lượng mạng và can thiệp bảo mật từ xa cho tất cả các máy con")
        sub_title.setStyleSheet("font-size: 12px; color: #64748B;")
        header_left.addWidget(title)
        header_left.addWidget(sub_title)
        header_layout.addLayout(header_left)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # 2. Thẻ chỉ số tổng quan toàn mạng (HUD Cards)
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(12)

        self.card_total = self._create_hud_card("🖥️ TỔNG SỐ MÁY", "0 máy", "#2563EB", "#EFF6FF")
        self.card_status = self._create_hud_card("🟢 TRỰC TUYẾN", "0 Online / 0 Offline", "#10B981", "#ECFDF5")
        self.card_down = self._create_hud_card("⬇️ TỔNG TẢI XUỐNG", "0 B/s", "#D97706", "#FFFBEB")
        self.card_up = self._create_hud_card("⬆️ TỔNG TẢI LÊN", "0 B/s", "#E11D48", "#FFF1F2")

        cards_layout.addWidget(self.card_total)
        cards_layout.addWidget(self.card_status)
        cards_layout.addWidget(self.card_down)
        cards_layout.addWidget(self.card_up)
        layout.addLayout(cards_layout)

        # 3. Thanh tìm kiếm và bộ lọc (Search & Filter Bar)
        filter_bar = QFrame()
        filter_bar.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                padding: 4px 10px;
            }
        """)
        fb_layout = QHBoxLayout(filter_bar)
        fb_layout.setContentsMargins(8, 4, 8, 4)
        fb_layout.setSpacing(10)

        lbl_search = QLabel("🔍 Tìm máy:")
        lbl_search.setStyleSheet("font-weight: bold; font-size: 12px; color: #475569;")
        fb_layout.addWidget(lbl_search)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Nhập tên máy (Hostname), IP hoặc UUID...")
        self.txt_search.setFixedHeight(30)
        self.txt_search.setStyleSheet("""
            QLineEdit {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 2px 10px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: #2563EB;
                background-color: #FFFFFF;
            }
        """)
        self.txt_search.textChanged.connect(self._apply_filter)
        fb_layout.addWidget(self.txt_search, stretch=1)

        lbl_filter = QLabel("Trạng thái:")
        lbl_filter.setStyleSheet("font-weight: bold; font-size: 12px; color: #475569;")
        fb_layout.addWidget(lbl_filter)

        self.cbo_status_filter = QComboBox()
        self.cbo_status_filter.addItems(["Tất cả máy", "Chỉ máy Online", "Chỉ máy Offline"])
        self.cbo_status_filter.setFixedHeight(30)
        self.cbo_status_filter.setStyleSheet("""
            QComboBox {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 2px 10px;
                font-size: 12px;
            }
        """)
        self.cbo_status_filter.currentIndexChanged.connect(self._apply_filter)
        fb_layout.addWidget(self.cbo_status_filter)

        layout.addWidget(filter_bar)

        # 4. Bảng danh sách máy con
        self.tbl_devices = QTableWidget()
        self.tbl_devices.setColumnCount(8)
        self.tbl_devices.setHorizontalHeaderLabels([
            "Trạng Thái", "Tên Máy & UUID", "Địa Chỉ IP", "Hệ Điều Hành",
            "Tải Xuống ↓", "Tải Lên ↑", "Chính Sách Áp Dụng", "Hành Động"
        ])
        self.tbl_devices.verticalHeader().setVisible(False)
        self.tbl_devices.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_devices.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_devices.setShowGrid(False)
        self.tbl_devices.setStyleSheet("""
            QTableWidget {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                gridline-color: transparent;
                font-size: 12px;
            }
            QHeaderView::section {
                background-color: #F1F5F9;
                color: #475569;
                font-weight: bold;
                font-size: 11px;
                padding: 8px;
                border: none;
                border-bottom: 1px solid #E2E8F0;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #F8FAFC;
            }
            QTableWidget::item:selected {
                background-color: #EFF6FF;
                color: #1E293B;
            }
        """)

        # Cấu hình kích thước các cột
        header = self.tbl_devices.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        self.tbl_devices.cellDoubleClicked.connect(self._on_table_row_double_clicked)
        layout.addWidget(self.tbl_devices, stretch=1)

    def _create_hud_card(self, title: str, value: str, accent_color: str, bg_color: str) -> QFrame:
        """Tạo thẻ hiển thị chỉ số HUD phong cách GlassWire."""
        card = QFrame()
        card.setFixedHeight(74)
        card.setStyleSheet(f"""
            QFrame {{
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid {accent_color};
                border-radius: 8px;
            }}
        """)
        l = QVBoxLayout(card)
        l.setContentsMargins(14, 8, 14, 8)
        l.setSpacing(2)

        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size: 10px; font-weight: 800; color: #64748B;")
        lbl_v = QLabel(value)
        lbl_v.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {accent_color};")
        card.lbl_val = lbl_v  # Gắn thuộc tính để cập nhật sau

        l.addWidget(lbl_t)
        l.addWidget(lbl_v)
        return card

    @staticmethod
    def _format_speed(speed_kbps: float) -> str:
        """Định dạng tốc độ mạng KB/s hoặc MB/s dễ đọc."""
        if speed_kbps >= 1024.0:
            return f"{speed_kbps / 1024.0:,.1f} MB/s"
        elif speed_kbps >= 1.0:
            return f"{speed_kbps:,.1f} KB/s"
        elif speed_kbps > 0:
            return f"{speed_kbps * 1024.0:,.0f} B/s"
        return "0 B/s"

    def update_fleet_data(self, devices: List[Any], summary: Dict[str, Any]):
        """Cập nhật dữ liệu toàn bộ danh sách máy và thẻ HUD."""
        self._raw_devices = devices
        self._current_summary = summary

        # 1. Cập nhật thẻ chỉ số HUD
        tot = summary.get("total_devices", len(devices))
        onl = summary.get("online_devices", sum(1 for d in devices if getattr(d, 'status', '') == 'online'))
        offl = summary.get("offline_devices", tot - onl)
        tot_down = summary.get("total_down_kbps", 0.0)
        tot_up = summary.get("total_up_kbps", 0.0)

        self.card_total.lbl_val.setText(f"{tot} máy")
        self.card_status.lbl_val.setText(f"{onl} Online  /  {offl} Offline")
        self.card_down.lbl_val.setText(f"{self._format_speed(tot_down)} ↓")
        self.card_up.lbl_val.setText(f"{self._format_speed(tot_up)} ↑")

        # 2. Lọc và nạp dữ liệu vào bảng
        self._apply_filter()

    def _apply_filter(self):
        """Lọc danh sách máy dựa trên từ khóa tìm kiếm và bộ lọc trạng thái."""
        search_kw = self.txt_search.text().strip().lower()
        status_mode = self.cbo_status_filter.currentIndex()  # 0: All, 1: Online, 2: Offline

        filtered = []
        for d in self._raw_devices:
            st = getattr(d, 'status', 'offline')
            if status_mode == 1 and st != 'online':
                continue
            if status_mode == 2 and st != 'offline':
                continue

            hname = getattr(d, 'hostname', '').lower()
            uuid_val = getattr(d, 'device_uuid', '').lower()
            ip = getattr(d, 'ip_address', '').lower()

            if search_kw:
                if (search_kw not in hname) and (search_kw not in uuid_val) and (search_kw not in ip):
                    continue

            filtered.append(d)

        self._render_table(filtered)

    def _render_table(self, devices: List[Any]):
        """Render danh sách máy con vào bảng giao diện."""
        self.tbl_devices.setRowCount(len(devices))

        for row, dev in enumerate(devices):
            uuid_val = getattr(dev, 'device_uuid', '')
            status = getattr(dev, 'status', 'offline')
            hostname = getattr(dev, 'hostname', 'Unknown')
            ip = getattr(dev, 'ip_address', '127.0.0.1')
            os_info = getattr(dev, 'os_info', 'Windows')
            down_kbps = getattr(dev, 'current_down_kbps', 0.0)
            up_kbps = getattr(dev, 'current_up_kbps', 0.0)
            applied = getattr(dev, 'applied_policies', {})

            # Cột 0: Trạng thái Badge
            status_item = QTableWidgetItem(f" {'● ONLINE' if status == 'online' else '○ OFFLINE'} ")
            status_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            if status == "online":
                status_item.setForeground(QtGui.QColor("#059669"))
                status_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                status_item.setForeground(QtGui.QColor("#94A3B8"))
                status_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Normal))
            self.tbl_devices.setItem(row, 0, status_item)

            # Cột 1: Tên máy & UUID
            short_uuid = uuid_val[:8] + "..." if len(uuid_val) > 8 else uuid_val
            name_item = QTableWidgetItem(f"💻  {hostname}  ({short_uuid})")
            name_item.setFont(QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.Bold))
            name_item.setData(QtCore.Qt.ItemDataRole.UserRole, uuid_val)
            self.tbl_devices.setItem(row, 1, name_item)

            # Cột 2: IP
            ip_item = QTableWidgetItem(ip or "N/A")
            ip_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            ip_item.setForeground(QtGui.QColor("#334155"))
            self.tbl_devices.setItem(row, 2, ip_item)

            # Cột 3: OS
            os_item = QTableWidgetItem(os_info)
            os_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            os_item.setForeground(QtGui.QColor("#64748B"))
            self.tbl_devices.setItem(row, 3, os_item)

            # Cột 4: Tải xuống ↓
            down_str = f" {self._format_speed(down_kbps)} ↓ "
            down_item = QTableWidgetItem(down_str)
            down_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            if status == "online" and down_kbps > 10.0:
                down_item.setForeground(QtGui.QColor("#D97706"))
                down_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                down_item.setForeground(QtGui.QColor("#94A3B8"))
            self.tbl_devices.setItem(row, 4, down_item)

            # Cột 5: Tải lên ↑
            up_str = f" {self._format_speed(up_kbps)} ↑ "
            up_item = QTableWidgetItem(up_str)
            up_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            if status == "online" and up_kbps > 10.0:
                up_item.setForeground(QtGui.QColor("#E11D48"))
                up_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                up_item.setForeground(QtGui.QColor("#94A3B8"))
            self.tbl_devices.setItem(row, 5, up_item)

            # Cột 6: Chính sách an ninh đang áp dụng
            if applied:
                num_block = sum(1 for p in applied.values() if p.get('action') == 'BLOCK_APP')
                num_throttle = sum(1 for p in applied.values() if p.get('action') == 'THROTTLE_SET')
                policy_parts = []
                if num_block > 0:
                    policy_parts.append(f"{num_block} Chặn")
                if num_throttle > 0:
                    policy_parts.append(f"{num_throttle} Giới hạn")
                policy_text = ", ".join(policy_parts) or f"{len(applied)} chính sách"
                policy_item = QTableWidgetItem(f"🛡️ {policy_text}")
                policy_item.setForeground(QtGui.QColor("#2563EB"))
                policy_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                policy_item = QTableWidgetItem("Bình thường")
                policy_item.setForeground(QtGui.QColor("#94A3B8"))
            policy_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.tbl_devices.setItem(row, 6, policy_item)

            # Cột 7: Nút bấm Hành Động
            btn_view = QPushButton("Xem & Điều khiển ➔")
            btn_view.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn_view.setFixedHeight(26)
            btn_view.setStyleSheet("""
                QPushButton {
                    background-color: #2563EB;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 6px;
                    padding: 2px 10px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #1D4ED8;
                }
            """)
            btn_view.clicked.connect(lambda checked, uid=uuid_val: self.machine_selected.emit(uid))
            self.tbl_devices.setCellWidget(row, 7, btn_view)

    def _on_table_row_double_clicked(self, row: int, col: int):
        """Khi double click vào dòng nào đó thì mở ngay máy đó."""
        item = self.tbl_devices.item(row, 1)
        if item:
            uuid_val = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if uuid_val:
                self.machine_selected.emit(uuid_val)
