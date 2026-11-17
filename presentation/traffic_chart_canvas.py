"""
Khung Biểu đồ Sóng PyQtGraph & Thanh HUD Thống kê Chi tiết (TrafficChartCanvas):
- Quản lý PlotWidget, TimeAxisItem, Curve Download/Upload, Crosshair & Hover Badge.
- Quản lý Thanh Point Inspection Bar và Thanh HUD dưới đáy (Download/Upload Speed, Arc Gauge, WAN/LAN Bar).
"""

from PyQt6 import QtWidgets, QtCore
import pyqtgraph as pg

from presentation.widgets.dashboard_widgets import (
    TimeAxisItem,
    GlassWireArcGauge,
    GlassWireWanLanWidget,
    GlassWirePointInspectionBar,
)


class TrafficChartCanvas(QtWidgets.QWidget):
    """Khung vẽ biểu đồ sóng thời gian thực bên phải của trang Traffic Monitor."""

    def __init__(self, icon_manager, parent=None):
        super().__init__(parent)
        self.icon_manager = icon_manager
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet("background-color: #FFFFFF;")
        right_layout = QtWidgets.QVBoxLayout(self)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(6)

        pg.setConfigOption("antialias", True)
        self.time_axis = TimeAxisItem(orientation="bottom")
        self.plot_widget = pg.PlotWidget(axisItems={"bottom": self.time_axis}, background="#FFFFFF")
        self.plot_widget.setMinimumHeight(350)

        vb = self.plot_widget.getViewBox()
        vb.setDefaultPadding(0.0)
        vb.setMouseEnabled(x=False, y=False)
        self.plot_widget.showGrid(x=False, y=True, alpha=0.08)
        self.plot_widget.setYRange(0, 1024, padding=0.0)
        self.plot_widget.enableAutoRange(axis="y", enable=False)

        self.plot_widget.getAxis("bottom").setPen(pg.mkPen("#E2E8F0", width=1))
        self.plot_widget.getAxis("bottom").setTextPen(pg.mkPen("#94A3B8"))
        self.plot_widget.getAxis("left").setStyle(showValues=False)
        self.plot_widget.getAxis("left").setPen(None)

        self.curve_down = self.plot_widget.plot(
            pen=pg.mkPen(color="#D97706", width=2.0),
            fillLevel=0,
            brush=(245, 158, 11, 220),
        )

        self.curve_up = self.plot_widget.plot(
            pen=pg.mkPen(color="#EA580C", width=1.5),
            fillLevel=0,
            brush=(244, 63, 94, 210),
        )

        self.v_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#3B82F6", width=1.2, style=QtCore.Qt.PenStyle.DashLine)
        )
        self.h_line = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen("#CBD5E1", width=1.0, style=QtCore.Qt.PenStyle.DotLine)
        )
        self.v_line.hide()
        self.h_line.hide()
        self.plot_widget.addItem(self.v_line, ignoreBounds=True)
        self.plot_widget.addItem(self.h_line, ignoreBounds=True)

        self.hover_marker = pg.ScatterPlotItem(
            size=8, pen=pg.mkPen("#2563EB", width=2), brush=pg.mkBrush("#FFFFFF"), symbol="o"
        )
        self.hover_marker.hide()
        self.plot_widget.addItem(self.hover_marker)

        self.pinned_vline = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#DC2626", width=1.5, style=QtCore.Qt.PenStyle.SolidLine)
        )
        self.pinned_vline.hide()
        self.plot_widget.addItem(self.pinned_vline, ignoreBounds=True)

        self.pinned_marker = pg.ScatterPlotItem(
            size=14, pen=pg.mkPen("#FFFFFF", width=2.5), brush=pg.mkBrush("#DC2626"), symbol="star"
        )
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

        right_layout.addWidget(self.plot_widget, stretch=1)

        self.point_inspection_bar = GlassWirePointInspectionBar(self.icon_manager)
        self.point_inspection_bar.hide()
        right_layout.addWidget(self.point_inspection_bar)


class SelectedAppHudBar(QtWidgets.QWidget):
    """Thanh HUD thống kê chi tiết dưới đáy trang Traffic Monitor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(78)
        self.setStyleSheet("background-color: #FFFFFF; border-top: 1px solid #F1F5F9;")
        hud_layout = QtWidgets.QHBoxLayout(self)
        hud_layout.setContentsMargins(18, 2, 18, 2)
        hud_layout.setSpacing(18)

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

        self.arc_gauge = GlassWireArcGauge()
        hud_layout.addWidget(self.arc_gauge)

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

        self.wan_lan_widget = GlassWireWanLanWidget()
        hud_layout.addWidget(self.wan_lan_widget, stretch=1)
