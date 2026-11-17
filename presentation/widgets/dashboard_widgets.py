"""
Module tổng hợp (Re-export Facade) cho các Widget Dashboard của tầng Presentation:
Các class widget đã được tách thành các file chuyên biệt:
- chart_scrubber_widgets.py: TimeAxisItem, GlassWireTimelineScrubber
- hud_widgets.py: GlassWireArcGauge, GlassWireWanLanWidget, GlassWireAppHoverPopup, GlassWireAppTriggerPill, GlassWirePointInspectionBar
- process_workers.py: ProcessIconManager, AsyncSignalBridge, HighFreqNetworkWorker
- protect_widgets.py: FirewallSwitchButton, ConnectionPillButton
- sparkline_widget.py: TrafficSparklineWidget
"""

from application.traffic_analytics import TIME_SCALE_CONFIGS, generate_glasswire_wave
from presentation.widgets.chart_scrubber_widgets import (
    TimeAxisItem,
    GlassWireTimelineScrubber,
)
from presentation.widgets.hud_widgets import (
    GlassWireArcGauge,
    GlassWireWanLanWidget,
    GlassWireAppHoverPopup,
    GlassWireAppTriggerPill,
    GlassWirePointInspectionBar,
)
from presentation.widgets.process_workers import (
    ProcessIconManager,
    AsyncSignalBridge,
    HighFreqNetworkWorker,
)
from presentation.widgets.protect_widgets import (
    FirewallSwitchButton,
    ConnectionPillButton,
)
from presentation.widgets.sparkline_widget import TrafficSparklineWidget

__all__ = [
    "TIME_SCALE_CONFIGS",
    "TimeAxisItem",
    "GlassWireArcGauge",
    "GlassWireWanLanWidget",
    "GlassWireAppHoverPopup",
    "GlassWireAppTriggerPill",
    "GlassWirePointInspectionBar",
    "GlassWireTimelineScrubber",
    "ProcessIconManager",
    "AsyncSignalBridge",
    "HighFreqNetworkWorker",
    "FirewallSwitchButton",
    "ConnectionPillButton",
    "TrafficSparklineWidget",
    "generate_glasswire_wave",
]
