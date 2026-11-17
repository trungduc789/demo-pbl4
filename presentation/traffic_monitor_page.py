"""
Trang Giám sát Lưu lượng Mạng (TrafficMonitorPage - Presentation Layer):
- Được tổ hợp (Composition) từ:
  + TrafficSidebarPanel (Panel danh sách ứng dụng & bộ lọc bên trái).
  + TrafficChartCanvas (Biểu đồ sóng PyQtGraph & thanh Point Inspection).
  + GlassWireUsageView (Chế độ xem thống kê Usage 4 cột).
  + SelectedAppHudBar (Thanh HUD chỉ số dưới đáy).
  + GlassWireTimelineScrubber (Thanh tua toàn cảnh dưới cùng).
- Giao tiếp với BandwidthDashboard qua Qt Signals, không gọi chéo vào các tab khác.
"""

import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple
import numpy as np
from PyQt6 import QtWidgets, QtCore, QtGui

from application.traffic_analytics import (
    TIME_SCALE_CONFIGS,
    CEILING_TIERS_KB,
    format_bytes,
    format_speed,
    format_ceiling_label,
    select_y_ceiling_tier,
    generate_glasswire_wave,
    compute_window_traffic,
    find_nearest_sample,
    get_scope_factor,
)
from config.settings import UPDATE_INTERVAL
from presentation.glasswire_icons import (
    get_graph_toggle_icon,
    get_usage_toggle_icon,
    get_filter_icon,
)
from presentation.traffic_usage_view import GlassWireUsageView
from presentation.traffic_sidebar_panel import TrafficSidebarPanel
from presentation.traffic_chart_canvas import TrafficChartCanvas, SelectedAppHudBar
from presentation.widgets.dashboard_widgets import GlassWireTimelineScrubber
from utils.safe_print import safe_print as print


