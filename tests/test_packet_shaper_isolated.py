import unittest
from unittest.mock import MagicMock, patch
import time
import queue

from infrastructure.packet_shaper import (
    WinDivertPacketShaper,
    AppThrottlePolicy,
    AppBlockPolicy,
    PORT_TTL_SECONDS
)


class TestIsolatedPacketShaper(unittest.TestCase):
    """Kiểm thử tính năng đa chính sách độc lập và logic bảo vệ của WinDivertPacketShaper."""

    def setUp(self):
        with patch("infrastructure.packet_shaper.remove_all_pbl4_firewall_rules"):
            self.shaper = WinDivertPacketShaper()

    def tearDown(self):
        with patch("infrastructure.packet_shaper.remove_all_pbl4_firewall_rules"), \
             patch("infrastructure.packet_shaper.remove_windows_firewall_rule"):
            self.shaper.stop()

    def test_01_multi_app_throttle_policies_stored_independently(self):
        """Kiểm tra nhiều ứng dụng có chính sách bóp băng thông độc lập, không bị ghi đè."""
        with patch.object(self.shaper, 'is_admin', return_value=True), \
             patch.object(self.shaper, '_ensure_engine_running'):

            ok1, _ = self.shaper.apply_limit("chrome", 500.0, "download")
            ok2, _ = self.shaper.apply_limit("discord", 200.0, "both")
            ok3, _ = self.shaper.apply_limit("zalo.exe", 100.0, "upload")

            self.assertTrue(ok1)
            self.assertTrue(ok2)
            self.assertTrue(ok3)

            # Phải có 3 chính sách riêng biệt
            self.assertEqual(len(self.shaper.throttled_apps), 3)

            status = self.shaper.get_status()
            throttled = status["throttled_apps"]
            self.assertIn("chrome.exe", throttled)
            self.assertIn("discord.exe", throttled)
            self.assertIn("zalo.exe", throttled)

            self.assertEqual(throttled["chrome.exe"]["limit_kbps"], 500.0)
            self.assertEqual(throttled["chrome.exe"]["direction"], "download")

            self.assertEqual(throttled["discord.exe"]["limit_kbps"], 200.0)
            self.assertEqual(throttled["discord.exe"]["direction"], "both")

            self.assertEqual(throttled["zalo.exe"]["limit_kbps"], 100.0)
            self.assertEqual(throttled["zalo.exe"]["direction"], "upload")

    def test_02_remove_single_app_limit_does_not_affect_others(self):
        """Gỡ bỏ limit của Discord thì Chrome và Zalo vẫn còn nguyên."""
        with patch.object(self.shaper, 'is_admin', return_value=True), \
             patch.object(self.shaper, '_ensure_engine_running'), \
             patch.object(self.shaper, '_update_target_ports'):

            self.shaper.apply_limit("chrome", 500.0)
            self.shaper.apply_limit("discord", 200.0)
            self.shaper.apply_limit("zalo", 100.0)

            # Gỡ discord
            ok, msg = self.shaper.remove_limit("discord")
            self.assertTrue(ok)
            self.assertNotIn("discord.exe", self.shaper.throttled_apps)
            self.assertIn("chrome.exe", self.shaper.throttled_apps)
            self.assertIn("zalo.exe", self.shaper.throttled_apps)

            # Gỡ tiếp tất cả
            self.shaper.remove_limit()
            self.assertEqual(len(self.shaper.throttled_apps), 0)

    def test_03_multi_app_block_and_unblock_isolated(self):
        """Chặn nhiều ứng dụng và gỡ chặn từng ứng dụng độc lập."""
        with patch.object(self.shaper, 'is_admin', return_value=True), \
             patch.object(self.shaper, '_ensure_engine_running'), \
             patch.object(self.shaper, '_apply_windows_firewall_rule', return_value=True), \
             patch.object(self.shaper, '_remove_windows_firewall_rule', return_value=True), \
             patch.object(self.shaper, '_terminate_app_sockets'):

            ok1, _ = self.shaper.block_app("chrome", "both")
            ok2, _ = self.shaper.block_app("firefox.exe", "both")
            self.assertTrue(ok1)
            self.assertTrue(ok2)

            self.assertEqual(len(self.shaper.blocked_apps), 2)
            self.assertTrue(self.shaper.is_blocked)

            # Gỡ chrome
            self.shaper.unblock_app("chrome")
            self.assertEqual(len(self.shaper.blocked_apps), 1)
            self.assertIn("firefox.exe", self.shaper.blocked_apps)
            self.assertTrue(self.shaper.is_blocked)

            # Gỡ firefox
            self.shaper.unblock_app("firefox")
            self.assertEqual(len(self.shaper.blocked_apps), 0)
            self.assertFalse(self.shaper.is_blocked)

    def test_04_worker_token_bucket_queue_isolation(self):
        """Kiểm tra mỗi policy có hàng đợi và worker thread riêng biệt."""
        policy1 = AppThrottlePolicy(
            app_key="chrome.exe",
            target_app="chrome.exe",
            limit_kbps=500.0
        )
        policy2 = AppThrottlePolicy(
            app_key="discord.exe",
            target_app="discord.exe",
            limit_kbps=200.0
        )

        # Hàng đợi riêng biệt
        self.assertIsNot(policy1.queue, policy2.queue)
        policy1.queue.put(("pkt1", 100, True))
        self.assertEqual(policy1.queue.qsize(), 1)
        self.assertEqual(policy2.queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
