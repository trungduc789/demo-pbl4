"""
Quản lý Khay Hệ thống & Thông Báo Máy Con (Client Tray Manager - Presentation Layer):
- Hiển thị biểu tượng động (QSystemTrayIcon) dưới khay Taskbar Windows.
- Đổi màu trạng thái: Xanh lá khi Online kết nối Server, Xám khi Mất kết nối, Đỏ khi có cảnh báo.
- Menu chuột phải hiển thị nhanh tốc độ mạng, trạng thái Server, số chính sách bảo vệ.
- Bắn thông báo Toast góc màn hình (Windows Balloon/Toast Notification) tức thời khi Admin
  thực hiện can thiệp Firewall hoặc Bóp băng thông (QoS) trên máy con này.
"""

import sys
import time
from typing import Optional
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QSystemTrayIcon, QMenu, QApplication, QMessageBox
)

from models.network_protocol import CommandAction
from utils.safe_print import safe_print as print


def create_client_tray_icon(is_connected: bool, is_alert: bool = False) -> QtGui.QIcon:
    """Tạo biểu tượng QIcon vector độ nét cao trực tiếp bằng QPainter."""
    size = 64
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

    # 1. Nền bo góc phong cách Modern Dark
    bg_color = QtGui.QColor("#0F172A")
    border_color = QtGui.QColor("#334155")
    painter.setBrush(QtGui.QBrush(bg_color))
    painter.setPen(QtGui.QPen(border_color, 2))
    painter.drawRoundedRect(QtCore.QRectF(4, 4, 56, 56), 14, 14)

    # 2. Vẽ biểu tượng Shield / Network Radar ở giữa
    icon_color = QtGui.QColor("#EF4444" if is_alert else ("#38BDF8" if is_connected else "#94A3B8"))
    pen_icon = QtGui.QPen(icon_color, 3)
    painter.setPen(pen_icon)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    # Vòng cung sóng tín hiệu mạng (Radar Waves)
    painter.drawArc(QtCore.QRectF(16, 16, 32, 32), 45 * 16, 90 * 16)
    painter.drawArc(QtCore.QRectF(22, 22, 20, 20), 45 * 16, 90 * 16)

    # Tâm trạm phát sóng
    painter.setBrush(QtGui.QBrush(icon_color))
    painter.drawEllipse(QtCore.QPointF(32, 38), 3, 3)

    # 3. Đèn LED trạng thái kết nối góc trên bên phải
    led_color = QtGui.QColor("#10B981" if is_connected else "#94A3B8")
    if is_alert:
        led_color = QtGui.QColor("#EF4444")
    painter.setBrush(QtGui.QBrush(led_color))
    painter.setPen(QtGui.QPen(QtGui.QColor("#FFFFFF"), 2))
    painter.drawEllipse(QtCore.QPointF(48, 16), 6, 6)

    painter.end()
    return QtGui.QIcon(pixmap)


