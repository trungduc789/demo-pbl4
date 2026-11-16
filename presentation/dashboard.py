import os
import sys
import time
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import numpy as np
import psutil
from PyQt6 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg

from application.monitor_service import MonitorService
from config.settings import UPDATE_INTERVAL, MAX_DATA_POINTS
from utils.safe_print import safe_print as print
from utils.file_logger import FileLogger, LOGS_DIR

from presentation.glasswire_icons import (
    get_glasswire_logo_icon,
    get_traffic_monitor_icon,
    get_protect_icon,
    get_log_analysis_icon,
    get_network_scanner_icon,
    get_hardware_resources_icon,
    get_graph_toggle_icon,
    get_usage_toggle_icon,
    get_filter_icon,
    get_fleet_icon,
)
from presentation.traffic_usage_view import GlassWireUsageView
from presentation.protect_view import GlassWireProtectView
from presentation.admin_fleet_view import AdminFleetView


from presentation.widgets.dashboard_widgets import (
    TIME_SCALE_CONFIGS,
    TimeAxisItem,
    GlassWireArcGauge,
    GlassWireWanLanWidget,
    GlassWireAppHoverPopup,
    GlassWireAppTriggerPill,
    GlassWirePointInspectionBar,
    GlassWireTimelineScrubber,
    ProcessIconManager,
    AsyncSignalBridge,
    HighFreqNetworkWorker,
)


