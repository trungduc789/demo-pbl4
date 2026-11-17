"""
Cửa sổ Chính Ứng dụng Giám sát & Điều tiết Băng thông (BandwidthDashboard - Presentation Layer):
- Tổ hợp (Composition) các trang chức năng độc lập bên trong QStackedWidget:
  + AdminFleetView: Quản lý đội ngũ máy trạm (chế độ Admin).
  + TrafficMonitorPage: Biểu đồ sóng thời gian thực, Sidebar tiến trình, Usage 4 cột & HUD.
  + GlassWireProtectView: Tường lửa & Bóp băng thông WinDivert Kernel Packet Shaper.
  + LogAnalysisView: Phân tích nhật ký chính sách và kết nối mạng.
  + NetworkScannerView: Quét kết nối Socket TCP.
- Giao tiếp giữa các trang qua Qt Signals, tuân thủ chặt chẽ kiến trúc 3 lớp.
"""

import time
from collections import deque
from typing import Dict, List, Optional, Tuple
import numpy as np
from PyQt6 import QtWidgets, QtCore, QtGui

from application.monitor_service import MonitorService
from application.data_source import DashboardDataSource, create_data_source
from application.traffic_analytics import (
    format_bytes,
    format_speed,
    generate_glasswire_wave,
)
from config.settings import UPDATE_INTERVAL
from utils.safe_print import safe_print as print

from presentation.glasswire_icons import (
    get_traffic_monitor_icon,
    get_protect_icon,
    get_log_analysis_icon,
    get_network_scanner_icon,
    get_fleet_icon,
)
from presentation.traffic_monitor_page import TrafficMonitorPage
from presentation.protect_view import GlassWireProtectView
from presentation.admin_fleet_view import AdminFleetView
from presentation.log_analysis_view import LogAnalysisView
from presentation.network_scanner_view import NetworkScannerView
from presentation.widgets.dashboard_widgets import (
    ProcessIconManager,
    AsyncSignalBridge,
    HighFreqNetworkWorker,
)


