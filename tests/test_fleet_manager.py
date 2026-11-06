"""
Kiểm thử tự động cho Giai đoạn 3: FleetManagerService quản lý tập trung và định tuyến lệnh.
"""

import sys
import os
import unittest
import time
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.network_protocol import (
    CommandAction,
    DeviceRegistrationData,
    TelemetryPacket,
    CommandResultPacket,
)
from application.fleet_manager_service import FleetManagerService
from application.client_agent_service import ClientAgentService


class TestFleetManagerService(unittest.TestCase):
    """Kiểm tra điều phối đa máy con, cập nhật cache, summary và gửi lệnh từ FleetManagerService."""

    TEST_HOST = "127.0.0.1"
    TEST_PORT = 19995

    def test_fleet_manager_coordination_and_command_dispatch(self):
        # 1. Khởi động FleetManagerService (enable_db=False để kiểm thử chạy độc lập không phụ thuộc MySQL service)
        fleet_service = FleetManagerService(
            host=self.TEST_HOST,
            port=self.TEST_PORT,
            enable_db=False,
        )
        self.assertTrue(fleet_service.start(), "FleetManagerService phải khởi động thành công")

        fleet_updated_event = threading.Event()
        telemetry_updated_event = threading.Event()
        command_result_event = threading.Event()

        def on_fleet_up():
            fleet_updated_event.set()

        def on_telem_up(uuid_val, telem):
            telemetry_updated_event.set()

        def on_res_up(res):
            command_result_event.set()

        fleet_service.on_fleet_updated = on_fleet_up
        fleet_service.on_telemetry_updated = on_telem_up
        fleet_service.on_command_result = on_res_up

        # 2. Khởi tạo Client Agent
        agent = ClientAgentService(
            server_host=self.TEST_HOST,
            server_port=self.TEST_PORT,
            update_interval=0.5,
            enable_etw=False,
        )

        try:
            # 3. Khởi động Agent
            agent.start()

            # 4. Chờ FleetManagerService đăng ký thiết bị vào cache
            self.assertTrue(fleet_updated_event.wait(timeout=4.0), "FleetManagerService phải nhận được đăng ký máy")
            devices = fleet_service.get_all_device_states()
            self.assertEqual(len(devices), 1)
            dev_state = devices[0]
            self.assertEqual(dev_state.device_uuid, agent.device_uuid)
            self.assertEqual(dev_state.status, "online")

            # 5. Kiểm tra Telemetry cập nhật
            self.assertTrue(telemetry_updated_event.wait(timeout=4.0), "FleetManagerService phải nhận được telemetry")
            self.assertIsNotNone(dev_state.latest_telemetry)
            self.assertGreaterEqual(len(dev_state.download_history), 1)

            # 6. Kiểm tra Fleet Summary
            summary = fleet_service.get_fleet_summary()
            self.assertEqual(summary["total_devices"], 1)
            self.assertEqual(summary["online_devices"], 1)
            self.assertEqual(summary["offline_devices"], 0)

            # 7. Gửi lệnh can thiệp từ xa (PING)
            command_result_event.clear()
            ok, msg = fleet_service.send_remote_command(
                device_uuid=agent.device_uuid,
                action=CommandAction.PING.value,
            )
            self.assertTrue(ok, f"Gửi lệnh PING phải thành công: {msg}")
            self.assertTrue(command_result_event.wait(timeout=4.0), "Phải nhận được phản hồi kết quả lệnh")

            # 8. Gửi lệnh THROTTLE_SET (giới hạn băng thông)
            command_result_event.clear()
            ok, msg = fleet_service.send_remote_command(
                device_uuid=agent.device_uuid,
                action=CommandAction.THROTTLE_SET.value,
                target_app="chrome.exe",
                limit_kbps=300.0,
                direction="both",
            )
            self.assertTrue(ok, f"Gửi lệnh THROTTLE_SET phải thành công: {msg}")
            self.assertIn("chrome.exe", dev_state.applied_policies)

            # 9. Kiểm tra ngắt kết nối
            fleet_updated_event.clear()
            agent.stop()
            self.assertTrue(fleet_updated_event.wait(timeout=4.0), "Phải nhận được sự kiện ngắt kết nối")
            self.assertEqual(dev_state.status, "offline")

            summary_after = fleet_service.get_fleet_summary()
            self.assertEqual(summary_after["online_devices"], 0)
            self.assertEqual(summary_after["offline_devices"], 1)

        finally:
            agent.stop()
            fleet_service.stop()


if __name__ == "__main__":
    unittest.main()