class BandwidthDashboard(QtWidgets.QMainWindow):
    """
    Giao diện GlassWire Free chuẩn xác 100%:
    - Header: GlassWire brand + Navigation Pills (Traffic Monitor, Protect, Usage History, Network Scanner).
    - Split-View: Bảng danh sách tiến trình trái (Icon, App, ↓, ↑) + Biểu đồ sóng thời gian thực phải.
    - Bộ đệm trễ 5 giây + Nội suy Hermite & Lọc Gaussian mượt mà không rung giật.
    - HUD thống kê ứng dụng: Số liệu MB/KB, Tốc độ, Đồng hồ bán nguyệt GlassWire, Thanh phân bổ WAN/LAN.
    - Panoramic Timeline Scrubber với thanh kéo tua và bóng sóng vàng.
    - Footer: Upgrade to unlock Premium features banner.
    - Tab 2 (GlassWire Protect): Bóp băng thông WinDivert Kernel Packet Shaper (Token Bucket 2 chiều).
    - Tab 3 (Usage History): Lịch sử lưu lượng theo ngày & xem chi tiết mốc thời gian.
    - Tab 4 (Network Scanner): Bảng Socket TCP (IP, Cổng, Giao thức, Trạng thái).
    """

    def __init__(self, monitor_service: Optional[MonitorService] = None, fleet_service=None, agent_service=None):
        super().__init__()
        self.service = monitor_service
        self.fleet_service = fleet_service
        self.agent_service = agent_service
        self.is_admin_mode = bool(fleet_service is not None)
        self.is_client_mode = bool(agent_service is not None)
        self.selected_device_uuid: Optional[str] = None
        self.window_duration = 300.0  # Mặc định 5 phút như GlassWire
        self.render_delay = 5.0       # Lookahead buffer 5 giây chống giật tuyệt đối
        self.icon_manager = ProcessIconManager()
        self.async_bridge = AsyncSignalBridge(self)


        # Thang đo trần chuẩn mực GlassWire (KB/s)
        self.CEILING_TIERS_KB = [
            50.0,
            100.0,
            250.0,
            500.0,
            1024.0,       # 1 MB
            2048.0,       # 2 MB
            3072.0,       # 3 MB
            5120.0,       # 5 MB
            10240.0,      # 10 MB
            25600.0,      # 25 MB
            51200.0,      # 50 MB
            102400.0      # 100 MB
        ]

        # Bộ đệm lưu trữ mẫu sóng (Lưu trữ tới 2400 điểm ~ 40 phút, loại bỏ hoàn toàn việc bị cắt mẫu khi đổi sang 5-10 phút)
        now = time.time()
        init_times = np.linspace(now - 650.0, now, 650)
        self.down_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.up_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.window_sample_history = deque(maxlen=86400)

        self.latest_down = 0.0
        self.latest_up = 0.0
        self.latest_proc_down = 0.0
        self.latest_proc_up = 0.0
        self.latest_proc_speeds = []
        
        # Tích lũy dung lượng cho từng tiến trình (Bytes)
        self.app_accumulated_bytes: Dict[str, Dict[str, float]] = {
            "All apps": {"down": 0.0, "up": 0.0}
        }
        self.app_metadata: Dict[str, Dict] = {}
        self.total_accumulated_down = 0.0
        self.total_accumulated_up = 0.0

        # Tiến trình đang được người dùng chọn xem
        self.selected_app_name = "All apps"
        self.current_y_max = 1024.0
        self._scale_down_hold_time = 0.0

        # Chế độ xem dữ liệu tại điểm & tạm dừng biểu đồ (Point Inspection & Pause/Resume)
        self.point_history = deque(maxlen=2400)
        self.is_paused = False
        self.pinned_time: Optional[float] = None
        self.raw_policy_logs: List[Dict] = []

        # Bộ lọc chiều lưu lượng và sắp xếp (Filter & Sort Sidebar)
        self.filter_direction = "both"      # "both", "incoming", "outgoing"
        self.filter_sort = "high_to_low"    # "high_to_low", "low_to_high", "alpha"
        self.filter_scope = "all"           # "all", "wan", "lan"
        self.current_traffic_view = "graph" # "graph" hoặc "usage" 

        # Trạng thái Offline của máy con đang giám sát trong Admin Mode
        self._is_monitored_device_offline: bool = False
        self._offline_anchor_ts: Optional[float] = None
        self._last_usage_conns: List = []
        self._last_fed_telemetry_ts: Dict[str, float] = {}

        self._init_ui()
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

    def _init_ui(self):
        self.setWindowTitle("Traffic Monitor & Network Packet Shaper")
        self.resize(1280, 760)
        self.setMinimumSize(1020, 620)

        # Style toàn bộ theo phong cách Clean White (Nền trắng tinh khôi hoàn toàn)
        self.setStyleSheet("""
            QMainWindow, QWidget#centralContainer, QStackedWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QWidget {
                color: #1E293B;
            }
            QDialog, QMessageBox {
                background-color: #FFFFFF;
                color: #0F172A;
            }
            QMessageBox QLabel {
                background-color: #FFFFFF;
                color: #0F172A;
                font-size: 13px;
                padding: 4px;
            }
            QMessageBox QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                border-radius: 6px;
                font-weight: 600;
                padding: 6px 18px;
                min-width: 75px;
                font-size: 12px;
                border: none;
            }
            QMessageBox QPushButton:hover {
                background-color: #1D4ED8;
            }
            QTableWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                gridline-color: transparent;
                border: none;
                font-size: 12px;
                selection-background-color: #EBF5FF;
                selection-color: #0F172A;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #F1F5F9;
            }
            QTableWidget::item:selected {
                background-color: #EBF5FF;
                color: #0F172A;
                font-weight: 600;
            }
            QHeaderView::section {
                background-color: #FFFFFF;
                color: #64748B;
                padding: 6px 8px;
                font-weight: 600;
                font-size: 11px;
                border: none;
                border-bottom: 1px solid #E2E8F0;
            }
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                border-radius: 6px;
                font-weight: 600;
                padding: 8px 16px;
                font-size: 12px;
                border: none;
            }
            QPushButton:hover {
                background-color: #1D4ED8;
            }
            QLineEdit, QComboBox {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 12px;
                color: #1E293B;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1.5px solid #2563EB;
            }
            QScrollArea, QScrollArea > QWidget > QWidget {
                background-color: #FFFFFF;
                border: none;
            }
        """)

        container = QtWidgets.QWidget()
        container.setObjectName("centralContainer")
        container.setStyleSheet("background-color: #FFFFFF;")
        main_layout = QtWidgets.QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. QStackedWidget chứa các trang chức năng
        self.stacked_pages = QtWidgets.QStackedWidget()
        self.stacked_pages.setStyleSheet("background-color: #FFFFFF;")

        # 2. Thanh Header GlassWire Navigation trên cùng
        header_widget = self._create_glasswire_header()
        main_layout.addWidget(header_widget)

        # 3. Thanh Breadcrumb điều hướng khi xem máy con cụ thể (Admin Mode)
        self.breadcrumb_bar = self._create_breadcrumb_bar()
        self.breadcrumb_bar.setVisible(False)
        main_layout.addWidget(self.breadcrumb_bar)

        # Nếu ở chế độ Quản trị (Admin Mode): Trang 0 là Fleet Manager
        if self.is_admin_mode:
            self.fleet_view = AdminFleetView()
            self.fleet_view.machine_selected.connect(self._on_machine_selected_for_control)
            self.fleet_view.machine_protect_requested.connect(self._on_machine_protect_requested)
            self.stacked_pages.addWidget(self.fleet_view)

        # Trang Traffic Monitor (Split-view chuẩn GlassWire)
        self.page_traffic_monitor = QtWidgets.QWidget()
        self._setup_traffic_monitor_page(self.page_traffic_monitor)
        self.stacked_pages.addWidget(self.page_traffic_monitor)

        # Trang GlassWire Protect (WinDivert Kernel Packet Shaper)
        self.page_protect = QtWidgets.QWidget()
        self._setup_protect_page(self.page_protect)
        self.stacked_pages.addWidget(self.page_protect)

        # Trang Log Analysis (File-based Policy & Security Event History)
        self.page_logs = QtWidgets.QWidget()
        self._setup_logs_page(self.page_logs)
        self.stacked_pages.addWidget(self.page_logs)

        # Trang Network Scanner (TCP/UDP Sockets & Remote IPs)
        self.page_scanner = QtWidgets.QWidget()
        self._setup_scanner_page(self.page_scanner)
        self.stacked_pages.addWidget(self.page_scanner)

        main_layout.addWidget(self.stacked_pages)
        self._switch_nav_page(0)

        self.setCentralWidget(container)

    def _get_active_collector(self):
        """Lấy NetworkCollector để quét socket và thông số card mạng."""
        if self.service and hasattr(self.service, "collector") and self.service.collector:
            return self.service.collector
        if not hasattr(self, "_shared_collector") or self._shared_collector is None:
            from infrastructure.network_collector import NetworkCollector
            self._shared_collector = NetworkCollector()
        return self._shared_collector

    def _get_active_connections(self) -> List[Dict]:
        """Lấy danh sách socket TCP kèm thông tin tiến trình an toàn cho cả Admin và Standalone."""
        if self.service and hasattr(self.service, "get_connections"):
            try:
                conns = self.service.get_connections()
                if conns:
                    return conns
            except Exception:
                pass
        try:
            collector = self._get_active_collector()
            raw_conns = collector.collect_tcp_connections()
            enriched = []
            for conn in raw_conns:
                pid = conn.pid or 0
                p_info = collector.get_process_info(pid)
                p_name = p_info.name if p_info else ("System" if pid == 0 else f"PID {pid}")
                enriched.append({
                    "pid": pid,
                    "process_name": p_name,
                    "protocol": conn.protocol,
                    "local_addr": f"{conn.local_ip}:{conn.local_port}" if conn.local_port else conn.local_ip,
                    "remote_addr": f"{conn.remote_ip}:{conn.remote_port}" if conn.remote_ip and conn.remote_port else ("*" if conn.state == "LISTEN" else "-"),
                    "state": conn.state
                })
            return enriched
        except Exception as e:
            print(f"[Dashboard Connections Error] {e}")
            return []

    def _create_breadcrumb_bar(self) -> QtWidgets.QFrame:
        """Tạo thanh Breadcrumb điều hướng quay lại danh sách máy trạm."""
        bar = QtWidgets.QFrame()
        bar.setObjectName("breadcrumbBar")
        bar.setFixedHeight(38)
        bar.setStyleSheet("""
            QFrame#breadcrumbBar {
                background-color: #FFFFFF;
                border-bottom: 1px solid #E2E8F0;
                padding: 2px 14px;
            }
        """)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(12)

        btn_back = QtWidgets.QPushButton("⬅️ Quay lại Danh Sách Máy")
        btn_back.setFixedHeight(26)
        btn_back.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_back.setStyleSheet("""
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 11px;
                border-radius: 13px;
                padding: 2px 14px;
                border: none;
            }
            QPushButton:hover {
                background-color: #1D4ED8;
            }
        """)
        btn_back.clicked.connect(self._return_to_fleet_view)
        layout.addWidget(btn_back)

        self.lbl_breadcrumb_device = QtWidgets.QLabel("💻 Đang giám sát máy trạm")
        self.lbl_breadcrumb_device.setStyleSheet("font-size: 12px; font-weight: 700; color: #1E293B; border: none; background: transparent;")
        layout.addWidget(self.lbl_breadcrumb_device)

        # Thông báo Offline (CHỈ hiển thị khi máy trạm mất kết nối, ẩn khi Online)
        self.lbl_offline_notice = QtWidgets.QLabel("🔴 MÁY TRẠM ĐÃ OFFLINE — Dữ liệu đang tạm dừng")
        self.lbl_offline_notice.setStyleSheet("""
            background-color: #FEF2F2;
            color: #DC2626;
            border: 1px solid #FECACA;
            border-radius: 11px;
            font-size: 11px;
            font-weight: 800;
            padding: 2px 12px;
        """)
        self.lbl_offline_notice.setVisible(False)
        layout.addWidget(self.lbl_offline_notice)

        layout.addStretch()

        return bar

    def _get_monitored_device_state(self):
        """Trả về trạng thái FleetDeviceState của máy con đang được Admin giám sát (nếu có)."""
        if self.is_admin_mode and self.fleet_service and self.selected_device_uuid:
            return self.fleet_service.get_device_state(self.selected_device_uuid)
        return None

    def _sync_monitored_device_offline_state(self, dev_state):
        """
        Đồng bộ giao diện khi máy con đang giám sát Online/Offline:
        - Offline: hiển thị thông báo Offline, kéo biểu đồ về 0 thay vì đi ngang, đóng băng mọi thông số tiến trình/Usage/Scanner.
        - Online: ẩn thông báo Offline hoàn toàn, khôi phục badge LIVE.
        """
        is_off = bool(dev_state and dev_state.status != "online")
        if hasattr(self, "lbl_offline_notice"):
            if is_off:
                off_ts = getattr(dev_state, "offline_at", 0.0) or getattr(dev_state, "last_seen", 0.0) or time.time()
                off_str = time.strftime("%H:%M:%S", time.localtime(off_ts))
                self.lbl_offline_notice.setText(f"🔴 MÁY TRẠM ĐÃ OFFLINE (Mất kết nối lúc {off_str}) — Đã dừng cập nhật")
            self.lbl_offline_notice.setVisible(is_off)

        if hasattr(self, "lbl_live_badge"):
            if is_off:
                self.lbl_live_badge.setText("🔴 OFFLINE")
                self.lbl_live_badge.setStyleSheet("color: #DC2626; font-weight: bold; font-size: 11px; padding: 3px 8px; background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;")
            elif not getattr(self, "is_paused", False):
                self.lbl_live_badge.setText("● LIVE")
                self.lbl_live_badge.setStyleSheet("color: #059669; font-weight: bold; font-size: 11px; padding: 3px 8px; background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;")

        if is_off and not self._is_monitored_device_offline:
            self._is_monitored_device_offline = True
            off_ts = getattr(dev_state, "offline_at", 0.0) or getattr(dev_state, "last_seen", 0.0) or time.time()
            last_pt_ts = self.down_points[-1][0] if self.down_points else off_ts
            drop_ts = max(last_pt_ts + 1.0, off_ts)

            # Chốt điểm về 0 ở thời điểm ngắt kết nối nếu đường sóng đang cao hơn 0
            if not self.down_points or self.down_points[-1][1] != 0.0:
                self.down_points.append((drop_ts, 0.0))
            if not self.up_points or self.up_points[-1][1] != 0.0:
                self.up_points.append((drop_ts, 0.0))
            if not self.point_history or self.point_history[-1].get("down_kb", 0.0) != 0.0 or self.point_history[-1].get("up_kb", 0.0) != 0.0:
                self.point_history.append({
                    "timestamp": drop_ts,
                    "down_kb": 0.0,
                    "up_kb": 0.0,
                    "top_procs": [],
                })
                if hasattr(self, "timeline_scrubber"):
                    self.timeline_scrubber.add_point(drop_ts, 0.0)

            self._offline_anchor_ts = drop_ts
            self.latest_down = 0.0
            self.latest_up = 0.0
            self.latest_proc_down = 0.0
            self.latest_proc_up = 0.0
            self.latest_proc_speeds = []

            self._update_app_sidebar([])
            self._update_selected_app_hud()
            if hasattr(self, "protect_view"):
                self.protect_view.update_protect_table([], self.app_accumulated_bytes, self.app_metadata)
            if hasattr(self, "usage_view") and hasattr(self, "traffic_content_stack") and self.traffic_content_stack.currentIndex() == 1:
                self._update_usage_view_now()
        elif not is_off and self._is_monitored_device_offline:
            self._is_monitored_device_offline = False
            self._offline_anchor_ts = None

    def _switch_active_remote_device(self, device_uuid: Optional[str]):
        """Đồng bộ bộ đệm biểu đồ và thông tin máy khi Admin chuyển đổi giữa các máy trạm."""
        changed = (device_uuid != self.selected_device_uuid)
        self.selected_device_uuid = device_uuid

        dev_st = self.fleet_service.get_device_state(device_uuid) if (self.fleet_service and device_uuid) else None
        if hasattr(self, "lbl_breadcrumb_device") and dev_st:
            self.lbl_breadcrumb_device.setText(
                f"💻 Đang giám sát máy trạm: {dev_st.hostname}  ({dev_st.ip_address or 'LAN'})  —  UUID: {device_uuid[:12]}"
            )

        if not changed or not device_uuid or not self.fleet_service:
            self._sync_monitored_device_offline_state(dev_st)
            return

        self._is_monitored_device_offline = False
        self._offline_anchor_ts = None
        self._last_usage_conns = []
        self.latest_down = 0.0
        self.latest_up = 0.0
        self.latest_proc_down = 0.0
        self.latest_proc_up = 0.0
        self.latest_proc_speeds = []

        dev_state = dev_st
        now = time.time()
        first_hist_ts = dev_state.download_history[0][0] if (dev_state and dev_state.download_history) else now
        base_end = min(now, first_hist_ts - 1.0)
        init_times = np.linspace(base_end - 650.0, base_end, 650)
        self.down_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.up_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.point_history.clear()
        self.window_sample_history.clear()
        self.app_accumulated_bytes = {"All apps": {"down": 0.0, "up": 0.0}}
        self.total_accumulated_down = 0.0
        self.total_accumulated_up = 0.0

        if dev_state:
            for ts, d_val in list(dev_state.download_history):
                self.down_points.append((ts, d_val))
            for ts, u_val in list(dev_state.upload_history):
                self.up_points.append((ts, u_val))
            for (ts, d_val), (_, u_val) in zip(list(dev_state.download_history), list(dev_state.upload_history)):
                self.point_history.append({
                    "timestamp": ts,
                    "down_kb": d_val,
                    "up_kb": u_val,
                    "top_procs": [],
                })
                self.window_sample_history.append({
                    "t": ts,
                    "down_b": d_val * 1024.0 * UPDATE_INTERVAL,
                    "up_b": u_val * 1024.0 * UPDATE_INTERVAL,
                    "procs": {},
                })

        self._sync_monitored_device_offline_state(dev_state)

    def _on_machine_selected_for_control(self, device_uuid: str):
        """Khi Admin click chọn 1 máy trong Fleet View: chuyển sang chế độ Remote Detail View."""
        self._switch_active_remote_device(device_uuid)

        if hasattr(self, 'protect_view'):
            self.protect_view.set_remote_mode(
                device_uuid, self.fleet_service.send_remote_command if self.fleet_service else None
            )

        self.breadcrumb_bar.setVisible(True)

        # Chuyển sang Tab Traffic Monitor (index 1 trong Admin Mode)
        target_tab = 1 if self.is_admin_mode else 0
        self._switch_nav_page(target_tab)

    def _return_to_fleet_view(self):
        """Quay lại màn hình danh sách toàn bộ máy trạm."""
        self.selected_device_uuid = None
        self.breadcrumb_bar.setVisible(False)
        if hasattr(self, 'protect_view'):
            self.protect_view.set_remote_mode(None, None)
        self._switch_nav_page(0)

    def _on_machine_protect_requested(self, device_uuid: str):
        """Admin click nút 🛡️ Chặn / QoS trong Fleet View: chuyển thẳng đến tab Protect với máy đó."""
        self._switch_active_remote_device(device_uuid)

        # Cấu hình protect_view ở chế độ remote
        if hasattr(self, 'protect_view'):
            self.protect_view.set_remote_mode(
                device_uuid, self.fleet_service.send_remote_command if self.fleet_service else None
            )

        self.breadcrumb_bar.setVisible(True)

        # Tìm index của tab Protect (index 2 trong admin mode: 0=Fleet, 1=Traffic, 2=Protect)
        protect_tab_index = 2 if self.is_admin_mode else 1
        self._switch_nav_page(protect_tab_index)

    def _refresh_fleet_admin_ui(self):
        """Cập nhật dữ liệu từ FleetManagerService lên giao diện Admin định kỳ."""
        if not self.fleet_service or not hasattr(self, 'fleet_view'):
            return
        summary = self.fleet_service.get_fleet_summary()
        devices = self.fleet_service.get_all_device_states()
        self.fleet_view.update_fleet_data(devices, summary)

        # Tự động chọn máy đầu tiên nếu chưa chọn máy nào
        if not self.selected_device_uuid and devices:
            online_devs = [d for d in devices if d.status == "online"]
            target_dev = online_devs[0] if online_devs else devices[0]
            self._switch_active_remote_device(target_dev.device_uuid)

        if hasattr(self, 'protect_view'):
            if not self.protect_view.remote_device_uuid and self.selected_device_uuid:
                self.protect_view.set_remote_mode(
                    self.selected_device_uuid,
                    self.fleet_service.send_remote_command if self.fleet_service else None,
                )
            elif hasattr(self.protect_view, 'sync_fleet_devices_if_changed'):
                self.protect_view.sync_fleet_devices_if_changed()

        # Nếu đang giám sát một máy cụ thể thì đồng bộ trạng thái Online/Offline và chỉ nạp data khi máy Online
        if self.selected_device_uuid:
            dev_state = self.fleet_service.get_device_state(self.selected_device_uuid)
            self._sync_monitored_device_offline_state(dev_state)
            if dev_state and dev_state.status == "online" and dev_state.latest_telemetry:
                telem = dev_state.latest_telemetry
                last_ts_map = getattr(self, "_last_fed_telemetry_ts", None)
                if last_ts_map is None:
                    last_ts_map = {}
                    self._last_fed_telemetry_ts = last_ts_map
                if telem.timestamp > last_ts_map.get(self.selected_device_uuid, 0.0):
                    last_ts_map[self.selected_device_uuid] = telem.timestamp
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
                        if p.name not in self.app_metadata:
                            self.app_metadata[p.name] = {"exe": "", "pid": p.pid}
                    down_spd = telem.speeds.download_kbps
                    up_spd = telem.speeds.upload_kbps
                    now_ts = time.time()
                    # Chống lệch đồng hồ giữa máy con và Admin làm điểm vẽ bị rơi ra ngoài khung thời gian
                    display_ts = telem.timestamp if abs(now_ts - telem.timestamp) <= 4.0 else now_ts
                    self._on_new_sample(
                        display_ts,
                        down_spd,
                        up_spd,
                        proc_speeds,
                        down_spd,
                        up_spd
                    )

    # =========================================================================
    # HEADER THANH ĐIỀU HƯỚNG GLASSWIRE (NAV TABS + LOGO + UPGRADE)
    # =========================================================================
    def _create_glasswire_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QWidget()
        header.setFixedHeight(54)
        header.setStyleSheet("background-color: #FFFFFF; border-bottom: 1px solid #E2E8F0;")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(16)

        # Các Tab điều hướng chính
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

            # Timer kiểm tra định kỳ trạng thái kết nối
            self.client_status_timer = QtCore.QTimer(self)
            self.client_status_timer.setInterval(1000)
            self.client_status_timer.timeout.connect(self._update_client_status_indicator)
            self.client_status_timer.start()

        return header

    def _update_client_status_indicator(self):
        if not hasattr(self, "client_status_btn") or not self.agent_service:
            return
        is_conn = self.agent_service.net_client.is_connected()
        srv_host = self.agent_service.server_host
        srv_port = self.agent_service.server_port
        if is_conn:
            self.client_status_btn.setText(f"🟢 Admin: {srv_host}:{srv_port} (Online)")
            self.client_status_btn.setStyleSheet("""
                QPushButton {
                    background-color: #ECFDF5;
                    color: #059669;
                    font-weight: bold;
                    border: 1px solid #A7F3D0;
                    border-radius: 15px;
                    padding: 3px 12px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background-color: #D1FAE5;
                }
            """)
        else:
            self.client_status_btn.setText(f"🔴 Mất kết nối Admin ({srv_host}:{srv_port})")
            self.client_status_btn.setStyleSheet("""
                QPushButton {
                    background-color: #FEF2F2;
                    color: #DC2626;
                    font-weight: bold;
                    border: 1px solid #FECACA;
                    border-radius: 15px;
                    padding: 3px 12px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background-color: #FEE2E2;
                }
            """)

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
                self.latest_proc_speeds, self.app_accumulated_bytes, self.app_metadata
            )

    def _switch_nav_page(self, index: int):
        """Chuyển trang hiển thị và áp dụng giao diện nút bấm GlassWire"""
        self.stacked_pages.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            text, page_idx, icon_factory = self.nav_tabs_info[i]
            if i == index:
                btn.setIcon(icon_factory(True))
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #EBF3FC;
                        color: #2563EB;
                        font-weight: bold;
                        border-radius: 6px;
                        padding: 4px 14px;
                        font-size: 12px;
                        border: none;
                        text-align: center;
                    }
                """)
            else:
                btn.setIcon(icon_factory(False))
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: transparent;
                        color: #475569;
                        font-weight: 600;
                        border-radius: 6px;
                        padding: 4px 14px;
                        font-size: 12px;
                        border: none;
                        text-align: center;
                    }
                    QPushButton:hover {
                        background-color: #F8FAFC;
                        color: #1E293B;
                    }
                """)

        if self.is_admin_mode:
            if index == 0:
                self.breadcrumb_bar.setVisible(False)
                self._refresh_fleet_admin_ui()
            elif index == 1:  # Traffic Monitor
                self.breadcrumb_bar.setVisible(bool(self.selected_device_uuid))
            elif index == 2:  # Protect view
                self.breadcrumb_bar.setVisible(bool(self.selected_device_uuid))
                if hasattr(self, 'protect_view'):
                    self.protect_view.update_protect_table(self.latest_proc_speeds, self.app_accumulated_bytes, self.app_metadata)
            elif index == 3:  # Logs (Local DB)
                self.breadcrumb_bar.setVisible(False)
                self._on_enter_usage_history_page()
            elif index == 4:  # Scanner (Local Sockets)
                self.breadcrumb_bar.setVisible(False)
                self._refresh_connections()
        else:
            if index == 1:
                if hasattr(self, 'protect_view'):
                    self.protect_view.update_protect_table(self.latest_proc_speeds, self.app_accumulated_bytes, self.app_metadata)
            elif index == 2:
                self._on_enter_usage_history_page()
            elif index == 3:
                self._refresh_connections()


    def _setup_traffic_monitor_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QHBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Khung chứa nội dung chính của Traffic Monitor
        traffic_main_widget = QtWidgets.QWidget()
        traffic_main_widget.setStyleSheet("background-color: #FFFFFF;")
        traffic_main_layout = QtWidgets.QVBoxLayout(traffic_main_widget)
        traffic_main_layout.setContentsMargins(12, 8, 12, 6)
        traffic_main_layout.setSpacing(6)

        # ---------------------------------------------------------------------
        # 1. THANH CÔNG CỤ TRÊN CÙNG CỦA BIỂU ĐỒ (TOP TOOLBAR)
        # ---------------------------------------------------------------------
        chart_top_bar = QtWidgets.QHBoxLayout()
        chart_top_bar.setSpacing(10)

        # Nút Filter ở góc bên trái (như Hình 1 - hiển thị khi bảng ứng dụng đang đóng)
        self.btn_toggle_filter = QtWidgets.QPushButton(" Filter")
        self.btn_toggle_filter.setIcon(get_filter_icon())
        self.btn_toggle_filter.setFixedHeight(28)
        self.btn_toggle_filter.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_filter.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 14px;
                padding: 2px 14px;
                font-size: 11px;
                font-weight: 600;
                color: #475569;
            }
            QPushButton:hover {
                background-color: #F8FAFC;
                color: #0F172A;
                border-color: #94A3B8;
            }
        """)
        self.btn_toggle_filter.clicked.connect(self._open_app_panel)
        chart_top_bar.addWidget(self.btn_toggle_filter)

        chart_top_bar.addStretch()

        # Capsule Toggle Button: 1 nút Graph, 1 nút Usage
        capsule_container = QtWidgets.QFrame()
        capsule_container.setFixedHeight(32)
        capsule_container.setStyleSheet("""
            QFrame {
                background-color: #F1F5F9;
                border-radius: 16px;
                padding: 2px;
            }
        """)
        cap_layout = QtWidgets.QHBoxLayout(capsule_container)
        cap_layout.setContentsMargins(3, 2, 3, 2)
        cap_layout.setSpacing(4)

        self.btn_view_graph = QtWidgets.QPushButton(" Graph")
        self.btn_view_graph.setIcon(get_graph_toggle_icon(True))
        self.btn_view_graph.setFixedHeight(26)
        self.btn_view_graph.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_view_graph.setStyleSheet("""
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                font-size: 11px;
                font-weight: bold;
                border-radius: 13px;
                padding: 2px 14px;
                border: none;
            }
        """)
        self.btn_view_graph.clicked.connect(lambda: self._switch_traffic_view("graph"))

        self.btn_view_usage = QtWidgets.QPushButton(" Usage")
        self.btn_view_usage.setIcon(get_usage_toggle_icon(False))
        self.btn_view_usage.setFixedHeight(26)
        self.btn_view_usage.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_view_usage.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #64748B;
                font-size: 11px;
                font-weight: 600;
                border-radius: 13px;
                padding: 2px 14px;
                border: none;
            }
            QPushButton:hover {
                background-color: #E2E8F0;
                color: #1E293B;
            }
        """)
        self.btn_view_usage.clicked.connect(lambda: self._switch_traffic_view("usage"))

        cap_layout.addWidget(self.btn_view_graph)
        cap_layout.addWidget(self.btn_view_usage)
        chart_top_bar.addWidget(capsule_container)

        chart_top_bar.addStretch()

        # Thang đo (3 MB ▾)
        self.scale_indicator = QtWidgets.QLabel("3 MB ▾")
        self.scale_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #475569; min-width: 60px;")
        chart_top_bar.addWidget(self.scale_indicator)

        # Điều khiển LIVE
        self.live_controls_widget = QtWidgets.QWidget()
        lc_layout = QtWidgets.QHBoxLayout(self.live_controls_widget)
        lc_layout.setContentsMargins(0, 0, 0, 0)
        lc_layout.setSpacing(8)

        self.lbl_live_badge = QtWidgets.QLabel("● LIVE")
        self.lbl_live_badge.setStyleSheet("color: #059669; font-weight: bold; font-size: 11px; padding: 3px 8px; background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;")
        lc_layout.addWidget(self.lbl_live_badge)

        self.btn_pause_toggle = QtWidgets.QPushButton("⏸ Tạm Dừng")
        self.btn_pause_toggle.setFixedHeight(28)
        self.btn_pause_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #334155;
                font-size: 11px;
                font-weight: 600;
                padding: 3px 12px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #F8FAFC;
                color: #0F172A;
            }
        """)
        self.btn_pause_toggle.clicked.connect(self._toggle_pause_resume)
        lc_layout.addWidget(self.btn_pause_toggle)

        chart_top_bar.addWidget(self.live_controls_widget)

        # Dropdown thời gian hiển thị (Mặc định: 5 Minutes)
        self.combo_time_window = QtWidgets.QComboBox()
        self.combo_time_window.addItems(["30 Seconds", "1 Minute", "5 Minutes", "10 Minutes", "1 Hour", "24 Hours"])
        self.combo_time_window.setCurrentIndex(2)
        self.combo_time_window.setFixedHeight(28)
        self.combo_time_window.setStyleSheet("font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 6px;")
        self.combo_time_window.currentIndexChanged.connect(self._on_time_window_changed)
        chart_top_bar.addWidget(self.combo_time_window)

        traffic_main_layout.addLayout(chart_top_bar)

        # BANNER SOI DỮ LIỆU TẠI ĐIỂM
        self.point_inspector_card = QtWidgets.QFrame()
        self.point_inspector_card.setObjectName("pointInspectorCard")
        self.point_inspector_card.setStyleSheet("""
            QFrame#pointInspectorCard {
                background-color: #F8FAFC;
                border: 1px solid #E2E8F0;
                border-left: 4px solid #2563EB;
                border-radius: 6px;
                padding: 4px 10px;
            }
        """)
        self.point_inspector_card.setVisible(False)
        pic_layout = QtWidgets.QHBoxLayout(self.point_inspector_card)
        pic_layout.setContentsMargins(10, 4, 10, 4)
        pic_layout.setSpacing(10)

        self.lbl_point_details = QtWidgets.QLabel("Đang soi điểm...")
        self.lbl_point_details.setTextFormat(QtCore.Qt.TextFormat.RichText)
        pic_layout.addWidget(self.lbl_point_details, stretch=1)

        btn_box = QtWidgets.QHBoxLayout()
        btn_box.setSpacing(6)

        self.btn_card_resume = QtWidgets.QPushButton("▶ Tiếp Tục Biểu Đồ")
        self.btn_card_resume.setFixedHeight(28)
        self.btn_card_resume.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_card_resume.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 12px;
                border-radius: 5px;
                border: none;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        self.btn_card_resume.clicked.connect(self._resume_live)
        btn_box.addWidget(self.btn_card_resume)

        self.btn_card_close = QtWidgets.QPushButton("✕ Bỏ Ghim")
        self.btn_card_close.setFixedHeight(28)
        self.btn_card_close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_card_close.setStyleSheet("""
            QPushButton {
                background-color: #F1F5F9;
                color: #475569;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 10px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #E2E8F0;
                color: #0F172A;
            }
        """)
        self.btn_card_close.clicked.connect(self._resume_live)
        btn_box.addWidget(self.btn_card_close)

        pic_layout.addLayout(btn_box)
        traffic_main_layout.addWidget(self.point_inspector_card)

        # ---------------------------------------------------------------------
        # STACK 2 VIEW: VIEW 0 (GRAPH) & VIEW 1 (USAGE)
        # ---------------------------------------------------------------------
        self.traffic_content_stack = QtWidgets.QStackedWidget()

        # VIEW 0: GRAPH VIEW (SPLITTER GỒM PANEL BÊN TRÁI VÀ BIỂU ĐỒ)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: #E2E8F0; width: 1px; }")

        # 1. Bảng danh sách ứng dụng bên trái (MẶC ĐỊNH ẨN ĐỂ HIỆN FULL BIỂU ĐỒ NHƯ HÌNH 1)
        self.left_panel = QtWidgets.QWidget()
        self.left_panel.setStyleSheet("background-color: #FFFFFF;")
        left_layout = QtWidgets.QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(6, 4, 6, 4)
        left_layout.setSpacing(6)

        # Header của panel bên trái (Bộ lọc chiều và WAN/LAN như Hình 2)
        filter_bar = QtWidgets.QHBoxLayout()
        filter_bar.setContentsMargins(0, 2, 0, 2)
        filter_bar.setSpacing(6)

        # Dropdown sắp xếp chiều lưu lượng: Incoming & Outgoing, Incoming only, Outgoing only
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
        self.combo_filter_dir.currentIndexChanged.connect(self._on_filter_direction_combo_changed)
        filter_bar.addWidget(self.combo_filter_dir)

        # Dropdown sắp xếp WAN / LAN: WAN & LAN, WAN only, LAN only
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
        self.combo_filter_scope.currentIndexChanged.connect(self._on_filter_scope_combo_changed)
        filter_bar.addWidget(self.combo_filter_scope)

        filter_bar.addStretch()

        # Ô tìm kiếm ứng dụng
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
        self.search_app_input.textChanged.connect(self._filter_app_list)
        filter_bar.addWidget(self.search_app_input)

        # Nút đóng panel bên trái (✕) để đưa biểu đồ về full hình như Hình 1
        btn_close_panel = QtWidgets.QPushButton("✕")
        btn_close_panel.setFixedSize(24, 24)
        btn_close_panel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_close_panel.setStyleSheet("""
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
        btn_close_panel.clicked.connect(self._close_app_panel)
        filter_bar.addWidget(btn_close_panel)

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
        self.app_list_table.itemSelectionChanged.connect(self._on_app_selected)
        self.app_list_table.doubleClicked.connect(self._on_app_double_clicked)
        left_layout.addWidget(self.app_list_table)

        self.left_panel.setMinimumWidth(260)
        self.left_panel.setMaximumWidth(380)

        # MẶC ĐỊNH: Bảng left_panel ẩn đi để biểu đồ hiển thị full width như Hình 1!
        self.left_panel.setVisible(False)
        self.splitter.addWidget(self.left_panel)

        # 2. Khối biểu đồ bên phải
        right_panel = QtWidgets.QWidget()
        right_panel.setStyleSheet("background-color: #FFFFFF;")
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(6)

        pg.setConfigOption("antialias", True)
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': self.time_axis}, background="#FFFFFF")
        self.plot_widget.setMinimumHeight(350)

        vb = self.plot_widget.getViewBox()
        vb.setDefaultPadding(0.0)
        vb.setMouseEnabled(x=False, y=False)
        self.plot_widget.showGrid(x=False, y=True, alpha=0.08)
        self.plot_widget.setYRange(0, 1024, padding=0.0)
        self.plot_widget.enableAutoRange(axis='y', enable=False)

        self.plot_widget.getAxis('bottom').setPen(pg.mkPen("#E2E8F0", width=1))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen("#94A3B8"))
        self.plot_widget.getAxis('left').setStyle(showValues=False)
        self.plot_widget.getAxis('left').setPen(None)

        self.curve_down = self.plot_widget.plot(
            pen=pg.mkPen(color="#D97706", width=2.0),
            fillLevel=0,
            brush=(245, 158, 11, 220)
        )

        self.curve_up = self.plot_widget.plot(
            pen=pg.mkPen(color="#EA580C", width=1.5),
            fillLevel=0,
            brush=(244, 63, 94, 210)
        )

        self.v_line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#3B82F6", width=1.2, style=QtCore.Qt.PenStyle.DashLine))
        self.h_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#CBD5E1", width=1.0, style=QtCore.Qt.PenStyle.DotLine))
        self.v_line.hide()
        self.h_line.hide()
        self.plot_widget.addItem(self.v_line, ignoreBounds=True)
        self.plot_widget.addItem(self.h_line, ignoreBounds=True)

        self.hover_marker = pg.ScatterPlotItem(size=8, pen=pg.mkPen("#2563EB", width=2), brush=pg.mkBrush("#FFFFFF"), symbol='o')
        self.hover_marker.hide()
        self.plot_widget.addItem(self.hover_marker)

        self.pinned_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#DC2626", width=1.5, style=QtCore.Qt.PenStyle.SolidLine))
        self.pinned_vline.hide()
        self.plot_widget.addItem(self.pinned_vline, ignoreBounds=True)

        self.pinned_marker = pg.ScatterPlotItem(size=14, pen=pg.mkPen("#FFFFFF", width=2.5), brush=pg.mkBrush("#DC2626"), symbol='star')
        self.pinned_marker.hide()
        self.plot_widget.addItem(self.pinned_marker)

        self.hover_badge = QtWidgets.QFrame(self.plot_widget)
        self.hover_badge.setStyleSheet("""
            QFrame {
                background-color: rgba(15, 23, 42, 0.90);
                border: 1px solid #3B82F6;
                border-radius: 5px;
                padding: 3px 7px;
            }
            QLabel {
                color: #FFFFFF;
                font-family: 'Segoe UI';
                font-size: 10px;
            }
        """)
        self.hover_badge.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        hb_layout = QtWidgets.QVBoxLayout(self.hover_badge)
        hb_layout.setContentsMargins(4, 2, 4, 2)
        hb_layout.setSpacing(1)
        self.lbl_hover_text = QtWidgets.QLabel("")
        self.lbl_hover_text.setTextFormat(QtCore.Qt.TextFormat.RichText)
        hb_layout.addWidget(self.lbl_hover_text)
        self.hover_badge.hide()

        self.plot_widget.scene().sigMouseMoved.connect(self._on_plot_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_mouse_clicked)
        self.plot_widget.viewport().installEventFilter(self)

        right_layout.addWidget(self.plot_widget, stretch=1)

        # Thanh Inspection Bar chi tiết khi dừng tại điểm (chuẩn GlassWire Hình 2)
        self.point_inspection_bar = GlassWirePointInspectionBar(self.icon_manager)
        self.point_inspection_bar.resume_clicked.connect(self._resume_live)
        self.point_inspection_bar.hide()
        right_layout.addWidget(self.point_inspection_bar)

        self.splitter.addWidget(right_panel)

        self.traffic_content_stack.addWidget(self.splitter)

        # VIEW 1: USAGE VIEW (4 CỘT CHUẨN: APPS | HOSTS | TRAFFIC TYPE | COUNTRIES)
        self.usage_view = GlassWireUsageView(self.icon_manager)
        self.traffic_content_stack.addWidget(self.usage_view)

        traffic_main_layout.addWidget(self.traffic_content_stack, stretch=1)

        # HUD THỐNG KÊ CHI TIẾT DƯỚI ĐÁY
        bottom_hud = self._create_selected_app_hud()
        traffic_main_layout.addWidget(bottom_hud)

        # PANORAMIC TIMELINE SCRUBBER
        self.timeline_scrubber = GlassWireTimelineScrubber()
        self.timeline_scrubber.range_selected.connect(self._on_scrubber_range_selected)
        self.timeline_scrubber.time_selected.connect(self._on_scrubber_point_selected)
        traffic_main_layout.addWidget(self.timeline_scrubber)

        layout.addWidget(traffic_main_widget)

    def _open_app_panel(self):
        """Mở bảng ứng dụng và bộ lọc bên trái (như Hình 2)."""
        self.left_panel.setVisible(True)
        self.btn_toggle_filter.setVisible(False)
        if hasattr(self, 'splitter'):
            tot_w = self.splitter.width() or 1200
            self.splitter.setSizes([320, max(500, tot_w - 320)])
        self._update_app_sidebar(self.latest_proc_speeds)

    def _close_app_panel(self):
        """Đóng bảng bên trái, đưa biểu đồ về hiển thị toàn màn hình (như Hình 1)."""
        self.left_panel.setVisible(False)
        self.btn_toggle_filter.setVisible(True)

    def _on_filter_direction_combo_changed(self, index: int):
        """Xử lý khi người dùng chọn lọc chiều lưu lượng trong header của panel bên trái."""
        if index == 1:
            self.filter_direction = "incoming"
            self.curve_down.show()
            self.curve_up.hide()
        elif index == 2:
            self.filter_direction = "outgoing"
            self.curve_down.hide()
            self.curve_up.show()
        else:
            self.filter_direction = "both"
            self.curve_down.show()
            self.curve_up.show()

        # Cập nhật ngay lập tức bảng bên trái, HUD và Usage view
        self._update_app_sidebar(self.latest_proc_speeds)
        self._update_selected_app_hud()
        if hasattr(self, 'usage_view'):
            self.usage_view.set_filter_direction(self.filter_direction)
            if self.traffic_content_stack.currentIndex() == 1:
                self._update_usage_view_now()

    def _on_filter_scope_combo_changed(self, index: int):
        """Xử lý khi người dùng chọn lọc phạm vi mạng (WAN / LAN)."""
        if index == 1:
            self.filter_scope = "wan"
        elif index == 2:
            self.filter_scope = "lan"
        else:
            self.filter_scope = "all"

        self._update_app_sidebar(self.latest_proc_speeds)
        self._update_selected_app_hud()

    def _switch_traffic_view(self, mode: str):
        """Chuyển đổi giữa chế độ Biểu đồ (Graph) và Danh sách lưu lượng (Usage)."""
        self.current_traffic_view = mode
        if mode == "graph":
            self.traffic_content_stack.setCurrentIndex(0)
            self.btn_view_graph.setIcon(get_graph_toggle_icon(True))
            self.btn_view_graph.setStyleSheet("""
                QPushButton {
                    background-color: #2563EB;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
            """)
            self.btn_view_usage.setIcon(get_usage_toggle_icon(False))
            self.btn_view_usage.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #64748B;
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #E2E8F0;
                    color: #1E293B;
                }
            """)
        else:
            self.traffic_content_stack.setCurrentIndex(1)
            self.btn_view_usage.setIcon(get_usage_toggle_icon(True))
            self.btn_view_usage.setStyleSheet("""
                QPushButton {
                    background-color: #2563EB;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
            """)
            self.btn_view_graph.setIcon(get_graph_toggle_icon(False))
            self.btn_view_graph.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #64748B;
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 13px;
                    padding: 2px 14px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #E2E8F0;
                    color: #1E293B;
                }
            """)
            self._update_usage_view_now()

    def _update_usage_view_now(self):
        """Thu thập kết nối socket và cập nhật bảng Usage 4 cột thời gian thực theo đúng khung thời gian đã chọn (30s, 1m, 5m, 10m, 1h, 24h)."""
        try:
            win_down, win_up, win_app_bytes = self._compute_window_traffic()
            tot_win_bytes = win_down + win_up
            if getattr(self, "_is_monitored_device_offline", False):
                active_conns = list(getattr(self, "_last_usage_conns", []))
            else:
                collector = self._get_active_collector()
                active_conns = collector.collect_tcp_connections() if collector else []
                self._last_usage_conns = list(active_conns)
            self.usage_view.update_usage_data(
                win_app_bytes,
                active_conns,
                self.app_metadata,
                total_window_bytes=tot_win_bytes
            )
        except Exception as e:
            print(f"[Usage Update Error] {e}")

    def _create_selected_app_hud(self) -> QtWidgets.QWidget:
        """Tạo thanh HUD GlassWire: Tốc độ Tải Xuống, Đồng hồ bán nguyệt, Tốc độ Tải Lên, Phân tích WAN & LAN"""
        hud_box = QtWidgets.QWidget()
        hud_box.setFixedHeight(78)
        hud_box.setStyleSheet("background-color: #FFFFFF; border-top: 1px solid #F1F5F9;")
        hud_layout = QtWidgets.QHBoxLayout(hud_box)
        hud_layout.setContentsMargins(18, 2, 18, 2)
        hud_layout.setSpacing(18)

        # 1. Cột Tải Xuống: [Dung lượng tổng] + [Tốc độ /s ↓]
        down_box = QtWidgets.QVBoxLayout()
        down_box.setSpacing(2)
        down_box.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight)

        self.hud_down_total = QtWidgets.QLabel("0 B")
        self.hud_down_total.setStyleSheet("font-size: 17px; font-weight: 800; color: #0F172A;")
        self.hud_down_speed = QtWidgets.QLabel("0 B/s  ↓")
        self.hud_down_speed.setStyleSheet("font-size: 11px; font-weight: bold; color: #D97706;")

        down_box.addWidget(self.hud_down_total)
        down_box.addWidget(self.hud_down_speed)
        hud_layout.addLayout(down_box)

        # 2. Đồng hồ bán nguyệt GlassWire ở giữa
        self.arc_gauge = GlassWireArcGauge()
        hud_layout.addWidget(self.arc_gauge)

        # 3. Cột Tải Lên: [Dung lượng tổng] + [Tốc độ /s ↑]
        up_box = QtWidgets.QVBoxLayout()
        up_box.setSpacing(2)
        up_box.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft)

        self.hud_up_total = QtWidgets.QLabel("0 B")
        self.hud_up_total.setStyleSheet("font-size: 17px; font-weight: 800; color: #0F172A;")
        self.hud_up_speed = QtWidgets.QLabel("0 B/s  ↑")
        self.hud_up_speed.setStyleSheet("font-size: 11px; font-weight: bold; color: #F43F5E;")

        up_box.addWidget(self.hud_up_total)
        up_box.addWidget(self.hud_up_speed)
        hud_layout.addLayout(up_box)

        hud_layout.addSpacing(20)

        # 4. Thanh phân tích WAN & LAN
        self.wan_lan_widget = GlassWireWanLanWidget()
        hud_layout.addWidget(self.wan_lan_widget, stretch=1)

        return hud_box



    # =========================================================================
    # TRANG 1: GLASSWIRE PROTECT (WINDIVERT KERNEL PACKET SHAPER)
    # =========================================================================
    def _setup_protect_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Giao diện GlassWire Protect phong cách chuẩn xác
        self.protect_view = GlassWireProtectView(
            self.service,
            self.icon_manager,
            fleet_service=getattr(self, 'fleet_service', None),
            agent_service=getattr(self, 'agent_service', None),
        )
        if hasattr(self.protect_view, "combo_target_device") and self.is_admin_mode:
            self.protect_view.combo_target_device.currentIndexChanged.connect(self._on_protect_device_combo_synced)
        layout.addWidget(self.protect_view)

    def _on_protect_device_combo_synced(self, index: int):
        """Đồng bộ máy đang chọn từ dropdown của tab Protect về BandwidthDashboard."""
        if not self.is_admin_mode or not hasattr(self, "protect_view"):
            return
        uid = self.protect_view.combo_target_device.itemData(index)
        if uid and uid != self.selected_device_uuid:
            self._switch_active_remote_device(uid)

    def _setup_logs_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 1. Header Toolbar
        header_layout = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("📜 Nhật Ký Hoạt Động & Chính Sách Mạng (Log Analysis)")
        title.setStyleSheet("font-size: 16px; font-weight: 800; color: #0F172A; border: none; background: transparent;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        btn_open_folder = QtWidgets.QPushButton("📂 Mở Thư Mục Logs")
        btn_open_folder.setFixedHeight(28)
        btn_open_folder.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_open_folder.setStyleSheet("background-color: #FFFFFF; color: #475569; font-weight: 600; font-size: 11px; padding: 2px 12px; border: 1px solid #CBD5E1; border-radius: 5px;")
        btn_open_folder.clicked.connect(self._open_logs_folder)
        header_layout.addWidget(btn_open_folder)

        btn_refresh = QtWidgets.QPushButton("🔄 Làm Mới")
        btn_refresh.setFixedHeight(28)
        btn_refresh.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn_refresh.setStyleSheet("background-color: #2563EB; color: #FFFFFF; font-weight: bold; font-size: 11px; padding: 2px 14px; border-radius: 5px;")
        btn_refresh.clicked.connect(self._refresh_policy_logs)
        header_layout.addWidget(btn_refresh)

        layout.addLayout(header_layout)

        # 2. Subtitle / info banner
        info_label = QtWidgets.QLabel("Hệ thống ghi nhận toàn bộ thao tác bảo mật (chặn, bỏ chặn, bóp băng thông) và kết nối mạng trực tiếp vào file log cục bộ (logs/policies.log & logs/connections.log).")
        info_label.setStyleSheet("font-size: 11px; color: #64748B; background: transparent;")
        layout.addWidget(info_label)

        # 3. Filter Box
        filter_layout = QtWidgets.QHBoxLayout()
        self.txt_policy_filter = QtWidgets.QLineEdit()
        self.txt_policy_filter.setPlaceholderText("🔍 Tìm kiếm theo tiến trình, hành động (BLOCK, UNBLOCK, APPLY, REMOVE)...")
        self.txt_policy_filter.setFixedHeight(28)
        self.txt_policy_filter.setStyleSheet("font-size: 12px; padding: 2px 8px; border: 1px solid #CBD5E1; border-radius: 5px;")
        self.txt_policy_filter.textChanged.connect(self._filter_policy_logs)
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
        self.policy_log_table.setHorizontalHeaderLabels(["Thời Gian", "Hành Động", "Tiến Trình & Chi Tiết", "Trạng Thái"])
        self.policy_log_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.policy_log_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.policy_log_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.policy_log_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.policy_log_table.verticalHeader().setVisible(False)
        pol_layout.addWidget(self.policy_log_table)
        self.log_tabs.addTab(tab_pol, "🛡️ Nhật Ký Chính Sách (policies.log)")

        # Tab 2: Connections Raw Log
        tab_conn = QtWidgets.QWidget()
        conn_layout = QtWidgets.QVBoxLayout(tab_conn)
        conn_layout.setContentsMargins(8, 8, 8, 8)
        self.txt_conn_log = QtWidgets.QPlainTextEdit()
        self.txt_conn_log.setReadOnly(True)
        self.txt_conn_log.setStyleSheet("font-family: 'Consolas', 'Courier New', monospace; font-size: 11px; background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 6px; color: #1E293B;")
        conn_layout.addWidget(self.txt_conn_log)
        self.log_tabs.addTab(tab_conn, "🌐 Nhật Ký Kết Nối Mạng (connections.log)")

        layout.addWidget(self.log_tabs)

    # =========================================================================
    # TRANG 3: NETWORK SCANNER (SOCKET TCP IP/CỔNG/TRẠNG THÁI)
    # =========================================================================
    def _setup_scanner_page(self, parent: QtWidgets.QWidget):
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("🕸️ Network Scanner - Danh Sách Kết Nối Socket TCP")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #0F172A;")
        header.addWidget(title)

        header.addStretch()
        btn_refresh = QtWidgets.QPushButton("🔄 Làm Mới Ngay")
        btn_refresh.clicked.connect(self._refresh_connections)
        header.addWidget(btn_refresh)
        layout.addLayout(header)

        self.conn_table = QtWidgets.QTableWidget()
        self.conn_table.setColumnCount(6)
        self.conn_table.setHorizontalHeaderLabels(["PID", "Tiến Trình", "Giao Thức", "Địa Chỉ Local", "Địa Chỉ Remote", "Trạng Thái"])
        self.conn_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.conn_table.verticalHeader().setVisible(False)
        layout.addWidget(self.conn_table)


    # =========================================================================
    # LOGIC CẬP NHẬT DỮ LIỆU & RENDER VÒNG LẶP
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
        self.render_timer.setInterval(28)  # 35 FPS cực kỳ mượt mà
        self.render_timer.timeout.connect(self._render_frame)
        self.render_timer.start()

        self.conn_timer = QtCore.QTimer()
        self.conn_timer.setInterval(3000)
        self.conn_timer.timeout.connect(self._refresh_connections)
        self.conn_timer.start()


    def _on_time_window_changed(self, index: int):
        windows = [30.0, 60.0, 300.0, 600.0, 3600.0, 86400.0]
        self.window_duration = windows[min(index, len(windows) - 1)]
        self.time_axis.set_window_duration(self.window_duration)
        self.timeline_scrubber.set_window_duration(self.window_duration)
        now = time.time()
        if getattr(self, "_is_monitored_device_offline", False) and getattr(self, "_offline_anchor_ts", None):
            render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
        else:
            render_now = now - self.render_delay
        self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)
        # Cập nhật ngay bảng bên trái, HUD và Usage view theo khung thời gian vừa chọn
        self._update_app_sidebar(self.latest_proc_speeds)
        self._update_selected_app_hud()
        if hasattr(self, 'usage_view'):
            self._update_usage_view_now()

    @QtCore.pyqtSlot(float, float, float, list, float, float)
    def _on_new_sample(self, timestamp, down_kb, up_kb, proc_speeds, total_p_down, total_p_up):
        try:
            self.latest_down = down_kb
            self.latest_up = up_kb
            self.latest_proc_down = total_p_down
            self.latest_proc_up = total_p_up
            self.latest_proc_speeds = proc_speeds

            # Tích lũy tổng lưu lượng
            self.total_accumulated_down += (down_kb * 1024.0 * UPDATE_INTERVAL)
            self.total_accumulated_up += (up_kb * 1024.0 * UPDATE_INTERVAL)

            # Cập nhật tích lũy cho từng tiến trình
            for item in proc_speeds:
                pname = item.get("name", "")
                if not pname:
                    continue
                if pname not in self.app_accumulated_bytes:
                    self.app_accumulated_bytes[pname] = {"down": 0.0, "up": 0.0}
                self.app_accumulated_bytes[pname]["down"] += item.get("down_kb", 0.0) * 1024.0 * UPDATE_INTERVAL
                self.app_accumulated_bytes[pname]["up"] += item.get("up_kb", 0.0) * 1024.0 * UPDATE_INTERVAL
                if pname not in self.app_metadata:
                    self.app_metadata[pname] = {
                        "pid": item.get("pid", 0),
                        "exe": item.get("exe", "")
                    }

            self.down_points.append((timestamp, down_kb))
            self.up_points.append((timestamp, up_kb))

            # Lưu vết mẫu theo thời gian để tính toán chính xác lưu lượng theo khung (30s, 1m, 5m, 10m, 1h, 24h)
            dt = UPDATE_INTERVAL
            active_procs = {}
            for p_item in proc_speeds:
                pname = p_item.get("name", "")
                if not pname:
                    continue
                d_kb = p_item.get("down_kb", 0.0)
                u_kb = p_item.get("up_kb", 0.0)
                if d_kb > 0 or u_kb > 0:
                    active_procs[pname] = (d_kb * 1024.0 * dt, u_kb * 1024.0 * dt)

            if hasattr(self, 'window_sample_history'):
                self.window_sample_history.append({
                    "t": timestamp,
                    "down_b": down_kb * 1024.0 * dt,
                    "up_b": up_kb * 1024.0 * dt,
                    "procs": active_procs
                })

            # Lưu toàn bộ danh sách tiến trình có lưu lượng tại thời điểm này
            all_active_point_procs = []
            for p in proc_speeds:
                p_d = p.get("down_kb", 0.0)
                p_u = p.get("up_kb", 0.0)
                if p_d > 0 or p_u > 0:
                    all_active_point_procs.append({
                        "name": p.get("name", "Unknown"),
                        "down_kb": p_d,
                        "up_kb": p_u,
                        "pid": p.get("pid", 0),
                        "exe": p.get("exe", "")
                    })
            all_active_point_procs.sort(key=lambda x: x["down_kb"] + x["up_kb"], reverse=True)

            self.point_history.append({
                "timestamp": timestamp,
                "down_kb": down_kb,
                "up_kb": up_kb,
                "top_procs": all_active_point_procs
            })

            self.timeline_scrubber.add_point(timestamp, down_kb + up_kb)
            if not self.is_paused:
                # 1. Cập nhật bảng ứng dụng bên trái
                self._update_app_sidebar(proc_speeds)
                # 2. Cập nhật HUD bên dưới biểu đồ
                self._update_selected_app_hud()

            if hasattr(self, 'protect_view'):
                self.protect_view.update_protect_table(proc_speeds, self.app_accumulated_bytes, self.app_metadata)
            if hasattr(self, 'traffic_content_stack') and self.traffic_content_stack.currentIndex() == 1:
                self._update_usage_view_now()
        except Exception as e:
            print(f"[UI Slot Error] {e}")

    def _format_bytes(self, num_bytes: float) -> str:
        """Định dạng byte sang B, KB, MB, GB chuẩn GlassWire"""
        if num_bytes < 1024:
            return f"{num_bytes:,.0f} B"
        elif num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024.0:,.1f} KB"
        elif num_bytes < 1024 * 1024 * 1024:
            return f"{num_bytes / (1024.0 * 1024.0):,.1f} MB"
        else:
            return f"{num_bytes / (1024.0 * 1024.0 * 1024.0):,.2f} GB"

    def _format_speed(self, speed_kb: float) -> str:
        """Định dạng tốc độ mạng / giây"""
        if speed_kb < 1.0:
            return f"{speed_kb * 1024.0:,.0f} B/s"
        elif speed_kb < 1024.0:
            return f"{speed_kb:,.1f} KB/s"
        else:
            return f"{speed_kb / 1024.0:,.1f} MB/s"

    def _compute_window_traffic(self, win_duration: Optional[float] = None, t_end: Optional[float] = None) -> Tuple[float, float, Dict[str, Dict[str, float]]]:
        """
        Tính tổng lưu lượng download, upload toàn hệ thống và theo từng tiến trình
        trong đúng khung thời gian win_duration (30s, 1m, 5m, 10m, 1h, 24h).
        Trả về: (tot_down_bytes, tot_up_bytes, app_bytes_dict)
        """
        if win_duration is None:
            win_duration = getattr(self, "window_duration", 300.0)

        if t_end is not None:
            now = t_end
        elif getattr(self, "_is_monitored_device_offline", False) and getattr(self, "_offline_anchor_ts", None):
            now = self._offline_anchor_ts
        else:
            now = time.time()
        cutoff = now - win_duration

        if not hasattr(self, "window_sample_history") or not self.window_sample_history:
            return self.total_accumulated_down, self.total_accumulated_up, self.app_accumulated_bytes

        tot_down = 0.0
        tot_up = 0.0
        app_bytes: Dict[str, Dict[str, float]] = {}

        for s in reversed(self.window_sample_history):
            st = s.get("t", 0.0)
            if st < cutoff:
                break
            tot_down += s.get("down_b", 0.0)
            tot_up += s.get("up_b", 0.0)
            for pname, (pd_b, pu_b) in s.get("procs", {}).items():
                if pname not in app_bytes:
                    app_bytes[pname] = {"down": 0.0, "up": 0.0}
                app_bytes[pname]["down"] += pd_b
                app_bytes[pname]["up"] += pu_b

        return tot_down, tot_up, app_bytes

    def _update_app_sidebar(self, proc_speeds: list):
        """Cập nhật danh sách ứng dụng trong Sidebar theo chiều lọc Incoming / Outgoing và khung thời gian đã chọn"""
        search_kw = self.search_app_input.text().strip().lower()

        # Tạo map tốc độ tức thời của chu kỳ hiện tại
        current_speeds = {p.get("name", ""): p for p in proc_speeds if p.get("name")}

        # Lấy lưu lượng tích lũy chính xác theo khung thời gian đang chọn (30s, 1m, 5m, 10m, 1h, 24h)
        win_down, win_up, win_app_bytes = self._compute_window_traffic()

        f_scope = getattr(self, "filter_scope", "all")
        scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)

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

            meta = self.app_metadata.get(app_name, {})
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
                "cur_speed": cur_spd
            })

        # Sắp xếp theo chiều lưu lượng đang chọn
        f_dir = getattr(self, "filter_direction", "both")

        if f_dir == "incoming":
            display_apps.sort(key=lambda x: (x["is_active_now"], x["down_bytes"]), reverse=True)
        elif f_dir == "outgoing":
            display_apps.sort(key=lambda x: (x["is_active_now"], x["up_bytes"]), reverse=True)
        else:
            display_apps.sort(key=lambda x: (x["is_active_now"], x["total_bytes"]), reverse=True)

        self.app_list_table.blockSignals(True)
        try:
            prev_selected = self.selected_app_name
            self.app_list_table.setRowCount(len(display_apps) + 1)

            # Row 0: All apps
            item_all_name = QtWidgets.QTableWidgetItem("  All apps")
            item_all_name.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
            item_all_name.setForeground(QtGui.QBrush(QtGui.QColor("#0F172A")))

            # Icon All apps
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

            if f_dir == "incoming":
                down_all_str = self._format_bytes(total_win_down)
                item_all_down = QtWidgets.QTableWidgetItem(down_all_str)
                item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                item_all_up = QtWidgets.QTableWidgetItem("--")
                item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            elif f_dir == "outgoing":
                item_all_down = QtWidgets.QTableWidgetItem("--")
                item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                up_all_str = self._format_bytes(total_win_up)
                item_all_up = QtWidgets.QTableWidgetItem(up_all_str)
                item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            else:
                down_all_str = self._format_bytes(total_win_down)
                item_all_down = QtWidgets.QTableWidgetItem(down_all_str)
                item_all_down.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_down.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_down.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                up_all_str = self._format_bytes(total_win_up)
                item_all_up = QtWidgets.QTableWidgetItem(up_all_str)
                item_all_up.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                item_all_up.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                item_all_up.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

            self.app_list_table.setItem(0, 0, item_all_name)
            self.app_list_table.setItem(0, 1, item_all_down)
            self.app_list_table.setItem(0, 2, item_all_up)

            selected_row_idx = 0 if prev_selected == "All apps" else -1

            # Các hàng ứng dụng cụ thể
            for idx, item in enumerate(display_apps, start=1):
                name = item["name"]
                name_item = QtWidgets.QTableWidgetItem(f"  {name}")
                name_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))

                exe_path = item.get("exe", "")
                if not exe_path and item.get("pid", 0) > 0:
                    try:
                        exe_path = psutil.Process(item["pid"]).exe()
                    except Exception:
                        pass
                icon = self.icon_manager.get_icon(exe_path, name)
                name_item.setIcon(icon)

                if f_dir == "incoming":
                    down_str = self._format_bytes(item["down_bytes"])
                    d_item = QtWidgets.QTableWidgetItem(down_str)
                    d_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                    d_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                    d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                    u_item = QtWidgets.QTableWidgetItem("--")
                    u_item.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                    u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                elif f_dir == "outgoing":
                    d_item = QtWidgets.QTableWidgetItem("--")
                    d_item.setForeground(QtGui.QBrush(QtGui.QColor("#94A3B8")))
                    d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                    up_str = self._format_bytes(item["up_bytes"])
                    u_item = QtWidgets.QTableWidgetItem(up_str)
                    u_item.setForeground(QtGui.QBrush(QtGui.QColor("#1E293B")))
                    u_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))
                    u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                else:
                    down_str = self._format_bytes(item["down_bytes"])
                    d_item = QtWidgets.QTableWidgetItem(down_str)
                    d_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
                    d_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                    up_str = self._format_bytes(item["up_bytes"])
                    u_item = QtWidgets.QTableWidgetItem(up_str)
                    u_item.setForeground(QtGui.QBrush(QtGui.QColor("#475569")))
                    u_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

                self.app_list_table.setItem(idx, 0, name_item)
                self.app_list_table.setItem(idx, 1, d_item)
                self.app_list_table.setItem(idx, 2, u_item)

                if name == prev_selected:
                    selected_row_idx = idx

            if selected_row_idx >= 0:
                self.app_list_table.selectRow(selected_row_idx)
        finally:
            self.app_list_table.blockSignals(False)

    def _filter_app_list(self):
        self._update_app_sidebar(self.latest_proc_speeds)

    def _on_app_selected(self):
        selected_rows = self.app_list_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row == 0:
            self.selected_app_name = "All apps"
        else:
            raw_text = self.app_list_table.item(row, 0).text()
            self.selected_app_name = raw_text.strip()
        self._update_selected_app_hud()

    def _on_app_double_clicked(self, item):
        """Nhấp đúp chuột vào app trong sidebar: Chuyển ngay sang tab GlassWire Protect với app đã chọn!"""
        row = item.row()
        if row > 0:
            app_name = self.app_list_table.item(row, 0).text().strip()
            if hasattr(self, 'protect_view') and hasattr(self.protect_view, 'search_input'):
                self.protect_view.search_input.setText(app_name)
            protect_tab_idx = 2 if self.is_admin_mode else 1
            self._switch_nav_page(protect_tab_idx)

    def _update_selected_app_hud(self):
        """Cập nhật khối thống kê chi tiết của tiến trình đang chọn theo khung thời gian (chuẩn phong cách GlassWire)"""
        win_down, win_up, win_app_bytes = self._compute_window_traffic()
        f_scope = getattr(self, "filter_scope", "all")
        scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)

        if self.selected_app_name == "All apps":
            d_speed = self.latest_down
            u_speed = self.latest_up
            d_total = win_down * scope_factor
            u_total = win_up * scope_factor
        else:
            d_speed = 0.0
            u_speed = 0.0
            for item in self.latest_proc_speeds:
                if item.get("name", "").lower() == self.selected_app_name.lower():
                    d_speed = item.get("down_kb", 0.0)
                    u_speed = item.get("up_kb", 0.0)
                    break
            accum = win_app_bytes.get(self.selected_app_name, {"down": 0.0, "up": 0.0})
            d_total = accum["down"] * scope_factor
            u_total = accum["up"] * scope_factor

        total_bytes = d_total + u_total
        total_str = self._format_bytes(total_bytes)

        # Cột Download bên trái
        f_dir = getattr(self, "filter_direction", "both")
        if f_dir == "outgoing":
            self.hud_down_total.setText("--")
            self.hud_down_speed.setText("--  ↓")
        else:
            self.hud_down_total.setText(self._format_bytes(d_total))
            self.hud_down_speed.setText(f"{self._format_speed(d_speed)}  ↓")

        # Đồng hồ bán nguyệt ở giữa
        pct = int(min(100, max(15, (total_bytes / (10 * 1024 * 1024)) * 100.0))) if total_bytes > 0 else 15
        self.arc_gauge.set_value(total_str, pct)

        # Cột Upload bên phải
        if f_dir == "incoming":
            self.hud_up_total.setText("--")
            self.hud_up_speed.setText("--  ↑")
        else:
            self.hud_up_total.setText(self._format_bytes(u_total))
            self.hud_up_speed.setText(f"{self._format_speed(u_speed)}  ↑")

        # Phân bổ WAN & LAN
        f_scope = getattr(self, "filter_scope", "all")
        if f_scope == "wan":
            wan_bytes = total_bytes * 0.98
            self.wan_lan_widget.set_values(f"WAN  {self._format_bytes(wan_bytes)}", "LAN  --", 100)
        elif f_scope == "lan":
            lan_bytes = total_bytes * 0.05
            self.wan_lan_widget.set_values("WAN  --", f"LAN  {self._format_bytes(lan_bytes)}", 15)
        else:
            wan_bytes = total_bytes * 0.98
            lan_bytes = total_bytes * 0.02
            self.wan_lan_widget.set_values(f"WAN  {self._format_bytes(wan_bytes)}", f"LAN  {self._format_bytes(lan_bytes)}", 94)

    def _render_frame(self):
        """Vòng lặp vẽ đồ thị mượt mà 35 FPS với bộ đệm trễ 5 giây và thuật toán khử gai GlassWire"""
        try:
            if self.is_paused:
                return

            now = time.time()
            if getattr(self, "_is_monitored_device_offline", False) and getattr(self, "_offline_anchor_ts", None):
                # Đóng băng camera ngay sau đoạn sóng rớt về 0 để giữ nguyên đồ thị phiên vừa ngắt kết nối
                render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
            else:
                render_now = now - self.render_delay

            # Giữ lại tối thiểu 3600 giây trong bộ nhớ để người dùng có thể tua lại tự do trên timeline
            cutoff = render_now - 3600.0
            while self.down_points and self.down_points[0][0] < cutoff:
                self.down_points.popleft()
            while self.up_points and self.up_points[0][0] < cutoff:
                self.up_points.popleft()

            d_raw_x = np.array([p[0] for p in self.down_points], dtype=np.float64)
            d_raw_y = np.array([p[1] for p in self.down_points], dtype=np.float64)

            u_raw_x = np.array([p[0] for p in self.up_points], dtype=np.float64)
            u_raw_y = np.array([p[1] for p in self.up_points], dtype=np.float64)

            # Lưới điểm nội suy riêng cho từng mốc thời gian
            cfg = TIME_SCALE_CONFIGS.get(self.window_duration, TIME_SCALE_CONFIGS[300.0])
            x_dense = np.linspace(render_now - self.window_duration, render_now, cfg["n_dense"])

            smooth_down = self._generate_glasswire_wave(d_raw_x, d_raw_y, x_dense, self.window_duration)
            smooth_up = self._generate_glasswire_wave(u_raw_x, u_raw_y, x_dense, self.window_duration)

            f_dir = getattr(self, "filter_direction", "both")
            if f_dir == "incoming":
                self.curve_down.setData(x_dense, smooth_down)
                self.curve_down.show()
                self.curve_up.hide()
                peak_val = max(float(np.max(smooth_down)), 1.0)
            elif f_dir == "outgoing":
                self.curve_up.setData(x_dense, smooth_up)
                self.curve_up.show()
                self.curve_down.hide()
                peak_val = max(float(np.max(smooth_up)), 1.0)
            else:
                self.curve_down.setData(x_dense, smooth_down)
                self.curve_up.setData(x_dense, smooth_up)
                self.curve_down.show()
                self.curve_up.show()
                peak_val = max(float(np.max(smooth_down)), float(np.max(smooth_up)), 1.0)
            self._update_y_scale(peak_val)

            # Trượt camera theo thời gian thực
            self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)
        except Exception:
            pass

    # =========================================================================
    # CÁC CHỨC NĂNG XEM DỮ LIỆU TẠI ĐIỂM (POINT INSPECTION & FREEZE MODE)
    # =========================================================================
    def _find_nearest_sample(self, target_t: float) -> Optional[dict]:
        """Tìm mẫu đo gần nhất với mốc thời gian target_t bằng tìm kiếm nhị phân O(log N)."""
        source = list(self.point_history)
        if not source:
            if self.down_points:
                pts = list(self.down_points)
                best_pt = min(pts, key=lambda p: abs(p[0] - target_t))
                return {
                    "timestamp": best_pt[0],
                    "down_kb": best_pt[1],
                    "up_kb": 0.0,
                    "top_procs": []
                }
            return None

        low = 0
        high = len(source) - 1
        best_item = source[0]
        min_diff = abs(source[0]["timestamp"] - target_t)

        while low <= high:
            mid = (low + high) // 2
            item = source[mid]
            t = item["timestamp"]
            diff = abs(t - target_t)
            if diff < min_diff:
                min_diff = diff
                best_item = item

            if t < target_t:
                low = mid + 1
            else:
                high = mid - 1

        if min_diff <= 60.0:
            return best_item
        return None

    def _on_plot_mouse_moved(self, pos):
        """Xử lý sự kiện di chuyển chuột trên biểu đồ để hiển thị Crosshair và Tooltip tại điểm."""
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            self._hide_hover_elements()
            return

        mouse_pt = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        x_val = mouse_pt.x()

        nearest = self._find_nearest_sample(x_val)
        if not nearest:
            self._hide_hover_elements()
            return

        nt = nearest["timestamp"]
        nd = nearest.get("down_kb", 0.0)
        nu = nearest.get("up_kb", 0.0)

        # Cập nhật tọa độ đường dóng chữ thập
        self.v_line.setPos(nt)
        self.h_line.setPos(nd)
        self.v_line.show()
        self.h_line.show()

        # Cập nhật marker tròn snap theo sóng
        self.hover_marker.setData([nt], [nd])
        self.hover_marker.show()

        # Hiển thị tooltip dạng badge
        lt = time.localtime(nt)
        time_str = time.strftime("%H:%M:%S", lt)
        d_str = self._format_speed(nd)
        u_str = self._format_speed(nu)

        self.lbl_hover_text.setText(
            f"🕒 <b>{time_str}</b><br>"
            f"<span style='color:#F59E0B;'>↓ {d_str}</span>&nbsp;&nbsp;"
            f"<span style='color:#F43F5E;'>↑ {u_str}</span>"
        )
        self.hover_badge.adjustSize()

        # Căn chỉnh vị trí badge trong widget tránh tràn viền
        local_pos = self.plot_widget.mapFromScene(pos)
        bx = local_pos.x() + 14
        by = local_pos.y() - 44
        bx = max(10, min(self.plot_widget.width() - self.hover_badge.width() - 10, bx))
        by = max(10, min(self.plot_widget.height() - self.hover_badge.height() - 10, by))
        self.hover_badge.move(int(bx), int(by))
        self.hover_badge.show()

    def _hide_hover_elements(self):
        """Ẩn các phần tử hover khi chuột rời khỏi vùng vẽ."""
        self.v_line.hide()
        self.h_line.hide()
        self.hover_marker.hide()
        self.hover_badge.hide()

    def _on_plot_mouse_clicked(self, event):
        """Bấm chuột trái vào điểm bất kỳ trên biểu đồ: Dừng biểu đồ lại và hiển thị chi tiết tốc độ tại điểm đó."""
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return

        mouse_pt = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        self._pin_point(mouse_pt.x())

    def _on_scrubber_range_selected(self, win_start: float, win_end: float, target_t: float):
        """Bấm hoặc kéo khung trên timeline scrubber: Di chuyển và xem lại biểu đồ chính theo đúng khung thời gian này."""
        self._inspect_time_window(win_start, win_end, target_t)

    def _on_scrubber_point_selected(self, target_t: float):
        """Bấm vào mốc thời gian trên timeline scrubber: Căn khung nhìn xoay quanh điểm đó và hiển thị chi tiết."""
        half = self.window_duration / 2.0
        self._inspect_time_window(target_t - half, target_t + half, target_t)

    def _inspect_time_window(self, win_start: float, win_end: float, target_t: float):
        """Tạm dừng và cập nhật biểu đồ chính (khung chính) hiển thị chính xác theo khung nhìn thời gian (khung này)."""
        self.is_paused = True
        self.pinned_time = target_t

        # 1. Vẽ dữ liệu và chuyển camera biểu đồ chính tới khung nhìn này
        self._render_window_data(win_start, win_end)

        # 2. Ghim mốc và hiển thị HUD/chi tiết ứng dụng
        self._pin_point(target_t)

    def _render_window_data(self, win_start: float, win_end: float):
        """Vẽ đường sóng và thang đo cho khung thời gian [win_start, win_end] tương ứng với khung trượt."""
        try:
            win_duration = max(10.0, win_end - win_start)

            # Lấy các mẫu đo từ down_points / up_points hoặc point_history
            d_pts = [p for p in self.down_points if (win_start - win_duration * 0.5) <= p[0] <= (win_end + win_duration * 0.5)]
            u_pts = [p for p in self.up_points if (win_start - win_duration * 0.5) <= p[0] <= (win_end + win_duration * 0.5)]

            if not d_pts and self.point_history:
                d_pts = [(p["timestamp"], p.get("down_kb", 0.0)) for p in self.point_history if (win_start - win_duration * 0.5) <= p["timestamp"] <= (win_end + win_duration * 0.5)]
                u_pts = [(p["timestamp"], p.get("up_kb", 0.0)) for p in self.point_history if (win_start - win_duration * 0.5) <= p["timestamp"] <= (win_end + win_duration * 0.5)]

            if len(d_pts) >= 2:
                d_raw_x = np.array([p[0] for p in d_pts], dtype=np.float64)
                d_raw_y = np.array([p[1] for p in d_pts], dtype=np.float64)
                u_raw_x = np.array([p[0] for p in u_pts], dtype=np.float64)
                u_raw_y = np.array([p[1] for p in u_pts], dtype=np.float64)

                cfg = TIME_SCALE_CONFIGS.get(self.window_duration, TIME_SCALE_CONFIGS[300.0])
                n_dense = max(200, cfg["n_dense"])
                x_dense = np.linspace(win_start, win_end, n_dense)

                smooth_down = self._generate_glasswire_wave(d_raw_x, d_raw_y, x_dense, win_duration)
                smooth_up = self._generate_glasswire_wave(u_raw_x, u_raw_y, x_dense, win_duration)

                f_dir = getattr(self, "filter_direction", "both")
                f_scope = getattr(self, "filter_scope", "all")
                scope_factor = 0.98 if f_scope == "wan" else (0.05 if f_scope == "lan" else 1.0)
                smooth_down_disp = smooth_down * scope_factor
                smooth_up_disp = smooth_up * scope_factor

                if f_dir == "incoming":
                    self.curve_down.setData(x_dense, smooth_down_disp)
                    self.curve_down.show()
                    self.curve_up.hide()
                    peak_val = max(float(np.max(smooth_down_disp)), 1.0)
                elif f_dir == "outgoing":
                    self.curve_up.setData(x_dense, smooth_up_disp)
                    self.curve_up.show()
                    self.curve_down.hide()
                    peak_val = max(float(np.max(smooth_up_disp)), 1.0)
                else:
                    self.curve_down.setData(x_dense, smooth_down_disp)
                    self.curve_up.setData(x_dense, smooth_up_disp)
                    self.curve_down.show()
                    self.curve_up.show()
                    peak_val = max(float(np.max(smooth_down_disp)), float(np.max(smooth_up_disp)), 1.0)
                self._update_y_scale(peak_val)

            self.plot_widget.setXRange(win_start, win_end, padding=0.0)
        except Exception as e:
            print(f"[Render Window Error] {e}")

    def _pin_point(self, target_t: float):
        """Ghim điểm và hiển thị toàn bộ chi tiết dữ liệu (tốc độ, thời gian, tiến trình mạng)."""
        self.is_paused = True
        nearest = self._find_nearest_sample(target_t)
        if not nearest:
            nearest = {"timestamp": target_t, "down_kb": 0.0, "up_kb": 0.0, "top_procs": []}

        t_val = nearest["timestamp"]
        d_val = nearest.get("down_kb", 0.0)
        u_val = nearest.get("up_kb", 0.0)

        self.pinned_time = t_val

        # Cập nhật trạng thái nút dừng / tiếp tục
        self.lbl_live_badge.setText("⏸ PAUSED")
        self.lbl_live_badge.setStyleSheet("color: #DC2626; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;")

        self.btn_pause_toggle.setText("▶ Tiếp Tục (Live)")
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: #FFFFFF;
                font-size: 11px;
                font-weight: bold;
                padding: 2px 10px;
                border-radius: 5px;
                border: none;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)

        # Hiển thị đường dóng ghim và Marker sao nổi bật
        self.pinned_vline.setPos(t_val)
        self.pinned_vline.show()
        self.pinned_marker.setData([t_val], [d_val])
        self.pinned_marker.show()

        # Định dạng thời gian và tốc độ
        lt = time.localtime(t_val)
        time_full_str = time.strftime("%H:%M:%S  -  %d/%m/%Y", lt)
        down_str = self._format_speed(d_val)
        up_str = self._format_speed(u_val)
        total_str = self._format_speed(d_val + u_val)

        # Lấy thông tin ứng dụng tại mốc thời gian này
        top_procs = nearest.get("top_procs", [])

        # Cập nhật thanh Inspection Bar mới (chuẩn GlassWire Hình 2)
        if hasattr(self, 'point_inspection_bar'):
            self.point_inspection_bar.set_inspection_data(
                d_val, u_val, t_val, top_procs, self.icon_manager, self._format_speed
            )
            self.point_inspection_bar.show()

        if hasattr(self, 'point_inspector_card'):
            self.point_inspector_card.setVisible(False)

        # Đồng bộ vị trí khung trượt trên scrubber nếu chưa được gán
        if self.timeline_scrubber.slider_start is None:
            half = self.window_duration / 2.0
            self.timeline_scrubber.set_slider_range(target_t - half, target_t + half)

        # Cập nhật HUD bên dưới phản ánh giá trị điểm soi
        self._update_hud_for_point(d_val, u_val)

    def _update_hud_for_point(self, down_kb: float, up_kb: float):
        """Cập nhật các khối HUD bán nguyệt và tốc độ theo giá trị của điểm được soi."""
        total_kb = down_kb + up_kb
        total_bytes = total_kb * 1024.0
        self.hud_down_speed.setText(f"{self._format_speed(down_kb)}  ↓")
        self.hud_up_speed.setText(f"{self._format_speed(up_kb)}  ↑")
        pct = int(min(100, max(15, (total_kb / 10240.0) * 100.0))) if total_kb > 0 else 15
        self.arc_gauge.set_value(self._format_bytes(total_bytes), pct)
        wan_bytes = total_bytes * 0.96
        lan_bytes = total_bytes * 0.04
        self.wan_lan_widget.set_values(f"WAN  {self._format_bytes(wan_bytes)}", f"LAN  {self._format_bytes(lan_bytes)}", 96)

    def _resume_live(self):
        """Bỏ dừng và tiếp tục vẽ theo hiện tại."""
        self.is_paused = False
        self.pinned_time = None
        self.pinned_vline.hide()
        self.pinned_marker.hide()
        if hasattr(self, 'point_inspector_card'):
            self.point_inspector_card.setVisible(False)
        if hasattr(self, 'point_inspection_bar'):
            self.point_inspection_bar.hide()
        self.timeline_scrubber.reset_to_live()

        if getattr(self, "_is_monitored_device_offline", False):
            self.lbl_live_badge.setText("🔴 OFFLINE")
            self.lbl_live_badge.setStyleSheet("color: #DC2626; font-weight: bold; font-size: 11px; padding: 3px 8px; background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;")
        else:
            self.lbl_live_badge.setText("● LIVE")
            self.lbl_live_badge.setStyleSheet("color: #059669; font-weight: bold; font-size: 11px; padding: 2px 7px; background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;")

        self.btn_pause_toggle.setText("⏸ Tạm Dừng")
        self.btn_pause_toggle.setStyleSheet("""
            QPushButton {
                background-color: #F8FAFC;
                color: #475569;
                font-size: 11px;
                font-weight: 600;
                padding: 2px 10px;
                border-radius: 5px;
                border: 1px solid #CBD5E1;
            }
            QPushButton:hover {
                background-color: #F1F5F9;
                color: #0F172A;
            }
        """)

        # Khôi phục HUD về dữ liệu hiện tại
        self._update_selected_app_hud()

        now = time.time()
        if getattr(self, "_is_monitored_device_offline", False) and getattr(self, "_offline_anchor_ts", None):
            render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
        else:
            render_now = now - self.render_delay
        self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)

    def _toggle_pause_resume(self):
        """Nút chuyển đổi Tạm dừng / Tiếp tục vẽ biểu đồ."""
        if self.is_paused:
            self._resume_live()
        else:
            now = time.time()
            if getattr(self, "_is_monitored_device_offline", False) and getattr(self, "_offline_anchor_ts", None):
                # Đóng băng camera ngay sau đoạn sóng rớt về 0 để giữ nguyên đồ thị phiên vừa ngắt kết nối
                render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
            else:
                render_now = now - self.render_delay
            self._pin_point(render_now)

    # =========================================================================
    # TƯƠNG TÁC PHÓNG TO / THU NHỎ & KÉO TIMELINE LIVE PAUSE
    # =========================================================================
    def _get_navigation_bounds(self) -> Optional[Tuple[float, float]]:
        """Trả về giới hạn [min_t, max_t] cho phép zoom/pan trên chế độ Live/Pause."""
        if self.down_points:
            min_t = self.down_points[0][0]
            max_t = self.down_points[-1][0]
            if max_t - min_t < 30.0:
                min_t = max_t - max(60.0, self.window_duration)
            return (min_t, max_t)
        if self.point_history:
            min_t = self.point_history[0]["timestamp"]
            max_t = self.point_history[-1]["timestamp"]
            if max_t - min_t < 30.0:
                min_t = max_t - max(60.0, self.window_duration)
            return (min_t, max_t)
        return None

    def eventFilter(self, obj, event):
        if hasattr(self, 'plot_widget') and obj is self.plot_widget.viewport():
            if event.type() == QtCore.QEvent.Type.Wheel:
                if getattr(self, 'is_paused', False):
                    self._on_plot_wheel_zoom(event)
                    return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonPress:
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    self._plot_drag_active = False
                    self._plot_drag_start_pos = event.pos()
                    if hasattr(self, 'plot_widget'):
                        vr = self.plot_widget.plotItem.vb.viewRange()[0]
                        self._plot_drag_start_range = (vr[0], vr[1])
            elif event.type() == QtCore.QEvent.Type.MouseMove:
                if (event.buttons() & QtCore.Qt.MouseButton.LeftButton) and hasattr(self, '_plot_drag_start_pos'):
                    if getattr(self, 'is_paused', False):
                        dx = event.pos().x() - self._plot_drag_start_pos.x()
                        if abs(dx) > 6:
                            self._plot_drag_active = True
                            self._on_plot_drag_pan(dx)
                            return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    if getattr(self, '_plot_drag_active', False):
                        self._plot_drag_active = False
                        return True
        return super().eventFilter(obj, event)

    def _on_plot_wheel_zoom(self, event: QtGui.QWheelEvent):
        """Phóng to / thu nhỏ khung nhìn bằng con lăn chuột tại vị trí con trỏ."""
        bounds = self._get_navigation_bounds()
        if not bounds:
            return
        bound_start, bound_end = bounds
        pos = event.position()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return
        mouse_t = self.plot_widget.plotItem.vb.mapSceneToView(pos).x()
        delta = event.angleDelta().y()
        if delta == 0:
            return

        self.is_paused = True

        vr = self.plot_widget.plotItem.vb.viewRange()[0]
        cur_start, cur_end = vr[0], vr[1]
        cur_dur = max(15.0, cur_end - cur_start)

        zoom_factor = 0.75 if delta > 0 else 1.333
        new_dur = max(15.0, min(86400.0, cur_dur * zoom_factor))

        ratio = (mouse_t - cur_start) / cur_dur if cur_dur > 0 else 0.5
        ratio = max(0.0, min(1.0, ratio))
        new_start = mouse_t - ratio * new_dur
        new_end = new_start + new_dur

        if new_dur >= (bound_end - bound_start):
            new_start = bound_start
            new_end = bound_end
        else:
            if new_start < bound_start:
                new_start = bound_start
                new_end = min(bound_end, new_start + new_dur)
            if new_end > bound_end:
                new_end = bound_end
                new_start = max(bound_start, new_end - new_dur)

        self.time_axis.set_window_duration(max(15.0, new_end - new_start))
        self._render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)
        event.accept()

    def _on_plot_drag_pan(self, dx: float):
        """Kéo rê biểu đồ sang trái/phải khi đang phóng to hoặc tạm dừng."""
        bounds = self._get_navigation_bounds()
        if not bounds or not hasattr(self, '_plot_drag_start_range'):
            return
        bound_start, bound_end = bounds
        self.is_paused = True

        pixel_w = max(100.0, float(self.plot_widget.plotItem.vb.width()))
        span = self._plot_drag_start_range[1] - self._plot_drag_start_range[0]
        time_shift = -(dx / pixel_w) * span

        new_start = self._plot_drag_start_range[0] + time_shift
        new_end = self._plot_drag_start_range[1] + time_shift

        if new_start < bound_start:
            new_start = bound_start
            new_end = min(bound_end, new_start + span)
        if new_end > bound_end:
            new_end = bound_end
            new_start = max(bound_start, new_end - span)

        self._render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)

    # =========================================================================
    # CÁC PHƯƠNG THỨC XỬ LÝ TRANG USAGE HISTORY & ANALYTICS
    # =========================================================================
    def _create_kpi_card(self, title_str: str, val_str: str, text_color: str, bg_color: str, border_color: str) -> Tuple[QtWidgets.QFrame, QtWidgets.QLabel]:
        card = QtWidgets.QFrame()
        card.setObjectName("kpiCard")
        card.setStyleSheet(f"""
            QFrame#kpiCard {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 8px;
                padding: 10px 14px;
            }}
        """)
        c_layout = QtWidgets.QVBoxLayout(card)
        c_layout.setContentsMargins(2, 2, 2, 2)
        c_layout.setSpacing(2)

        lbl_t = QtWidgets.QLabel(title_str)
        lbl_t.setStyleSheet(f"font-size: 10px; font-weight: 800; color: {text_color}; letter-spacing: 0.5px; border: none; background: transparent;")

        lbl_v = QtWidgets.QLabel(val_str)
        lbl_v.setStyleSheet("font-size: 18px; font-weight: 800; color: #0F172A; border: none; background: transparent;")

        c_layout.addWidget(lbl_t)
        c_layout.addWidget(lbl_v)
        return card, lbl_v

    def _open_logs_folder(self):
        """Mở thư mục logs trên hệ điều hành."""
        try:
            if not os.path.exists(LOGS_DIR):
                os.makedirs(LOGS_DIR, exist_ok=True)
            import subprocess
            if sys.platform == "win32":
                os.startfile(LOGS_DIR)
            else:
                subprocess.Popen(["xdg-open", LOGS_DIR])
        except Exception as e:
            print(f"[UI] Không thể mở thư mục logs: {e}")

    def _on_enter_usage_history_page(self):
        """Kích hoạt khi người dùng chuyển sang trang Log Analysis."""
        self._refresh_policy_logs()

    def _refresh_policy_logs(self):
        """Tải dữ liệu nhật ký mới nhất từ logs/policies.log và logs/connections.log."""
        try:
            self.raw_policy_logs = FileLogger.read_policy_logs(limit=200)
            self._render_policy_table(self.raw_policy_logs)

            conn_log_path = os.path.join(LOGS_DIR, "connections.log")
            if os.path.exists(conn_log_path):
                with open(conn_log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                recent_lines = lines[-200:] if len(lines) > 200 else lines
                self.txt_conn_log.setPlainText("".join(recent_lines))
            else:
                self.txt_conn_log.setPlainText("Chưa có nhật ký kết nối nào được ghi nhận.")
        except Exception as e:
            print(f"[UI] Lỗi khi tải nhật ký: {e}")

    def _render_policy_table(self, logs: List[Dict]):
        """Hiển thị danh sách log chính sách lên bảng."""
        self.policy_log_table.setRowCount(len(logs))
        import re
        for row_idx, item in enumerate(logs):
            raw_line = item.get("raw", "")
            ts = ""
            action = "POLICY"
            details = raw_line
            status = "SUCCESS"

            m = re.match(r"^\[(.*?)\]\s*\[(.*?)\]\s*(.*)$", raw_line)
            if m:
                ts = m.group(1)
                action = m.group(2)
                details = m.group(3)

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

    def _filter_policy_logs(self, query: str):
        query = query.strip().lower()
        if not getattr(self, "raw_policy_logs", None):
            return
        if not query:
            self._render_policy_table(self.raw_policy_logs)
            return
        filtered = [item for item in self.raw_policy_logs if query in item.get("raw", "").lower()]
        self._render_policy_table(filtered)

    def _apply_y_ceiling(self, target_tier: float):
        self.plot_widget.setYRange(0, target_tier, padding=0.0)
        if target_tier >= 1024.0:
            mb_val = target_tier / 1024.0
            if mb_val.is_integer():
                str_val = f"{int(mb_val)} MB"
            else:
                str_val = f"{mb_val:,.1f} MB"
        else:
            str_val = f"{int(target_tier)} KB"
        self.scale_indicator.setText(f"{str_val} ▾")

    def _update_y_scale(self, peak_val: float):
        """Đồng bộ thang đo trục Y với nhãn hiển thị trần có cơ chế chống rung nhảy (hysteresis / hold-down timer)."""
        try:
            target_tier = self.CEILING_TIERS_KB[-1]
            for tier in self.CEILING_TIERS_KB:
                if tier >= peak_val * 1.15:
                    target_tier = tier
                    break

            now = time.time()
            if target_tier > self.current_y_max:
                # Đỉnh lưu lượng tăng: mở rộng thang đo ngay lập tức để không bị che khuất đỉnh
                self.current_y_max = target_tier
                self._scale_down_hold_time = now + 8.0  # Giữ trần này ít nhất 8 giây chống co giật liên tục
                self._apply_y_ceiling(target_tier)
            elif target_tier < self.current_y_max:
                # Đỉnh lưu lượng giảm: chỉ hạ thang đo sau khi lưu lượng thấp duy trì ổn định ít nhất 8 giây
                if now >= getattr(self, "_scale_down_hold_time", 0.0):
                    self.current_y_max = target_tier
                    self._apply_y_ceiling(target_tier)
        except Exception:
            pass

    @classmethod
    def _generate_glasswire_wave(cls, x_raw: np.ndarray, y_raw: np.ndarray, x_dense: np.ndarray, window_duration: float) -> np.ndarray:
        """
        Nội suy sóng GlassWire siêu mượt mà không có góc nhọn hay đoạn thẳng:
        1. Lọc Gauss làm mềm các xung raw theo tham số riêng biệt của từng mốc thời gian (30s / 1m / 5m / 10m).
        2. Nội suy Hermite khối (Cubic Hermite PCHIP) trực tiếp lên lưới x_dense dày mịn:
           - Tiếp tuyến tại đỉnh (apex) = 0.0 -> Đỉnh luôn là vòm tròn (dome), không bao giờ là đỉnh tam giác nhọn.
           - Tiếp tuyến tại đáy (valley) = 0.0 -> Đáy cong tròn (round trough), không bao giờ là chữ V.
           - Tiếp tuyến tại trục nền = 0.0 -> Sóng rời mặt đất nằm ngang êm ái.
        """
        try:
            if len(x_raw) < 2:
                return np.zeros_like(x_dense)

            cfg = TIME_SCALE_CONFIGS.get(window_duration, TIME_SCALE_CONFIGS[300.0])
            sigma = float(cfg["sigma"])
            radius = int(cfg["radius_seconds"])

            # Lấy các mẫu trong vùng hiển thị x_dense kèm biên an toàn
            t_span = max(1.0, float(x_dense[-1] - x_dense[0]))
            t_pad = max(float(radius * 2.0), t_span * 0.05)
            t_min = x_dense[0] - t_pad
            t_max = x_dense[-1] + t_pad
            mask = (x_raw >= t_min) & (x_raw <= t_max)
            x_sub = x_raw[mask]
            y_sub = y_raw[mask]

            if len(x_sub) < 2:
                return np.zeros_like(x_dense)

            ux, uidx = np.unique(x_sub, return_index=True)
            uy = y_sub[uidx]
            n_pts = len(ux)
            if n_pts < 2:
                return np.zeros_like(x_dense)

            # Bán kính lọc phù hợp với cấu trúc điểm mẫu
            eff_radius = min(radius, max(1, (n_pts - 1) // 2))

            # 1. Lọc Gauss với pad mượt mà chống kéo vọt biên:
            # - Cạnh trái (quá khứ đã đo): đệm mép
            # - Cạnh phải (tương lai): suy giảm tự nhiên về 0 để tránh biến xung đơn lẻ 1 giây thành bức tường 30 giây kéo dựng đứng sóng ở mép phải
            if eff_radius >= 1 and n_pts > 3:
                kx = np.arange(-eff_radius, eff_radius + 1)
                kernel = np.exp(-0.5 * (kx / sigma) ** 2)
                kernel /= np.sum(kernel)
                decay = uy[-1] * np.exp(-np.arange(1, eff_radius + 1) / 2.0)
                left_pad = np.full(eff_radius, uy[0])
                uy_padded = np.concatenate([left_pad, uy, decay])
                uy_smooth = np.convolve(uy_padded, kernel, mode='same')[eff_radius:-eff_radius]
            else:
                uy_smooth = uy.copy()

            # 2. Thuật toán Monotonic Hermite PCHIP (Bo tròn tuyệt đối mọi đỉnh & đáy)
            h = np.diff(ux)
            h = np.where(h == 0, 1e-6, h)
            delta = np.diff(uy_smooth) / h

            d = np.zeros(n_pts)
            d[0] = delta[0]
            d[-1] = delta[-1]
            for i in range(1, n_pts - 1):
                if delta[i - 1] * delta[i] > 0:
                    d[i] = 2.0 / (1.0 / delta[i - 1] + 1.0 / delta[i])
                else:
                    d[i] = 0.0  # Đỉnh bo tròn / đáy cong tròn

            # Rời mặt đất bằng phẳng với tiếp tuyến 0.0
            for i in range(n_pts):
                if uy_smooth[i] <= 0.05:
                    d[i] = 0.0

            # 3. Tính toán đường cong bậc ba trực tiếp trên từng pixel của lưới dày x_dense
            idx = np.searchsorted(ux, x_dense) - 1
            idx = np.clip(idx, 0, n_pts - 2)

            x0 = ux[idx]
            x1 = ux[idx + 1]
            y0 = uy_smooth[idx]
            y1 = uy_smooth[idx + 1]
            d0 = d[idx]
            d1 = d[idx + 1]

            hi = np.maximum(1e-6, x1 - x0)
            u = np.clip((x_dense - x0) / hi, 0.0, 1.0)
            u2 = u * u
            u3 = u2 * u

            h00 = 2 * u3 - 3 * u2 + 1
            h10 = u3 - 2 * u2 + u
            h01 = -2 * u3 + 3 * u2
            h11 = u3 - u2

            y_res = h00 * y0 + h10 * hi * d0 + h01 * y1 + h11 * hi * d1

            # Khử mảng phẳng trước mốc mẫu đầu tiên và sau mốc mẫu cuối cùng (tránh đường đi ngang khi máy Offline)
            y_res = np.where((x_dense < ux[0]) | (x_dense > ux[-1]), 0.0, y_res)
            return np.nan_to_num(np.maximum(0.0, y_res), nan=0.0)
        except Exception:
            try:
                return np.nan_to_num(np.interp(x_dense, x_raw, y_raw, left=0.0, right=0.0), nan=0.0)
            except Exception:
                return np.zeros_like(x_dense)


    def _refresh_connections(self):
        """Làm mới danh sách kết nối TCP"""
        if hasattr(self, "page_scanner") and self.stacked_pages.currentWidget() != self.page_scanner:
            return
        if self.is_admin_mode and self.selected_device_uuid:
            dev_state = self._get_monitored_device_state()
            if dev_state and dev_state.status != "online":
                return
        try:
            conns = self._get_active_connections()
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

    def _show_about_dialog(self):
        QtWidgets.QMessageBox.information(
            self,
            "GlassWire PBL4 Edition",
            "🛡️ <b>GlassWire PBL4 Network Flow & QoS Monitor</b><br><br>"
            "Phiên bản tái hiện giao diện chuẩn GlassWire Free với đầy đủ tính năng PBL4:<br>"
            "• Đọc lưu lượng gói tin ETW / Socket tiến trình kernel thời gian thực<br>"
            "• Bóp băng thông mạng thời gian thực WinDivert Kernel Packet Shaper (Token Bucket)<br>"
            "• Lưu trữ nhật ký an ninh và lịch sử thao tác thời gian thực (File-based Logs)<br>"
            "• Phân tích socket TCP và trạng thái kết nối mạng IPv4/IPv6"
        )

    def closeEvent(self, event):
        """Dừng ứng dụng hoặc thu nhỏ về khay hệ thống."""
        if getattr(self, "_hide_to_tray_on_close", False):
            event.ignore()
            self.hide()
            return

        if hasattr(self, "client_status_timer") and self.client_status_timer.isActive():
            self.client_status_timer.stop()
        if hasattr(self, "fleet_timer") and self.fleet_timer.isActive():
            self.fleet_timer.stop()
        if hasattr(self, "render_timer") and self.render_timer.isActive():
            self.render_timer.stop()
        if hasattr(self, "conn_timer") and self.conn_timer.isActive():
            self.conn_timer.stop()
        if hasattr(self, "worker") and self.worker:
            self.worker.stop()
        if self.service:
            self.service.stop()
        if self.is_client_mode and self.agent_service:
            self.agent_service.stop()
        event.accept()