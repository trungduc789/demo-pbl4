import unittest
from unittest.mock import MagicMock, patch

from application.client_agent_service import ClientAgentService
from application.fleet_manager_service import FleetManagerService
from infrastructure.packet_shaper import WinDivertPacketShaper
from models.network_protocol import CommandAction, CommandPacket, CommandResultPacket


class TestBandwidthLimitingFixes(unittest.TestCase):
    """Kiểm thử hồi quy cho các lỗi giới hạn băng thông (Agent tự giới hạn & Admin giới hạn Agent)."""

    def test_01_local_switch_between_block_and_throttle_cleans_previous_shaper_state(self):
        mock_shaper = MagicMock()
        mock_shaper.block_app.return_value = (True, "Blocked")
        mock_shaper.unblock_app.return_value = (True, "Unblocked")
        mock_shaper.apply_limit.return_value = (True, "Limited")
        mock_shaper.remove_limit.return_value = (True, "Removed")

        mock_monitor = MagicMock()
        mock_monitor.packet_shaper = mock_shaper
        mock_monitor.collector = MagicMock()
        mock_monitor.calculator = MagicMock()
        mock_monitor.etw_consumer = MagicMock()

        agent = ClientAgentService(
            server_host="127.0.0.1",
            server_port=19991,
            enable_etw=False,
            monitor_service=mock_monitor,
        )

        # 1. Tự chặn -> chuyển sang tự giới hạn băng thông -> phải gọi unblock_app trước apply_limit
        ok1, _ = agent.apply_local_policy("chrome.exe", action="BLOCK")
        self.assertTrue(ok1)
        ok2, _ = agent.apply_local_policy("chrome.exe", action="THROTTLE", limit_kbps=250.0)
        self.assertTrue(ok2)
        mock_shaper.unblock_app.assert_called_with("chrome.exe")
        mock_shaper.apply_limit.assert_called_with("chrome.exe", limit_kbps=250.0, direction="both")

        # 2. Từ giới hạn băng thông -> chuyển sang chặn -> phải gọi remove_limit trước block_app
        ok3, _ = agent.apply_local_policy("chrome.exe", action="BLOCK")
        self.assertTrue(ok3)
        mock_shaper.remove_limit.assert_called_with("chrome.exe")

    def test_02_remote_unblock_all_preserves_throttle_policies_on_agent(self):
        mock_shaper = MagicMock()
        mock_shaper.block_app.return_value = (True, "Blocked")
        mock_shaper.unblock_all.return_value = (True, "Unblocked all")
        mock_shaper.apply_limit.return_value = (True, "Limited")

        mock_monitor = MagicMock()
        mock_monitor.packet_shaper = mock_shaper
        mock_monitor.collector = MagicMock()
        mock_monitor.calculator = MagicMock()
        mock_monitor.etw_consumer = MagicMock()

        agent = ClientAgentService(
            server_host="127.0.0.1",
            server_port=19992,
            enable_etw=False,
            monitor_service=mock_monitor,
        )

        agent.handle_remote_command(
            CommandPacket(
                command_id="CMD-1",
                target_device_uuid=agent.device_uuid,
                action=CommandAction.THROTTLE_SET.value,
                target_app="discord.exe",
                limit_kbps=120.0,
                direction="both",
            )
        )
        agent.handle_remote_command(
            CommandPacket(
                command_id="CMD-2",
                target_device_uuid=agent.device_uuid,
                action=CommandAction.BLOCK_APP.value,
                target_app="chrome.exe",
                direction="both",
            )
        )
        agent.handle_remote_command(
            CommandPacket(
                command_id="CMD-3",
                target_device_uuid=agent.device_uuid,
                action=CommandAction.UNBLOCK_ALL.value,
                target_app="",
                direction="both",
            )
        )

        self.assertIsNone(agent.get_policy_for_app("chrome.exe"))
        discord_pol = agent.get_policy_for_app("discord.exe")
        self.assertIsNotNone(discord_pol)
        self.assertEqual(discord_pol["action"], "THROTTLE")
        self.assertEqual(discord_pol["limit_kbps"], 120.0)

    def test_03_fleet_manager_marks_failed_throttle_and_ui_ignores_it(self):
        fleet = FleetManagerService(host="127.0.0.1", port=19993)
        fleet.server.send_command = MagicMock(return_value=True)

        from models.network_protocol import DeviceRegistrationData
        fleet._on_server_device_registered(
            DeviceRegistrationData(
                device_uuid="dev-fail-test",
                hostname="TEST-PC",
                os_name="Windows",
                os_version="11",
                agent_version="1.0.0",
                mac_address="00:11:22:33:44:55",
                ip_address="192.168.1.50",
            )
        )

        ok, _ = fleet.send_remote_command(
            device_uuid="dev-fail-test",
            action=CommandAction.THROTTLE_SET.value,
            target_app="chrome.exe",
            limit_kbps=300.0,
            direction="both",
        )
        self.assertTrue(ok)
        dev_state = fleet.get_device_state("dev-fail-test")
        self.assertIn("chrome.exe", dev_state.applied_policies)

        cmd_id = next(iter(fleet._pending_commands.keys()))
        fleet._on_server_command_result(
            CommandResultPacket(
                command_id=cmd_id,
                device_uuid="dev-fail-test",
                status="FAILED",
                message="Thiếu quyền Administrator",
            )
        )
        self.assertEqual(dev_state.applied_policies["chrome.exe"]["status"], "FAILED")
        active_applied = [p for p in dev_state.applied_policies.values() if p.get("status") != "FAILED"]
        self.assertEqual(active_applied, [])

    def test_04_packet_shaper_apply_limit_drains_existing_queue(self):
        shaper = WinDivertPacketShaper()
        try:
            with patch.object(shaper, "is_admin", return_value=True), \
                 patch.object(shaper, "_ensure_engine_running"), \
                 patch.object(shaper, "_app_throttle_worker_loop"):
                ok1, _ = shaper.apply_limit("chrome.exe", 500.0, "both")
                self.assertTrue(ok1)
                shaper.throttled_apps["chrome.exe"].queue.put(("pkt1", 100, True))
                ok2, _ = shaper.apply_limit("chrome.exe", 200.0, "both")
                self.assertTrue(ok2)
                self.assertEqual(shaper.throttled_apps["chrome.exe"].queue.qsize(), 1)
                self.assertEqual(shaper.throttled_apps["chrome.exe"].limit_kbps, 200.0)
        finally:
            shaper.stop()


if __name__ == "__main__":
    unittest.main()