class TrafficMonitorPage(QtWidgets.QWidget):
    """Trang Traffic Monitor độc lập, tự sở hữu toàn bộ thành phần con."""

    # Phát ra khi người dùng nhấp đúp vào 1 ứng dụng để chuyển sang trang Protect
    app_protect_requested = QtCore.pyqtSignal(str)
    # Phát ra sau khi nhận và tích lũy mẫu lưu lượng mới: (proc_speeds, app_accumulated_bytes, app_metadata)
    sample_processed = QtCore.pyqtSignal(list, dict, dict)

    def __init__(
        self,
        icon_manager,
        fetch_raw_conns_cb: Optional[Callable[[], List]] = None,
        resolve_exe_cb: Optional[Callable[[int], str]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.icon_manager = icon_manager
        self._fetch_raw_conns_cb = fetch_raw_conns_cb
        self._resolve_exe_cb = resolve_exe_cb

        self.window_duration = 300.0
        self.render_delay = 5.0
        self.CEILING_TIERS_KB = list(CEILING_TIERS_KB)

        now = time.time()
        init_times = np.linspace(now - 650.0, now, 650)
        self.down_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.up_points = deque([(t, 0.0) for t in init_times], maxlen=2400)
        self.window_sample_history = deque(maxlen=86400)
        self.point_history = deque(maxlen=2400)

        self.latest_down = 0.0
        self.latest_up = 0.0
        self.latest_proc_down = 0.0
        self.latest_proc_up = 0.0
        self.latest_proc_speeds: List[Dict] = []

        self.app_accumulated_bytes: Dict[str, Dict[str, float]] = {
            "All apps": {"down": 0.0, "up": 0.0}
        }
        self.app_metadata: Dict[str, Dict] = {}
        self.total_accumulated_down = 0.0
        self.total_accumulated_up = 0.0

        self.selected_app_name = "All apps"
        self.current_y_max = 1024.0
        self._scale_down_hold_time = 0.0

        self.is_paused = False
        self.pinned_time: Optional[float] = None
        self.filter_direction = "both"
        self.filter_sort = "high_to_low"
        self.filter_scope = "all"
        self.current_traffic_view = "graph"

        self._is_monitored_device_offline = False
        self._offline_anchor_ts: Optional[float] = None
        self._last_usage_conns: List = []

        self._init_ui()

    def _init_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        traffic_main_widget = QtWidgets.QWidget()
        traffic_main_widget.setStyleSheet("background-color: #FFFFFF;")
        traffic_main_layout = QtWidgets.QVBoxLayout(traffic_main_widget)
        traffic_main_layout.setContentsMargins(12, 8, 12, 6)
        traffic_main_layout.setSpacing(6)

        # 1. TOP TOOLBAR
        chart_top_bar = self._build_top_toolbar()
        traffic_main_layout.addLayout(chart_top_bar)

        # 2. POINT INSPECTOR BANNER
        self.point_inspector_card = self._build_point_inspector_banner()
        traffic_main_layout.addWidget(self.point_inspector_card)

        # 3. STACK 2 VIEW: GRAPH (0) & USAGE (1)
        self.traffic_content_stack = QtWidgets.QStackedWidget()

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.splitter.setStyleSheet("QSplitter::handle { background-color: #E2E8F0; width: 1px; }")

        # 3a. Sidebar Panel (Left)
        self.sidebar_panel = TrafficSidebarPanel(self.icon_manager, self._resolve_exe_cb)
        self.sidebar_panel.filter_direction_changed.connect(self._on_filter_direction_combo_changed)
        self.sidebar_panel.filter_scope_changed.connect(self._on_filter_scope_combo_changed)
        self.sidebar_panel.search_changed.connect(self._filter_app_list)
        self.sidebar_panel.close_requested.connect(self.close_app_panel)
        self.sidebar_panel.app_selected.connect(self._on_sidebar_app_selected)
        self.sidebar_panel.app_double_clicked.connect(self.app_protect_requested.emit)

        self.left_panel = self.sidebar_panel
        self.combo_filter_dir = self.sidebar_panel.combo_filter_dir
        self.combo_filter_scope = self.sidebar_panel.combo_filter_scope
        self.search_app_input = self.sidebar_panel.search_app_input
        self.app_list_table = self.sidebar_panel.app_list_table
        self.splitter.addWidget(self.sidebar_panel)

        # 3b. Chart Canvas (Right)
        self.chart_canvas = TrafficChartCanvas(self.icon_manager)
        self.time_axis = self.chart_canvas.time_axis
        self.plot_widget = self.chart_canvas.plot_widget
        self.curve_down = self.chart_canvas.curve_down
        self.curve_up = self.chart_canvas.curve_up
        self.v_line = self.chart_canvas.v_line
        self.h_line = self.chart_canvas.h_line
        self.hover_marker = self.chart_canvas.hover_marker
        self.pinned_vline = self.chart_canvas.pinned_vline
        self.pinned_marker = self.chart_canvas.pinned_marker
        self.hover_badge = self.chart_canvas.hover_badge
        self.lbl_hover_text = self.chart_canvas.lbl_hover_text
        self.point_inspection_bar = self.chart_canvas.point_inspection_bar
        self.point_inspection_bar.resume_clicked.connect(self.resume_live)

        self.plot_widget.scene().sigMouseMoved.connect(self._on_plot_mouse_moved)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_mouse_clicked)
        self.plot_widget.viewport().installEventFilter(self)

        self.splitter.addWidget(self.chart_canvas)
        self.traffic_content_stack.addWidget(self.splitter)

        # 3c. Usage View
        self.usage_view = GlassWireUsageView(self.icon_manager)
        self.traffic_content_stack.addWidget(self.usage_view)
        traffic_main_layout.addWidget(self.traffic_content_stack, stretch=1)

        # 4. BOTTOM HUD BAR
        self.hud_bar = SelectedAppHudBar()
        self.hud_down_total = self.hud_bar.hud_down_total
        self.hud_down_speed = self.hud_bar.hud_down_speed
        self.arc_gauge = self.hud_bar.arc_gauge
        self.hud_up_total = self.hud_bar.hud_up_total
        self.hud_up_speed = self.hud_bar.hud_up_speed
        self.wan_lan_widget = self.hud_bar.wan_lan_widget
        traffic_main_layout.addWidget(self.hud_bar)

        # 5. PANORAMIC TIMELINE SCRUBBER
        self.timeline_scrubber = GlassWireTimelineScrubber()
        self.timeline_scrubber.range_selected.connect(self._on_scrubber_range_selected)
        self.timeline_scrubber.time_selected.connect(self._on_scrubber_point_selected)
        traffic_main_layout.addWidget(self.timeline_scrubber)

        layout.addWidget(traffic_main_widget)

    def _build_top_toolbar(self) -> QtWidgets.QHBoxLayout:
        chart_top_bar = QtWidgets.QHBoxLayout()
        chart_top_bar.setSpacing(10)

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
        self.btn_toggle_filter.clicked.connect(self.open_app_panel)
        chart_top_bar.addWidget(self.btn_toggle_filter)
        chart_top_bar.addStretch()

        capsule_container = QtWidgets.QFrame()
        capsule_container.setFixedHeight(32)
        capsule_container.setStyleSheet("QFrame { background-color: #F1F5F9; border-radius: 16px; padding: 2px; }")
        cap_layout = QtWidgets.QHBoxLayout(capsule_container)
        cap_layout.setContentsMargins(3, 2, 3, 2)
        cap_layout.setSpacing(4)

        self.btn_view_graph = QtWidgets.QPushButton(" Graph")
        self.btn_view_graph.setIcon(get_graph_toggle_icon(True))
        self.btn_view_graph.setFixedHeight(26)
        self.btn_view_graph.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_view_graph.setStyleSheet(
            "QPushButton { background-color: #2563EB; color: #FFFFFF; font-size: 11px; "
            "font-weight: bold; border-radius: 13px; padding: 2px 14px; border: none; }"
        )
        self.btn_view_graph.clicked.connect(lambda: self.switch_traffic_view("graph"))

        self.btn_view_usage = QtWidgets.QPushButton(" Usage")
        self.btn_view_usage.setIcon(get_usage_toggle_icon(False))
        self.btn_view_usage.setFixedHeight(26)
        self.btn_view_usage.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_view_usage.setStyleSheet(
            "QPushButton { background-color: transparent; color: #64748B; font-size: 11px; "
            "font-weight: 600; border-radius: 13px; padding: 2px 14px; border: none; }"
            "QPushButton:hover { background-color: #E2E8F0; color: #1E293B; }"
        )
        self.btn_view_usage.clicked.connect(lambda: self.switch_traffic_view("usage"))

        cap_layout.addWidget(self.btn_view_graph)
        cap_layout.addWidget(self.btn_view_usage)
        chart_top_bar.addWidget(capsule_container)
        chart_top_bar.addStretch()

        self.scale_indicator = QtWidgets.QLabel("3 MB ▾")
        self.scale_indicator.setStyleSheet("font-size: 13px; font-weight: bold; color: #475569; min-width: 60px;")
        chart_top_bar.addWidget(self.scale_indicator)

        self.live_controls_widget = QtWidgets.QWidget()
        lc_layout = QtWidgets.QHBoxLayout(self.live_controls_widget)
        lc_layout.setContentsMargins(0, 0, 0, 0)
        lc_layout.setSpacing(8)

        self.lbl_live_badge = QtWidgets.QLabel("● LIVE")
        self.lbl_live_badge.setStyleSheet(
            "color: #059669; font-weight: bold; font-size: 11px; padding: 3px 8px; "
            "background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;"
        )
        lc_layout.addWidget(self.lbl_live_badge)

        self.btn_pause_toggle = QtWidgets.QPushButton("⏸ Tạm Dừng")
        self.btn_pause_toggle.setFixedHeight(28)
        self.btn_pause_toggle.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_pause_toggle.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; color: #334155; font-size: 11px; "
            "font-weight: 600; padding: 3px 12px; border-radius: 5px; border: 1px solid #CBD5E1; }"
            "QPushButton:hover { background-color: #F8FAFC; color: #0F172A; }"
        )
        self.btn_pause_toggle.clicked.connect(self.toggle_pause_resume)
        lc_layout.addWidget(self.btn_pause_toggle)
        chart_top_bar.addWidget(self.live_controls_widget)

        self.combo_time_window = QtWidgets.QComboBox()
        self.combo_time_window.addItems(["30 Seconds", "1 Minute", "5 Minutes", "10 Minutes", "1 Hour", "24 Hours"])
        self.combo_time_window.setCurrentIndex(2)
        self.combo_time_window.setFixedHeight(28)
        self.combo_time_window.setStyleSheet("font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 6px;")
        self.combo_time_window.currentIndexChanged.connect(self.on_time_window_changed)
        chart_top_bar.addWidget(self.combo_time_window)
        return chart_top_bar

    def _build_point_inspector_banner(self) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setObjectName("pointInspectorCard")
        card.setStyleSheet(
            "QFrame#pointInspectorCard { background-color: #F8FAFC; border: 1px solid #E2E8F0; "
            "border-left: 4px solid #2563EB; border-radius: 6px; padding: 4px 10px; }"
        )
        card.setVisible(False)
        pic_layout = QtWidgets.QHBoxLayout(card)
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
        self.btn_card_resume.setStyleSheet(
            "QPushButton { background-color: #10B981; color: #FFFFFF; font-weight: bold; "
            "font-size: 11px; padding: 4px 12px; border-radius: 5px; border: none; }"
            "QPushButton:hover { background-color: #059669; }"
        )
        self.btn_card_resume.clicked.connect(self.resume_live)
        btn_box.addWidget(self.btn_card_resume)

        self.btn_card_close = QtWidgets.QPushButton("✕ Bỏ Ghim")
        self.btn_card_close.setFixedHeight(28)
        self.btn_card_close.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_card_close.setStyleSheet(
            "QPushButton { background-color: #F1F5F9; color: #475569; font-weight: bold; "
            "font-size: 11px; padding: 4px 10px; border-radius: 5px; border: 1px solid #CBD5E1; }"
            "QPushButton:hover { background-color: #E2E8F0; color: #0F172A; }"
        )
        self.btn_card_close.clicked.connect(self.resume_live)
        btn_box.addWidget(self.btn_card_close)
        pic_layout.addLayout(btn_box)
        return card

    # =========================================================================
    # PANEL & VIEW SWITCHING
    # =========================================================================
    def open_app_panel(self):
        self.left_panel.setVisible(True)
        self.btn_toggle_filter.setVisible(False)
        tot_w = self.splitter.width() or 1200
        self.splitter.setSizes([320, max(500, tot_w - 320)])
        self.update_app_sidebar(self.latest_proc_speeds)

    def close_app_panel(self):
        self.left_panel.setVisible(False)
        self.btn_toggle_filter.setVisible(True)

    def _on_filter_direction_combo_changed(self, index: int):
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

        self.update_app_sidebar(self.latest_proc_speeds)
        self.update_selected_app_hud()
        self.usage_view.set_filter_direction(self.filter_direction)
        if self.traffic_content_stack.currentIndex() == 1:
            self.update_usage_view_now()

    def _on_filter_scope_combo_changed(self, index: int):
        if index == 1:
            self.filter_scope = "wan"
        elif index == 2:
            self.filter_scope = "lan"
        else:
            self.filter_scope = "all"
        self.update_app_sidebar(self.latest_proc_speeds)
        self.update_selected_app_hud()

    def switch_traffic_view(self, mode: str):
        self.current_traffic_view = mode
        active_css = (
            "QPushButton { background-color: #2563EB; color: #FFFFFF; font-size: 11px; "
            "font-weight: bold; border-radius: 13px; padding: 2px 14px; border: none; }"
        )
        inactive_css = (
            "QPushButton { background-color: transparent; color: #64748B; font-size: 11px; "
            "font-weight: 600; border-radius: 13px; padding: 2px 14px; border: none; }"
            "QPushButton:hover { background-color: #E2E8F0; color: #1E293B; }"
        )
        if mode == "graph":
            self.traffic_content_stack.setCurrentIndex(0)
            self.btn_view_graph.setIcon(get_graph_toggle_icon(True))
            self.btn_view_graph.setStyleSheet(active_css)
            self.btn_view_usage.setIcon(get_usage_toggle_icon(False))
            self.btn_view_usage.setStyleSheet(inactive_css)
        else:
            self.traffic_content_stack.setCurrentIndex(1)
            self.btn_view_usage.setIcon(get_usage_toggle_icon(True))
            self.btn_view_usage.setStyleSheet(active_css)
            self.btn_view_graph.setIcon(get_graph_toggle_icon(False))
            self.btn_view_graph.setStyleSheet(inactive_css)
            self.update_usage_view_now()

    def update_usage_view_now(self):
        try:
            win_down, win_up, win_app_bytes = self.compute_window_traffic()
            tot_win_bytes = win_down + win_up
            if self._is_monitored_device_offline:
                active_conns = list(self._last_usage_conns)
            else:
                active_conns = self._fetch_raw_conns_cb() if self._fetch_raw_conns_cb else []
                self._last_usage_conns = list(active_conns)
            self.usage_view.update_usage_data(
                win_app_bytes, active_conns, self.app_metadata, total_window_bytes=tot_win_bytes
            )
        except Exception as e:
            print(f"[Usage Update Error] {e}")

    # =========================================================================
    # TRAFFIC COMPUTATION, SIDEBAR & HUD
    # =========================================================================
    def compute_window_traffic(
        self, win_duration: Optional[float] = None, t_end: Optional[float] = None
    ) -> Tuple[float, float, Dict[str, Dict[str, float]]]:
        if win_duration is None:
            win_duration = self.window_duration
        if t_end is not None:
            anchor_ts = t_end
        elif self._is_monitored_device_offline and self._offline_anchor_ts:
            anchor_ts = self._offline_anchor_ts
        else:
            anchor_ts = time.time()

        return compute_window_traffic(
            window_sample_history=self.window_sample_history,
            win_duration=win_duration,
            anchor_ts=anchor_ts,
            fallback_down=self.total_accumulated_down,
            fallback_up=self.total_accumulated_up,
            fallback_apps=self.app_accumulated_bytes,
        )

    def update_app_sidebar(self, proc_speeds: list):
        win_down, win_up, win_app_bytes = self.compute_window_traffic()
        self.sidebar_panel.update_sidebar_table(
            proc_speeds=proc_speeds,
            win_down=win_down,
            win_up=win_up,
            win_app_bytes=win_app_bytes,
            app_metadata=self.app_metadata,
            filter_direction=self.filter_direction,
            filter_scope=self.filter_scope,
            selected_app_name=self.selected_app_name,
        )

    def _filter_app_list(self):
        self.update_app_sidebar(self.latest_proc_speeds)

    def _on_sidebar_app_selected(self, app_name: str):
        self.selected_app_name = app_name
        self.update_selected_app_hud()

    def update_selected_app_hud(self):
        win_down, win_up, win_app_bytes = self.compute_window_traffic()
        scope_factor = get_scope_factor(self.filter_scope)

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
        total_str = format_bytes(total_bytes)

        if self.filter_direction == "outgoing":
            self.hud_down_total.setText("--")
            self.hud_down_speed.setText("--  ↓")
        else:
            self.hud_down_total.setText(format_bytes(d_total))
            self.hud_down_speed.setText(f"{format_speed(d_speed)}  ↓")

        pct = int(min(100, max(15, (total_bytes / (10 * 1024 * 1024)) * 100.0))) if total_bytes > 0 else 15
        self.arc_gauge.set_value(total_str, pct)

        if self.filter_direction == "incoming":
            self.hud_up_total.setText("--")
            self.hud_up_speed.setText("--  ↑")
        else:
            self.hud_up_total.setText(format_bytes(u_total))
            self.hud_up_speed.setText(f"{format_speed(u_speed)}  ↑")

        if self.filter_scope == "wan":
            wan_bytes = total_bytes * 0.98
            self.wan_lan_widget.set_values(f"WAN  {format_bytes(wan_bytes)}", "LAN  --", 100)
        elif self.filter_scope == "lan":
            lan_bytes = total_bytes * 0.05
            self.wan_lan_widget.set_values("WAN  --", f"LAN  {format_bytes(lan_bytes)}", 15)
        else:
            wan_bytes = total_bytes * 0.98
            lan_bytes = total_bytes * 0.02
            self.wan_lan_widget.set_values(f"WAN  {format_bytes(wan_bytes)}", f"LAN  {format_bytes(lan_bytes)}", 94)

    # =========================================================================
    # SAMPLE INGESTION & RENDER LOOP
    # =========================================================================
    def on_time_window_changed(self, index: int):
        windows = [30.0, 60.0, 300.0, 600.0, 3600.0, 86400.0]
        self.window_duration = windows[min(index, len(windows) - 1)]
        self.time_axis.set_window_duration(self.window_duration)
        self.timeline_scrubber.set_window_duration(self.window_duration)
        now = time.time()
        if self._is_monitored_device_offline and self._offline_anchor_ts:
            render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
        else:
            render_now = now - self.render_delay
        self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)
        self.update_app_sidebar(self.latest_proc_speeds)
        self.update_selected_app_hud()
        self.update_usage_view_now()

    def on_new_sample(self, timestamp, down_kb, up_kb, proc_speeds, total_p_down, total_p_up):
        try:
            self.latest_down = down_kb
            self.latest_up = up_kb
            self.latest_proc_down = total_p_down
            self.latest_proc_up = total_p_up
            self.latest_proc_speeds = proc_speeds

            self.total_accumulated_down += down_kb * 1024.0 * UPDATE_INTERVAL
            self.total_accumulated_up += up_kb * 1024.0 * UPDATE_INTERVAL

            for item in proc_speeds:
                pname = item.get("name", "")
                if not pname:
                    continue
                if pname not in self.app_accumulated_bytes:
                    self.app_accumulated_bytes[pname] = {"down": 0.0, "up": 0.0}
                self.app_accumulated_bytes[pname]["down"] += item.get("down_kb", 0.0) * 1024.0 * UPDATE_INTERVAL
                self.app_accumulated_bytes[pname]["up"] += item.get("up_kb", 0.0) * 1024.0 * UPDATE_INTERVAL
                if pname not in self.app_metadata:
                    self.app_metadata[pname] = {"pid": item.get("pid", 0), "exe": item.get("exe", "")}

            self.down_points.append((timestamp, down_kb))
            self.up_points.append((timestamp, up_kb))

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

            self.window_sample_history.append({
                "t": timestamp,
                "down_b": down_kb * 1024.0 * dt,
                "up_b": up_kb * 1024.0 * dt,
                "procs": active_procs,
            })

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
                        "exe": p.get("exe", ""),
                    })
            all_active_point_procs.sort(key=lambda x: x["down_kb"] + x["up_kb"], reverse=True)

            self.point_history.append({
                "timestamp": timestamp,
                "down_kb": down_kb,
                "up_kb": up_kb,
                "top_procs": all_active_point_procs,
            })

            self.timeline_scrubber.add_point(timestamp, down_kb + up_kb)
            if not self.is_paused:
                self.update_app_sidebar(proc_speeds)
                self.update_selected_app_hud()

            self.sample_processed.emit(proc_speeds, self.app_accumulated_bytes, self.app_metadata)
            if self.traffic_content_stack.currentIndex() == 1:
                self.update_usage_view_now()
        except Exception as e:
            print(f"[UI Slot Error] {e}")

    def render_frame(self):
        try:
            if self.is_paused:
                return
            now = time.time()
            if self._is_monitored_device_offline and self._offline_anchor_ts:
                render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
            else:
                render_now = now - self.render_delay

            cutoff = render_now - 3600.0
            while self.down_points and self.down_points[0][0] < cutoff:
                self.down_points.popleft()
            while self.up_points and self.up_points[0][0] < cutoff:
                self.up_points.popleft()

            d_raw_x = np.array([p[0] for p in self.down_points], dtype=np.float64)
            d_raw_y = np.array([p[1] for p in self.down_points], dtype=np.float64)
            u_raw_x = np.array([p[0] for p in self.up_points], dtype=np.float64)
            u_raw_y = np.array([p[1] for p in self.up_points], dtype=np.float64)

            cfg = TIME_SCALE_CONFIGS.get(self.window_duration, TIME_SCALE_CONFIGS[300.0])
            x_dense = np.linspace(render_now - self.window_duration, render_now, cfg["n_dense"])

            smooth_down = generate_glasswire_wave(d_raw_x, d_raw_y, x_dense, self.window_duration)
            smooth_up = generate_glasswire_wave(u_raw_x, u_raw_y, x_dense, self.window_duration)

            if self.filter_direction == "incoming":
                self.curve_down.setData(x_dense, smooth_down)
                self.curve_down.show()
                self.curve_up.hide()
                peak_val = max(float(np.max(smooth_down)), 1.0)
            elif self.filter_direction == "outgoing":
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
            self.update_y_scale(peak_val)
            self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)
        except Exception:
            pass

    def apply_y_ceiling(self, target_tier: float):
        self.plot_widget.setYRange(0, target_tier, padding=0.0)
        self.scale_indicator.setText(format_ceiling_label(target_tier))

    def update_y_scale(self, peak_val: float):
        try:
            new_max, new_hold, changed = select_y_ceiling_tier(
                peak_val_kb=peak_val,
                current_y_max=self.current_y_max,
                now_ts=time.time(),
                hold_until_ts=self._scale_down_hold_time,
                hold_duration_sec=8.0,
            )
            self._scale_down_hold_time = new_hold
            if changed:
                self.current_y_max = new_max
                self.apply_y_ceiling(new_max)
        except Exception:
            pass

    # =========================================================================
    # POINT INSPECTION, SCRUBBER, ZOOM & PAN
    # =========================================================================
    def find_nearest_sample(self, target_t: float) -> Optional[dict]:
        return find_nearest_sample(self.point_history, self.down_points, self.up_points, target_t)

    def _on_plot_mouse_moved(self, pos):
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            self._hide_hover_elements()
            return
        mouse_pt = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        nearest = self.find_nearest_sample(mouse_pt.x())
        if not nearest:
            self._hide_hover_elements()
            return

        nt = nearest["timestamp"]
        nd = nearest.get("down_kb", 0.0)
        nu = nearest.get("up_kb", 0.0)

        self.v_line.setPos(nt)
        self.h_line.setPos(nd)
        self.v_line.show()
        self.h_line.show()
        self.hover_marker.setData([nt], [nd])
        self.hover_marker.show()

        time_str = time.strftime("%H:%M:%S", time.localtime(nt))
        self.lbl_hover_text.setText(
            f"🕒 <b>{time_str}</b><br>"
            f"<span style='color:#F59E0B;'>↓ {format_speed(nd)}</span>&nbsp;&nbsp;"
            f"<span style='color:#F43F5E;'>↑ {format_speed(nu)}</span>"
        )
        self.hover_badge.adjustSize()

        local_pos = self.plot_widget.mapFromScene(pos)
        bx = max(10, min(self.plot_widget.width() - self.hover_badge.width() - 10, local_pos.x() + 14))
        by = max(10, min(self.plot_widget.height() - self.hover_badge.height() - 10, local_pos.y() - 44))
        self.hover_badge.move(int(bx), int(by))
        self.hover_badge.show()

    def _hide_hover_elements(self):
        self.v_line.hide()
        self.h_line.hide()
        self.hover_marker.hide()
        self.hover_badge.hide()

    def _on_plot_mouse_clicked(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return
        mouse_pt = self.plot_widget.plotItem.vb.mapSceneToView(pos)
        self.pin_point(mouse_pt.x())

    def _on_scrubber_range_selected(self, win_start: float, win_end: float, target_t: float):
        self.inspect_time_window(win_start, win_end, target_t)

    def _on_scrubber_point_selected(self, target_t: float):
        half = self.window_duration / 2.0
        self.inspect_time_window(target_t - half, target_t + half, target_t)

    def inspect_time_window(self, win_start: float, win_end: float, target_t: float):
        self.is_paused = True
        self.pinned_time = target_t
        self.render_window_data(win_start, win_end)
        self.pin_point(target_t)

    def render_window_data(self, win_start: float, win_end: float):
        try:
            win_duration = max(10.0, win_end - win_start)
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
                x_dense = np.linspace(win_start, win_end, max(200, cfg["n_dense"]))

                scope_factor = get_scope_factor(self.filter_scope)
                smooth_down_disp = generate_glasswire_wave(d_raw_x, d_raw_y, x_dense, win_duration) * scope_factor
                smooth_up_disp = generate_glasswire_wave(u_raw_x, u_raw_y, x_dense, win_duration) * scope_factor

                if self.filter_direction == "incoming":
                    self.curve_down.setData(x_dense, smooth_down_disp)
                    self.curve_down.show()
                    self.curve_up.hide()
                    peak_val = max(float(np.max(smooth_down_disp)), 1.0)
                elif self.filter_direction == "outgoing":
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
                self.update_y_scale(peak_val)

            self.plot_widget.setXRange(win_start, win_end, padding=0.0)
        except Exception as e:
            print(f"[Render Window Error] {e}")

    def pin_point(self, target_t: float):
        self.is_paused = True
        nearest = self.find_nearest_sample(target_t)
        if not nearest:
            nearest = {"timestamp": target_t, "down_kb": 0.0, "up_kb": 0.0, "top_procs": []}

        t_val = nearest["timestamp"]
        d_val = nearest.get("down_kb", 0.0)
        u_val = nearest.get("up_kb", 0.0)
        self.pinned_time = t_val

        self.lbl_live_badge.setText("⏸ PAUSED")
        self.lbl_live_badge.setStyleSheet(
            "color: #DC2626; font-weight: bold; font-size: 11px; padding: 2px 7px; "
            "background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;"
        )
        self.btn_pause_toggle.setText("▶ Tiếp Tục (Live)")
        self.btn_pause_toggle.setStyleSheet(
            "QPushButton { background-color: #10B981; color: #FFFFFF; font-size: 11px; "
            "font-weight: bold; padding: 2px 10px; border-radius: 5px; border: none; }"
            "QPushButton:hover { background-color: #059669; }"
        )

        self.pinned_vline.setPos(t_val)
        self.pinned_vline.show()
        self.pinned_marker.setData([t_val], [d_val])
        self.pinned_marker.show()

        top_procs = nearest.get("top_procs", [])
        self.point_inspection_bar.set_inspection_data(
            d_val, u_val, t_val, top_procs, self.icon_manager, format_speed
        )
        self.point_inspection_bar.show()
        self.point_inspector_card.setVisible(False)

        if self.timeline_scrubber.slider_start is None:
            half = self.window_duration / 2.0
            self.timeline_scrubber.set_slider_range(target_t - half, target_t + half)

        self._update_hud_for_point(d_val, u_val)

    def _update_hud_for_point(self, down_kb: float, up_kb: float):
        total_kb = down_kb + up_kb
        total_bytes = total_kb * 1024.0
        self.hud_down_speed.setText(f"{format_speed(down_kb)}  ↓")
        self.hud_up_speed.setText(f"{format_speed(up_kb)}  ↑")
        pct = int(min(100, max(15, (total_kb / 10240.0) * 100.0))) if total_kb > 0 else 15
        self.arc_gauge.set_value(format_bytes(total_bytes), pct)
        self.wan_lan_widget.set_values(
            f"WAN  {format_bytes(total_bytes * 0.96)}",
            f"LAN  {format_bytes(total_bytes * 0.04)}",
            96,
        )

    def resume_live(self):
        self.is_paused = False
        self.pinned_time = None
        self.pinned_vline.hide()
        self.pinned_marker.hide()
        self.point_inspector_card.setVisible(False)
        self.point_inspection_bar.hide()
        self.timeline_scrubber.reset_to_live()

        if self._is_monitored_device_offline:
            self.lbl_live_badge.setText("🔴 OFFLINE")
            self.lbl_live_badge.setStyleSheet(
                "color: #DC2626; font-weight: bold; font-size: 11px; padding: 3px 8px; "
                "background-color: #FEF2F2; border-radius: 4px; border: 1px solid #FECACA;"
            )
        else:
            self.lbl_live_badge.setText("● LIVE")
            self.lbl_live_badge.setStyleSheet(
                "color: #059669; font-weight: bold; font-size: 11px; padding: 2px 7px; "
                "background-color: #ECFDF5; border-radius: 4px; border: 1px solid #A7F3D0;"
            )

        self.btn_pause_toggle.setText("⏸ Tạm Dừng")
        self.btn_pause_toggle.setStyleSheet(
            "QPushButton { background-color: #F8FAFC; color: #475569; font-size: 11px; "
            "font-weight: 600; padding: 2px 10px; border-radius: 5px; border: 1px solid #CBD5E1; }"
            "QPushButton:hover { background-color: #F1F5F9; color: #0F172A; }"
        )

        self.update_selected_app_hud()
        now = time.time()
        if self._is_monitored_device_offline and self._offline_anchor_ts:
            render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
        else:
            render_now = now - self.render_delay
        self.plot_widget.setXRange(render_now - self.window_duration, render_now, padding=0.0)

    def toggle_pause_resume(self):
        if self.is_paused:
            self.resume_live()
        else:
            now = time.time()
            if self._is_monitored_device_offline and self._offline_anchor_ts:
                render_now = min(now - self.render_delay, self._offline_anchor_ts + 3.0)
            else:
                render_now = now - self.render_delay
            self.pin_point(render_now)

    def _get_navigation_bounds(self) -> Optional[Tuple[float, float]]:
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
        if hasattr(self, "plot_widget") and obj is self.plot_widget.viewport():
            if event.type() == QtCore.QEvent.Type.Wheel and self.is_paused:
                self._on_plot_wheel_zoom(event)
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._plot_drag_active = False
                self._plot_drag_start_pos = event.pos()
                vr = self.plot_widget.plotItem.vb.viewRange()[0]
                self._plot_drag_start_range = (vr[0], vr[1])
            elif event.type() == QtCore.QEvent.Type.MouseMove:
                if (event.buttons() & QtCore.Qt.MouseButton.LeftButton) and hasattr(self, "_plot_drag_start_pos") and self.is_paused:
                    dx = event.pos().x() - self._plot_drag_start_pos.x()
                    if abs(dx) > 6:
                        self._plot_drag_active = True
                        self._on_plot_drag_pan(dx)
                        return True
            elif event.type() == QtCore.QEvent.Type.MouseButtonRelease and event.button() == QtCore.Qt.MouseButton.LeftButton:
                if getattr(self, "_plot_drag_active", False):
                    self._plot_drag_active = False
                    return True
        return super().eventFilter(obj, event)

    def _on_plot_wheel_zoom(self, event: QtGui.QWheelEvent):
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
        ratio = max(0.0, min(1.0, (mouse_t - cur_start) / cur_dur if cur_dur > 0 else 0.5))
        new_start = mouse_t - ratio * new_dur
        new_end = new_start + new_dur

        if new_dur >= (bound_end - bound_start):
            new_start, new_end = bound_start, bound_end
        else:
            if new_start < bound_start:
                new_start = bound_start
                new_end = min(bound_end, new_start + new_dur)
            if new_end > bound_end:
                new_end = bound_end
                new_start = max(bound_start, new_end - new_dur)

        self.time_axis.set_window_duration(max(15.0, new_end - new_start))
        self.render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)
        event.accept()

    def _on_plot_drag_pan(self, dx: float):
        bounds = self._get_navigation_bounds()
        if not bounds or not hasattr(self, "_plot_drag_start_range"):
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

        self.render_window_data(new_start, new_end)
        self.timeline_scrubber.set_slider_range(new_start, new_end)
