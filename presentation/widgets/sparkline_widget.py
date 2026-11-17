"""
Widget Đồ thị Sóng Lưu lượng Cao cấp 60 giây (Smooth Bezier Traffic Sparkline):
- Dùng cho cửa sổ ClientStatusWindow.
- Làm mịn bằng đường cong Bezier (Cubic Spline) kèm Gradient chuyển sắc.
"""

from typing import List
from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import QWidget
from application.traffic_analytics import format_speed


class TrafficSparklineWidget(QWidget):
    """Widget đồ thị sóng thời gian thực 60 giây cho Tải xuống & Tải lên."""

    def __init__(self, max_points: int = 60, parent=None):
        super().__init__(parent)
        self.max_points = max_points
        self.down_points: List[float] = [0.0] * max_points
        self.up_points: List[float] = [0.0] * max_points
        self.smooth_max: float = 20.0
        self.setFixedHeight(135)
        self.setStyleSheet("background-color: transparent; border: none;")

    def add_sample(self, down_kb: float, up_kb: float):
        """Thêm một điểm dữ liệu mới vào chuỗi thời gian."""
        self.down_points.pop(0)
        self.down_points.append(max(0.0, float(down_kb)))
        self.up_points.pop(0)
        self.up_points.append(max(0.0, float(up_kb)))

        curr_max = max(max(self.down_points), max(self.up_points), 10.0)
        self.smooth_max = self.smooth_max * 0.8 + curr_max * 0.2
        self.update()

    def _build_smooth_path(self, points: List[float], max_val: float, w: float, bottom_y: float) -> QtGui.QPainterPath:
        """Tạo đường dẫn Bezier mượt mà đi qua các điểm dữ liệu."""
        path = QtGui.QPainterPath()
        n = len(points)
        if n < 2:
            return path

        step = w / (n - 1)
        avail_h = bottom_y - 20

        first_y = bottom_y - (points[0] / max_val) * avail_h
        path.moveTo(0, first_y)

        for i in range(1, n):
            x_prev = (i - 1) * step
            y_prev = bottom_y - (points[i - 1] / max_val) * avail_h
            x_curr = i * step
            y_curr = bottom_y - (points[i] / max_val) * avail_h

            cx = (x_prev + x_curr) / 2.0
            path.cubicTo(cx, y_prev, cx, y_curr, x_curr, y_curr)

        return path

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        bottom_y = h - 22

        bg_path = QtGui.QPainterPath()
        bg_path.addRoundedRect(QtCore.QRectF(0, 0, w, h), 8, 8)
        painter.fillPath(bg_path, QtGui.QColor("#FFFFFF"))

        painter.setPen(QtGui.QPen(QtGui.QColor("#F8FAFC"), 1, QtCore.Qt.PenStyle.DashLine))
        grid_steps = 3
        for g in range(1, grid_steps + 1):
            yg = int(bottom_y * g / (grid_steps + 1))
            painter.drawLine(0, yg, w, yg)

        max_v = max(self.smooth_max, 10.0)

        path_down = self._build_smooth_path(self.down_points, max_v, w, bottom_y)
        if not path_down.isEmpty():
            path_down_fill = QtGui.QPainterPath(path_down)
            path_down_fill.lineTo(w, bottom_y)
            path_down_fill.lineTo(0, bottom_y)
            path_down_fill.closeSubpath()

            grad_down = QtGui.QLinearGradient(0, 10, 0, bottom_y)
            grad_down.setColorAt(0.0, QtGui.QColor(245, 158, 11, 95))
            grad_down.setColorAt(0.5, QtGui.QColor(245, 158, 11, 35))
            grad_down.setColorAt(1.0, QtGui.QColor(245, 158, 11, 5))
            painter.fillPath(path_down_fill, grad_down)

            painter.setPen(QtGui.QPen(QtGui.QColor("#F59E0B"), 2.0))
            painter.drawPath(path_down)

        path_up = self._build_smooth_path(self.up_points, max_v, w, bottom_y)
        if not path_up.isEmpty():
            path_up_fill = QtGui.QPainterPath(path_up)
            path_up_fill.lineTo(w, bottom_y)
            path_up_fill.lineTo(0, bottom_y)
            path_up_fill.closeSubpath()

            grad_up = QtGui.QLinearGradient(0, 10, 0, bottom_y)
            grad_up.setColorAt(0.0, QtGui.QColor(225, 29, 72, 85))
            grad_up.setColorAt(0.5, QtGui.QColor(225, 29, 72, 30))
            grad_up.setColorAt(1.0, QtGui.QColor(225, 29, 72, 5))
            painter.fillPath(path_up_fill, grad_up)

            painter.setPen(QtGui.QPen(QtGui.QColor("#E11D48"), 1.8))
            painter.drawPath(path_up)

        painter.setPen(QtGui.QPen(QtGui.QColor("#E2E8F0"), 1))
        painter.drawLine(0, int(bottom_y), w, int(bottom_y))

        font = QtGui.QFont("Segoe UI", 8)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#94A3B8"))
        painter.drawText(8, h - 6, "60s trước")
        painter.drawText(int(w / 2) - 24, h - 6, "30s trước")
        painter.drawText(w - 48, h - 6, "Hiện tại")

        peak_val = max(max(self.down_points), max(self.up_points))
        peak_text = f"Đỉnh: {format_speed(peak_val)}"
        painter.drawText(w - 110, 16, peak_text)
