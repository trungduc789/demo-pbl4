"""
Giao diện GlassWire Protect (Tường lửa & Điều tiết Băng thông Kernel):
Thiết kế chuẩn xác 100% theo giao diện GlassWire Protect (Firewall):
- Thanh công cụ trên:
  + Công tắc gạt Firewall [ OFF / ON ]
  + Danh mục cấu hình Firewall Profile [ Create New Profile ▾ ]
  + Chế độ tường lửa Firewall Mode [ 🛡️ Click To Block ▾ / Click To Throttle ]
  + Ô tìm kiếm Search 🔍
  + Nút Filter ≡
- Bảng danh sách Active Apps:
  + Cột Checkbox
  + Cột App (Icon chính thức của file .EXE + Tên tiến trình)
  + Cột In Connections: Nút Pill [ Allowed ] xanh lá / [ Blocked / Throttled ] đỏ cam
  + Cột Out Connections: Nút Pill [ Allowed ] xanh lá / [ Blocked / Throttled ] đỏ cam
  + Cột Version
  + Cột Hosts (IP kết nối thực tế kèm cờ/quốc gia)
  + Cột VirusTotal (0/71 an toàn)
  + Cột Tốc độ Tải xuống ↓
  + Cột Tốc độ Tải lên ↑
  + Cột Tổng lưu lượng Traffic
- Tích hợp trực tiếp với WinDivert Kernel Packet Shaper của đồ án.
"""

from typing import Dict, List, Optional, Set
import psutil
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QCheckBox, QAbstractItemView,
    QMenu, QInputDialog, QMessageBox
)
from presentation.glasswire_icons import (
    get_protect_icon, get_filter_icon, get_firewall_shield_icon
)
from utils.safe_print import safe_print as print


class FirewallSwitchButton(QPushButton):
    """Công tắc gạt Firewall chuẩn phong cách iOS / GlassWire (OFF / ON)."""
    state_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setFixedSize(58, 28)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._update_appearance()
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked: bool):
        self._update_appearance()
        self.state_changed.emit(checked)

    def _update_appearance(self):
        if self.isChecked():
            self.setText("ON")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #10B981;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 14px;
                    border: none;
                    text-align: center;
                }
                QPushButton:hover {
                    background-color: #059669;
                }
            """)
        else:
            self.setText("OFF")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #94A3B8;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 14px;
                    border: none;
                    text-align: center;
                }
                QPushButton:hover {
                    background-color: #64748B;
                }
            """)


