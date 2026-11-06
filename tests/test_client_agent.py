"""
Kiểm thử tự động cho Giai đoạn 2: Khởi chạy ClientAgentService, phát Telemetry và xử lý lệnh từ xa.
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
    CommandPacket,
    CommandResultPacket,
)
from infrastructure.network_server import NetworkServer
from application.client_agent_service import ClientAgentService


class TestClientAgentService(unittest.TestCase):
    """Kiểm tra tích hợp toàn diện ClientAgentService với NetworkServer."""

    TEST_HOST = "127.0.0.1"
    TEST_PORT = 19990

    def test_client_agent_lifecycle_and_remote_commands(self):
        # 1. Khởi động Server thử nghiệm
        server = NetworkServer(host=self.TEST_HOST, port=self.TEST_PORT)
        self.assertTrue(server.start(), "Server phải khởi động thành công")

        reg_event = threading.Event()
        telem_event = threading.Event()
        result_event = threading.Event()

        registered_devices: list = []
        received_telemetry: list = []
        received_results: list = []

        def on_reg(reg_data: DeviceRegistrationData):
            registered_devices.append(reg_data)
            reg_event.set()

        def on_telem(uuid_val: str, pkt: TelemetryPacket):
            received_telemetry.append((uuid_val, pkt))
            telem_event.set()

        def on_res(res: CommandResultPacket):
            received_results.append(res)
            result_event.set()

        server.on_device_registered = on_reg
        server.on_telemetry_received = on_telem
        server.on_command_result = on_res

        # 2. Khởi tạo ClientAgentService (enable_etw=False để kiểm thử không phụ thuộc UAC driver)
        agent = ClientAgentService(
            server_host=self.TEST_HOST,
            server_port=self.TEST_PORT,
            update_interval=0.5,
            enable_etw=False,
        )

        try:
            # 3. Khởi động Agent
            agent.start()

            # 4. Xác nhận đăng ký máy thành công
            self.assertTrue(reg_event.wait(timeout=4.0), "Server phải nhận được gói đăng ký từ Agent")
            self.assertEqual(len(registered_devices), 1)
            client_uuid = registered_devices[0].device_uuid
            self.assertEqual(client_uuid, agent.device_uuid)
            self.assertTrue(len(registered_devices[0].hostname) > 0)

            # 5. Xác nhận nhận được Telemetry thực tế
            self.assertTrue(telem_event.wait(timeout=4.0), "Server phải nhận được gói Telemetry định kỳ")
            self.assertGreaterEqual(len(received_telemetry), 1)
            latest_telem = received_telemetry[-1][1]
            self.assertIn("cpu_percent", latest_telem.system_stats)
            self.assertIn("ram_percent", latest_telem.system_stats)
            self.assertGreaterEqual(latest_telem.active_conns_count, 0)

            # 6. Thử nghiệm gửi lệnh PING từ Server xuống Client
            result_event.clear()
            ping_cmd = CommandPacket(
                command_id="CMD-PING-001",
                target_device_uuid=client_uuid,
                action=CommandAction.PING.value,
            )
            self.assertTrue(server.send_command(client_uuid, ping_cmd))
            self.assertTrue(result_event.wait(timeout=4.0), "Phải nhận được phản hồi PONG từ Agent")
            self.assertEqual(received_results[-1].command_id, "CMD-PING-001")
            self.assertEqual(received_results[-1].status, "SUCCESS")
            self.assertIn("Pong", received_results[-1].message)

            # 7. Thử nghiệm gửi lệnh REMOVE_LIMIT từ Server xuống Client
            result_event.clear()
            remove_cmd = CommandPacket(
                command_id="CMD-REMOVE-002",
                target_device_uuid=client_uuid,
                action=CommandAction.REMOVE_LIMIT.value,
            )
            self.assertTrue(server.send_command(client_uuid, remove_cmd))
            self.assertTrue(result_event.wait(timeout=4.0), "Phải nhận được phản hồi REMOVE_LIMIT từ Agent")
            self.assertEqual(received_results[-1].command_id, "CMD-REMOVE-002")

        finally:
            agent.stop()
            server.stop()


if __name__ == "__main__":
    unittest.main()
