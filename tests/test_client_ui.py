"""
Kiểm thử tự động cho Giao diện Máy Con (Client GUI & System Tray):
- Kiểm tra ClientStatusWindow hiển thị đầy đủ thông tin máy, bảng chính sách, số liệu mạng.
- Kiểm tra tạo icon khay hệ thống động (create_client_tray_icon) ở các trạng thái.
- Kiểm tra ClientTrayManager xử lý tín hiệu Toast an toàn luồng khi có can thiệp an ninh.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.network_protocol import DeviceRegistrationData, CommandAction
from presentation.client_status_window import ClientStatusWindow
from presentation.client_tray import ClientTrayManager, create_client_tray_icon

# Chạy Qt ở chế độ offscreen
os.environ["QT_QPA_PLATFORM"] = "offscreen"
app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)


class DummyAgentService:
    """Mock Agent Service cho mục đích kiểm thử UI."""
    def __init__(self):
        self.server_host = "127.0.0.1"
        self.server_port = 9988
        self.enable_etw = True
        self.show_toast_notifications = True
        self.device_uuid = "mock-client-uuid-12345"
        self.registration_data = DeviceRegistrationData(
            device_uuid="mock-client-uuid-12345",
            hostname="MOCK-WORKSTATION",
            os_name="Windows",
            os_version="11 Pro",
            mac_address="00:11:22:33:44:55",
            ip_address="192.168.1.150",
        )
        self.total_rx_bytes = 1024 * 1024 * 50   # 50 MB
        self.total_tx_bytes = 1024 * 1024 * 10   # 10 MB
        self.active_policies = {}
        self.packet_shaper = MagicMock()
        self.packet_shaper.is_active = True
        self.packet_shaper.current_down_kbps = 450.5
        self.packet_shaper.current_up_kbps = 85.2

        self.net_client = MagicMock()
        self.net_client.is_connected.return_value = True

        self.on_policy_applied_locally = None
        self.on_connected = None
        self.on_disconnected = None

    def update_server_target(self, host: str, port: int):
        self.server_host = host
        self.server_port = port

    def stop(self):
        pass


class TestClientUI(unittest.TestCase):
    """Kiểm tra toàn diện các thành phần giao diện máy con."""

    def setUp(self):
        self.mock_agent = DummyAgentService()
        self.status_window = ClientStatusWindow(self.mock_agent)
        self.tray_mgr = ClientTrayManager(self.mock_agent, self.status_window)

    def tearDown(self):
        self.tray_mgr.update_timer.stop()
        self.status_window.refresh_timer.stop()
        self.tray_mgr.tray_icon.hide()

    def test_status_window_initialization(self):
        """Kiểm tra khởi tạo ClientStatusWindow với 4 tab."""
        self.assertEqual(self.status_window.tab_widget.count(), 4)
        self.assertEqual(self.status_window.agent.registration_data.hostname, "MOCK-WORKSTATION")
        self.assertEqual(self.status_window.txt_server_ip.text(), "127.0.0.1")
        self.assertEqual(self.status_window.spin_port.value(), 9988)

    def test_status_window_refresh_policies(self):
        """Kiểm tra bảng chính sách cập nhật chính xác dữ liệu từ agent."""
        self.mock_agent.active_policies = {
            "chrome.exe": {
                "action": "BLOCK",
                "app": "chrome.exe",
                "limit_kbps": 0.0,
                "direction": "both",
                "time": 1000.0,
            },
            "torrent.exe": {
                "action": "THROTTLE",
                "app": "torrent.exe",
                "limit_kbps": 200.0,
                "direction": "both",
                "time": 1050.0,
            }
        }
        self.status_window._refresh_ui()

        # Bảng phải hiển thị 2 dòng
        self.assertEqual(self.status_window.tbl_policies.rowCount(), 2)

    def test_tray_icon_generation(self):
        """Kiểm tra tạo icon QIcon cho khay hệ thống ở các trạng thái khác nhau."""
        icon_connected = create_client_tray_icon(is_connected=True, is_alert=False)
        self.assertFalse(icon_connected.isNull())

        icon_disconnected = create_client_tray_icon(is_connected=False, is_alert=False)
        self.assertFalse(icon_disconnected.isNull())

        icon_alert = create_client_tray_icon(is_connected=True, is_alert=True)
        self.assertFalse(icon_alert.isNull())

    def test_tray_manager_policy_toast(self):
        """Kiểm tra bắt tín hiệu can thiệp an ninh và bắn Toast."""
        # Mock signal toast
        toast_calls = []
        self.tray_mgr.sig_toast.connect(lambda t, m, i: toast_calls.append((t, m, i)))

        # Giả lập Admin can thiệp BLOCK_APP
        self.tray_mgr._on_agent_policy_applied(
            action=CommandAction.BLOCK_APP.value,
            app_name="game.exe",
            status="SUCCESS",
            limit_kbps=0.0
        )

        self.assertEqual(len(toast_calls), 1)
        self.assertIn("game.exe", toast_calls[0][1])
        self.assertIn("CHẶN", toast_calls[0][1])

        # Giả lập Admin can thiệp THROTTLE_SET
        self.tray_mgr._on_agent_policy_applied(
            action=CommandAction.THROTTLE_SET.value,
            app_name="youtube.exe",
            status="SUCCESS",
            limit_kbps=150.0
        )

        self.assertEqual(len(toast_calls), 2)
        self.assertIn("150.0", toast_calls[1][1])


if __name__ == "__main__":
    unittest.main()
