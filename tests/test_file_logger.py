"""
Kiểm thử cơ chế xoay vòng file nhật ký (Log Rotation) trong utils/file_logger.py.
"""

import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import utils.file_logger as file_logger_mod
from utils.file_logger import FileLogger


class TestFileLoggerRotation(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_logs_dir = file_logger_mod.LOGS_DIR
        self._orig_max_bytes = FileLogger.MAX_LOG_BYTES
        self._orig_backup_count = FileLogger.BACKUP_COUNT
        file_logger_mod.LOGS_DIR = self._tmp_dir.name
        FileLogger._seen_connections.clear()

    def tearDown(self):
        file_logger_mod.LOGS_DIR = self._orig_logs_dir
        FileLogger.MAX_LOG_BYTES = self._orig_max_bytes
        FileLogger.BACKUP_COUNT = self._orig_backup_count
        FileLogger._seen_connections.clear()
        self._tmp_dir.cleanup()

    def test_connection_log_rotates_and_respects_backup_count(self):
        FileLogger.MAX_LOG_BYTES = 200
        FileLogger.BACKUP_COUNT = 2

        for i in range(15):
            FileLogger.log_connection(
                proc_name=f"app_{i}.exe",
                pid=1000 + i,
                protocol="TCP",
                local_ip="127.0.0.1",
                local_port=50000 + i,
                remote_ip="8.8.8.8",
                remote_port=443,
                state="ESTABLISHED",
            )

        base = os.path.join(self._tmp_dir.name, "connections.log")
        self.assertTrue(os.path.exists(base))
        self.assertTrue(os.path.exists(f"{base}.1"))
        self.assertTrue(os.path.exists(f"{base}.2"))
        self.assertFalse(os.path.exists(f"{base}.3"))
        self.assertLessEqual(os.path.getsize(base), FileLogger.MAX_LOG_BYTES)

    def test_read_policy_logs_spans_rotated_backups_newest_first(self):
        FileLogger.MAX_LOG_BYTES = 450
        FileLogger.BACKUP_COUNT = 2

        for i in range(4):
            FileLogger.log_policy(
                action="BLOCK",
                policy_type="Firewall",
                target_app=f"proc_{i}.exe",
                status="SUCCESS",
            )

        base = os.path.join(self._tmp_dir.name, "policies.log")
        self.assertTrue(os.path.exists(f"{base}.1"))

        entries = FileLogger.read_policy_logs(limit=4)
        self.assertEqual(len(entries), 4)
        self.assertIn("proc_3.exe", entries[0]["raw"])
        self.assertIn("proc_0.exe", entries[-1]["raw"])


if __name__ == "__main__":
    unittest.main()
