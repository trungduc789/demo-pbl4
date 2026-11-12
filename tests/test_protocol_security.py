import socket
import struct
import threading
import time
import unittest

from infrastructure.network_client import NetworkClient
from infrastructure.network_server import NetworkServer
from infrastructure.protocol_framing import (
    DIR_CLIENT_TO_SERVER,
    DIR_SERVER_TO_CLIENT,
    RecvTimeoutError,
    recv_authenticated_message,
    recv_exact,
    send_authenticated_message,
    send_message,
)
from models.network_protocol import (
    CommandAction,
    CommandPacket,
    CommandResultPacket,
    DeviceRegistrationData,
    MessageType,
    ProtocolMessage,
    SpeedMetricsData,
    TelemetryPacket,
)


class TestProtocolSecurity(unittest.TestCase):
    """Kiểm thử bảo mật PSK+HMAC, Sequence Number, Session Binding, Telemetry ACK và Timeout."""

    HOST = "127.0.0.1"

    def _get_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.HOST, 0))
            return s.getsockname()[1]

    def test_01_recv_exact_timeout_distinction(self):
        """Phân biệt idle poll timeout và fatal/stall timeout trong recv_exact."""
        s1, s2 = socket.socketpair()
        try:
            s1.settimeout(0.05)
            # Idle poll timeout -> ném RecvTimeoutError khi timeout_is_fatal=False
            with self.assertRaises(RecvTimeoutError):
                recv_exact(s1, 4, timeout_is_fatal=False)

            # Handshake/fatal timeout -> trả về None khi timeout_is_fatal=True
            res_fatal = recv_exact(s1, 4, timeout_is_fatal=True)
            self.assertIsNone(res_fatal)

            # Partial-frame stall (đã đọc 2 bytes trên 4 bytes rồi timeout) -> trả về None kể cả timeout_is_fatal=False
            s2.sendall(b"\x01\x02")
            res_stall = recv_exact(s1, 4, timeout_is_fatal=False)
            self.assertIsNone(res_stall)
        finally:
            s1.close()
            s2.close()

    def test_02_wrong_psk_rejected_at_handshake(self):
        """Agent dùng sai PSK phải bị Server từ chối kết nối ngay ở bước bắt tay."""
        port = self._get_free_port()
        server = NetworkServer(host=self.HOST, port=port, psk="SECRET-KEY-1")
        self.assertTrue(server.start())

        reg = DeviceRegistrationData(
            device_uuid="attacker-uuid",
            hostname="ATTACKER",
            os_name="Windows",
            os_version="11",
        )
        client = NetworkClient(
            server_host=self.HOST,
            server_port=port,
            reg_data=reg,
            reconnect_delay=5.0,
            psk="WRONG-KEY-2",
        )
        try:
            client.start()
            time.sleep(0.4)
            self.assertFalse(client.is_connected())
            self.assertEqual(server.get_connected_uuids(), [])
        finally:
            client.stop()
            server.stop()

    def test_03_session_binding_and_telemetry_ack(self):
        """
        - Danh tính phiên gắn cứng vào UUID đã xác thực (bỏ qua UUID giả mạo trong payload).
        - Agent nhận được TELEMETRY_ACK sau khi gửi Telemetry.
        """
        port = self._get_free_port()
        server = NetworkServer(host=self.HOST, port=port, psk="TEST-PSK")
        received_telem_uuids = []
        received_cmd_res_uuids = []

        server.on_telemetry_received = lambda uid, pkt: received_telem_uuids.append((uid, pkt.device_uuid))
        server.on_command_result = lambda res: received_cmd_res_uuids.append(res.device_uuid)
        self.assertTrue(server.start())

        reg = DeviceRegistrationData(
            device_uuid="real-agent-uuid",
            hostname="REAL-PC",
            os_name="Windows",
            os_version="11",
        )
        client = NetworkClient(
            server_host=self.HOST,
            server_port=port,
            reg_data=reg,
            psk="TEST-PSK",
        )
        try:
            client.start()
            deadline = time.time() + 3.0
            while not client.is_connected() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(client.is_connected())

            # Gửi Telemetry nhưng cố tình nhét device_uuid giả vào payload
            spoofed_telem = TelemetryPacket(
                device_uuid="spoofed-victim-uuid",
                timestamp=123456.78,
                speeds=SpeedMetricsData(upload_kbps=10.0, download_kbps=20.0),
            )
            self.assertTrue(client.send_telemetry(spoofed_telem))

            # Gửi CommandResult với device_uuid giả
            spoofed_res = CommandResultPacket(
                command_id="CMD-1",
                device_uuid="spoofed-victim-uuid",
                status="SUCCESS",
                message="OK",
            )
            self.assertTrue(client.send_command_result(spoofed_res))

            time.sleep(0.3)
            self.assertEqual(received_telem_uuids, [("real-agent-uuid", "real-agent-uuid")])
            self.assertEqual(received_cmd_res_uuids, ["real-agent-uuid"])
            self.assertGreaterEqual(client.telemetry_ack_count, 1)
            self.assertAlmostEqual(client.last_telemetry_ack_ts, 123456.78, places=2)
        finally:
            client.stop()
            server.stop()

    def test_04_replay_and_tamper_protection(self):
        """Gói tin sai sequence number (replay) hoặc sai HMAC phải bị từ chối."""
        s1, s2 = socket.socketpair()
        session_key = b"0123456789abcdef0123456789abcdef"
        try:
            msg = ProtocolMessage(msg_type=MessageType.COMMAND.value, payload={"action": "PING"})
            self.assertTrue(send_authenticated_message(s1, msg, session_key, DIR_SERVER_TO_CLIENT, seq=1))

            # Đọc hợp lệ với expected_seq=1
            recv_ok = recv_authenticated_message(s2, session_key, DIR_SERVER_TO_CLIENT, expected_seq=1)
            self.assertIsNotNone(recv_ok)

            # Gửi lại gói seq=1 khi bên nhận kỳ vọng seq=2 (Replay attack) -> bị từ chối (None)
            self.assertTrue(send_authenticated_message(s1, msg, session_key, DIR_SERVER_TO_CLIENT, seq=1))
            recv_replay = recv_authenticated_message(s2, session_key, DIR_SERVER_TO_CLIENT, expected_seq=2)
            self.assertIsNone(recv_replay)
        finally:
            s1.close()
            s2.close()

        # Kiểm tra Reflection attack trên cặp socket mới
        s3, s4 = socket.socketpair()
        try:
            msg = ProtocolMessage(msg_type=MessageType.COMMAND.value, payload={"action": "PING"})
            self.assertTrue(send_authenticated_message(s3, msg, session_key, DIR_CLIENT_TO_SERVER, seq=1))
            recv_reflect = recv_authenticated_message(s4, session_key, DIR_SERVER_TO_CLIENT, expected_seq=1)
            self.assertIsNone(recv_reflect)
        finally:
            s3.close()
            s4.close()

    def test_05_command_deduplication_on_client(self):
        """Lệnh trùng command_id chỉ được thực thi đúng 1 lần trên Agent."""
        port = self._get_free_port()
        server = NetworkServer(host=self.HOST, port=port, psk="DEDUP-PSK")
        self.assertTrue(server.start())

        executed_ids = []
        reg = DeviceRegistrationData(
            device_uuid="dedup-agent-uuid",
            hostname="DEDUP-PC",
            os_name="Windows",
            os_version="11",
        )
        client = NetworkClient(
            server_host=self.HOST,
            server_port=port,
            reg_data=reg,
            psk="DEDUP-PSK",
        )

        def on_cmd(cmd: CommandPacket):
            executed_ids.append(cmd.command_id)
            return CommandResultPacket(
                command_id=cmd.command_id,
                device_uuid=reg.device_uuid,
                status="SUCCESS",
                message="Done",
            )

        client.on_command_received = on_cmd
        try:
            client.start()
            deadline = time.time() + 3.0
            while not client.is_connected() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(client.is_connected())

            cmd = CommandPacket(
                command_id="CMD-DUP-999",
                target_device_uuid="dedup-agent-uuid",
                action=CommandAction.PING.value,
            )
            server.send_command("dedup-agent-uuid", cmd)
            server.send_command("dedup-agent-uuid", cmd)  # Gửi trùng command_id
            time.sleep(0.3)

            self.assertEqual(executed_ids, ["CMD-DUP-999"])
        finally:
            client.stop()
            server.stop()


if __name__ == "__main__":
    unittest.main()
