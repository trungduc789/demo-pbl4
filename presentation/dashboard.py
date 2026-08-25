import time
from collections import deque
import numpy as np
from PyQt6 import QtWidgets, QtCore
import pyqtgraph as pg

from application.monitor_service import MonitorService
from config.settings import UPDATE_INTERVAL, MAX_DATA_POINTS

class TimeAxisItem(pg.AxisItem):
    """Trục hoành biểu đồ hiển thị mốc giờ hệ thống (HH:MM:SS)"""
    def tickStrings(self, values, scale, spacing):
        return [time.strftime("%H:%M:%S", time.localtime(val)) for val in values]


class HighFreqNetworkWorker(QtCore.QThread):
    """Luồng phụ đọc chỉ số từ kernel định kỳ mỗi 100ms[cite: 1, 2]"""
    sample_ready = QtCore.pyqtSignal(float, float, float)

    def __init__(self, service: MonitorService):
        super().__init__()
        self.service = service
        self.running = True

    def run(self):
        while self.running:
            speed = self.service.fetch_latest_speed()
            self.sample_ready.emit(time.time(), speed.download_speed_kb, speed.upload_speed_kb)
            self.msleep(int(UPDATE_INTERVAL * 1000))

    def stop(self):
        self.running = False
        self.wait()


