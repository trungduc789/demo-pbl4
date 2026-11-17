"""
Các Widget Trục Thời gian & Thanh Tua Toàn cảnh (TimeAxisItem & GlassWireTimelineScrubber):
- TimeAxisItem: Trục hoành PyQtGraph hiển thị mốc giờ định hình theo từng scale.
- GlassWireTimelineScrubber: Thanh mini-timeline kéo tua với bóng sóng vàng và 2 tay nắm.
"""

import time
from collections import deque
from typing import Optional, Tuple
from PyQt6 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg

from application.traffic_analytics import TIME_SCALE_CONFIGS


class TimeAxisItem(pg.AxisItem):
    """Trục hoành hiển thị mốc giờ hệ thống được định hình chuyên biệt theo từng mốc scale thời gian."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_window = 300.0

    def set_window_duration(self, duration: float):
        self.current_window = duration
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        span = max(1.0, maxVal - minVal)
        if span > 600.0:
            if span <= 1800.0:
                step = 180.0
            elif span <= 3600.0:
                step = 300.0
            elif span <= 7200.0:
                step = 600.0
            elif span <= 21600.0:
                step = 1800.0
            elif span <= 43200.0:
                step = 3600.0
            elif span <= 86400.0:
                step = 7200.0
            else:
                step = 14400.0
        else:
            cfg = TIME_SCALE_CONFIGS.get(self.current_window, TIME_SCALE_CONFIGS[300.0])
            step = cfg["tick_step"]

        start = (int(minVal) // int(step)) * int(step)
        ticks = []
        curr = start
        while curr <= maxVal + step:
            if curr >= minVal - step:
                ticks.append(curr)
            curr += step
            if len(ticks) > 30:
                break
        return [(step, ticks)]

    def tickStrings(self, values, scale, spacing):
        span = self.current_window
        if values and len(values) >= 2:
            span = max(span, values[-1] - values[0])

        res = []
        for val in values:
            try:
                lt = time.localtime(val)
                hr = lt.tm_hour % 12
                hr = 12 if hr == 0 else hr
                am_pm = "PM" if lt.tm_hour >= 12 else "AM"
                if span >= 3600.0:
                    res.append(f"{hr}:{lt.tm_min:02d} {am_pm}")
                else:
                    res.append(f"{hr}:{lt.tm_min:02d}:{lt.tm_sec:02d} {am_pm}")
            except Exception:
                res.append("")
        return res


class GlassWireTimelineScrubber(QtWidgets.QWidget):
    """Thanh toàn cảnh mini-timeline kéo tua phong cách GlassWire với bóng sóng vàng và nút kéo tròn."""

    range_selected = QtCore.pyqtSignal(float, float, float)  # (win_start, win_end, target_t)
    time_selected = QtCore.pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.history_points = deque(maxlen=2400)
        self.window_duration = 300.0
        self.slider_start: Optional[float] = None
        self.slider_end: Optional[float] = None
        self.drag_mode: Optional[str] = None
        self.drag_start_x: float = 0.0
        self.drag_start_win: Tuple[float, float] = (0.0, 0.0)
        self.drag_offset_x: float = 0.0

    def set_window_duration(self, duration: float):
        self.window_duration = duration
        if self.slider_start is not None and self.slider_end is not None:
            mid = (self.slider_start + self.slider_end) / 2.0
            self.slider_start = mid - duration / 2.0
            self.slider_end = mid + duration / 2.0
        self.update()

    def add_point(self, timestamp: float, value: float):
        self.history_points.append((timestamp, value))
        self.update()

    def set_slider_range(self, start: float, end: float):
        """Đặt vị trí khung trượt thủ công."""
        self.slider_start = start
        self.slider_end = end
        self.update()

    def reset_to_live(self):
        """Quay lại chế độ live monitor bám sát thời gian thực."""
        self.slider_start = None
        self.slider_end = None
        self.drag_mode = None
        self.update()

    def get_time_bounds(self) -> Tuple[float, float]:
        """Tính toán khoảng thời gian bao quát hiển thị trên thanh timeline."""
        now = time.time()
        if len(self.history_points) > 1:
            t_max = max(now, self.history_points[-1][0])
            first_t = self.history_points[0][0]
            span = max(600.0, self.window_duration * 1.5, t_max - first_t)
            t_min = t_max - span
        else:
            t_max = now
            t_min = now - max(600.0, self.window_duration * 1.5)
        return t_min, t_max

    def get_slider_window(self, t_min: float, t_max: float) -> Tuple[float, float]:
        """Lấy khoảng thời gian của khung trượt hiện tại (win_start, win_end)."""
        if self.slider_start is not None and self.slider_end is not None:
            s = max(t_min, min(t_max, self.slider_start))
            e = max(t_min, min(t_max, self.slider_end))
            if e <= s:
                e = min(t_max, s + self.window_duration)
            return s, e
        e = t_max
        s = max(t_min, t_max - self.window_duration)
        return s, e

    def time_to_x(self, t: float, t_min: float, t_max: float, w: float) -> float:
        if t_max <= t_min:
            return 0.0
        pct = max(0.0, min(1.0, (t - t_min) / (t_max - t_min)))
        return pct * w

    def x_to_time(self, x: float, t_min: float, t_max: float, w: float) -> float:
        pct = max(0.0, min(1.0, x / max(1.0, float(w))))
        return t_min + pct * (t_max - t_min)

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        w = max(1, self.width())
        x = event.position().x()
        t_min, t_max = self.get_time_bounds()
        s, e = self.get_slider_window(t_min, t_max)
        x1 = self.time_to_x(s, t_min, t_max, w)
        x2 = self.time_to_x(e, t_min, t_max, w)
        box_w = max(36.0, x2 - x1)
        x2 = x1 + box_w

        if abs(x - x1) <= 9:
            self.drag_mode = "left_handle"
            self.drag_start_x = x
            self.drag_start_win = (s, e)
        elif abs(x - x2) <= 9:
            self.drag_mode = "right_handle"
            self.drag_start_x = x
            self.drag_start_win = (s, e)
        elif x1 <= x <= x2:
            self.drag_mode = "move_box"
            self.drag_start_x = x
            self.drag_offset_x = x - x1
            self.drag_start_win = (s, e)
        else:
            clicked_t = self.x_to_time(x, t_min, t_max, w)
            half = (e - s) / 2.0
            new_s = clicked_t - half
            new_e = clicked_t + half
            if new_s < t_min:
                new_s = t_min
                new_e = min(t_max, t_min + (e - s))
            if new_e > t_max:
                new_e = t_max
                new_s = max(t_min, t_max - (e - s))

            self.slider_start = new_s
            self.slider_end = new_e
            self.drag_mode = "move_box"
            self.drag_start_x = x
            self.drag_offset_x = (x2 - x1) / 2.0
            self.drag_start_win = (new_s, new_e)
            self.update()
            self.range_selected.emit(new_s, new_e, clicked_t)
            self.time_selected.emit(clicked_t)

    def mouseMoveEvent(self, event):
        w = max(1, self.width())
        x = event.position().x()
        t_min, t_max = self.get_time_bounds()
        s, e = self.get_slider_window(t_min, t_max)
        x1 = self.time_to_x(s, t_min, t_max, w)
        x2 = self.time_to_x(e, t_min, t_max, w)
        box_w = max(36.0, x2 - x1)
        x2 = x1 + box_w

        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            win_dur = self.drag_start_win[1] - self.drag_start_win[0]
            if win_dur <= 0.0:
                win_dur = self.window_duration

            if self.drag_mode == "move_box":
                target_x1 = x - getattr(self, "drag_offset_x", box_w / 2.0)
                new_s = self.x_to_time(target_x1, t_min, t_max, w)
                new_e = new_s + win_dur
                if new_s < t_min:
                    new_s = t_min
                    new_e = min(t_max, t_min + win_dur)
                if new_e > t_max:
                    new_e = t_max
                    new_s = max(t_min, t_max - win_dur)
                self.slider_start = new_s
                self.slider_end = new_e
                self.update()
                target_t = (new_s + new_e) / 2.0
                self.range_selected.emit(new_s, new_e, target_t)
                self.time_selected.emit(target_t)

            elif self.drag_mode == "left_handle":
                new_s = self.x_to_time(x, t_min, t_max, w)
                new_s = max(t_min, min(e - 10.0, new_s))
                self.slider_start = new_s
                self.window_duration = max(10.0, e - new_s)
                self.update()
                self.range_selected.emit(new_s, e, new_s)
                self.time_selected.emit(new_s)

            elif self.drag_mode == "right_handle":
                new_e = self.x_to_time(x, t_min, t_max, w)
                new_e = min(t_max, max(s + 10.0, new_e))
                self.slider_end = new_e
                self.window_duration = max(10.0, new_e - s)
                self.update()
                self.range_selected.emit(s, new_e, new_e)
                self.time_selected.emit(new_e)

            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        else:
            if abs(x - x1) <= 9 or abs(x - x2) <= 9:
                self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
            elif x1 <= x <= x2:
                self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        self.drag_mode = None
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        painter.fillRect(0, 0, w, h, QtGui.QColor("#FFFFFF"))
        painter.setPen(QtGui.QColor("#E2E8F0"))
        painter.drawLine(0, 0, w, 0)
        painter.drawLine(0, h - 1, w, h - 1)

        t_min, t_max = self.get_time_bounds()

        if len(self.history_points) > 3:
            max_v = max([p[1] for p in self.history_points] + [1.0])
            path = QtGui.QPainterPath()
            path.moveTo(0, h - 12)
            first_x = self.time_to_x(self.history_points[0][0], t_min, t_max, w)
            path.lineTo(first_x, h - 12)

            for t, v in self.history_points:
                norm_y = (v / max_v) * (h - 16)
                cur_x = self.time_to_x(t, t_min, t_max, w)
                cur_y = (h - 12) - norm_y
                path.lineTo(cur_x, cur_y)

            last_x = self.time_to_x(self.history_points[-1][0], t_min, t_max, w)
            path.lineTo(last_x, h - 12)
            path.lineTo(w, h - 12)
            path.closeSubpath()

            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(245, 158, 11, 140))
            painter.drawPath(path)

            painter.setPen(QtGui.QPen(QtGui.QColor("#F59E0B"), 1.0))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        s, e = self.get_slider_window(t_min, t_max)
        x1 = self.time_to_x(s, t_min, t_max, w)
        x2 = self.time_to_x(e, t_min, t_max, w)
        box_w = max(36.0, x2 - x1)
        if x1 + box_w > w:
            x1 = max(0.0, w - box_w)
            x2 = w
        else:
            x2 = x1 + box_w

        win_rect = QtCore.QRectF(x1, 2, box_w, h - 14)

        painter.setPen(QtGui.QPen(QtGui.QColor("#64748B"), 1.4))
        painter.setBrush(QtGui.QColor(255, 255, 255, 205))
        painter.drawRoundedRect(win_rect, 4, 4)

        handle_y = (h - 14) / 2.0 + 2.0
        painter.setBrush(QtGui.QColor("#FFFFFF"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#475569"), 1.8))
        painter.drawEllipse(QtCore.QPointF(x1, handle_y), 4.5, 4.5)
        painter.drawEllipse(QtCore.QPointF(x2, handle_y), 4.5, 4.5)

        painter.setPen(QtGui.QColor("#94A3B8"))
        painter.setFont(QtGui.QFont("Segoe UI", 7))
        for i in range(6):
            cur_t = t_min + (i / 5.0) * (t_max - t_min)
            lt = time.localtime(cur_t)
            hr = lt.tm_hour % 12
            hr = 12 if hr == 0 else hr
            am_pm = "PM" if lt.tm_hour >= 12 else "AM"
            if (t_max - t_min) >= 43200.0:
                lbl_time = f"{hr}:00 {am_pm}"
            else:
                lbl_time = f"{hr}:{lt.tm_min:02d}:{lt.tm_sec:02d} {am_pm}"
            lbl_x = int(i * (w / 5.3)) + 6
            painter.drawText(lbl_x, h - 2, lbl_time)
