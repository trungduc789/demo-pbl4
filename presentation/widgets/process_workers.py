"""
Các bộ quản lý Icon tiến trình, Cầu nối tín hiệu bất đồng bộ & Worker thu thập tốc độ cao:
- ProcessIconManager: Trích xuất và cache QIcon từ đường dẫn thực thi Windows.
- AsyncSignalBridge: Chuyển tiếp callback từ luồng nền về luồng GUI chính an toàn.
- HighFreqNetworkWorker: QThread lấy mẫu lưu lượng định kỳ từ MonitorService.
"""

import time
from typing import Dict
from PyQt6 import QtWidgets, QtCore, QtGui

from application.monitor_service import MonitorService
from config.settings import UPDATE_INTERVAL
from utils.safe_print import safe_print as print


class ProcessIconManager:
    """Quản lý và trích xuất Icon chính thức của ứng dụng Windows."""

    def __init__(self):
        self._provider = QtWidgets.QFileIconProvider()
        self._cache: Dict[str, QtGui.QIcon] = {}

    def get_icon(self, exe_path: str, proc_name: str) -> QtGui.QIcon:
        if proc_name in self._cache:
            return self._cache[proc_name]

        if exe_path:
            fi = QtCore.QFileInfo(exe_path)
            if fi.exists():
                icon = self._provider.icon(fi)
                if not icon.isNull():
                    self._cache[proc_name] = icon
                    return icon

        pixmap = QtGui.QPixmap(22, 22)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pixmap)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        palette = ["#2563EB", "#059669", "#D97706", "#7C3AED", "#DB2777", "#0891B2"]
        color = palette[abs(hash(proc_name)) % len(palette)]

        p.setBrush(QtGui.QColor(color))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(1, 1, 20, 20, 4, 4)

        p.setPen(QtGui.QColor("#FFFFFF"))
        p.setFont(QtGui.QFont("Segoe UI", 8, QtGui.QFont.Weight.Bold))
        char = proc_name[0].upper() if proc_name else "?"
        p.drawText(QtCore.QRect(0, 0, 22, 22), QtCore.Qt.AlignmentFlag.AlignCenter, char)
        p.end()

        icon = QtGui.QIcon(pixmap)
        self._cache[proc_name] = icon
        return icon


class AsyncSignalBridge(QtCore.QObject):
    """Cầu nối tín hiệu luồng ngầm an toàn tuyệt đối 100% về luồng giao diện chính PyQt6."""

    sig_callback = QtCore.pyqtSignal(object, tuple, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sig_callback.connect(self._on_callback)

    def _on_callback(self, fn, args, kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as e:
            print(f"[AsyncBridge Callback Error] {e}")

    def run_on_main(self, fn, *args, **kwargs):
        self.sig_callback.emit(fn, args, kwargs)


class HighFreqNetworkWorker(QtCore.QThread):
    """Luồng phụ đọc chỉ số từ kernel định kỳ, không làm đơ giao diện đồ họa."""

    sample_ready = QtCore.pyqtSignal(float, float, float, list, float, float)

    def __init__(self, service: MonitorService):
        super().__init__()
        self.service = service
        self.running = True

    def run(self):
        while self.running:
            try:
                speed, proc_speeds, total_p_down, total_p_up = self.service.fetch_latest_speed()
                self.sample_ready.emit(
                    time.time(),
                    speed.download_speed_kb,
                    speed.upload_speed_kb,
                    proc_speeds,
                    total_p_down,
                    total_p_up,
                )
            except Exception as e:
                print(f"[Worker Error] {e}")

            self.msleep(int(UPDATE_INTERVAL * 1000))

    def stop(self):
        self.running = False
        self.wait()