class ClientTrayManager(QtCore.QObject):
    """Bộ điều phối Tray Icon và Thông báo máy con."""

    sig_toast = QtCore.pyqtSignal(str, str, int)       # (title, message, icon_type_int)
    sig_conn_state = QtCore.pyqtSignal(bool)           # (is_connected)

    def __init__(self, agent_service, status_window, parent=None):
        super().__init__(parent)
        self.agent = agent_service
        self.status_window = status_window
        self.app = QApplication.instance()
        self._is_connected_cache = False
        self._alert_until = 0.0

        # Kiểm tra xem hệ thống có hỗ trợ Tray không
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[ClientTray Warning] Hệ thống không hỗ trợ System Tray Icon.")

        # 1. Khởi tạo QSystemTrayIcon
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(create_client_tray_icon(is_connected=False))
        self.tray_icon.setToolTip("PBL4 Client Agent - Đang khởi động...")

        # 2. Khởi tạo Context Menu
        self._build_context_menu()

        # 3. Kết nối sự kiện click khay hệ thống
        self.tray_icon.activated.connect(self._on_tray_activated)

        # 4. Đăng ký tín hiệu an toàn luồng Qt (Thread-safe signals)
        self.sig_toast.connect(self._handle_toast_signal)
        self.sig_conn_state.connect(self._handle_conn_state_signal)

        # 5. Đăng ký callback từ Agent Service
        self.agent.on_policy_applied_locally = self._on_agent_policy_applied
        self.agent.on_connected = self._on_agent_connected
        self.agent.on_disconnected = self._on_agent_disconnected

        # 6. Timer định kỳ cập nhật Tooltip, Menu và Icon
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setInterval(1200)
        self.update_timer.timeout.connect(self._periodic_update)
        self.update_timer.start()

        # Hiển thị icon
        self.tray_icon.show()

    def _build_context_menu(self):
        """Khởi tạo menu chuột phải phong cách hiện đại."""
        self.menu = QMenu()
        self.menu.setStyleSheet("""
            QMenu {
                background-color: #FFFFFF;
                color: #1E293B;
                border: 1px solid #CBD5E1;
                border-radius: 8px;
                padding: 6px;
                font-family: 'Segoe UI', system-ui, sans-serif;
                font-size: 12px;
            }
            QMenu::item {
                padding: 6px 20px 6px 12px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #EFF6FF;
                color: #2563EB;
            }
            QMenu::separator {
                height: 1px;
                background-color: #E2E8F0;
                margin: 4px 6px;
            }
        """)

        # Tiêu đề ứng dụng
        self.act_title = self.menu.addAction(f"💻 Máy: {self.agent.registration_data.hostname}")
        self.act_title.setEnabled(False)
        f = self.act_title.font()
        f.setBold(True)
        self.act_title.setFont(f)

        # Trạng thái Server
        self.act_server = self.menu.addAction("🌐 Server: Đang kết nối...")
        self.act_server.setEnabled(False)

        self.menu.addSeparator()

        # Mở cửa sổ Dashboard/Status
        self.act_open = self.menu.addAction("📊 Mở Bảng Trạng Thái & Cấu Hình...")
        self.act_open.triggered.connect(self.show_status_window)
        f_open = self.act_open.font()
        f_open.setBold(True)
        self.act_open.setFont(f_open)

        self.menu.addSeparator()

        # Tốc độ thời gian thực
        self.act_speed_down = self.menu.addAction("⬇ Tải xuống: 0.0 KB/s")
        self.act_speed_down.setEnabled(False)
        self.act_speed_up = self.menu.addAction("⬆ Tải lên: 0.0 KB/s")
        self.act_speed_up.setEnabled(False)
        self.act_policy_cnt = self.menu.addAction("🛡 Chính sách an ninh: 0")
        self.act_policy_cnt.setEnabled(False)

        self.menu.addSeparator()

        # Thoát
        self.act_exit = self.menu.addAction("🚪 Thoát Client Agent")
        self.act_exit.triggered.connect(self.quit_application)

        self.tray_icon.setContextMenu(self.menu)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """Xử lý khi người dùng click chuột vào biểu tượng khay hệ thống."""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.status_window.isVisible():
                if self.status_window.isMinimized():
                    self.status_window.showNormal()
                self.status_window.raise_()
                self.status_window.activateWindow()
            else:
                self.show_status_window()

    def show_status_window(self):
        """Mở và đưa cửa sổ cấu hình trạng thái lên hàng đầu."""
        self.status_window.show()
        self.status_window.showNormal()
        self.status_window.raise_()
        self.status_window.activateWindow()

    # =========================================================================
    # THREAD-SAFE EVENT CALLBACKS TỪ CLIENT AGENT SERVICE
    # =========================================================================
    def _on_agent_policy_applied(self, action: str, app_name: str, status: str, limit_kbps: float):
        """Được gọi từ luồng socket nền khi Admin áp dụng chính sách lên máy con."""
        if status != "SUCCESS":
            return

        title = "PBL4 - An Ninh Mạng"
        body = ""
        icon_type = QSystemTrayIcon.MessageIcon.Information.value

        if action == CommandAction.BLOCK_APP.value:
            title = "🚫 Cảnh Báo Tường Lửa (Firewall)"
            body = f"Quản trị viên đã CHẶN truy cập mạng của ứng dụng: '{app_name}'."
            icon_type = QSystemTrayIcon.MessageIcon.Warning.value
            self._alert_until = time.time() + 5.0

        elif action == CommandAction.UNBLOCK_APP.value:
            title = "✅ Gỡ Bỏ Chặn Mạng"
            body = f"Ứng dụng '{app_name}' đã được cho phép kết nối mạng trở lại."
            icon_type = QSystemTrayIcon.MessageIcon.Information.value

        elif action == CommandAction.UNBLOCK_ALL.value:
            title = "✅ Khôi Phục Toàn Diện"
            body = "Tất cả các lệnh chặn ứng dụng trên máy này đã được gỡ bỏ."
            icon_type = QSystemTrayIcon.MessageIcon.Information.value

        elif action == CommandAction.THROTTLE_SET.value:
            title = "⚡ Giới Hạn Băng Thông (QoS)"
            body = f"Ứng dụng '{app_name}' đã bị giới hạn tốc độ tối đa {limit_kbps:,.1f} KB/s."
            icon_type = QSystemTrayIcon.MessageIcon.Warning.value
            self._alert_until = time.time() + 5.0

        elif action == CommandAction.REMOVE_LIMIT.value:
            title = "✅ Gỡ Bỏ Giới Hạn Tốc Độ"
            body = "Tốc độ mạng đã được khôi phục về bình thường không giới hạn."
            icon_type = QSystemTrayIcon.MessageIcon.Information.value

        if body:
            self.sig_toast.emit(title, body, icon_type)

    def _on_agent_connected(self):
        """Được gọi từ luồng socket nền khi kết nối thành công tới Server."""
        self.sig_conn_state.emit(True)
        title = "PBL4 Client Agent"
        body = f"Đã kết nối thành công tới máy chủ Admin ({self.agent.server_host}:{self.agent.server_port})."
        self.sig_toast.emit(title, body, QSystemTrayIcon.MessageIcon.Information.value)

    def _on_agent_disconnected(self):
        """Được gọi từ luồng socket nền khi mất kết nối tới Server."""
        self.sig_conn_state.emit(False)
        title = "PBL4 Client Agent - Mất Kết Nối"
        body = f"Mất kết nối tới Server ({self.agent.server_host}:{self.agent.server_port}). Đang thử kết nối lại..."
        self.sig_toast.emit(title, body, QSystemTrayIcon.MessageIcon.Warning.value)

    # =========================================================================
    # QT MAIN THREAD HANDLERS
    # =========================================================================
    def _handle_toast_signal(self, title: str, message: str, icon_int: int):
        """Hiển thị Toast trên khay hệ thống (Chạy trên main GUI thread)."""
        # Kiểm tra người dùng có bật tùy chọn thông báo không
        if not getattr(self.agent, "show_toast_notifications", True):
            return

        icon = QSystemTrayIcon.MessageIcon(icon_int)
        self.tray_icon.showMessage(title, message, icon, 4000)

    def _handle_conn_state_signal(self, is_connected: bool):
        """Cập nhật biểu tượng ngay khi có sự kiện kết nối thay đổi."""
        self._is_connected_cache = is_connected
        self._update_icon()

    def _periodic_update(self):
        """Cập nhật định kỳ nội dung menu, tooltip và icon."""
        is_conn = self.agent.net_client.is_connected()
        self._is_connected_cache = is_conn

        # 1. Cập nhật icon
        self._update_icon()

        # 2. Cập nhật tốc độ
        shaper = self.agent.packet_shaper
        down_kb = shaper.current_down_kbps if shaper.is_active else 0.0
        up_kb = shaper.current_up_kbps if shaper.is_active else 0.0
        pol_count = len(self.agent.active_policies)

        # 3. Cập nhật menu items
        status_text = "🟢 Đã kết nối" if is_conn else "⚪ Mất kết nối"
        self.act_server.setText(f"🌐 Server: {self.agent.server_host}:{self.agent.server_port} ({status_text})")
        self.act_speed_down.setText(f"⬇ Tải xuống: {down_kb:,.1f} KB/s")
        self.act_speed_up.setText(f"⬆ Tải lên: {up_kb:,.1f} KB/s")
        self.act_policy_cnt.setText(f"🛡 Chính sách an ninh: {pol_count} đang áp dụng")

        # 4. Cập nhật Tooltip
        tip = (
            f"PBL4 Client Agent - {self.agent.registration_data.hostname}\n"
            f"Trạng thái: {'Online (Admin Quản lý)' if is_conn else 'Offline (Đang tìm Server)'}\n"
            f"Server: {self.agent.server_host}:{self.agent.server_port}\n"
            f"Tải xuống: {down_kb:,.1f} KB/s | Tải lên: {up_kb:,.1f} KB/s\n"
            f"Chính sách bảo vệ: {pol_count}"
        )
        self.tray_icon.setToolTip(tip)

    def _update_icon(self):
        """Đổi biểu tượng tray theo trạng thái."""
        is_alert = time.time() < self._alert_until
        icon = create_client_tray_icon(is_connected=self._is_connected_cache, is_alert=is_alert)
        self.tray_icon.setIcon(icon)

    def quit_application(self):
        """Thoát ứng dụng an toàn."""
        print("[ClientTray] Đang thoát Client Agent...")
        self.update_timer.stop()
        self.tray_icon.hide()

        # Dừng agent service
        try:
            self.agent.stop()
        except Exception as e:
            print(f"[ClientTray] Lỗi khi dừng agent: {e}")

        # Đóng cửa sổ và thoát
        self.status_window.closeEvent = lambda event: event.accept()
        self.status_window.close()

        if self.app:
            self.app.quit()
        else:
            sys.exit(0)
