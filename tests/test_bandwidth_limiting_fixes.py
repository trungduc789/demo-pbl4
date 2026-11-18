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
        with patch("infrastructure.packet_shaper.remove_all_pbl4_firewall_rules"), \
             patch("infrastructure.packet_shaper.remove_windows_firewall_rule"):
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

    def test_05_worker_token_bucket_high_rate_does_not_choke_connection(self):
        """
        Kiểm tra worker Token Bucket ở tốc độ cao (10,240 KB/s = 10 MB/s) không bị kẹt bởi
        độ phân giải timer 15.6ms của Windows và gỡ sạch rule Firewall cũ khi apply_limit.
        """
        with patch("infrastructure.packet_shaper.remove_all_pbl4_firewall_rules"), \
             patch("infrastructure.packet_shaper.remove_windows_firewall_rule"):
            shaper = WinDivertPacketShaper()
            try:
                mock_handle = MagicMock()
                mock_handle.is_open = True
                shaper._divert_handle = mock_handle

                with patch.object(shaper, "is_admin", return_value=True), \
                     patch.object(shaper, "_ensure_engine_running"), \
                     patch.object(shaper, "_remove_windows_firewall_rule", return_value=True) as mock_rm_fw:
                    ok, _ = shaper.apply_limit("msedge.exe", 10240.0, "both")
                    self.assertTrue(ok)
                    mock_rm_fw.assert_called_with("msedge.exe")

                    policy = shaper.throttled_apps["msedge.exe"]
                    # Bơm 50 gói tin 1500 bytes (75 KB, nhỏ hơn burst_capacity 2.5 MB của mức 10 MB/s)
                    for i in range(50):
                        policy.queue.put_nowait((f"pkt_{i}", 1500, False))

                    # Đợi tối đa 0.3s -> Toàn bộ 50 gói phải được gửi qua handle mà không bị nghẽn
                    import time
                    deadline = time.time() + 0.3
                    while not policy.queue.empty() and time.time() < deadline:
                        time.sleep(0.01)

                    self.assertTrue(policy.queue.empty())
                    self.assertEqual(mock_handle.send.call_count, 50)
            finally:
                shaper.stop()

    def test_06_apply_limit_after_unblock_does_not_kill_new_policy_worker(self):
        """
        Kiểm tra hồi quy: Khi một ứng dụng vừa được unblock_app (khiến is_active=False)
        và ngay lập tức được gọi apply_limit(), hàm _ensure_engine_running() KHÔNG ĐƯỢC
        set policy.stop_event của policy mới vừa tạo!
        """
        with patch("infrastructure.packet_shaper.remove_all_pbl4_firewall_rules"), \
             patch("infrastructure.packet_shaper.remove_windows_firewall_rule"), \
             patch("infrastructure.packet_shaper.apply_windows_firewall_rule", return_value=True), \
             patch("infrastructure.packet_shaper.terminate_app_sockets"):
            shaper = WinDivertPacketShaper()
            try:
                mock_handle = MagicMock()
                mock_handle.is_open = True

                def fake_capture_loop(stop_ev):
                    shaper._divert_handle = mock_handle
                    stop_ev.wait(0.5)

                with patch.object(shaper, "is_admin", return_value=True), \
                     patch.object(shaper, "_capture_loop", side_effect=fake_capture_loop):
                    ok_b, _ = shaper.block_app("msedge.exe")
                    self.assertTrue(ok_b)
                    ok_u, _ = shaper.unblock_app("msedge.exe")
                    self.assertTrue(ok_u)
                    self.assertFalse(shaper.is_active)

                    ok_t, _ = shaper.apply_limit("msedge.exe", 1024.0, "both")
                    self.assertTrue(ok_t)

                    policy = shaper.throttled_apps["msedge.exe"]
                    self.assertFalse(policy.stop_event.is_set(), "policy.stop_event bị set nhầm bởi _stop_engine_threads!")
                    self.assertTrue(policy.worker_thread.is_alive(), "Worker thread của msedge.exe phải đang sống!")

                    for i in range(20):
                        policy.queue.put_nowait((f"pkt_{i}", 1400, True))

                    import time
                    deadline = time.time() + 0.4
                    while not policy.queue.empty() and time.time() < deadline:
                        time.sleep(0.01)

                    self.assertTrue(policy.queue.empty(), "Worker phải tiêu thụ hết gói tin trong queue!")
                    self.assertEqual(mock_handle.send.call_count, 20)
            finally:
                shaper.stop()


if __name__ == "__main__":
    unittest.main()
