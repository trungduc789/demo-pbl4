"""
Kiểm thử đơn vị cho các hàm tính toán thuần túy (Pure Functions) trong `application/traffic_analytics.py`:
- Không phụ thuộc Qt / GUI.
- Kiểm tra công thức quy đổi đơn vị (format_bytes, format_speed, format_ceiling_label).
- Kiểm tra thuật toán làm mượt sóng (Gaussian + PCHIP Hermite) và chống rung thang đo (Hysteresis).
- Kiểm tra tổng hợp lưu lượng cửa sổ thời gian (compute_window_traffic) & tìm kiếm nhị phân (find_nearest_sample).
- Kiểm tra phân loại WAN/LAN, khu vực mạng và giao thức theo cổng.
"""

import unittest
import numpy as np

from application.traffic_analytics import (
    format_bytes,
    format_speed,
    format_ceiling_label,
    select_y_ceiling_tier,
    generate_glasswire_wave,
    compute_window_traffic,
    find_nearest_sample,
    is_private_or_loopback_ip,
    classify_protocol_by_port,
    classify_network_region,
    aggregate_connections_usage,
    get_scope_factor,
)


class _DummyConn:
    def __init__(self, remote_ip: str, remote_port: int):
        self.remote_ip = remote_ip
        self.remote_port = remote_port


class TestTrafficAnalyticsPureFunctions(unittest.TestCase):
    """Kiểm thử toàn diện các công thức và hàm thuần túy."""

    def test_format_bytes_and_speed_and_ceiling(self):
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(2048), "2.0 KB")
        self.assertEqual(format_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(format_bytes(2 * 1024 * 1024 * 1024), "2.00 GB")

        self.assertEqual(format_speed(0.0), "0 B/s")
        self.assertEqual(format_speed(0.5), "512 B/s")
        self.assertEqual(format_speed(120.5), "120.5 KB/s")
        self.assertEqual(format_speed(2048.0), "2.0 MB/s")

        self.assertEqual(format_ceiling_label(500.0), "500 KB ▾")
        self.assertEqual(format_ceiling_label(1024.0), "1 MB ▾")
        self.assertEqual(format_ceiling_label(2560.0), "2.5 MB ▾")

    def test_select_y_ceiling_hysteresis(self):
        # Tăng vọt lên 600 KB/s -> vượt 500 KB, phải chọn trần 1024 KB ngay lập tức
        new_max, hold_until, changed = select_y_ceiling_tier(
            peak_val_kb=600.0, current_y_max=500.0, now_ts=100.0, hold_until_ts=0.0, hold_duration_sec=8.0
        )
        self.assertTrue(changed)
        self.assertEqual(new_max, 1024.0)
        self.assertEqual(hold_until, 108.0)

        # Lưu lượng giảm về 10 KB/s nhưng chưa hết 8s hold -> giữ nguyên 1024 KB
        new_max2, hold_until2, changed2 = select_y_ceiling_tier(
            peak_val_kb=10.0, current_y_max=new_max, now_ts=104.0, hold_until_ts=hold_until
        )
        self.assertFalse(changed2)
        self.assertEqual(new_max2, 1024.0)

        # Sau mốc 108s -> cho phép hạ trần xuống 50 KB
        new_max3, _, changed3 = select_y_ceiling_tier(
            peak_val_kb=10.0, current_y_max=new_max2, now_ts=109.0, hold_until_ts=hold_until2
        )
        self.assertTrue(changed3)
        self.assertEqual(new_max3, 50.0)

    def test_generate_glasswire_wave_non_negative_and_smooth(self):
        x_raw = np.linspace(100.0, 160.0, 61)
        y_raw = np.zeros_like(x_raw)
        y_raw[25:35] = 400.0
        x_dense = np.linspace(100.0, 160.0, 300)

        wave = generate_glasswire_wave(x_raw, y_raw, x_dense, window_duration=60.0)
        self.assertEqual(len(wave), 300)
        self.assertTrue(np.all(wave >= 0.0))
        self.assertGreater(float(np.max(wave)), 200.0)
        # Ngoài vùng mẫu phải về 0
        x_out = np.linspace(200.0, 250.0, 50)
        wave_out = generate_glasswire_wave(x_raw, y_raw, x_out, window_duration=60.0)
        self.assertTrue(np.allclose(wave_out, 0.0))

    def test_compute_window_traffic_and_find_nearest_sample(self):
        history = [
            {"t": 10.0, "down_b": 1000.0, "up_b": 500.0, "procs": {"chrome.exe": (1000.0, 500.0)}},
            {"t": 50.0, "down_b": 2000.0, "up_b": 800.0, "procs": {"chrome.exe": (1500.0, 500.0), "zalo.exe": (500.0, 300.0)}},
            {"t": 90.0, "down_b": 3000.0, "up_b": 1200.0, "procs": {"chrome.exe": (3000.0, 1200.0)}},
        ]
        # Cửa sổ 60s tính từ mốc t=100 -> chỉ lấy mẫu t=50 và t=90
        tot_down, tot_up, apps = compute_window_traffic(history, win_duration=60.0, anchor_ts=100.0)
        self.assertEqual(tot_down, 5000.0)
        self.assertEqual(tot_up, 2000.0)
        self.assertEqual(apps["chrome.exe"]["down"], 4500.0)
        self.assertEqual(apps["zalo.exe"]["up"], 300.0)

        pts = [
            {"timestamp": 10.0, "down_kb": 12.0, "up_kb": 4.0, "top_procs": []},
            {"timestamp": 20.0, "down_kb": 80.0, "up_kb": 15.0, "top_procs": []},
            {"timestamp": 30.0, "down_kb": 25.0, "up_kb": 5.0, "top_procs": []},
        ]
        nearest = find_nearest_sample(pts, [], [], target_t=21.2)
        self.assertIsNotNone(nearest)
        self.assertEqual(nearest["timestamp"], 20.0)

    def test_wan_lan_and_protocol_classification(self):
        self.assertTrue(is_private_or_loopback_ip("127.0.0.1"))
        self.assertTrue(is_private_or_loopback_ip("192.168.1.50"))
        self.assertTrue(is_private_or_loopback_ip("10.0.0.5"))
        self.assertFalse(is_private_or_loopback_ip("8.8.8.8"))

        self.assertEqual(classify_protocol_by_port(443), "HTTPS (TCP/443)")
        self.assertEqual(classify_protocol_by_port(53), "DNS (UDP/TCP 53)")
        self.assertEqual(classify_protocol_by_port(9988), "PBL4 Control Channel (9988)")

        self.assertIn("LAN", classify_network_region("192.168.1.1"))
        self.assertIn("Google", classify_network_region("8.8.8.8"))
        self.assertIn("WAN", classify_network_region("93.184.216.34"))

        conns = [
            _DummyConn("8.8.8.8", 53),
            _DummyConn("192.168.1.10", 9988),
            _DummyConn("142.250.199.14", 443),
        ]
        hosts, protos, regions, total = aggregate_connections_usage(conns)
        self.assertEqual(total, 3)
        self.assertEqual(hosts["8.8.8.8:53"], 1)
        self.assertEqual(protos["HTTPS (TCP/443)"], 1)
        self.assertEqual(regions["Google Cloud / DNS"], 2)
        self.assertEqual(get_scope_factor("wan"), 0.98)
        self.assertEqual(get_scope_factor("lan"), 0.05)


if __name__ == "__main__":
    unittest.main()
