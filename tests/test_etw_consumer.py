import ctypes
import os
import struct
import tempfile
import threading
import time
import unittest

from infrastructure.etw_consumer import (
    ETWNetworkConsumer,
    EVENT_RECORD,
    GUID_KERNEL_NETWORK,
    KERNEL_NETWORK_RECV_IDS,
    KERNEL_NETWORK_SEND_IDS,
    str_to_guid,
)


class TestETWNetworkConsumer(unittest.TestCase):
    def _make_record(self, provider_guid: str, event_id: int, payload: bytes, header_pid: int = 0xFFFFFFFF):
        rec = EVENT_RECORD()
        rec.EventHeader.ProviderId = str_to_guid(provider_guid)
        rec.EventHeader.ProcessId = header_pid
        rec.EventHeader.EventDescriptor.Id = event_id
        rec.EventHeader.EventDescriptor.Version = 0
        c_buf = ctypes.create_string_buffer(payload)
        rec.UserDataLength = len(payload)
        rec.UserData = ctypes.addressof(c_buf)
        return rec, c_buf

    def test_tdh_resolves_all_tcp_and_udp_v4_v6_events(self):
        """Xác minh TDH giải mã và cộng dồn đủ 8 loại sự kiện TCPv4/TCPv6/UDPv4/UDPv6 Send & Recv."""
        consumer = ETWNetworkConsumer()
        keep_alive = []

        for idx, eid in enumerate(sorted(KERNEL_NETWORK_SEND_IDS)):
            pid = 1000 + idx
            size = 1000 + idx * 100
            rec, c_buf = self._make_record(GUID_KERNEL_NETWORK, eid, struct.pack("<II", pid, size))
            keep_alive.append(c_buf)
            consumer._event_callback(ctypes.pointer(rec))

        for idx, eid in enumerate(sorted(KERNEL_NETWORK_RECV_IDS)):
            pid = 2000 + idx
            size = 2000 + idx * 100
            rec, c_buf = self._make_record(GUID_KERNEL_NETWORK, eid, struct.pack("<II", pid, size))
            keep_alive.append(c_buf)
            consumer._event_callback(ctypes.pointer(rec))

        snap = consumer.flush_and_aggregate()
        self.assertEqual(snap[1000], {"rx_bytes": 0, "tx_bytes": 1000})
        self.assertEqual(snap[1001], {"rx_bytes": 0, "tx_bytes": 1100})
        self.assertEqual(snap[1002], {"rx_bytes": 0, "tx_bytes": 1200})
        self.assertEqual(snap[1003], {"rx_bytes": 0, "tx_bytes": 1300})
        self.assertEqual(snap[2000], {"rx_bytes": 2000, "tx_bytes": 0})
        self.assertEqual(snap[2001], {"rx_bytes": 2100, "tx_bytes": 0})
        self.assertEqual(snap[2002], {"rx_bytes": 2200, "tx_bytes": 0})
        self.assertEqual(snap[2003], {"rx_bytes": 2300, "tx_bytes": 0})

    def test_provider_scoping_ignores_other_providers(self):
        """Sự kiện có Event ID trùng (ví dụ 10) nhưng từ Provider khác phải bị bỏ qua."""
        consumer = ETWNetworkConsumer()
        other_guid = "{2F07E2E4-900D-47C5-85D0-E4530C158691}"
        rec, _buf = self._make_record(other_guid, 10, struct.pack("<II", 9999, 512))
        consumer._event_callback(ctypes.pointer(rec))
        self.assertEqual(consumer.flush_and_aggregate(), {})

    def test_large_size_accepted_and_truncated_payload_never_uses_userdata_length(self):
        """Chấp nhận gói > 64KB/1MB và không bao giờ dùng UserDataLength khi thiếu trường size."""
        consumer = ETWNetworkConsumer()
        rec_large, _buf1 = self._make_record(GUID_KERNEL_NETWORK, 10, struct.pack("<II", 5555, 2_000_000))
        consumer._event_callback(ctypes.pointer(rec_large))

        rec_trunc, _buf2 = self._make_record(GUID_KERNEL_NETWORK, 10, struct.pack("<I", 7777))
        consumer._event_callback(ctypes.pointer(rec_trunc))

        snap = consumer.flush_and_aggregate()
        self.assertEqual(snap, {5555: {"rx_bytes": 0, "tx_bytes": 2_000_000}})

    def test_tdh_get_property_dynamic_path(self):
        """Khi layout cố định là None, TdhGetProperty vẫn đọc đúng PID và size."""
        consumer = ETWNetworkConsumer()
        consumer._tdh_schema_cache[(11, 0)] = None
        rec, _buf = self._make_record(GUID_KERNEL_NETWORK, 11, struct.pack("<II", 6666, 131072))
        consumer._event_callback(ctypes.pointer(rec))
        self.assertEqual(consumer.flush_and_aggregate(), {6666: {"rx_bytes": 131072, "tx_bytes": 0}})

    def test_non_admin_fallback_reports_is_etw_active_false_and_no_fake_io(self):
        """Khi không có quyền Admin, is_etw_active() là False và không bịa số liệu mạng từ Disk I/O."""
        if ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("Đang chạy với quyền Administrator")
        consumer = ETWNetworkConsumer()
        consumer.start()
        try:
            time.sleep(0.3)
            self.assertTrue(consumer.is_alive())
            self.assertFalse(consumer.is_etw_active())
            self.assertTrue(consumer._use_fallback)
            self.assertEqual(consumer.flush_and_aggregate(), {})
        finally:
            consumer.stop()


    def test_flush_and_aggregate_reconciliation_exact_sum_and_reset(self):
        """Đối chiếu tổng rx/tx qua flush_and_aggregate() khớp chính xác 100% và reset sau mỗi chu kỳ."""
        consumer = ETWNetworkConsumer()
        events = [
            (301, 10, 1200),   # TCPv4 Send
            (301, 42, 800),    # UDPv4 Send
            (301, 11, 4096),   # TCPv4 Recv
            (302, 26, 70000),  # TCPv6 Send (>64KB)
            (302, 59, 512),    # UDPv6 Recv
        ]
        keep_alive = []
        for pid, eid, size in events:
            rec, c_buf = self._make_record(GUID_KERNEL_NETWORK, eid, struct.pack("<II", pid, size))
            keep_alive.append(c_buf)
            consumer._event_callback(ctypes.pointer(rec))

        snap = consumer.flush_and_aggregate()
        self.assertEqual(snap, {
            301: {"rx_bytes": 4096, "tx_bytes": 2000},
            302: {"rx_bytes": 512, "tx_bytes": 70000},
        })
        total_rx = sum(v["rx_bytes"] for v in snap.values())
        total_tx = sum(v["tx_bytes"] for v in snap.values())
        self.assertEqual(total_rx, 4096 + 512)
        self.assertEqual(total_tx, 2000 + 70000)

        # Chu kỳ tiếp theo không có sự kiện phải trả về rỗng
        self.assertEqual(consumer.flush_and_aggregate(), {})

    def test_disk_io_does_not_leak_into_fallback_flush(self):
        """Ghi file lớn xuống đĩa ở chế độ fallback không được bị tính nhầm thành upload mạng."""
        if ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("Đang chạy với quyền Administrator")
        consumer = ETWNetworkConsumer()
        consumer.start()
        try:
            time.sleep(0.2)
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(b"X" * (2 * 1024 * 1024))
                tmp.flush()
            try:
                time.sleep(0.2)
                self.assertEqual(consumer.flush_and_aggregate(), {})
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        finally:
            consumer.stop()

    def test_concurrent_callback_and_flush_thread_safety(self):
        """Đối chiếu tính toàn vẹn khi _event_callback và flush_and_aggregate chạy song song đa luồng."""
        consumer = ETWNetworkConsumer()
        per_thread_events = 200
        num_threads = 4
        payload_size = 512
        total_rx = 0
        total_tx = 0
        lock = threading.Lock()

        def worker(pid_base: int):
            rec_tx, _b1 = self._make_record(GUID_KERNEL_NETWORK, 10, struct.pack("<II", pid_base, payload_size))
            rec_rx, _b2 = self._make_record(GUID_KERNEL_NETWORK, 11, struct.pack("<II", pid_base, payload_size))
            for _ in range(per_thread_events):
                consumer._event_callback(ctypes.pointer(rec_tx))
                consumer._event_callback(ctypes.pointer(rec_rx))

        threads = [threading.Thread(target=worker, args=(4000 + i,)) for i in range(num_threads)]
        for t in threads:
            t.start()

        while any(t.is_alive() for t in threads):
            snap = consumer.flush_and_aggregate()
            with lock:
                total_rx += sum(v["rx_bytes"] for v in snap.values())
                total_tx += sum(v["tx_bytes"] for v in snap.values())
            time.sleep(0.002)

        for t in threads:
            t.join()

        snap = consumer.flush_and_aggregate()
        total_rx += sum(v["rx_bytes"] for v in snap.values())
        total_tx += sum(v["tx_bytes"] for v in snap.values())

        expected = num_threads * per_thread_events * payload_size
        self.assertEqual(total_rx, expected)
        self.assertEqual(total_tx, expected)


if __name__ == "__main__":
    unittest.main()
