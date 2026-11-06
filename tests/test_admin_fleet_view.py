"""
Kiểm thử tự động cho Giai đoạn 4: Giao diện AdminFleetView (chạy offscreen với PyQt6).
"""

import sys
import os
import unittest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.network_protocol import DeviceRegistrationData
from application.fleet_manager_service import FleetDeviceState
from presentation.admin_fleet_view import AdminFleetView

# Đảm bảo có QApplication chạy chế độ offscreen
os.environ["QT_QPA_PLATFORM"] = "offscreen"
app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)


class TestAdminFleetView(unittest.TestCase):
    """Kiểm tra giao diện AdminFleetView render dữ liệu và phát tín hiệu chọn máy."""

    def setUp(self):
        self.view = AdminFleetView()

    def test_update_fleet_data_and_table_render(self):
        # 1. Tạo 2 máy giả lập: 1 online, 1 offline
        dev1 = FleetDeviceState(
            device_uuid="uuid-client-1111",
            status="online",
            ip_address="192.168.1.10",
            current_down_kbps=1500.0,
            current_up_kbps=120.0,
            applied_policies={"chrome.exe": {"action": "BLOCK_APP"}},
            registration=DeviceRegistrationData(
                device_uuid="uuid-client-1111",
                hostname="CLIENT-ALPHA",
                os_name="Windows",
                os_version="11",
            ),
        )

        dev2 = FleetDeviceState(
            device_uuid="uuid-client-2222",
            status="offline",
            ip_address="192.168.1.20",
            current_down_kbps=0.0,
            current_up_kbps=0.0,
            registration=DeviceRegistrationData(
                device_uuid="uuid-client-2222",
                hostname="CLIENT-BETA",
                os_name="Windows",
                os_version="10",
            ),
        )

        summary = {
            "total_devices": 2,
            "online_devices": 1,
            "offline_devices": 1,
            "total_down_kbps": 1500.0,
            "total_up_kbps": 120.0,
        }

        # 2. Cập nhật dữ liệu vào view
        self.view.update_fleet_data([dev1, dev2], summary)

        # 3. Kiểm tra các thẻ HUD Cards
        self.assertEqual(self.view.card_total.lbl_val.text(), "2 máy")
        self.assertIn("1 Online", self.view.card_status.lbl_val.text())
        self.assertIn("1 Offline", self.view.card_status.lbl_val.text())

        # 4. Kiểm tra số dòng trong bảng
        self.assertEqual(self.view.tbl_devices.rowCount(), 2)

        # Dòng 0: CLIENT-ALPHA
        row0_status = self.view.tbl_devices.item(0, 0).text()
        self.assertIn("ONLINE", row0_status)

        row0_name = self.view.tbl_devices.item(0, 1).text()
        self.assertIn("CLIENT-ALPHA", row0_name)

        row0_policy = self.view.tbl_devices.item(0, 6).text()
        self.assertIn("1 Chặn", row0_policy)

        # Dòng 1: CLIENT-BETA
        row1_status = self.view.tbl_devices.item(1, 0).text()
        self.assertIn("OFFLINE", row1_status)

    def test_filter_and_search(self):
        dev1 = FleetDeviceState(
            device_uuid="uuid-client-1111",
            status="online",
            ip_address="192.168.1.10",
            registration=DeviceRegistrationData(
                device_uuid="uuid-client-1111",
                hostname="CLIENT-ALPHA",
                os_name="Windows",
                os_version="11",
            ),
        )
        dev2 = FleetDeviceState(
            device_uuid="uuid-client-2222",
            status="offline",
            ip_address="192.168.1.20",
            registration=DeviceRegistrationData(
                device_uuid="uuid-client-2222",
                hostname="CLIENT-BETA",
                os_name="Windows",
                os_version="10",
            ),
        )

        self.view.update_fleet_data([dev1, dev2], {"total_devices": 2, "online_devices": 1, "offline_devices": 1})

        # Tìm kiếm theo từ khóa 'alpha'
        self.view.txt_search.setText("alpha")
        self.assertEqual(self.view.tbl_devices.rowCount(), 1)
        self.assertIn("CLIENT-ALPHA", self.view.tbl_devices.item(0, 1).text())

        # Xóa tìm kiếm
        self.view.txt_search.setText("")
        self.assertEqual(self.view.tbl_devices.rowCount(), 2)

        # Lọc chỉ máy Online
        self.view.cbo_status_filter.setCurrentIndex(1)
        self.assertEqual(self.view.tbl_devices.rowCount(), 1)
        self.assertIn("ONLINE", self.view.tbl_devices.item(0, 0).text())

    def test_machine_selected_signal(self):
        dev = FleetDeviceState(
            device_uuid="uuid-selected-9999",
            status="online",
            registration=DeviceRegistrationData(
                device_uuid="uuid-selected-9999",
                hostname="SELECT-PC",
                os_name="Windows",
                os_version="11",
            ),
        )
        self.view.update_fleet_data([dev], {"total_devices": 1, "online_devices": 1, "offline_devices": 0})

        emitted_uuids = []
        self.view.machine_selected.connect(lambda uid: emitted_uuids.append(uid))

        # Kích hoạt double click vào dòng 0
        self.view._on_table_row_double_clicked(0, 1)
        self.assertEqual(len(emitted_uuids), 1)
        self.assertEqual(emitted_uuids[0], "uuid-selected-9999")


if __name__ == "__main__":
    unittest.main()