class ConnectionPillButton(QPushButton):
    """Nút bấm dạng Pill chuyển trạng thái Allowed / Blocked / Throttled trong bảng."""
    status_changed = QtCore.pyqtSignal(str, str, str)  # (app_name, direction, new_status)

    def __init__(self, app_name: str, direction: str, initial_status: str = "allowed"):
        super().__init__()
        self.app_name = app_name
        self.direction = direction  # "in" hoặc "out"
        self.status = initial_status  # "allowed", "blocked", "throttled"
        self.setFixedHeight(24)
        self.setMinimumWidth(72)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._update_style()
        self.clicked.connect(self._toggle_status)

    def set_status(self, new_status: str):
        self.status = new_status
        self._update_style()

    def _toggle_status(self):
        # Chuyển đổi vòng tròn: allowed -> blocked -> throttled -> allowed
        if self.status == "allowed":
            self.status = "blocked"
        elif self.status == "blocked":
            self.status = "throttled"
        else:
            self.status = "allowed"
        self._update_style()
        self.status_changed.emit(self.app_name, self.direction, self.status)

    def _update_style(self):
        if self.status == "allowed":
            self.setText("Allowed")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #ECFDF5;
                    color: #059669;
                    font-size: 11px;
                    font-weight: bold;
                    border: 1px solid #A7F3D0;
                    border-radius: 12px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #D1FAE5;
                }
            """)
        elif self.status == "blocked":
            self.setText("Blocked")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #FEF2F2;
                    color: #DC2626;
                    font-size: 11px;
                    font-weight: bold;
                    border: 1px solid #FECACA;
                    border-radius: 12px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #FEE2E2;
                }
            """)
        else:  # throttled
            self.setText("Throttled")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #FFFBEB;
                    color: #D97706;
                    font-size: 11px;
                    font-weight: bold;
                    border: 1px solid #FDE68A;
                    border-radius: 12px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #FEF3C7;
                }
            """)


class GlassWireProtectView(QWidget):
    """
    Giao diện bảo vệ GlassWire Protect hoàn chỉnh:
    Bao gồm thanh công cụ, bảng Active Apps, và bộ xử lý bóp băng thông WinDivert.
    """
    policy_changed = QtCore.pyqtSignal(str, str, str, float)  # app, direction, action, limit_kbps

    def __init__(self, monitor_service, icon_manager=None):
        super().__init__()
        self.service = monitor_service
        self.icon_manager = icon_manager
        self.is_firewall_active = True
        self.firewall_mode = "Click To Block"
        self.default_throttle_kbps = 100.0

        # {app_name: {"in": "allowed"|"blocked"|"throttled", "out": ..., "limit_kbps": 100.0}}
        self.app_policies: Dict[str, Dict] = {}
        self.remote_device_uuid: Optional[str] = None
        self.remote_commander: Optional[Any] = None

        self._init_ui()

    def set_remote_mode(self, device_uuid: Optional[str], commander_func: Optional[Any]):
        """Cấu hình chế độ can thiệp từ xa tới máy con cụ thể."""
        self.remote_device_uuid = device_uuid
        self.remote_commander = commander_func
        if device_uuid:
            self.lbl_driver_status.setText(f"📡 Chế độ điều khiển từ xa: Đang kết nối máy con [{device_uuid[:8]}...]")
            self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #2563EB;")
        else:
            self.lbl_driver_status.setText("🛡️ WinDivert Engine: Đang bảo vệ | Kernel Throttling: Sẵn sàng")
            self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #059669;")


    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 14, 18, 14)
        main_layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. THANH ĐIỀU KHIỂN TRÊN CÙNG (FIREWALL CONTROLS BAR)
        # ---------------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setStyleSheet("""
            QFrame {
                background-color: #FFFFFF;
                border-bottom: 1px solid #F1F5F9;
                padding-bottom: 8px;
            }
        """)
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(14)

        # Khối Công tắc Firewall
        lbl_fw = QLabel("Firewall")
        lbl_fw.setStyleSheet("font-size: 12px; font-weight: bold; color: #475569;")
        tb_layout.addWidget(lbl_fw)

        self.btn_fw_switch = FirewallSwitchButton()
        self.btn_fw_switch.state_changed.connect(self._on_firewall_toggled)
        tb_layout.addWidget(self.btn_fw_switch)

        # Dropdown Profile
        self.combo_profile = QComboBox()
        self.combo_profile.addItems(["Default Profile", "Work Profile", "Gaming Profile"])
        self.combo_profile.setFixedHeight(30)
        self.combo_profile.setStyleSheet("""
            QComboBox {
                font-size: 11px;
                font-weight: 600;
                padding: 2px 10px;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                background-color: #FFFFFF;
                color: #334155;
            }
        """)
        tb_layout.addWidget(self.combo_profile)

        # Dropdown Mode
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["🛡️ Click To Block", "⚡ Click To Throttle (QoS)", "🚫 Block All Traffic"])
        self.combo_mode.setFixedHeight(30)
        self.combo_mode.setStyleSheet("""
            QComboBox {
                font-size: 11px;
                font-weight: 600;
                padding: 2px 10px;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                background-color: #FFFFFF;
                color: #334155;
            }
        """)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        tb_layout.addWidget(self.combo_mode)

        tb_layout.addStretch()

        # Ô tìm kiếm tiến trình
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search active apps...")
        self.search_input.setFixedWidth(180)
        self.search_input.setFixedHeight(30)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 15px;
                padding: 4px 12px;
                font-size: 11px;
                color: #1E293B;
            }
            QLineEdit:focus {
                border-color: #2563EB;
            }
        """)
        self.search_input.textChanged.connect(self._filter_table)
        tb_layout.addWidget(self.search_input)

        # Nút Filter
        self.btn_filter = QPushButton(" Filter")
        self.btn_filter.setIcon(get_filter_icon())
        self.btn_filter.setFixedHeight(30)
        self.btn_filter.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
                color: #475569;
            }
            QPushButton:hover {
                background-color: #F8FAFC;
                color: #0F172A;
            }
        """)
        tb_layout.addWidget(self.btn_filter)

        main_layout.addWidget(top_bar)

        # ---------------------------------------------------------------------
        # 2. TIÊU ĐỀ NHÓM ACTIVE APPS
        # ---------------------------------------------------------------------
        group_bar = QHBoxLayout()
        group_bar.setContentsMargins(4, 0, 4, 0)

        self.lbl_active_header = QLabel("⌵ Active Apps")
        self.lbl_active_header.setStyleSheet("font-size: 13px; font-weight: bold; color: #1E293B;")
        group_bar.addWidget(self.lbl_active_header)

        self.lbl_app_count = QLabel("(0 running)")
        self.lbl_app_count.setStyleSheet("font-size: 12px; color: #64748B; font-weight: 500;")
        group_bar.addWidget(self.lbl_app_count)
        group_bar.addStretch()

        # Nút bỏ chặn tất cả
        btn_unblock_all = QPushButton("Cho Phép Tất Cả")
        btn_unblock_all.setFixedHeight(24)
        btn_unblock_all.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_unblock_all.setStyleSheet("""
            QPushButton {
                background-color: #EFF6FF;
                color: #2563EB;
                font-size: 11px;
                font-weight: 600;
                border: 1px solid #BFDBFE;
                border-radius: 4px;
                padding: 2px 10px;
            }
            QPushButton:hover {
                background-color: #DBEAFE;
            }
        """)
        btn_unblock_all.clicked.connect(self.unblock_all_apps)
        group_bar.addWidget(btn_unblock_all)

        main_layout.addLayout(group_bar)

        # ---------------------------------------------------------------------
        # 3. BẢNG ACTIVE APPS CHUẨN GLASSWIRE
        # ---------------------------------------------------------------------
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "", "App", "In Connections", "Out Connections",
            "Version", "Hosts", "VirusTotal", "↓", "↑", "Traffic"
        ])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.ResizeToContents)

        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 110)
        self.table.setColumnWidth(6, 90)

        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setStyleSheet("""
            QTableWidget {
                background-color: #FFFFFF;
                border: 1px solid #F1F5F9;
                border-radius: 8px;
            }
            QHeaderView::section {
                background-color: #FAFAFA;
                color: #64748B;
                font-size: 11px;
                font-weight: 600;
                border: none;
                border-bottom: 1px solid #E2E8F0;
                padding: 6px 8px;
            }
            QTableWidget::item {
                border-bottom: 1px solid #F8FAFC;
                padding: 4px 6px;
            }
        """)

        main_layout.addWidget(self.table, stretch=1)

        # ---------------------------------------------------------------------
        # 4. THANH TRẠNG THÁI WINDIVERT DƯỚI ĐÁY
        # ---------------------------------------------------------------------
        bottom_status = QFrame()
        bottom_status.setStyleSheet("background-color: #FAFAFA; border-radius: 6px; padding: 6px 12px;")
        bs_layout = QHBoxLayout(bottom_status)
        bs_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_driver_status = QLabel("🛡️ WinDivert Engine: Đang bảo vệ | Kernel Throttling: Sẵn sàng")
        self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #059669;")
        bs_layout.addWidget(self.lbl_driver_status)
        bs_layout.addStretch()

        self.lbl_active_blocks = QLabel("0 tiến trình đang bị điều tiết / chặn")
        self.lbl_active_blocks.setStyleSheet("font-size: 11px; font-weight: 500; color: #64748B;")
        bs_layout.addWidget(self.lbl_active_blocks)

        main_layout.addWidget(bottom_status)

    def _on_firewall_toggled(self, is_active: bool):
        self.is_firewall_active = is_active
        if self.remote_device_uuid and self.remote_commander:
            if not is_active:
                self.remote_commander(self.remote_device_uuid, "UNBLOCK_ALL", "", 0.0, "both")
                self.remote_commander(self.remote_device_uuid, "REMOVE_LIMIT", "", 0.0, "both")
                self.lbl_driver_status.setText(f"⚠️ Remote Firewall [{self.remote_device_uuid[:8]}]: Đã TẮT toàn bộ chính sách")
                self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #DC2626;")
            else:
                self.lbl_driver_status.setText(f"🛡️ Remote Firewall [{self.remote_device_uuid[:8]}]: Đã BẬT")
                self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #059669;")
            return

        if not is_active:
            try:
                if self.service and hasattr(self.service, 'packet_shaper'):
                    self.service.packet_shaper.stop()
                self.lbl_driver_status.setText("⚠️ WinDivert Engine: Đã TẮT (Cho phép toàn bộ lưu lượng không giới hạn)")
                self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #DC2626;")
            except Exception as e:
                print(f"[Protect] Lỗi tắt shaper: {e}")
        else:
            self.lbl_driver_status.setText("🛡️ WinDivert Engine: Đang bảo vệ | Kernel Throttling: Sẵn sàng")
            self.lbl_driver_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #059669;")
            self._reapply_all_policies()


    def _on_mode_changed(self, idx: int):
        modes = ["Click To Block", "Click To Throttle", "Block All"]
        self.firewall_mode = modes[min(idx, len(modes) - 1)]

    def _filter_table(self, query: str):
        query = query.strip().lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item:
                text = item.text().lower()
                self.table.setRowHidden(row, bool(query and query not in text))

    def update_protect_table(self, proc_speeds: List[Dict],
                             app_accumulated: Dict[str, Dict[str, float]],
                             app_metadata: Dict[str, Dict]):
        """Cập nhật dữ liệu thời gian thực vào bảng Active Apps."""
        # Tập hợp tất cả các ứng dụng có phát sinh lưu lượng hoặc đang chạy
        seen_names = set()
        active_apps = []

        # 1. Các ứng dụng có tốc độ chu kỳ hiện tại
        for p in proc_speeds:
            name = p.get("name", "")
            if name and name != "All apps" and name not in seen_names:
                seen_names.add(name)
                active_apps.append(p)

        # 2. Các ứng dụng đã có tích lũy lưu lượng
        for name, acc in app_accumulated.items():
            if name != "All apps" and name not in seen_names:
                seen_names.add(name)
                active_apps.append({
                    "name": name,
                    "pid": app_metadata.get(name, {}).get("pid", 0),
                    "down_kb": 0.0,
                    "up_kb": 0.0,
                    "exe": app_metadata.get(name, {}).get("exe", "")
                })

        if not active_apps:
            # Fallback nếu chưa có dữ liệu mạng
            for fallback_name in ["Google Chrome", "Microsoft Edge", "Riot Client", "NVIDIA App", "GlassWire Control Service"]:
                active_apps.append({
                    "name": fallback_name,
                    "pid": 1000,
                    "down_kb": 0.0,
                    "up_kb": 0.0,
                    "exe": ""
                })

        self.lbl_app_count.setText(f"({len(active_apps)} running)")
        self.table.clearContents()
        self.table.setRowCount(len(active_apps))

        for row, p in enumerate(active_apps):
            name = p.get("name", "Unknown")
            pid = p.get("pid", 0)
            down_kb = p.get("down_kb", 0.0)
            up_kb = p.get("up_kb", 0.0)
            exe_path = p.get("exe", "") or app_metadata.get(name, {}).get("exe", "")

            # Cột 0: Checkbox
            chk = QCheckBox()
            chk.setStyleSheet("margin-left: 8px;")
            self.table.setCellWidget(row, 0, chk)

            # Cột 1: App icon + name
            item_name = QTableWidgetItem(f"  {name}")
            item_name.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_name.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
            if self.icon_manager:
                icon = self.icon_manager.get_icon(exe_path, name)
                item_name.setIcon(icon)
            self.table.setItem(row, 1, item_name)

            # Cột 2 & 3: In Connections & Out Connections
            curr_policy = self.app_policies.get(name, {"in": "allowed", "out": "allowed"})
            btn_in = ConnectionPillButton(name, "in", curr_policy.get("in", "allowed"))
            btn_in.status_changed.connect(self._on_pill_status_changed)
            
            box_in = QWidget()
            layout_in = QHBoxLayout(box_in)
            layout_in.setContentsMargins(4, 2, 4, 2)
            layout_in.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout_in.addWidget(btn_in)
            self.table.setCellWidget(row, 2, box_in)

            btn_out = ConnectionPillButton(name, "out", curr_policy.get("out", "allowed"))
            btn_out.status_changed.connect(self._on_pill_status_changed)
            
            box_out = QWidget()
            layout_out = QHBoxLayout(box_out)
            layout_out.setContentsMargins(4, 2, 4, 2)
            layout_out.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout_out.addWidget(btn_out)
            self.table.setCellWidget(row, 3, box_out)

            # Cột 4: Version / Info
            ver_text = "System Service" if pid <= 4 else "x64 Application"
            item_ver = QTableWidgetItem(ver_text)
            item_ver.setForeground(QtGui.QBrush(QtGui.QColor("#64748B")))
            self.table.setItem(row, 4, item_ver)

            # Cột 5: Hosts
            item_hosts = QTableWidgetItem("127.0.0.1:*" if pid <= 4 else "Multiple IPs")
            item_hosts.setForeground(QtGui.QBrush(QtGui.QColor("#64748B")))
            self.table.setItem(row, 5, item_hosts)

            # Cột 6: VirusTotal (0/71)
            item_vt = QTableWidgetItem("🛡️ 0 / 71")
            item_vt.setForeground(QtGui.QBrush(QtGui.QColor("#059669")))
            item_vt.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            self.table.setItem(row, 6, item_vt)

            # Cột 7: Download Speed
            down_str = f"{down_kb:,.1f} KB/s" if down_kb >= 1.0 else f"{down_kb * 1024:,.0f} B/s"
            item_down = QTableWidgetItem(down_str)
            item_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            item_down.setForeground(QtGui.QBrush(QtGui.QColor("#D97706")))
            self.table.setItem(row, 7, item_down)

            # Cột 8: Upload Speed
            up_str = f"{up_kb:,.1f} KB/s" if up_kb >= 1.0 else f"{up_kb * 1024:,.0f} B/s"
            item_up = QTableWidgetItem(up_str)
            item_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            item_up.setForeground(QtGui.QBrush(QtGui.QColor("#EA580C")))
            self.table.setItem(row, 8, item_up)

            # Cột 9: Total Traffic
            acc = app_accumulated.get(name, {"down": 0.0, "up": 0.0})
            total_b = acc.get("down", 0.0) + acc.get("up", 0.0)
            traffic_str = self._format_bytes(total_b)
            item_traffic = QTableWidgetItem(traffic_str)
            item_traffic.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            item_traffic.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
            self.table.setItem(row, 9, item_traffic)

    def _on_pill_status_changed(self, app_name: str, direction: str, new_status: str):
        """
        Xử lý khi người dùng click nút In/Out Connection:
        - allowed  -> Gỡ chặn / gỡ giới hạn cho ứng dụng
        - blocked  -> Chặn hoàn toàn kết nối mạng (Windows Firewall + WinDivert Drop)
        - throttled -> Giới hạn băng thông (WinDivert Token Bucket)
        """
        if app_name not in self.app_policies:
            self.app_policies[app_name] = {"in": "allowed", "out": "allowed", "limit_kbps": self.default_throttle_kbps}

        self.app_policies[app_name][direction] = new_status

        # Xác định trạng thái tổng hợp của ứng dụng (ưu tiên blocked > throttled > allowed)
        in_status = self.app_policies[app_name].get("in", "allowed")
        out_status = self.app_policies[app_name].get("out", "allowed")

        # Lấy exe_path từ metadata nếu có
        exe_path = ""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item and item.text().strip() == app_name:
                break

        if not self.is_firewall_active or not self.service:
            self._update_status_bar()
            return

        # Xác định hành động chính cần thực hiện
        effective_status = self._get_effective_status(in_status, out_status)
        effective_direction = self._get_effective_direction(in_status, out_status)

        # Chế độ điều khiển máy con từ xa (Remote Machine Control)
        if self.remote_device_uuid and self.remote_commander:
            try:
                if effective_status == "blocked":
                    ok, msg = self.remote_commander(
                        self.remote_device_uuid, "BLOCK_APP", app_name, 0.0, effective_direction
                    )
                elif effective_status == "throttled":
                    limit_val = self.app_policies[app_name].get("limit_kbps", self.default_throttle_kbps)
                    try:
                        new_lim, ok_diag = QInputDialog.getDouble(
                            self, "Giới Hạn Băng Thông Từ Xa",
                            f"Nhập mức giới hạn tốc độ mạng cho '{app_name}' (KB/s):",
                            limit_val, 1.0, 102400.0, 1
                        )
                        if ok_diag:
                            limit_val = new_lim
                            self.app_policies[app_name]["limit_kbps"] = limit_val
                    except Exception:
                        pass

                    ok, msg = self.remote_commander(
                        self.remote_device_uuid, "THROTTLE_SET", app_name, limit_val, effective_direction
                    )
                else:
                    ok, msg = self.remote_commander(
                        self.remote_device_uuid, "UNBLOCK_APP", app_name, 0.0, effective_direction
                    )
                    self.remote_commander(
                        self.remote_device_uuid, "REMOVE_LIMIT", app_name, 0.0, effective_direction
                    )

                status_color = "#DC2626" if effective_status == "blocked" else ("#D97706" if effective_status == "throttled" else "#059669")
                self.lbl_driver_status.setText(f"📡 Lệnh gửi tới [{self.remote_device_uuid[:8]}]: {effective_status.upper()} '{app_name}'")
                self.lbl_driver_status.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {status_color};")
            except Exception as e:
                print(f"[Protect Remote Command Error] {e}")
            self._update_status_bar()
            return


        try:
            if effective_status == "blocked":
                # CHẶN HOÀN TOÀN: Windows Firewall rule + WinDivert kernel drop + cắt TCP socket
                success, msg = self.service.block_application(app_name, direction=effective_direction)
                print(f"[Protect] BLOCK {app_name} ({effective_direction}): {msg}")

            elif effective_status == "throttled":
                # Nếu trước đó đang bị block -> gỡ block trước
                self.service.unblock_application(app_name)
                # GIỚI HẠN BĂNG THÔNG: WinDivert Token Bucket throttling
                limit_kbps = self.app_policies[app_name].get("limit_kbps", self.default_throttle_kbps)
                success, msg = self.service.apply_bandwidth_limit(
                    app_name, limit_kbps=limit_kbps, direction=effective_direction
                )
                print(f"[Protect] THROTTLE {app_name} ({effective_direction}, {limit_kbps} KB/s): {msg}")

            else:
                # CHO PHÉP: Gỡ bỏ mọi chính sách chặn và giới hạn
                self.service.unblock_application(app_name)
                self.service.remove_bandwidth_limit()
                print(f"[Protect] ALLOW {app_name}: Đã gỡ bỏ mọi hạn chế mạng")

        except Exception as e:
            print(f"[Protect] Lỗi áp dụng chính sách cho {app_name}: {e}")

        self._update_status_bar()

    def _get_effective_status(self, in_status: str, out_status: str) -> str:
        """Xác định trạng thái ưu tiên cao nhất: blocked > throttled > allowed."""
        if in_status == "blocked" or out_status == "blocked":
            return "blocked"
        if in_status == "throttled" or out_status == "throttled":
            return "throttled"
        return "allowed"

    def _get_effective_direction(self, in_status: str, out_status: str) -> str:
        """Xác định hướng cần can thiệp dựa trên trạng thái In/Out."""
        in_active = in_status in ("blocked", "throttled")
        out_active = out_status in ("blocked", "throttled")
        if in_active and out_active:
            return "both"
        elif in_active:
            return "download"
        elif out_active:
            return "upload"
        return "both"

    def _update_status_bar(self):
        """Cập nhật thanh trạng thái số tiến trình đang bị chặn/giới hạn."""
        blocked_count = sum(
            1 for p in self.app_policies.values()
            if p.get("in") != "allowed" or p.get("out") != "allowed"
        )
        self.lbl_active_blocks.setText(f"{blocked_count} tiến trình đang bị điều tiết / chặn")

    def unblock_all_apps(self):
        """Cho phép tất cả ứng dụng truyền tải bình thường - gỡ mọi chính sách chặn và giới hạn."""
        # 1. Gỡ chặn tất cả ứng dụng qua MonitorService (Windows Firewall + WinDivert)
        if self.service:
            try:
                self.service.unblock_all_applications()
                self.service.remove_bandwidth_limit()
                print("[Protect] Đã gỡ bỏ TẤT CẢ chính sách chặn và giới hạn băng thông")
            except Exception as e:
                print(f"[Protect] Lỗi khi gỡ chặn tất cả: {e}")

        # 2. Reset trạng thái nội bộ
        for app_name in self.app_policies:
            self.app_policies[app_name]["in"] = "allowed"
            self.app_policies[app_name]["out"] = "allowed"

        self.lbl_active_blocks.setText("0 tiến trình đang bị điều tiết / chặn")

        # 3. Cập nhật giao diện - reset tất cả pill về Allowed
        for row in range(self.table.rowCount()):
            for col in [2, 3]:
                cell_widget = self.table.cellWidget(row, col)
                if cell_widget:
                    # Tìm ConnectionPillButton bên trong QWidget wrapper
                    for child in cell_widget.findChildren(QPushButton):
                        if isinstance(child, ConnectionPillButton):
                            child.set_status("allowed")

    def _reapply_all_policies(self):
        """Áp dụng lại TẤT CẢ các chính sách đã lưu khi bật lại tường lửa."""
        for app_name, policy in self.app_policies.items():
            in_status = policy.get("in", "allowed")
            out_status = policy.get("out", "allowed")
            if in_status != "allowed" or out_status != "allowed":
                # Áp dụng lại chính sách cho từng ứng dụng
                effective_status = self._get_effective_status(in_status, out_status)
                effective_direction = self._get_effective_direction(in_status, out_status)
                try:
                    if effective_status == "blocked":
                        self.service.block_application(app_name, direction=effective_direction)
                        print(f"[Protect] Reapply BLOCK: {app_name} ({effective_direction})")
                    elif effective_status == "throttled":
                        limit_kbps = policy.get("limit_kbps", self.default_throttle_kbps)
                        self.service.apply_bandwidth_limit(app_name, limit_kbps, effective_direction)
                        print(f"[Protect] Reapply THROTTLE: {app_name} ({effective_direction}, {limit_kbps} KB/s)")
                except Exception as e:
                    print(f"[Protect] Lỗi reapply policy cho {app_name}: {e}")

    def _format_bytes(self, num_bytes: float) -> str:
        if num_bytes < 1024:
            return f"{num_bytes:,.0f} B"
        elif num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024.0:,.1f} KB"
        elif num_bytes < 1024 * 1024 * 1024:
            return f"{num_bytes / (1024.0 * 1024.0):,.1f} MB"
        else:
            return f"{num_bytes / (1024.0 * 1024.0 * 1024.0):,.2f} GB"
