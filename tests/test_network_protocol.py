"""
Kiểm thử tự động cho Giai đoạn 1: Giao thức truyền thông & Tầng hạ tầng mạng Client - Server.
"""

import sys
import os
import unittest
import time
import threading

# Đảm bảo đường dẫn gốc dự án có trong sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models.network_protocol import (
    MessageType,
    CommandAction,
    DeviceRegistrationData,
    SpeedMetricsData,
    ProcessMetricData,
    TelemetryPacket,
    CommandPacket,
    CommandResultPacket,
    ProtocolMessage,
)
from infrastructure.network_server import NetworkServer
from infrastructure.network_client import NetworkClient


class TestNetworkProtocolAndTransport(unittest.TestCase):
    """Kiểm thử DTOs, Framing và truyền nhận 2 chiều giữa Server và Client."""

    TEST_HOST = "127.0.0.1"
    TEST_PORT = 19988  # Port riêng biệt phục vụ unit test

    def test_01_dto_serialization(self):
        """Kiểm thử tính toàn vẹn khi chuyển đổi DTO sang Dict và ngược lại."""
        reg = DeviceRegistrationData(
            device_uuid="test-uuid-12345",
            hostname="TEST-PC",
            os_name="Windows",
            os_version="10.0.19045",
            agent_version="1.0.0",
            mac_address="00:11:22:33:44:55",
            ip_address="192.168.1.100",
        )
        d = reg.to_dict()
        reg_reconstructed = DeviceRegistrationData.from_dict(d)
        self.assertEqual(reg.device_uuid, reg_reconstructed.device_uuid)
        self.assertEqual(reg.hostname, reg_reconstructed.hostname)
        self.assertEqual(reg.ip_address, reg_reconstructed.ip_address)

        # Telemetry packet
        speeds = SpeedMetricsData(upload_kbps=150.5, download_kbps=1024.0, total_up_bytes=5000, total_down_bytes=20000)
        procs = [ProcessMetricData(name="chrome.exe", pid=1001, down_kbps=800.0, up_kbps=20.0, connections_count=5)]
        telem = TelemetryPacket(
            device_uuid="test-uuid-12345",
            timestamp=time.time(),
            speeds=speeds,
            system_stats={"cpu": 25.0, "ram": 55.0},
            top_processes=procs,
            active_conns_count=12,
        )
        telem_d = telem.to_dict()
        telem_reconstructed = TelemetryPacket.from_dict(telem_d)
        self.assertEqual(telem.device_uuid, telem_reconstructed.device_uuid)
        self.assertEqual(len(telem_reconstructed.top_processes), 1)
        self.assertEqual(telem_reconstructed.top_processes[0].name, "chrome.exe")
        self.assertEqual(telem_reconstructed.speeds.download_kbps, 1024.0)

        # Command packet
        cmd = CommandPacket(
            command_id="cmd-999",
            target_device_uuid="test-uuid-12345",
            action=CommandAction.THROTTLE_SET.value,
            target_app="brave.exe",
            limit_kbps=250.0,
            direction="both",
        )
        cmd_d = cmd.to_dict()
        cmd_reconstructed = CommandPacket.from_dict(cmd_d)
        self.assertEqual(cmd.command_id, cmd_reconstructed.command_id)
        self.assertEqual(cmd.limit_kbps, 250.0)
        self.assertEqual(cmd.action, CommandAction.THROTTLE_SET.value)

    def test_02_server_client_bidirectional_communication(self):
        """
        Kiểm thử tích hợp luồng:
        1. Server lắng nghe kết nối.
        2. Client kết nối và đăng ký Device.
        3. Client gửi Telemetry ➔ Server nhận được.
        4. Server gửi Command ➔ Client nhận và thực thi callback.
        5. Client phản hồi CommandResult ➔ Server nhận được kết quả.
        """
        server = NetworkServer(host=self.TEST_HOST, port=self.TEST_PORT)
        server_started = server.start()
        self.assertTrue(server_started, "Server phải khởi động thành công")

        # Đồng bộ hóa sự kiện giữa 2 luồng
        registered_event = threading.Event()
        telemetry_event = threading.Event()
        command_received_event = threading.Event()
        result_received_event = threading.Event()

        received_reg: list = []
        received_telemetry: list = []
        received_cmd: list = []
        received_res: list = []

        # Cài đặt Callbacks cho Server
        def on_reg(reg_data: DeviceRegistrationData):
            received_reg.append(reg_data)
            registered_event.set()

        def on_telem(uuid_val: str, telem: TelemetryPacket):
            received_telemetry.append((uuid_val, telem))
            telemetry_event.set()

        def on_cmd_res(res: CommandResultPacket):
            received_res.append(res)
            result_received_event.set()

        server.on_device_registered = on_reg
        server.on_telemetry_received = on_telem
        server.on_command_result = on_cmd_res

        # Khởi tạo Client
        client_reg = DeviceRegistrationData(
            device_uuid="client-unit-test-uuid",
            hostname="UNITTEST-CLIENT",
            os_name="Windows",
            os_version="11",
            agent_version="1.0.0",
            mac_address="AA:BB:CC:DD:EE:FF",
            ip_address="127.0.0.1",
        )
        client = NetworkClient(
            server_host=self.TEST_HOST,
            server_port=self.TEST_PORT,
            reg_data=client_reg,
            heartbeat_interval=1.0,
            reconnect_delay=1.0,
        )

        # Cài đặt Callback nhận lệnh tại Client
        def on_client_cmd(cmd: CommandPacket):
            received_cmd.append(cmd)
            command_received_event.set()
            # Trả về kết quả thực thi
            return CommandResultPacket(
                command_id=cmd.command_id,
                device_uuid=client_reg.device_uuid,
                status="SUCCESS",
                message=f"Đã thực hiện bóp băng thông '{cmd.target_app}' thành công",
                applied_at=time.time(),
            )

        client.on_command_received = on_client_cmd

        try:
            # 1. Bắt đầu Client
            client.start()

            # 2. Chờ đăng ký thành công (tối đa 4 giây)
            is_reg_ok = registered_event.wait(timeout=4.0)
            self.assertTrue(is_reg_ok, "Server phải nhận được gói đăng ký từ Client")
            self.assertEqual(len(received_reg), 1)
            self.assertEqual(received_reg[0].device_uuid, "client-unit-test-uuid")
            self.assertEqual(received_reg[0].hostname, "UNITTEST-CLIENT")

            # Chờ Client kích hoạt cờ kết nối hoàn tất
            t_end = time.time() + 2.0
            while not client.is_connected() and time.time() < t_end:
                time.sleep(0.02)

            # 3. Client gửi Telemetry
            telem_packet = TelemetryPacket(
                device_uuid="client-unit-test-uuid",
                timestamp=time.time(),
                speeds=SpeedMetricsData(upload_kbps=350.0, download_kbps=1800.0),
                system_stats={"cpu_percent": 18.5, "ram_percent": 60.0},
                top_processes=[
                    ProcessMetricData(name="chrome.exe", pid=2024, down_kbps=1500.0, up_kbps=100.0)
                ],
                active_conns_count=28,
            )
            sent_telem_ok = client.send_telemetry(telem_packet)
            self.assertTrue(sent_telem_ok, "Client phải gửi telemetry thành công")

            is_telem_ok = telemetry_event.wait(timeout=4.0)
            self.assertTrue(is_telem_ok, "Server phải nhận được telemetry từ Client")
            self.assertEqual(len(received_telemetry), 1)
            self.assertEqual(received_telemetry[0][0], "client-unit-test-uuid")
            self.assertEqual(received_telemetry[0][1].speeds.download_kbps, 1800.0)

            # 4. Server gửi Command can thiệp xuống Client
            test_cmd = CommandPacket(
                command_id="CMD-TEST-001",
                target_device_uuid="client-unit-test-uuid",
                action=CommandAction.THROTTLE_SET.value,
                target_app="chrome.exe",
                limit_kbps=500.0,
                direction="both",
            )
            sent_cmd_ok = server.send_command("client-unit-test-uuid", test_cmd)
            self.assertTrue(sent_cmd_ok, "Server phải gửi lệnh thành công tới Client")

            # 5. Chờ Client nhận lệnh và phản hồi kết quả
            is_cmd_ok = command_received_event.wait(timeout=4.0)
            self.assertTrue(is_cmd_ok, "Client phải nhận được lệnh từ Server")
            self.assertEqual(len(received_cmd), 1)
            self.assertEqual(received_cmd[0].target_app, "chrome.exe")
            self.assertEqual(received_cmd[0].limit_kbps, 500.0)

            # 6. Server nhận được Command Result
            is_res_ok = result_received_event.wait(timeout=4.0)
            self.assertTrue(is_res_ok, "Server phải nhận được phản hồi CommandResult từ Client")
            self.assertEqual(len(received_res), 1)
            self.assertEqual(received_res[0].command_id, "CMD-TEST-001")
            self.assertEqual(received_res[0].status, "SUCCESS")

        finally:
            client.stop()
            server.stop()

    def test_03_client_auto_reconnect(self):
        """Kiểm thử tính năng tự động kết nối lại khi Server bị khởi động lại."""
        reconnect_port = 19989
        server = NetworkServer(host=self.TEST_HOST, port=reconnect_port)
        self.assertTrue(server.start())

        reconnected_event = threading.Event()
        reg_count = 0

        def on_reg(reg_data: DeviceRegistrationData):
            nonlocal reg_count
            reg_count += 1
            if reg_count >= 2:
                reconnected_event.set()

        server.on_device_registered = on_reg

        client_reg = DeviceRegistrationData(
            device_uuid="client-reconnect-uuid",
            hostname="RECONNECT-CLIENT",
            os_name="Windows",
            os_version="11",
        )
        client = NetworkClient(
            server_host=self.TEST_HOST,
            server_port=reconnect_port,
            reg_data=client_reg,
            heartbeat_interval=0.5,
            reconnect_delay=0.5,
        )

        try:
            client.start()
            time.sleep(0.5)
            self.assertEqual(reg_count, 1, "Lần đăng ký đầu tiên phải thành công")

            # Mô phỏng Server bị tắt đột ngột
            server.stop()
            time.sleep(0.5)

            # Server khởi động lại
            server = NetworkServer(host=self.TEST_HOST, port=reconnect_port)
            server.on_device_registered = on_reg
            self.assertTrue(server.start())

            # Chờ Client tự động kết nối lại và đăng ký lần 2
            is_reconnected = reconnected_event.wait(timeout=4.0)
            self.assertTrue(is_reconnected, "Client phải tự động phát hiện và kết nối lại thành công sau khi Server bật lại")
            self.assertGreaterEqual(reg_count, 2)
        finally:
            client.stop()
            server.stop()


if __name__ == "__main__":
    unittest.main()

