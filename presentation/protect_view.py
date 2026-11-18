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

from typing import Dict, List, Optional, Set, Any, Callable
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QCheckBox, QAbstractItemView,
    QMenu, QInputDialog, QMessageBox
)
from application.data_source import (
    DashboardDataSource,
    RemoteFleetDataSource,
    create_data_source,
)
from application.traffic_analytics import format_bytes
from presentation.glasswire_icons import (
    get_protect_icon, get_filter_icon, get_firewall_shield_icon
)
from presentation.widgets.protect_widgets import (
    FirewallSwitchButton,
    ConnectionPillButton,
)
from utils.safe_print import safe_print as print


class GlassWireProtectView(QWidget):
    """
    Giao diện bảo vệ GlassWire Protect hoàn chỉnh:
    Bao gồm thanh công cụ, bảng Active Apps, và bộ xử lý bóp băng thông WinDivert.
    """
    policy_changed = QtCore.pyqtSignal(str, str, str, float)  # app, direction, action, limit_kbps

    def __init__(self, monitor_service=None, icon_manager=None, fleet_service=None, agent_service=None, data_source: Optional[DashboardDataSource] = None):
        super().__init__()
        self.service = monitor_service
        self.icon_manager = icon_manager
        self.fleet_service = fleet_service
        self.agent_service = agent_service
        self.data_source: DashboardDataSource = data_source or create_data_source(
            monitor_service=monitor_service,
            fleet_service=fleet_service,
            agent_service=agent_service,
        )
        self.is_firewall_active = True
        self.firewall_mode = "Click To Block"
        self.default_throttle_kbps = 100.0
        self._status_filter_mode = "all"  # "all", "blocked_or_throttled", "allowed"

        # {app_name: {"in": "allowed"|"blocked"|"throttled", "out": ..., "limit_kbps": 100.0, "source": "ADMIN"|"LOCAL", "locked_by_admin": bool}}
        self.app_policies: Dict[str, Dict] = {}
        self._effective_status_history: Dict[str, str] = {}
        self.remote_device_uuid: Optional[str] = None
        self.remote_commander: Optional[Any] = None

        # Cấu trúc giữ cố định thứ tự dòng và widget để bảng KHÔNG BAO GIỜ nhảy loạn hoặc crash
        self._row_app_order: List[str] = []
        self._app_row_map: Dict[str, int] = {}
        self._row_pills: Dict[str, Dict[str, ConnectionPillButton]] = {}
        self._known_device_snapshot: List[tuple] = []

        self._init_ui()

    @property
    def _cmd_queue(self):
        """Thuộc tính ủy quyền tới hàng đợi lệnh của DataSource ở tầng Application."""
        return self.data_source.dispatcher._queue

    def _get_active_data_source(self) -> DashboardDataSource:
        """Lấy chiến lược DataSource tương ứng với trạng thái hiện tại."""
        if self.remote_device_uuid and self.remote_commander:
            if not isinstance(self.data_source, RemoteFleetDataSource):
                self.data_source = RemoteFleetDataSource(
                    fleet_service=self.fleet_service,
                    device_uuid=self.remote_device_uuid,
                    commander_func=self.remote_commander,
                    dispatcher=self.data_source.dispatcher,
                )
            else:
                self.data_source.set_target_device(self.remote_device_uuid, self.remote_commander)
            return self.data_source

        self.data_source = create_data_source(
            monitor_service=self.service,
            fleet_service=None,
            agent_service=self.agent_service,
            dispatcher=self.data_source.dispatcher,
        )
        return self.data_source

    def _resolve_next_pill_status(self, current_status: str) -> str:
        """
        Xác định trạng thái kế tiếp khi click vào nút Pill dựa trên chế độ Firewall Mode:
        - Click To Block: Chuyển trực tiếp Allowed <-> Blocked (không bật nhầm popup Throttle).
        - Click To Throttle: Chuyển trực tiếp Allowed <-> Throttled.
        - Block All: Chuyển trực tiếp Allowed <-> Blocked.
        """
        if self.firewall_mode == "Click To Throttle":
            return "throttled" if current_status == "allowed" else "allowed"
        return "blocked" if current_status == "allowed" else "allowed"

    def set_fleet_service(self, fleet_service):
        """Kích hoạt dịch vụ quản trị Fleet và thanh chọn máy con trên giao diện Protect."""
        self.fleet_service = fleet_service
        if hasattr(self, "device_selector_widget"):
            self.device_selector_widget.setVisible(bool(fleet_service is not None))
            self.refresh_fleet_devices_dropdown()

    def _clear_table_state(self):
        """Làm sạch bảng khi chuyển đổi giữa các máy con khác nhau."""
        self._row_app_order.clear()
        self._app_row_map.clear()
        self._row_pills.clear()
        self.table.setRowCount(0)

    def set_remote_mode(self, device_uuid: Optional[str], commander_func: Optional[Any]):
        """Cấu hình chế độ can thiệp từ xa tới máy con cụ thể."""
        device_changed = (device_uuid != self.remote_device_uuid)
        self.remote_device_uuid = device_uuid
        self.remote_commander = commander_func

        if device_changed:
            self.app_policies.clear()
            self._effective_status_history.clear()
            self._clear_table_state()

        if device_uuid:
            dev_state = self.fleet_service.get_device_state(device_uuid) if self.fleet_service else None

            # Đồng bộ lại chính sách đã áp dụng trên máy này
            if dev_state and dev_state.applied_policies:
                for app_k, pol in dev_state.applied_policies.items():
                    if pol.get("status") == "FAILED":
                        continue
                    act = pol.get("action")
                    # Khớp lại tên hiển thị nếu đã có trong bảng
                    matched_name = app_k
                    for existing_name in self._row_app_order:
                        if existing_name.lower() == app_k.lower():
                            matched_name = existing_name
                            break
                    if act == "BLOCK_APP":
                        self.app_policies[matched_name] = {"in": "blocked", "out": "blocked"}
                        self._effective_status_history[matched_name] = "blocked"
                    elif act == "THROTTLE_SET":
                        lim = pol.get("limit_kbps", 100.0)
                        self.app_policies[matched_name] = {"in": "throttled", "out": "throttled", "limit_kbps": lim}
                        self._effective_status_history[matched_name] = "throttled"

            # Đảm bảo combobox hiển thị đúng máy con đang chọn
            self.refresh_fleet_devices_dropdown()
        else:
            if hasattr(self, "combo_target_device"):
                self.combo_target_device.blockSignals(True)
                self.combo_target_device.setCurrentIndex(0)
                self.combo_target_device.blockSignals(False)

        self._sync_all_pills_from_policies()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 14, 18, 14)
        main_layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # 1. THANH ĐIỀU KHIỂN TRÊN CÙNG (FIREWALL CONTROLS BAR)
        # ---------------------------------------------------------------------
        tb_layout = QHBoxLayout()
        tb_layout.setContentsMargins(0, 0, 0, 4)
        tb_layout.setSpacing(14)

        # Khối Công tắc Firewall
        lbl_fw = QLabel("Firewall")
        lbl_fw.setStyleSheet("font-size: 12px; font-weight: bold; color: #475569; border: none; background: transparent;")
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
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedWidth(200)
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
        self.btn_filter.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
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
        self.btn_filter.clicked.connect(self._show_filter_menu)
        tb_layout.addWidget(self.btn_filter)

        main_layout.addLayout(tb_layout)

        # ---------------------------------------------------------------------
        # 1.5. THANH CHỌN MÁY CON CHO CHẾ ĐỘ QUẢN TRỊ ADMIN (FLEET DEVICE SELECTOR)
        # ---------------------------------------------------------------------
        self.device_selector_widget = QWidget()
        self.device_selector_widget.setStyleSheet("background: transparent; border: none;")
        dsw_layout = QHBoxLayout(self.device_selector_widget)
        dsw_layout.setContentsMargins(0, 0, 0, 0)
        dsw_layout.setSpacing(10)

        lbl_target = QLabel("🎯 Máy con mục tiêu:")
        lbl_target.setStyleSheet("font-size: 12px; font-weight: bold; color: #1E40AF; border: none; background: transparent;")
        dsw_layout.addWidget(lbl_target)

        self.combo_target_device = QComboBox()
        self.combo_target_device.setFixedHeight(30)
        self.combo_target_device.setStyleSheet("""
            QComboBox {
                font-size: 12px;
                font-weight: 600;
                padding: 2px 10px;
                border: 1px solid #93C5FD;
                border-radius: 6px;
                background-color: #FFFFFF;
                color: #1E3A8A;
                min-width: 280px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #1E293B;
                selection-background-color: #DBEAFE;
                selection-color: #1E3A8A;
                border: 1px solid #93C5FD;
                padding: 4px;
            }
        """)
        self.combo_target_device.currentIndexChanged.connect(self._on_device_combo_changed)
        dsw_layout.addWidget(self.combo_target_device, stretch=1)

        self.btn_refresh_devices = QPushButton("🔄 Quét lại danh sách máy")
        self.btn_refresh_devices.setFixedHeight(28)
        self.btn_refresh_devices.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_devices.setStyleSheet("""
            QPushButton {
                background-color: #DBEAFE;
                color: #1E40AF;
                font-size: 11px;
                font-weight: bold;
                border-radius: 5px;
                padding: 2px 12px;
                border: none;
            }
            QPushButton:hover { background-color: #BFDBFE; }
        """)
        self.btn_refresh_devices.clicked.connect(self.refresh_fleet_devices_dropdown)
        dsw_layout.addWidget(self.btn_refresh_devices)

        main_layout.addWidget(self.device_selector_widget)
        self.device_selector_widget.setVisible(bool(self.fleet_service is not None))

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

        # Nút chặn ứng dụng theo tên tùy chọn
        btn_block_custom = QPushButton("➕ Chặn Theo Tên (.exe)")
        btn_block_custom.setFixedHeight(24)
        btn_block_custom.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_block_custom.setStyleSheet("""
            QPushButton {
                background-color: #FEF2F2;
                color: #DC2626;
                font-size: 11px;
                font-weight: 600;
                border: 1px solid #FECACA;
                border-radius: 4px;
                padding: 2px 10px;
            }
            QPushButton:hover {
                background-color: #FEE2E2;
            }
        """)
        btn_block_custom.clicked.connect(self._prompt_block_custom_app)
        group_bar.addWidget(btn_block_custom)

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
        # 3. BẢNG ACTIVE APPS CHUẨN GLASSWIRE (DỮ LIỆU THỰC TẾ 100%)
        # ---------------------------------------------------------------------
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "", "App", "In Connections", "Out Connections",
            "PID", "Nguồn Luật", "Mức Trần QoS", "↓", "↑", "Traffic"
        ])

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)

        # Cố định độ rộng các cột để bảng không bị co giật ngang khi số liệu nhảy
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 115)
        self.table.setColumnWidth(3, 115)
        self.table.setColumnWidth(4, 70)
        self.table.setColumnWidth(5, 110)
        self.table.setColumnWidth(6, 115)
        self.table.setColumnWidth(7, 90)
        self.table.setColumnWidth(8, 90)
        self.table.setColumnWidth(9, 95)

        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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

    def _on_firewall_toggled(self, is_active: bool):
        self.is_firewall_active = is_active
        ds = self._get_active_data_source()
        ds.set_firewall_enabled(
            enabled=is_active,
            app_policies=self.app_policies,
            default_throttle_kbps=self.default_throttle_kbps,
        )

    def _on_mode_changed(self, idx: int):
        modes = ["Click To Block", "Click To Throttle", "Block All"]
        self.firewall_mode = modes[min(idx, len(modes) - 1)]

    def _show_filter_menu(self):
        """Hiển thị menu lọc trạng thái ứng dụng."""
        menu = QMenu(self)
        act_all = menu.addAction("📋 Tất cả ứng dụng")
        act_blocked = menu.addAction("🚫 Đang bị Chặn / Giới hạn (Blocked / Throttled)")
        act_allowed = menu.addAction("✅ Đang Cho phép (Allowed)")

        chosen = menu.exec(self.btn_filter.mapToGlobal(QtCore.QPoint(0, self.btn_filter.height())))
        if not chosen:
            return
        if chosen == act_all:
            self._status_filter_mode = "all"
            self.btn_filter.setText(" Filter")
        elif chosen == act_blocked:
            self._status_filter_mode = "blocked_or_throttled"
            self.btn_filter.setText(" Filter: Chặn/QoS")
        elif chosen == act_allowed:
            self._status_filter_mode = "allowed"
            self.btn_filter.setText(" Filter: Allowed")
        self._apply_all_rows_visibility()

    def _should_hide_row(self, app_name: str) -> bool:
        """Kiểm tra dòng của `app_name` có cần ẩn theo ô tìm kiếm và bộ lọc trạng thái hay không."""
        query = self.search_input.text().strip().lower()
        if query and query not in app_name.lower():
            return True

        if self._status_filter_mode != "all":
            pol = self.app_policies.get(app_name, {"in": "allowed", "out": "allowed"})
            eff = self._get_effective_status(pol.get("in", "allowed"), pol.get("out", "allowed"))
            if self._status_filter_mode == "blocked_or_throttled" and eff == "allowed":
                return True
            if self._status_filter_mode == "allowed" and eff != "allowed":
                return True

        return False

    def _apply_all_rows_visibility(self):
        """Áp dụng bộ lọc tìm kiếm lên toàn bộ các dòng mà không làm xáo trộn thứ tự."""
        visible_count = 0
        for row, app_name in enumerate(self._row_app_order):
            if row < self.table.rowCount():
                hide = self._should_hide_row(app_name)
                self.table.setRowHidden(row, hide)
                if not hide:
                    visible_count += 1

        total = len(self._row_app_order)
        if self.search_input.text().strip() or self._status_filter_mode != "all":
            self.lbl_app_count.setText(f"({visible_count}/{total} running)")
        else:
            self.lbl_app_count.setText(f"({total} running)")

    def _filter_table(self, _query: str = ""):
        self._apply_all_rows_visibility()

    def sync_from_agent_policies(self):
        """Đồng bộ trạng thái chính sách 2 lớp từ ClientAgentService sang bảng Protect."""
        if not self.agent_service or self.remote_device_uuid:
            return
        active_pols = getattr(self.agent_service, "active_policies", {})
        matched_apps = set()

        for app_k, pol in list(active_pols.items()):
            raw_app_name = pol.get("app", app_k)
            matched_name = raw_app_name
            for existing_name in list(self._row_app_order) + list(self.app_policies.keys()):
                if existing_name.lower() == app_k.lower() or existing_name.lower() == raw_app_name.lower():
                    matched_name = existing_name
                    break
            matched_apps.add(matched_name)
            act = pol.get("action", "BLOCK")
            st = "blocked" if act == "BLOCK" else "throttled"
            lim = float(pol.get("limit_kbps", self.default_throttle_kbps))
            is_admin = bool(pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN"))
            self.app_policies[matched_name] = {
                "in": st,
                "out": st,
                "limit_kbps": lim,
                "source": "ADMIN" if is_admin else "LOCAL",
                "locked_by_admin": is_admin,
            }
            self._effective_status_history[matched_name] = st

        for app_name in list(self.app_policies.keys()):
            if app_name not in matched_apps:
                if self.agent_service.get_policy_for_app(app_name) is None:
                    self.app_policies[app_name]["in"] = "allowed"
                    self.app_policies[app_name]["out"] = "allowed"
                    self.app_policies[app_name]["source"] = ""
                    self.app_policies[app_name]["locked_by_admin"] = False
                    self._effective_status_history[app_name] = "allowed"

    def _format_policy_columns(self, app_name: str) -> tuple:
        """Trả về (source_text, source_color, qos_text, qos_color) thực tế cho ứng dụng."""
        pol = self.app_policies.get(app_name, {"in": "allowed", "out": "allowed"})
        eff = self._get_effective_status(pol.get("in", "allowed"), pol.get("out", "allowed"))
        if eff == "allowed":
            return "—", "#94A3B8", "Không giới hạn", "#059669"

        if self.remote_device_uuid:
            src_text, src_color = "🔒 Admin Server", "#DC2626"
        elif pol.get("locked_by_admin") or pol.get("source") == "ADMIN":
            src_text, src_color = "🔒 Admin Server", "#DC2626"
        else:
            src_text, src_color = "🔓 Cục Bộ", "#2563EB"

        if eff == "blocked":
            return src_text, src_color, "🚫 Chặn (0 KB/s)", "#DC2626"
        lim = float(pol.get("limit_kbps", self.default_throttle_kbps))
        return src_text, src_color, f"⚡ {lim:,.0f} KB/s", "#D97706"

    def _update_row_policy_columns(self, row: int, app_name: str, pid: Optional[int] = None):
        """Cập nhật cột PID (4), Nguồn Luật (5), và Mức Trần QoS (6) theo thời gian thực."""
        if pid is not None:
            pid_str = str(pid) if pid > 0 else "—"
            it_pid = self.table.item(row, 4)
            if it_pid and it_pid.text() != pid_str:
                it_pid.setText(pid_str)

        src_text, src_color, qos_text, qos_color = self._format_policy_columns(app_name)
        it_src = self.table.item(row, 5)
        if it_src:
            if it_src.text() != src_text:
                it_src.setText(src_text)
            it_src.setForeground(QtGui.QBrush(QtGui.QColor(src_color)))

        it_qos = self.table.item(row, 6)
        if it_qos:
            if it_qos.text() != qos_text:
                it_qos.setText(qos_text)
            it_qos.setForeground(QtGui.QBrush(QtGui.QColor(qos_color)))

    def _sync_all_pills_from_policies(self):
        """Đồng bộ trạng thái hiển thị của các nút Pill hiện có theo self.app_policies."""
        for app_name, pills in self._row_pills.items():
            pol = self.app_policies.get(app_name, {"in": "allowed", "out": "allowed"})
            in_btn = pills.get("in")
            out_btn = pills.get("out")
            if in_btn:
                in_btn.set_status(pol.get("in", "allowed"))
            if out_btn:
                out_btn.set_status(pol.get("out", "allowed"))
            row = self._app_row_map.get(app_name)
            if row is not None and row < self.table.rowCount():
                self._update_row_policy_columns(row, app_name)

    def _append_new_app_row(self, info: Dict):
        """Thêm một dòng mới cố định vào cuối bảng cho ứng dụng lần đầu xuất hiện."""
        name = info.get("name", "Unknown")
        pid = info.get("pid", 0)
        down_kb = info.get("down_kb", 0.0)
        up_kb = info.get("up_kb", 0.0)
        exe_path = info.get("exe", "")
        total_b = info.get("total_bytes", 0.0)

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 38)

        self._row_app_order.append(name)
        self._app_row_map[name] = row

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

        # Cột 2 & 3: In Connections & Out Connections (Tạo 1 lần duy nhất, không bao giờ xóa khi update)
        curr_policy = self.app_policies.get(name, {"in": "allowed", "out": "allowed"})
        btn_in = ConnectionPillButton(
            name, "in", curr_policy.get("in", "allowed"),
            next_status_resolver=self._resolve_next_pill_status
        )
        btn_in.status_changed.connect(self._on_pill_status_changed)

        box_in = QWidget()
        layout_in = QHBoxLayout(box_in)
        layout_in.setContentsMargins(4, 2, 4, 2)
        layout_in.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout_in.addWidget(btn_in)
        self.table.setCellWidget(row, 2, box_in)

        btn_out = ConnectionPillButton(
            name, "out", curr_policy.get("out", "allowed"),
            next_status_resolver=self._resolve_next_pill_status
        )
        btn_out.status_changed.connect(self._on_pill_status_changed)

        box_out = QWidget()
        layout_out = QHBoxLayout(box_out)
        layout_out.setContentsMargins(4, 2, 4, 2)
        layout_out.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout_out.addWidget(btn_out)
        self.table.setCellWidget(row, 3, box_out)

        self._row_pills[name] = {"in": btn_in, "out": btn_out}

        # Cột 4: PID thực tế
        pid_str = str(pid) if pid > 0 else "—"
        item_pid = QTableWidgetItem(pid_str)
        item_pid.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        item_pid.setForeground(QtGui.QBrush(QtGui.QColor("#64748B")))
        self.table.setItem(row, 4, item_pid)

        src_text, src_color, qos_text, qos_color = self._format_policy_columns(name)

        # Cột 5: Nguồn Luật (Admin / Cục bộ)
        item_src = QTableWidgetItem(src_text)
        item_src.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        item_src.setForeground(QtGui.QBrush(QtGui.QColor(src_color)))
        item_src.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        self.table.setItem(row, 5, item_src)

        # Cột 6: Mức Trần QoS thực tế
        item_qos = QTableWidgetItem(qos_text)
        item_qos.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        item_qos.setForeground(QtGui.QBrush(QtGui.QColor(qos_color)))
        item_qos.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
        self.table.setItem(row, 6, item_qos)

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
        traffic_str = self._format_bytes(total_b)
        item_traffic = QTableWidgetItem(traffic_str)
        item_traffic.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        item_traffic.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
        self.table.setItem(row, 9, item_traffic)

        # Áp dụng ngay trạng thái ẩn/hiện nếu người dùng đang tìm kiếm
        self.table.setRowHidden(row, self._should_hide_row(name))

    def _update_existing_app_row(self, row: int, info: Dict):
        """Cập nhật tại chỗ (in-place) các ô số liệu của dòng hiện có mà không tạo lại widget."""
        name = info.get("name", "Unknown")
        pid = info.get("pid", 0)
        down_kb = info.get("down_kb", 0.0)
        up_kb = info.get("up_kb", 0.0)
        total_b = info.get("total_bytes", 0.0)

        down_str = f"{down_kb:,.1f} KB/s" if down_kb >= 1.0 else f"{down_kb * 1024:,.0f} B/s"
        item_down = self.table.item(row, 7)
        if item_down and item_down.text() != down_str:
            item_down.setText(down_str)

        up_str = f"{up_kb:,.1f} KB/s" if up_kb >= 1.0 else f"{up_kb * 1024:,.0f} B/s"
        item_up = self.table.item(row, 8)
        if item_up and item_up.text() != up_str:
            item_up.setText(up_str)

        traffic_str = self._format_bytes(total_b)
        item_traffic = self.table.item(row, 9)
        if item_traffic and item_traffic.text() != traffic_str:
            item_traffic.setText(traffic_str)

        self._update_row_policy_columns(row, name, pid if pid > 0 else None)

        # Đảm bảo pill khớp với chính sách lưu trữ
        pol = self.app_policies.get(name, {"in": "allowed", "out": "allowed"})
        pills = self._row_pills.get(name)
        if pills:
            in_btn = pills.get("in")
            out_btn = pills.get("out")
            if in_btn:
                in_btn.set_status(pol.get("in", "allowed"))
            if out_btn:
                out_btn.set_status(pol.get("out", "allowed"))

    def update_protect_table(self, proc_speeds: List[Dict],
                             app_accumulated: Dict[str, Dict[str, float]],
                             app_metadata: Dict[str, Dict]):
        """
        Cập nhật dữ liệu thời gian thực vào bảng Active Apps:
        - Giữ cố định 100% vị trí các dòng đã hiển thị (không nhảy thứ tự).
        - Cập nhật tại chỗ (in-place) tốc độ & lưu lượng mà không xóa/tạo lại nút bấm.
        - Giữ nguyên kết quả tìm kiếm và vị trí cuộn của người dùng.
        """
        if self.agent_service and not self.remote_device_uuid:
            self.sync_from_agent_policies()

        apps_by_name: Dict[str, Dict] = {}
        ordered_candidates: List[str] = []

        # 1. Thu thập tốc độ chu kỳ hiện tại
        for p in proc_speeds:
            name = p.get("name", "")
            if not name or name == "All apps":
                continue
            if name not in apps_by_name:
                acc = app_accumulated.get(name, {"down": 0.0, "up": 0.0})
                total_b = acc.get("down", 0.0) + acc.get("up", 0.0)
                apps_by_name[name] = {
                    "name": name,
                    "pid": p.get("pid", 0) or app_metadata.get(name, {}).get("pid", 0),
                    "down_kb": float(p.get("down_kb", 0.0)),
                    "up_kb": float(p.get("up_kb", 0.0)),
                    "exe": p.get("exe", "") or app_metadata.get(name, {}).get("exe", ""),
                    "total_bytes": total_b,
                }
                ordered_candidates.append(name)

        # 2. Thu thập các ứng dụng đã có tích lũy lưu lượng
        for name, acc in app_accumulated.items():
            if not name or name == "All apps":
                continue
            if name not in apps_by_name:
                total_b = acc.get("down", 0.0) + acc.get("up", 0.0)
                apps_by_name[name] = {
                    "name": name,
                    "pid": app_metadata.get(name, {}).get("pid", 0),
                    "down_kb": 0.0,
                    "up_kb": 0.0,
                    "exe": app_metadata.get(name, {}).get("exe", ""),
                    "total_bytes": total_b,
                }
                ordered_candidates.append(name)

        # 3. Giữ lại các ứng dụng đang có trong bảng hoặc đang có chính sách chặn/QoS
        for name in list(self._row_app_order) + list(self.app_policies.keys()):
            if name and name != "All apps" and name not in apps_by_name:
                acc = app_accumulated.get(name, {"down": 0.0, "up": 0.0})
                total_b = acc.get("down", 0.0) + acc.get("up", 0.0)
                apps_by_name[name] = {
                    "name": name,
                    "pid": app_metadata.get(name, {}).get("pid", 0),
                    "down_kb": 0.0,
                    "up_kb": 0.0,
                    "exe": app_metadata.get(name, {}).get("exe", ""),
                    "total_bytes": total_b,
                }
                ordered_candidates.append(name)

        self.table.setUpdatesEnabled(False)
        try:
            # Cập nhật tại chỗ các dòng đã tồn tại theo đúng vị trí cố định
            for row, existing_name in enumerate(self._row_app_order):
                info = apps_by_name.get(existing_name)
                if info is not None:
                    self._update_existing_app_row(row, info)

            # Chỉ thêm vào cuối bảng những ứng dụng mới xuất hiện lần đầu
            for cand_name in ordered_candidates:
                if cand_name not in self._app_row_map:
                    self._append_new_app_row(apps_by_name[cand_name])
        finally:
            self.table.setUpdatesEnabled(True)

        self._apply_all_rows_visibility()

    def _on_pill_status_changed(self, app_name: str, direction: str, new_status: str):
        """
        Xử lý khi người dùng click nút In/Out Connection:
        - allowed   -> Gỡ chặn / gỡ giới hạn cho ứng dụng
        - blocked   -> Chặn hoàn toàn kết nối mạng (Windows Firewall + WinDivert Drop)
        - throttled -> Giới hạn băng thông (WinDivert Token Bucket)
        """
        # Kiểm tra khóa 2 tầng (2-Tier Policy Lock) nếu đang ở chế độ Client Mode
        if not self.remote_device_uuid and self.agent_service and self.agent_service.is_locked_by_admin(app_name):
            self.sync_from_agent_policies()
            self._sync_all_pills_from_policies()
            QtWidgets.QMessageBox.warning(
                self,
                "Khóa Bởi Admin Server (🔒)",
                f"Ứng dụng '{app_name}' đang chịu sự kiểm soát bắt buộc từ Admin Server.\n"
                "Máy con không được phép tự ý gỡ bỏ hoặc thay đổi chính sách của Quản trị viên!"
            )
            return

        if app_name not in self.app_policies:
            self.app_policies[app_name] = {
                "in": "allowed",
                "out": "allowed",
                "limit_kbps": self.default_throttle_kbps,
                "source": "ADMIN" if self.remote_device_uuid else "LOCAL",
                "locked_by_admin": bool(self.remote_device_uuid),
            }

        prev_pol_snapshot = dict(self.app_policies[app_name])
        prev_effective = self._effective_status_history.get(app_name, "allowed")

        limit_val = float(self.app_policies[app_name].get("limit_kbps", self.default_throttle_kbps))
        if new_status == "throttled":
            try:
                new_lim, ok_diag = QInputDialog.getDouble(
                    self, "Giới Hạn Băng Thông (QoS)",
                    f"Nhập mức giới hạn tốc độ mạng cho '{app_name}' (KB/s):",
                    limit_val, 1.0, 102400.0, 1
                )
                if not ok_diag:
                    return
                limit_val = float(new_lim)
                self.app_policies[app_name]["limit_kbps"] = limit_val
            except Exception:
                pass

        # Nếu ở chế độ Click To Block / Click To Throttle thông thường, đồng bộ cả 2 chiều In & Out
        # để người dùng chỉ cần 1 click là Chặn hoặc Mở chặn trọn vẹn cho ứng dụng đó
        self.app_policies[app_name][direction] = new_status
        other_dir = "out" if direction == "in" else "in"
        self.app_policies[app_name][other_dir] = new_status
        self.app_policies[app_name]["source"] = "ADMIN" if self.remote_device_uuid else "LOCAL"
        self.app_policies[app_name]["locked_by_admin"] = bool(self.remote_device_uuid)

        pills = self._row_pills.get(app_name)
        if pills:
            if pills.get("in"):
                pills["in"].set_status(new_status)
            if pills.get("out"):
                pills["out"].set_status(new_status)

        in_status = self.app_policies[app_name].get("in", "allowed")
        out_status = self.app_policies[app_name].get("out", "allowed")
        effective_status = self._get_effective_status(in_status, out_status)
        effective_direction = self._get_effective_direction(in_status, out_status)
        self._effective_status_history[app_name] = effective_status

        row_idx = self._app_row_map.get(app_name)
        if row_idx is not None and row_idx < self.table.rowCount():
            self._update_row_policy_columns(row_idx, app_name)

        if self._status_filter_mode != "all":
            self._apply_all_rows_visibility()

        ds = self._get_active_data_source()
        ds.apply_app_policy(
            app_name=app_name,
            effective_status=effective_status,
            prev_effective=prev_effective,
            direction=effective_direction,
            limit_kbps=limit_val,
            is_firewall_active=self.is_firewall_active,
        )

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

    def unblock_all_apps(self):
        """Cho phép tất cả ứng dụng truyền tải bình thường thông qua chiến lược DataSource."""
        ds = self._get_active_data_source()
        for app_name in list(self.app_policies.keys()):
            if ds.is_locked_by_admin(app_name):
                continue
            self.app_policies[app_name]["in"] = "allowed"
            self.app_policies[app_name]["out"] = "allowed"
            self.app_policies[app_name]["source"] = ""
            self.app_policies[app_name]["locked_by_admin"] = False
            self._effective_status_history[app_name] = "allowed"

        self._sync_all_pills_from_policies()
        if self._status_filter_mode != "all":
            self._apply_all_rows_visibility()

        ds.unblock_all_policies()

    def refresh_fleet_devices_dropdown(self):
        """Làm mới danh sách máy con trong combo_target_device từ FleetManagerService."""
        if not self.fleet_service or not hasattr(self, "combo_target_device"):
            return

        try:
            devices = self.fleet_service.get_all_device_states()
        except Exception as e:
            print(f"[Protect] Lỗi refresh fleet devices: {e}")
            return

        snapshot = [
            (
                getattr(d, "device_uuid", ""),
                getattr(d, "hostname", "Unknown"),
                getattr(d, "ip_address", ""),
                getattr(d, "status", "offline"),
            )
            for d in devices
        ]

        self.combo_target_device.blockSignals(True)
        try:
            self.combo_target_device.clear()
            self.combo_target_device.addItem("— Chọn máy con mục tiêu —", None)

            selected_idx = 0
            for uid, hostname, ip, status in snapshot:
                badge = "🟢" if status == "online" else "⚪"
                label = f"{badge}  {hostname}  ({ip})  [{uid[:8]}...]"
                self.combo_target_device.addItem(label, uid)
                if uid and uid == self.remote_device_uuid:
                    selected_idx = self.combo_target_device.count() - 1

            self.combo_target_device.setCurrentIndex(selected_idx)
            self._known_device_snapshot = snapshot
        finally:
            self.combo_target_device.blockSignals(False)

    def sync_fleet_devices_if_changed(self):
        """Chỉ cập nhật dropdown khi danh sách máy con hoặc trạng thái kết nối thay đổi."""
        if not self.fleet_service or not hasattr(self, "combo_target_device"):
            return
        try:
            devices = self.fleet_service.get_all_device_states()
            snapshot = [
                (
                    getattr(d, "device_uuid", ""),
                    getattr(d, "hostname", "Unknown"),
                    getattr(d, "ip_address", ""),
                    getattr(d, "status", "offline"),
                )
                for d in devices
            ]
            current_uid = self.combo_target_device.currentData()
            if snapshot != self._known_device_snapshot or current_uid != self.remote_device_uuid:
                self.refresh_fleet_devices_dropdown()
        except Exception:
            pass

    def _on_device_combo_changed(self, index: int):
        """Khi Admin chọn máy con từ dropdown -> tự động set_remote_mode về máy đó."""
        if not self.fleet_service or not hasattr(self, "combo_target_device"):
            return

        uid = self.combo_target_device.itemData(index)
        if uid:
            commander = getattr(self.fleet_service, "send_remote_command", None)
            self.set_remote_mode(uid, commander)
        else:
            self.set_remote_mode(None, None)

    def _prompt_block_custom_app(self):
        """Hiển thị dialog nhập tên .exe để chặn thủ công (không cần app xuất hiện trong traffic table)."""
        app_name, ok = QInputDialog.getText(
            self, "Chặn Ứng Dụng Theo Tên",
            "Nhập tên tiến trình (.exe) cần chặn:\n(Ví dụ: chrome.exe, discord.exe, steam.exe)",
        )
        if not ok or not app_name.strip():
            return

        app_name = app_name.strip()
        if not self.remote_device_uuid and self.agent_service and self.agent_service.is_locked_by_admin(app_name):
            QtWidgets.QMessageBox.warning(
                self,
                "Khóa Bởi Admin Server (🔒)",
                f"Ứng dụng '{app_name}' đang chịu sự kiểm soát bắt buộc từ Admin Server.\n"
                "Máy con không được phép tự ý ghi đè chính sách của Quản trị viên!"
            )
            return

        # Khởi tạo policy
        self.app_policies[app_name] = {
            "in": "blocked",
            "out": "blocked",
            "limit_kbps": self.default_throttle_kbps,
            "source": "ADMIN" if self.remote_device_uuid else "LOCAL",
            "locked_by_admin": bool(self.remote_device_uuid),
        }
        self._effective_status_history[app_name] = "blocked"

        if app_name not in self._app_row_map:
            self._append_new_app_row({
                "name": app_name,
                "pid": 0,
                "down_kb": 0.0,
                "up_kb": 0.0,
                "exe": "",
                "total_bytes": 0.0,
            })
        else:
            self._sync_all_pills_from_policies()

        self._apply_all_rows_visibility()

        ds = self._get_active_data_source()
        ds.apply_app_policy(
            app_name=app_name,
            effective_status="blocked",
            prev_effective="allowed",
            direction="both",
            limit_kbps=0.0,
            is_firewall_active=self.is_firewall_active,
        )

    def _reapply_all_policies(self):
        """Áp dụng lại TẤT CẢ các chính sách đã lưu khi bật lại tường lửa."""
        ds = self._get_active_data_source()
        ds.set_firewall_enabled(
            enabled=True,
            app_policies=self.app_policies,
            default_throttle_kbps=self.default_throttle_kbps,
        )

    def _format_bytes(self, num_bytes: float) -> str:
        return format_bytes(num_bytes)