class BandwidthDashboard(QtWidgets.QMainWindow):
    """Cửa sổ chính điều phối QStackedWidget và kết nối tín hiệu giữa các trang chức năng."""

    def __init__(self, monitor_service: Optional[MonitorService] = None, fleet_service=None, agent_service=None):
        super().__init__()
        self.service = monitor_service
        self.fleet_service = fleet_service
        self.agent_service = agent_service
        self.data_source: DashboardDataSource = create_data_source(
            monitor_service=monitor_service,
            fleet_service=fleet_service,
            agent_service=agent_service,
        )
        self.is_admin_mode = self.data_source.has_fleet_management
        self.is_client_mode = (self.data_source.mode_name == "client")
        self.selected_device_uuid: Optional[str] = None
        self.icon_manager = ProcessIconManager()
        self.async_bridge = AsyncSignalBridge(self)
        self.raw_policy_logs: List[Dict] = []
        self._last_fed_telemetry_ts: Dict[str, float] = {}

        self._init_ui()
        self._bind_traffic_page_aliases()
        self._start_worker()
        self._start_render_loop()

        if self.is_client_mode and self.agent_service:
            original_policy_cb = getattr(self.agent_service, "on_policy_applied_locally", None)

            def _on_policy_with_ui_refresh(action, app_name, status, limit_kbps):
                if original_policy_cb:
                    try:
                        original_policy_cb(action, app_name, status, limit_kbps)
                    except Exception:
                        pass
                try:
                    self.async_bridge.run_on_main(self._on_remote_policy_updated)
                except Exception:
                    pass

            self.agent_service.on_policy_applied_locally = _on_policy_with_ui_refresh

    def _bind_traffic_page_aliases(self):
        """Liên kết các thuộc tính của TrafficMonitorPage lên Dashboard để giữ tương thích 100%."""
        tp = self.traffic_page
        self.CEILING_TIERS_KB = tp.CEILING_TIERS_KB
        self.down_points = tp.down_points
        self.up_points = tp.up_points
        self.window_sample_history = tp.window_sample_history
        self.point_history = tp.point_history
        self.app_accumulated_bytes = tp.app_accumulated_bytes
        self.app_metadata = tp.app_metadata
        self.plot_widget = tp.plot_widget
        self.time_axis = tp.time_axis
        self.curve_down = tp.curve_down
        self.curve_up = tp.curve_up
        self.timeline_scrubber = tp.timeline_scrubber
        self.usage_view = tp.usage_view
        self.traffic_content_stack = tp.traffic_content_stack
        self.app_list_table = tp.app_list_table
        self.search_app_input = tp.search_app_input
        self.lbl_live_badge = tp.lbl_live_badge
        self.btn_pause_toggle = tp.btn_pause_toggle
        self.scale_indicator = tp.scale_indicator
        self.hud_down_total = tp.hud_down_total
        self.hud_down_speed = tp.hud_down_speed
        self.hud_up_total = tp.hud_up_total
        self.hud_up_speed = tp.hud_up_speed
        self.arc_gauge = tp.arc_gauge
        self.wan_lan_widget = tp.wan_lan_widget

    def _init_ui(self):
        self.setWindowTitle("Traffic Monitor & Network Packet Shaper")
        self.resize(1280, 760)
        self.setMinimumSize(1020, 620)
        self.setStyleSheet("""
            QMainWindow, QWidget#centralContainer, QStackedWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QWidget { color: #1E293B; }
            QDialog, QMessageBox { background-color: #FFFFFF; color: #0F172A; }
            QMessageBox QLabel { background-color: #FFFFFF; color: #0F172A; font-size: 13px; padding: 4px; }
            QMessageBox QPushButton {
                background-color: #2563EB; color: #FFFFFF; border-radius: 6px;
                font-weight: 600; padding: 6px 18px; min-width: 75px; font-size: 12px; border: none;
            }
            QMessageBox QPushButton:hover { background-color: #1D4ED8; }
            QTableWidget {
                background-color: #FFFFFF; color: #1E293B; gridline-color: transparent;
                border: none; font-size: 12px; selection-background-color: #EBF5FF; selection-color: #0F172A;
            }
            QTableWidget::item { padding: 6px 8px; border-bottom: 1px solid #F1F5F9; }
            QTableWidget::item:selected { background-color: #EBF5FF; color: #0F172A; font-weight: 600; }
            QHeaderView::section {
                background-color: #FFFFFF; color: #64748B; padding: 6px 8px;
                font-weight: 600; font-size: 11px; border: none; border-bottom: 1px solid #E2E8F0;
            }
            QPushButton {
                background-color: #2563EB; color: #FFFFFF; border-radius: 6px;
                font-weight: 600; padding: 8px 16px; font-size: 12px; border: none;
            }
            QPushButton:hover { background-color: #1D4ED8; }
            QLineEdit, QComboBox {
                background-color: #FFFFFF; border: 1px solid #CBD5E1;
                border-radius: 6px; padding: 5px 10px; font-size: 12px; color: #1E293B;
            }
            QLineEdit:focus, QComboBox:focus { border: 1.5px solid #2563EB; }
            QScrollArea, QScrollArea > QWidget > QWidget { background-color: #FFFFFF; border: none; }
        """)

        container = QtWidgets.QWidget()
        container.setObjectName("centralContainer")
        container.setStyleSheet("background-color: #FFFFFF;")
        main_layout = QtWidgets.QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.stacked_pages = QtWidgets.QStackedWidget()
        self.stacked_pages.setStyleSheet("background-color: #FFFFFF;")

        header_widget = self._create_glasswire_header()
        main_layout.addWidget(header_widget)

        self.breadcrumb_bar = self._create_breadcrumb_bar()
        self.breadcrumb_bar.setVisible(False)
        main_layout.addWidget(self.breadcrumb_bar)

        # Trang Fleet Manager (chỉ có trong Admin Mode)
        if self.is_admin_mode:
            self.fleet_view = AdminFleetView()
            self.fleet_view.machine_selected.connect(self._on_machine_selected_for_control)
            self.fleet_view.machine_protect_requested.connect(self._on_machine_protect_requested)
            self.stacked_pages.addWidget(self.fleet_view)

        # Trang Traffic Monitor (Composition)
        self.traffic_page = TrafficMonitorPage(
            icon_manager=self.icon_manager,
            fetch_raw_conns_cb=self._get_raw_tcp_connections,
            resolve_exe_cb=self._resolve_process_exe,
        )
        self.traffic_page.app_protect_requested.connect(self._on_traffic_app_protect_requested)
        self.traffic_page.sample_processed.connect(self._on_traffic_sample_processed)
        self.page_traffic_monitor = self.traffic_page
        self.stacked_pages.addWidget(self.traffic_page)

        # Trang GlassWire Protect
        self.page_protect = QtWidgets.QWidget()
        self._setup_protect_page(self.page_protect)
        self.stacked_pages.addWidget(self.page_protect)

        # Trang Log Analysis
        self.page_logs = QtWidgets.QWidget()
        self._setup_logs_page(self.page_logs)
        self.stacked_pages.addWidget(self.page_logs)

        # Trang Network Scanner
        self.page_scanner = QtWidgets.QWidget()
        self._setup_scanner_page(self.page_scanner)
        self.stacked_pages.addWidget(self.page_scanner)

        main_layout.addWidget(self.stacked_pages)
        self._switch_nav_page(0)
        self.setCentralWidget(container)

    # =========================================================================
    # KẾT NỐI TẦNG APPLICATION THÔNG QUA DATA SOURCE STRATEGY
    # =========================================================================
    def _get_raw_tcp_connections(self) -> List:
        try:
            return self.data_source.get_raw_tcp_connections()
        except Exception:
            return []

    def _resolve_process_exe(self, pid: int) -> str:
        try:
            return self.data_source.resolve_executable_path(pid)
        except Exception:
            return ""

    def _get_active_connections(self) -> List[Dict]:
        try:
            return self.data_source.get_connections()
        except Exception as e:
            print(f"[Dashboard Connections Error] {e}")
            return []

    def _fetch_policy_logs_from_backend(self, limit: int = 200) -> List[Dict]:
        return self.data_source.get_policy_history(limit)

    def _fetch_connection_logs_from_backend(self, limit: int = 200) -> str:
        return self.data_source.get_connection_history_text(limit)

    def _get_logs_dir_from_backend(self) -> str:
        return self.data_source.get_logs_directory()

    # =========================================================================
    # THIẾT LẬP CÁC TRANG CON & HEADER / BREADCRUMB
    # =========================================================================
    def _setup_protect_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.protect_view = GlassWireProtectView(
            self.service,
            self.icon_manager,
            fleet_service=self.fleet_service,
            agent_service=self.agent_service,
            data_source=self.data_source,
        )
        if hasattr(self.protect_view, "combo_target_device") and self.is_admin_mode:
            self.protect_view.combo_target_device.currentIndexChanged.connect(self._on_protect_device_combo_synced)
        layout.addWidget(self.protect_view)

    def _setup_logs_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.log_analysis_view = LogAnalysisView(
            fetch_policy_logs=self._fetch_policy_logs_from_backend,
            fetch_connection_logs=self._fetch_connection_logs_from_backend,
            get_logs_dir=self._get_logs_dir_from_backend,
            open_logs_folder_cb=self.data_source.open_logs_folder,
            parent=parent,
        )
        self.txt_policy_filter = self.log_analysis_view.txt_policy_filter
        self.log_tabs = self.log_analysis_view.log_tabs
        self.policy_log_table = self.log_analysis_view.policy_log_table
        self.txt_conn_log = self.log_analysis_view.txt_conn_log
        layout.addWidget(self.log_analysis_view)

    def _setup_scanner_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scanner_view = NetworkScannerView(
            fetch_connections=self._get_active_connections,
            parent=parent,
        )
        self.conn_table = self.scanner_view.conn_table
        layout.addWidget(self.scanner_view)

    def _create_breadcrumb_bar(self) -> QtWidgets.QFrame:
        bar = QtWidgets.QFrame()
        bar.setObjectName("breadcrumbBar")
        bar.setFixedHeight(38)
        bar.setStyleSheet(
            "QFrame#breadcrumbBar { background-color: #FFFFFF; border-bottom: 1px solid #E2E8F0; padding: 2px 14px; }"
        )
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(12)

        btn_back = QtWidgets.QPushButton("⬅️ Quay lại Danh Sách Máy")
        btn_back.setFixedHeight(26)
        btn_back.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_back.setStyleSheet(
            "QPushButton { background-color: #2563EB; color: #FFFFFF; font-weight: bold; "
            "font-size: 11px; border-radius: 13px; padding: 2px 14px; border: none; }"
            "QPushButton:hover { background-color: #1D4ED8; }"
        )
        btn_back.clicked.connect(self._return_to_fleet_view)
        layout.addWidget(btn_back)

        self.lbl_breadcrumb_device = QtWidgets.QLabel("💻 Đang giám sát máy trạm")
        self.lbl_breadcrumb_device.setStyleSheet(
            "font-size: 12px; font-weight: 700; color: #1E293B; border: none; background: transparent;"
        )
        layout.addWidget(self.lbl_breadcrumb_device)

        self.lbl_offline_notice = QtWidgets.QLabel("🔴 MÁY TRẠM ĐÃ OFFLINE — Dữ liệu đang tạm dừng")
        self.lbl_offline_notice.setStyleSheet(
            "background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; "
            "border-radius: 11px; font-size: 11px; font-weight: 800; padding: 2px 12px;"
        )
        self.lbl_offline_notice.setVisible(False)
        layout.addWidget(self.lbl_offline_notice)
        layout.addStretch()
        return bar

    def _create_glasswire_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QWidget()
        header.setFixedHeight(54)
        header.setStyleSheet("background-color: #FFFFFF; border-bottom: 1px solid #E2E8F0;")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(16)

        self.nav_buttons: List[QtWidgets.QPushButton] = []
        if self.is_admin_mode:
            self.nav_tabs_info = [
                ("Fleet\nManager", 0, get_fleet_icon),
                ("Traffic\nMonitor", 1, get_traffic_monitor_icon),
                ("Protect", 2, get_protect_icon),
                ("Log\nAnalysis", 3, get_log_analysis_icon),
                ("Network\nScanner", 4, get_network_scanner_icon),
            ]
        else:
            self.nav_tabs_info = [
                ("Traffic\nMonitor", 0, get_traffic_monitor_icon),
                ("Protect", 1, get_protect_icon),
                ("Log\nAnalysis", 2, get_log_analysis_icon),
                ("Network\nScanner", 3, get_network_scanner_icon),
            ]

        nav_container = QtWidgets.QHBoxLayout()
        nav_container.setSpacing(8)

        for text, page_idx, icon_factory in self.nav_tabs_info:
            btn = QtWidgets.QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(40)
            btn.setIcon(icon_factory(page_idx == 0))
            btn.setIconSize(QtCore.QSize(22, 22))
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=page_idx: self._switch_nav_page(idx))
            nav_container.addWidget(btn)
            self.nav_buttons.append(btn)

        layout.addLayout(nav_container)
        layout.addStretch()

        if self.is_client_mode and self.agent_service:
            self.client_status_btn = QtWidgets.QPushButton()
            self.client_status_btn.setFixedHeight(30)
            self.client_status_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.client_status_btn.setToolTip("Nhấn để mở cửa sổ chi tiết kết nối Máy chủ Admin")
            self.client_status_btn.clicked.connect(self._open_client_status_dialog)
            layout.addWidget(self.client_status_btn)
            self._update_client_status_indicator()

            self.client_status_timer = QtCore.QTimer(self)
            self.client_status_timer.setInterval(1000)
            self.client_status_timer.timeout.connect(self._update_client_status_indicator)
            self.client_status_timer.start()

        return header

    # =========================================================================
    # ĐIỀU PHỐI SỰ KIỆN & ĐỒNG BỘ TRẠNG THÁI GIỮA CÁC TRANG
    # =========================================================================
    def _on_traffic_app_protect_requested(self, app_name: str):
        if hasattr(self, "protect_view") and hasattr(self.protect_view, "search_input"):
            self.protect_view.search_input.setText(app_name)
        protect_tab_idx = 2 if self.is_admin_mode else 1
        self._switch_nav_page(protect_tab_idx)

    def _on_traffic_sample_processed(self, proc_speeds: list, app_accumulated_bytes: dict, app_metadata: dict):
        if hasattr(self, "protect_view"):
            self.protect_view.update_protect_table(proc_speeds, app_accumulated_bytes, app_metadata)

    def _on_protect_device_combo_synced(self, index: int):
        if not self.is_admin_mode or not hasattr(self, "protect_view"):
            return
        uid = self.protect_view.combo_target_device.itemData(index)
        if uid and uid != self.selected_device_uuid:
            self._switch_active_remote_device(uid)

    def _get_monitored_device_state(self):
        if self.is_admin_mode and self.fleet_service and self.selected_device_uuid:
            return self.fleet_service.get_device_state(self.selected_device_uuid)
        return None

    def _sync_monitored_device_offline_state(self, dev_state):
        tp = self.traffic_page
        is_off = bool(dev_state and dev_state.status != "online")
        if hasattr(self, "lbl_offline_notice"):
            if is_off:
                off_ts = getattr(dev_state, "offline_at", 0.0) or getattr(dev_state, "last_seen", 0.0) or time.time()
                off_str = time.strftime("%H:%M:%S", time.localtime(off_ts))
                self.lbl_offline_notice.setText(f"🔴 MÁY TRẠM ĐÃ OFFLINE (Mất kết nối lúc {off_str}) — Đã dừng cập nhật")
            self.lbl_offline_notice.setVisible(is_off)

        if is_off:
            tp.lbl_live_badge.setText("🔴 OFFLINE")
            tp.lbl_live_badge.setStyleSheet(
                "color: #DC2626; font-weight: bold; font-size: 11px; padding: 3px 8px; "
                "background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;"
            )
        elif not tp.is_paused:
            tp.lbl_live_badge.setText("● LIVE")
            tp.lbl_live_badge.setStyleSheet(
                "color: #059669; font-weight: bold; font-size: 11px; padding: 3px 8px; "
                "background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;"
            )

        if is_off and not tp._is_monitored_device_offline:
            tp._is_monitored_device_offline = True
            off_ts = getattr(dev_state, "offline_at", 0.0) or getattr(dev_state, "last_seen", 0.0) or time.time()
            last_pt_ts = tp.down_points[-1][0] if tp.down_points else off_ts
            drop_ts = max(last_pt_ts + 1.0, off_ts)

            if not tp.down_points or tp.down_points[-1][1] != 0.0:
                tp.down_points.append((drop_ts, 0.0))
            if not tp.up_points or tp.up_points[-1][1] != 0.0:
                tp.up_points.append((drop_ts, 0.0))
            if not tp.point_history or tp.point_history[-1].get("down_kb", 0.0) != 0.0 or tp.point_history[-1].get("up_kb", 0.0) != 0.0:
                tp.point_history.append({"timestamp": drop_ts, "down_kb": 0.0, "up_kb": 0.0, "top_procs": []})
                tp.timeline_scrubber.add_point(drop_ts, 0.0)

            tp._offline_anchor_ts = drop_ts
            tp.latest_down = 0.0
            tp.latest_up = 0.0
            tp.latest_proc_down = 0.0
            tp.latest_proc_up = 0.0
            tp.latest_proc_speeds = []

            tp.update_app_sidebar([])
            tp.update_selected_app_hud()
            if hasattr(self, "protect_view"):
                self.protect_view.update_protect_table([], tp.app_accumulated_bytes, tp.app_metadata)
            if tp.traffic_content_stack.currentIndex() == 1:
                tp.update_usage_view_now()
        elif not is_off and tp._is_monitored_device_offline:
            tp._is_monitored_device_offline = False
            tp._offline_anchor_ts = None

    def _switch_active_remote_device(self, device_uuid: Optional[str]):
        changed = device_uuid != self.selected_device_uuid
        self.selected_device_uuid = device_uuid
        tp = self.traffic_page

        dev_st = self.fleet_service.get_device_state(device_uuid) if (self.fleet_service and device_uuid) else None
        if hasattr(self, "lbl_breadcrumb_device") and dev_st:
            self.lbl_breadcrumb_device.setText(
                f"💻 Đang giám sát máy trạm: {dev_st.hostname}  ({dev_st.ip_address or 'LAN'})  —  UUID: {device_uuid[:12]}"
            )

        if not changed or not device_uuid or not self.fleet_service:
            self._sync_monitored_device_offline_state(dev_st)
            return

        tp._is_monitored_device_offline = False
        tp._offline_anchor_ts = None
        tp._last_usage_conns = []
        tp.latest_down = 0.0
        tp.latest_up = 0.0
        tp.latest_proc_down = 0.0
        tp.latest_proc_up = 0.0
        tp.latest_proc_speeds = []

        now = time.time()
        first_hist_ts = dev_st.download_history[0][0] if (dev_st and dev_st.download_history) else now
        base_end = min(now, first_hist_ts - 1.0)
        init_times = np.linspace(base_end - 650.0, base_end, 650)
        tp.down_points.clear()
        tp.down_points.extend((t, 0.0) for t in init_times)
        tp.up_points.clear()
        tp.up_points.extend((t, 0.0) for t in init_times)
        tp.point_history.clear()
        tp.window_sample_history.clear()
        tp.app_accumulated_bytes = {"All apps": {"down": 0.0, "up": 0.0}}
        self.app_accumulated_bytes = tp.app_accumulated_bytes
        tp.total_accumulated_down = 0.0
        tp.total_accumulated_up = 0.0

        if dev_st:
            for ts, d_val in list(dev_st.download_history):
                tp.down_points.append((ts, d_val))
            for ts, u_val in list(dev_st.upload_history):
                tp.up_points.append((ts, u_val))
            for (ts, d_val), (_, u_val) in zip(list(dev_st.download_history), list(dev_st.upload_history)):
                tp.point_history.append({"timestamp": ts, "down_kb": d_val, "up_kb": u_val, "top_procs": []})
                tp.window_sample_history.append({
                    "t": ts,
                    "down_b": d_val * 1024.0 * UPDATE_INTERVAL,
                    "up_b": u_val * 1024.0 * UPDATE_INTERVAL,
                    "procs": {},
                })

        self._sync_monitored_device_offline_state(dev_st)

    def _on_machine_selected_for_control(self, device_uuid: str):
        self._switch_active_remote_device(device_uuid)
        if hasattr(self, "protect_view"):
            self.protect_view.set_remote_mode(
                device_uuid, self.fleet_service.send_remote_command if self.fleet_service else None
            )
        self.breadcrumb_bar.setVisible(True)
        self._switch_nav_page(1 if self.is_admin_mode else 0)

    def _return_to_fleet_view(self):
        self.selected_device_uuid = None
        self.breadcrumb_bar.setVisible(False)
        if hasattr(self, "protect_view"):
            self.protect_view.set_remote_mode(None, None)
        self._switch_nav_page(0)

    def _on_machine_protect_requested(self, device_uuid: str):
        self._switch_active_remote_device(device_uuid)
        if hasattr(self, "protect_view"):
            self.protect_view.set_remote_mode(
                device_uuid, self.fleet_service.send_remote_command if self.fleet_service else None
            )
        self.breadcrumb_bar.setVisible(True)
        self._switch_nav_page(2 if self.is_admin_mode else 1)

    def _refresh_fleet_admin_ui(self):
        if not self.fleet_service or not hasattr(self, "fleet_view"):
            return
        summary = self.fleet_service.get_fleet_summary()
        devices = self.fleet_service.get_all_device_states()
        self.fleet_view.update_fleet_data(devices, summary)

        if not self.selected_device_uuid and devices:
            online_devs = [d for d in devices if d.status == "online"]
            target_dev = online_devs[0] if online_devs else devices[0]
            self._switch_active_remote_device(target_dev.device_uuid)

        if hasattr(self, "protect_view"):
            if not self.protect_view.remote_device_uuid and self.selected_device_uuid:
                self.protect_view.set_remote_mode(
                    self.selected_device_uuid,
                    self.fleet_service.send_remote_command if self.fleet_service else None,
                )
            elif hasattr(self.protect_view, "sync_fleet_devices_if_changed"):
                self.protect_view.sync_fleet_devices_if_changed()

        if self.selected_device_uuid:
            dev_state = self.fleet_service.get_device_state(self.selected_device_uuid)
            self._sync_monitored_device_offline_state(dev_state)
            if dev_state and dev_state.status == "online" and dev_state.latest_telemetry:
                telem = dev_state.latest_telemetry
                if telem.timestamp > self._last_fed_telemetry_ts.get(self.selected_device_uuid, 0.0):
                    self._last_fed_telemetry_ts[self.selected_device_uuid] = telem.timestamp
                    proc_speeds = []
                    for p in telem.top_processes:
                        proc_speeds.append({
                            "name": p.name,
                            "pid": p.pid,
                            "down_kb": p.down_kbps,
                            "up_kb": p.up_kbps,
                            "rx_bytes": int(p.down_kbps * 1024),
                            "tx_bytes": int(p.up_kbps * 1024),
                            "exe": "",
                        })
                        if p.name not in self.traffic_page.app_metadata:
                            self.traffic_page.app_metadata[p.name] = {"exe": "", "pid": p.pid}
                    down_spd = telem.speeds.download_kbps
                    up_spd = telem.speeds.upload_kbps
                    now_ts = time.time()
                    display_ts = telem.timestamp if abs(now_ts - telem.timestamp) <= 4.0 else now_ts
                    self._on_new_sample(display_ts, down_spd, up_spd, proc_speeds, down_spd, up_spd)

    def _update_client_status_indicator(self):
        if not hasattr(self, "client_status_btn") or not self.agent_service:
            return
        is_conn = self.agent_service.net_client.is_connected()
        srv_host = self.agent_service.server_host
        srv_port = self.agent_service.server_port
        if is_conn:
            self.client_status_btn.setText(f"🟢 Admin: {srv_host}:{srv_port} (Online)")
            self.client_status_btn.setStyleSheet(
                "QPushButton { background-color: #ECFDF5; color: #059669; font-weight: bold; "
                "border: 1px solid #A7F3D0; border-radius: 15px; padding: 3px 12px; font-size: 11px; }"
                "QPushButton:hover { background-color: #D1FAE5; }"
            )
        else:
            self.client_status_btn.setText(f"🔴 Mất kết nối Admin ({srv_host}:{srv_port})")
            self.client_status_btn.setStyleSheet(
                "QPushButton { background-color: #FEF2F2; color: #DC2626; font-weight: bold; "
                "border: 1px solid #FECACA; border-radius: 15px; padding: 3px 12px; font-size: 11px; }"
                "QPushButton:hover { background-color: #FEE2E2; }"
            )

    def _open_client_status_dialog(self):
        if not self.agent_service:
            return
        from presentation.client_status_window import ClientStatusWindow
        if not hasattr(self, "_client_status_dialog") or self._client_status_dialog is None:
            self._client_status_dialog = ClientStatusWindow(self.agent_service)
        self._client_status_dialog.show()
        self._client_status_dialog.raise_()
        self._client_status_dialog.activateWindow()

    def _on_remote_policy_updated(self):
        if hasattr(self, "protect_view"):
            self.protect_view.sync_from_agent_policies()
            self.protect_view._sync_all_pills_from_policies()
            self.protect_view.update_protect_table(
                self.traffic_page.latest_proc_speeds,
                self.traffic_page.app_accumulated_bytes,
                self.traffic_page.app_metadata,
            )

    def _switch_nav_page(self, index: int):
        self.stacked_pages.setCurrentIndex(index)
        active_css = (
            "QPushButton { background-color: #EBF3FC; color: #2563EB; font-weight: bold; "
            "border-radius: 6px; padding: 4px 14px; font-size: 12px; border: none; text-align: center; }"
        )
        inactive_css = (
            "QPushButton { background-color: transparent; color: #475569; font-weight: 600; "
            "border-radius: 6px; padding: 4px 14px; font-size: 12px; border: none; text-align: center; }"
            "QPushButton:hover { background-color: #F8FAFC; color: #1E293B; }"
        )
        for i, btn in enumerate(self.nav_buttons):
            _text, _page_idx, icon_factory = self.nav_tabs_info[i]
            btn.setIcon(icon_factory(i == index))
            btn.setStyleSheet(active_css if i == index else inactive_css)

        if self.is_admin_mode:
            if index == 0:
                self.breadcrumb_bar.setVisible(False)
                self._refresh_fleet_admin_ui()
            elif index == 1:
                self.breadcrumb_bar.setVisible(bool(self.selected_device_uuid))
            elif index == 2:
                self.breadcrumb_bar.setVisible(bool(self.selected_device_uuid))
                if hasattr(self, "protect_view"):
                    self.protect_view.update_protect_table(
                        self.traffic_page.latest_proc_speeds,
                        self.traffic_page.app_accumulated_bytes,
                        self.traffic_page.app_metadata,
                    )
            elif index == 3:
                self.breadcrumb_bar.setVisible(False)
                self._on_enter_usage_history_page()
            elif index == 4:
                self.breadcrumb_bar.setVisible(False)
                self._refresh_connections()
        else:
            if index == 1 and hasattr(self, "protect_view"):
                self.protect_view.update_protect_table(
                    self.traffic_page.latest_proc_speeds,
                    self.traffic_page.app_accumulated_bytes,
                    self.traffic_page.app_metadata,
                )
            elif index == 2:
                self._on_enter_usage_history_page()
            elif index == 3:
                self._refresh_connections()

    # =========================================================================
    # WORKER, TIMERS & DELEGATED METHODS
    # =========================================================================
    def _start_worker(self):
        if self.service:
            self.worker = HighFreqNetworkWorker(self.service)
            self.worker.sample_ready.connect(self._on_new_sample)
            self.worker.start()

        if self.is_admin_mode and self.fleet_service:
            self.fleet_timer = QtCore.QTimer()
            self.fleet_timer.setInterval(1000)
            self.fleet_timer.timeout.connect(self._refresh_fleet_admin_ui)
            self.fleet_timer.start()
            QtCore.QTimer.singleShot(150, self._refresh_fleet_admin_ui)

    def _start_render_loop(self):
        self.render_timer = QtCore.QTimer()
        self.render_timer.setInterval(28)
        self.render_timer.timeout.connect(self._render_frame)
        self.render_timer.start()

        self.conn_timer = QtCore.QTimer()
        self.conn_timer.setInterval(3000)
        self.conn_timer.timeout.connect(self._refresh_connections)
        self.conn_timer.start()

    @QtCore.pyqtSlot(float, float, float, list, float, float)
    def _on_new_sample(self, timestamp, down_kb, up_kb, proc_speeds, total_p_down, total_p_up):
        self.traffic_page.on_new_sample(timestamp, down_kb, up_kb, proc_speeds, total_p_down, total_p_up)

    def _render_frame(self):
        self.traffic_page.render_frame()

    def _format_bytes(self, num_bytes: float) -> str:
        return format_bytes(num_bytes)

    def _format_speed(self, speed_kb: float) -> str:
        return format_speed(speed_kb)

    def _compute_window_traffic(self, win_duration: Optional[float] = None, t_end: Optional[float] = None):
        return self.traffic_page.compute_window_traffic(win_duration, t_end)

    def _find_nearest_sample(self, target_t: float) -> Optional[dict]:
        return self.traffic_page.find_nearest_sample(target_t)

    @classmethod
    def _generate_glasswire_wave(cls, x_raw: np.ndarray, y_raw: np.ndarray, x_dense: np.ndarray, window_duration: float) -> np.ndarray:
        return generate_glasswire_wave(x_raw, y_raw, x_dense, window_duration)

    def _open_logs_folder(self):
        if hasattr(self, "log_analysis_view"):
            self.log_analysis_view.open_logs_folder()

    def _on_enter_usage_history_page(self):
        self._refresh_policy_logs()

    def _refresh_policy_logs(self):
        if hasattr(self, "log_analysis_view"):
            self.log_analysis_view.refresh_logs()
            self.raw_policy_logs = self.log_analysis_view.raw_policy_logs

    def _render_policy_table(self, logs: List[Dict]):
        if hasattr(self, "log_analysis_view"):
            self.log_analysis_view.render_policy_table(logs)

    def _filter_policy_logs(self, query: str):
        if hasattr(self, "log_analysis_view"):
            self.log_analysis_view.filter_policy_logs(query)

    def _refresh_connections(self):
        if hasattr(self, "page_scanner") and self.stacked_pages.currentWidget() != self.page_scanner:
            return
        if self.is_admin_mode and self.selected_device_uuid:
            dev_state = self._get_monitored_device_state()
            if dev_state and dev_state.status != "online":
                return
        if hasattr(self, "scanner_view"):
            self.scanner_view.refresh_connections()

    def closeEvent(self, event):
        if getattr(self, "_hide_to_tray_on_close", False):
            event.ignore()
            self.hide()
            return

        for timer_attr in ("client_status_timer", "fleet_timer", "render_timer", "conn_timer"):
            t = getattr(self, timer_attr, None)
            if t and t.isActive():
                t.stop()
        if hasattr(self, "worker") and self.worker:
            self.worker.stop()
        if self.service:
            self.service.stop()
        if self.is_client_mode and self.agent_service:
            self.agent_service.stop()
        event.accept()