class BandwidthDashboard(QtWidgets.QMainWindow):
    """Giao diện vẽ sóng GlassWire với thẻ Ngày riêng biệt và trục Timeline giờ thực[cite: 2, 3]"""
    def __init__(self, monitor_service: MonitorService):
        super().__init__()
        self.service = monitor_service
        self.window_duration = 30.0  # Dải thời gian hiển thị 30 giây

        # Khởi tạo buffer phủ kín màn hình
        now = time.time()
        init_times = np.linspace(now - self.window_duration, now, MAX_DATA_POINTS)
        self.down_points = deque([(t, 0.0) for t in init_times], maxlen=MAX_DATA_POINTS * 2)
        self.up_points = deque([(t, 0.0) for t in init_times], maxlen=MAX_DATA_POINTS * 2)

        self.latest_down = 0.0
        self.latest_up = 0.0

        self._init_ui()
        self._start_worker()
        self._start_render_loop()

    def _init_ui(self):
        self.setWindowTitle("Network Flow Monitor")
        self.resize(1020, 570)
        self.setStyleSheet("background-color: #0B0E17; color: #FFFFFF;")

        main_layout = QtWidgets.QVBoxLayout()
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        # Thanh HUD thông tin: Thẻ Ngày riêng biệt + Thẻ Băng thông tức thời[cite: 2]
        cards_layout = QtWidgets.QHBoxLayout()
        cards_layout.setSpacing(10)

        # 1. Thẻ Ngày riêng biệt (Date Card)
        self.date_card = QtWidgets.QLabel("📅 Ngày: --/--/----")
        self.date_card.setStyleSheet("""
            background-color: #151C2C; color: #E2E8F0; font-size: 13px; font-weight: 600;
            padding: 8px 16px; border-radius: 6px; border: 1px solid #334155;
        """)

        # 2. Thẻ Download tức thời[cite: 2]
        self.down_card = QtWidgets.QLabel("📥 Download: 0.00 KB/s")
        self.down_card.setStyleSheet("""
            background-color: #151C2C; color: #FBBF24; font-size: 13px; font-weight: bold;
            padding: 8px 16px; border-radius: 6px; border: 1px solid #FBBF2440;
        """)

        # 3. Thẻ Upload tức thời[cite: 2]
        self.up_card = QtWidgets.QLabel("📤 Upload: 0.00 KB/s")
        self.up_card.setStyleSheet("""
            background-color: #151C2C; color: #FB923C; font-size: 13px; font-weight: bold;
            padding: 8px 16px; border-radius: 6px; border: 1px solid #FB923C40;
        """)

        cards_layout.addWidget(self.date_card)
        cards_layout.addWidget(self.down_card)
        cards_layout.addWidget(self.up_card)
        cards_layout.addStretch()
        main_layout.addLayout(cards_layout)

        # Cấu hình biểu đồ kèm trục thời gian
        pg.setConfigOption("antialias", True)
        self.time_axis = TimeAxisItem(orientation='bottom')
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': self.time_axis}, background="#0B0E17")
        
        vb = self.plot_widget.getViewBox()
        vb.setDefaultPadding(0.0)
        vb.setMouseEnabled(x=False, y=True)
        self.plot_widget.showGrid(x=False, y=True, alpha=0.12)

        # Khóa cứng trục Y tự động co giãn theo biên độ
        self.plot_widget.setYRange(0, 100, padding=0.08)
        self.plot_widget.enableAutoRange(axis='y', enable=True)

        self.plot_widget.getAxis('bottom').setPen(pg.mkPen("#334155"))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen("#94A3B8"))
        self.plot_widget.getAxis('left').setPen(pg.mkPen("#334155"))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen("#94A3B8"))

        # Dải màu Download (Vàng hổ phách)
        self.curve_down = self.plot_widget.plot(
            pen=pg.mkPen(color="#FBBF24", width=2.2),
            fillLevel=0,
            brush=(251, 191, 36, 175)
        )

        # Dải màu Upload (Cam san hô)
        self.curve_up = self.plot_widget.plot(
            pen=pg.mkPen(color="#FB923C", width=2.2),
            fillLevel=0,
            brush=(251, 146, 60, 205)
        )

        main_layout.addWidget(self.plot_widget)

        container = QtWidgets.QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def _start_worker(self):
        self.worker = HighFreqNetworkWorker(self.service)
        self.worker.sample_ready.connect(self._on_new_sample)
        self.worker.start()

    def _start_render_loop(self):
        self.render_timer = QtCore.QTimer()
        self.render_timer.setInterval(16)  # 60 FPS
        self.render_timer.timeout.connect(self._render_frame)
        self.render_timer.start()

    @QtCore.pyqtSlot(float, float, float)
    def _on_new_sample(self, timestamp, down_kb, up_kb):
        self.latest_down = down_kb
        self.latest_up = up_kb
        self.down_points.append((timestamp, down_kb))
        self.up_points.append((timestamp, up_kb))

        # Cập nhật số liệu tức thời
        self.down_card.setText(f"📥 Download: {down_kb:,.2f} KB/s")
        self.up_card.setText(f"📤 Upload: {up_kb:,.2f} KB/s")

    @staticmethod
    def _generate_smooth_bell_wave(x_raw, y_raw, x_dense):
        """Khử nhọn bằng bộ lọc Gaussian 5 điểm và nội suy Hermite liên tục"""
        n = len(x_raw)
        if n < 4:
            return x_dense, np.zeros_like(x_dense)

        # Lọc Gaussian
        kernel = np.array([0.08, 0.25, 0.34, 0.25, 0.08])
        y_smooth = np.convolve(y_raw, kernel, mode='same')

        # Bù biên độ đỉnh thật
        max_raw = np.max(y_raw)
        max_smooth = np.max(y_smooth)
        if max_smooth > 0 and max_raw > 0:
            scale_factor = max_raw / max_smooth
            y_smooth = np.where(y_smooth > 0.05 * max_smooth, y_smooth * scale_factor, y_smooth)

        dx = np.gradient(x_raw)
        dy = np.gradient(y_smooth)
        dx = np.where(dx == 0, 1e-6, dx)
        tangents = dy / dx

        indices = np.searchsorted(x_raw, x_dense) - 1
        indices = np.clip(indices, 0, n - 2)

        x0, x1 = x_raw[indices], x_raw[indices + 1]
        y0, y1 = y_smooth[indices], y_smooth[indices + 1]
        m0, m1 = tangents[indices], tangents[indices + 1]

        h = np.where(x1 - x0 == 0, 1e-6, x1 - x0)
        t = (x_dense - x0) / h
        t2 = t * t
        t3 = t2 * t

        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + t
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2

        y_dense = h00 * y0 + h10 * h * m0 + h01 * y1 + h11 * h * m1
        return np.clip(y_dense, a_min=0.0, a_max=None)

    def _render_frame(self):
        now = time.time()

        # 1. Cập nhật thẻ hiển thị ngày hệ thống riêng biệt
        current_date_str = time.strftime("📅 Ngày: %d/%m/%Y")
        if self.date_card.text() != current_date_str:
            self.date_card.setText(current_date_str)

        # 2. Dọn dẹp mẫu quá cũ ngoài màn hình
        cutoff = now - self.window_duration - 2.0
        while self.down_points and self.down_points[0][0] < cutoff:
            self.down_points.popleft()
        while self.up_points and self.up_points[0][0] < cutoff:
            self.up_points.popleft()

        d_raw_x = np.array([p[0] for p in self.down_points], dtype=np.float64)
        d_raw_y = np.array([p[1] for p in self.down_points], dtype=np.float64)

        u_raw_x = np.array([p[0] for p in self.up_points], dtype=np.float64)
        u_raw_y = np.array([p[1] for p in self.up_points], dtype=np.float64)

        # Chốt ngòi bút sát mép phải (now)
        if len(d_raw_x) > 0 and d_raw_x[-1] < now:
            d_raw_x = np.append(d_raw_x, now)
            d_raw_y = np.append(d_raw_y, self.latest_down)
        if len(u_raw_x) > 0 and u_raw_x[-1] < now:
            u_raw_x = np.append(u_raw_x, now)
            u_raw_y = np.append(u_raw_y, self.latest_up)

        x_dense = np.linspace(now - self.window_duration, now, 300)

        smooth_down = self._generate_smooth_bell_wave(d_raw_x, d_raw_y, x_dense)
        smooth_up = self._generate_smooth_bell_wave(u_raw_x, u_raw_y, x_dense)

        self.curve_down.setData(x_dense, smooth_down)
        self.curve_up.setData(x_dense, smooth_up)

        # Trượt trục camera liên tục
        self.plot_widget.setXRange(now - self.window_duration, now, padding=0.0)

    def closeEvent(self, event):
        self.render_timer.stop()
        self.worker.stop()
        event.accept()