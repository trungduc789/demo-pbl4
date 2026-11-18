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
    # Tín hiệu khi Admin muốn chuyển thẳng đến tab Protect cho 1 máy: phát ra (device_uuid)
    machine_protect_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_devices: List[Any] = []
        self._current_summary: Dict[str, Any] = {}
        self._rendered_uuids: List[str] = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(14)
        self.setStyleSheet("background-color: #F8FAFC;")

        # 1. Header Toolbar
        header_layout = QHBoxLayout()
        title = QLabel("🖥️ Quản Trị Hệ Thống Mạng (Fleet Management)")
        title.setStyleSheet("font-size: 19px; font-weight: 800; color: #0F172A; border: none; background: transparent;")
        header_layout.addWidget(title)
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
        fb_layout = QHBoxLayout()
        fb_layout.setContentsMargins(0, 0, 0, 0)
        fb_layout.setSpacing(10)

        lbl_search = QLabel("🔍 Tìm máy:")
        lbl_search.setStyleSheet("font-weight: bold; font-size: 12px; color: #475569; border: none; background: transparent;")
        fb_layout.addWidget(lbl_search)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Nhập tên máy (Hostname), IP hoặc UUID...")
        self.txt_search.setFixedHeight(30)
        self.txt_search.setStyleSheet("""
            QLineEdit {
                background-color: #FFFFFF;
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
        lbl_filter.setStyleSheet("font-weight: bold; font-size: 12px; color: #475569; border: none; background: transparent;")
        fb_layout.addWidget(lbl_filter)

        self.cbo_status_filter = QComboBox()
        self.cbo_status_filter.addItems(["Tất cả máy", "Chỉ máy Online", "Chỉ máy Offline"])
        self.cbo_status_filter.setFixedHeight(30)
        self.cbo_status_filter.setStyleSheet("""
            QComboBox {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 2px 10px;
                font-size: 12px;
            }
        """)
        self.cbo_status_filter.currentIndexChanged.connect(self._apply_filter)
        fb_layout.addWidget(self.cbo_status_filter)

        layout.addLayout(fb_layout)

        # 4. Bảng danh sách máy con
        self.tbl_devices = QTableWidget()
        self.tbl_devices.setColumnCount(8)
        self.tbl_devices.setHorizontalHeaderLabels([
            "Trạng Thái", "Tên Máy & UUID", "Địa Chỉ IP", "Hệ Điều Hành",
            "Tải Xuống ↓", "Tải Lên ↑", "Chính Sách Áp Dụng", "Hành Động"
        ])
        self.tbl_devices.verticalHeader().setVisible(False)
        self.tbl_devices.verticalHeader().setDefaultSectionSize(48)
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
                background-color: #F8FAFC;
                color: #475569;
                font-weight: 700;
                font-size: 11px;
                padding: 10px 8px;
                border: none;
                border-bottom: 2px solid #E2E8F0;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #F1F5F9;
            }
            QTableWidget::item:selected {
                background-color: #EFF6FF;
                color: #1E293B;
            }
        """)

        # Cấu hình kích thước các cột cân đối, thoáng đãng
        header = self.tbl_devices.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(0, 110)   # Trạng Thái

        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # Tên Máy & UUID co giãn linh hoạt

        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(2, 130)   # Địa Chỉ IP

        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(3, 145)   # Hệ Điều Hành

        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(4, 110)   # Tải Xuống

        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(5, 110)   # Tải Lên

        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(6, 140)   # Chính Sách Áp Dụng

        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self.tbl_devices.setColumnWidth(7, 245)   # Hành Động (Đủ chỗ cho 2 nút bấm)

        self.tbl_devices.cellDoubleClicked.connect(self._on_table_row_double_clicked)
        layout.addWidget(self.tbl_devices, stretch=1)

    def _create_hud_card(self, title: str, value: str, accent_color: str, bg_color: str) -> QFrame:
        """Tạo thẻ hiển thị chỉ số HUD phong cách GlassWire."""
        card = QFrame()
        card.setObjectName("hudCard")
        card.setFixedHeight(74)
        card.setStyleSheet(f"""
            QFrame#hudCard {{
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
        lbl_t.setStyleSheet("font-size: 10px; font-weight: 800; color: #64748B; border: none; background: transparent;")
        lbl_v = QLabel(value)
        lbl_v.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {accent_color}; border: none; background: transparent;")
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
        """Render danh sách máy con vào bảng giao diện (hỗ trợ cập nhật tại chỗ In-place tốc độ cao)."""
        new_uuids = [getattr(d, 'device_uuid', '') for d in devices]
        structure_changed = (new_uuids != self._rendered_uuids) or (self.tbl_devices.rowCount() != len(devices))

        if structure_changed:
            self.tbl_devices.setRowCount(len(devices))
            self._rendered_uuids = new_uuids

        for row, dev in enumerate(devices):
            uuid_val = getattr(dev, 'device_uuid', '')
            status = getattr(dev, 'status', 'offline')
            hostname = getattr(dev, 'hostname', 'Unknown')
            ip = getattr(dev, 'ip_address', '127.0.0.1')
            os_info = getattr(dev, 'os_info', 'Windows')
            down_kbps = getattr(dev, 'current_down_kbps', 0.0)
            up_kbps = getattr(dev, 'current_up_kbps', 0.0)
            applied = getattr(dev, 'applied_policies', {})

            if structure_changed:
                self.tbl_devices.setRowHeight(row, 48)

            # Cột 0: Trạng thái Badge
            status_text = f" {'● ONLINE' if status == 'online' else '○ OFFLINE'} "
            status_item = self.tbl_devices.item(row, 0)
            if not status_item:
                status_item = QTableWidgetItem()
                status_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.tbl_devices.setItem(row, 0, status_item)
            status_item.setText(status_text)
            if status == "online":
                status_item.setForeground(QtGui.QColor("#059669"))
                status_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                status_item.setForeground(QtGui.QColor("#94A3B8"))
                status_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Normal))

            # Cột 1: Tên máy & UUID
            short_uuid = uuid_val[:8] + "..." if len(uuid_val) > 8 else uuid_val
            name_text = f"💻  {hostname}  ({short_uuid})"
            name_item = self.tbl_devices.item(row, 1)
            if not name_item:
                name_item = QTableWidgetItem()
                name_item.setFont(QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.Bold))
                name_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft)
                self.tbl_devices.setItem(row, 1, name_item)
            name_item.setText(name_text)
            name_item.setData(QtCore.Qt.ItemDataRole.UserRole, uuid_val)

            # Cột 2: IP
            ip_item = self.tbl_devices.item(row, 2)
            if not ip_item:
                ip_item = QTableWidgetItem()
                ip_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                ip_item.setForeground(QtGui.QColor("#334155"))
                self.tbl_devices.setItem(row, 2, ip_item)
            ip_item.setText(ip or "N/A")

            # Cột 3: OS
            os_item = self.tbl_devices.item(row, 3)
            if not os_item:
                os_item = QTableWidgetItem()
                os_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                os_item.setForeground(QtGui.QColor("#64748B"))
                self.tbl_devices.setItem(row, 3, os_item)
            os_item.setText(os_info)

            # Cột 4: Tải xuống ↓
            down_str = f" {self._format_speed(down_kbps)} ↓ "
            down_item = self.tbl_devices.item(row, 4)
            if not down_item:
                down_item = QTableWidgetItem()
                down_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.tbl_devices.setItem(row, 4, down_item)
            down_item.setText(down_str)
            if status == "online" and down_kbps > 10.0:
                down_item.setForeground(QtGui.QColor("#D97706"))
                down_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                down_item.setForeground(QtGui.QColor("#94A3B8"))
                down_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Normal))

            # Cột 5: Tải lên ↑
            up_str = f" {self._format_speed(up_kbps)} ↑ "
            up_item = self.tbl_devices.item(row, 5)
            if not up_item:
                up_item = QTableWidgetItem()
                up_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.tbl_devices.setItem(row, 5, up_item)
            up_item.setText(up_str)
            if status == "online" and up_kbps > 10.0:
                up_item.setForeground(QtGui.QColor("#E11D48"))
                up_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                up_item.setForeground(QtGui.QColor("#94A3B8"))
                up_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Normal))

            # Cột 6: Chính sách an ninh đang áp dụng
            policy_item = self.tbl_devices.item(row, 6)
            if not policy_item:
                policy_item = QTableWidgetItem()
                policy_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.tbl_devices.setItem(row, 6, policy_item)
            active_applied = [p for p in applied.values() if p.get('status') != 'FAILED'] if applied else []
            if active_applied:
                num_block = sum(1 for p in active_applied if p.get('action') == 'BLOCK_APP')
                num_throttle = sum(1 for p in active_applied if p.get('action') == 'THROTTLE_SET')
                policy_parts = []
                if num_block > 0:
                    policy_parts.append(f"{num_block} Chặn")
                if num_throttle > 0:
                    policy_parts.append(f"{num_throttle} Giới hạn")
                policy_text = ", ".join(policy_parts) or f"{len(active_applied)} chính sách"
                policy_item.setText(f"🛡️ {policy_text}")
                policy_item.setForeground(QtGui.QColor("#2563EB"))
                policy_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            else:
                policy_item.setText("Bình thường")
                policy_item.setForeground(QtGui.QColor("#94A3B8"))
                policy_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Normal))

            # Cột 7: Nút bấm Hành Động (Chỉ tạo mới khi cấu trúc bảng thay đổi)
            if structure_changed or self.tbl_devices.cellWidget(row, 7) is None:
                action_widget = QWidget()
                action_layout = QHBoxLayout(action_widget)
                action_layout.setContentsMargins(6, 6, 6, 6)
                action_layout.setSpacing(8)

                btn_view = QPushButton("Xem Chi Tiết ➔")
                btn_view.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                btn_view.setFixedHeight(30)
                btn_view.setStyleSheet("""
                    QPushButton {
                        background-color: #2563EB;
                        color: #FFFFFF;
                        font-size: 11px;
                        font-weight: bold;
                        border-radius: 6px;
                        padding: 4px 10px;
                        border: none;
                    }
                    QPushButton:hover {
                        background-color: #1D4ED8;
                    }
                """)
                btn_view.clicked.connect(lambda checked, uid=uuid_val: self.machine_selected.emit(uid))
                action_layout.addWidget(btn_view)

                btn_protect = QPushButton("🛡️ Chặn / QoS")
                btn_protect.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                btn_protect.setFixedHeight(30)
                btn_protect.setStyleSheet("""
                    QPushButton {
                        background-color: #DC2626;
                        color: #FFFFFF;
                        font-size: 11px;
                        font-weight: bold;
                        border-radius: 6px;
                        padding: 4px 10px;
                        border: none;
                    }
                    QPushButton:hover {
                        background-color: #B91C1C;
                    }
                """)
                btn_protect.clicked.connect(lambda checked, uid=uuid_val: self.machine_protect_requested.emit(uid))
                action_layout.addWidget(btn_protect)

                self.tbl_devices.setCellWidget(row, 7, action_widget)

    def _on_table_row_double_clicked(self, row: int, col: int):
        """Khi double click vào dòng nào đó thì mở ngay máy đó."""
        item = self.tbl_devices.item(row, 1)
        if item:
            uuid_val = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if uuid_val:
                self.machine_selected.emit(uuid_val)
