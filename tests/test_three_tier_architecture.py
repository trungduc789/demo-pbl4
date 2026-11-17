"""
Kiểm thử tự động xác minh tính tuân thủ Kiến trúc 3 Lớp (3-Tier Architecture):
1. Presentation Layer (`presentation/`):
   - Không được phép import trực tiếp `infrastructure`, `psutil`, `socket`, `winreg`, hoặc `FileLogger`.
2. Application Layer (`application/`):
   - Không được phép import ngược lên `presentation` hoặc `PyQt6`.
   - Không được phép gọi trực tiếp `psutil`, `socket`, `winreg` (phải thông qua `infrastructure`).
3. Infrastructure Layer (`infrastructure/`):
   - Không được phép import ngược lên `application`, `presentation`, hoặc `PyQt6`.
4. Kiểm thử hoạt động của các View đã tách (`LogAnalysisView`, `NetworkScannerView`) và các hàm ủy quyền.
"""

import os
import sys
import ast
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from PyQt6.QtWidgets import QApplication
from presentation.log_analysis_view import LogAnalysisView
from presentation.network_scanner_view import NetworkScannerView
from infrastructure.network_collector import NetworkCollector
from application.data_source import (
    create_data_source,
    LocalMonitorDataSource,
    ClientAgentDataSource,
    RemoteFleetDataSource,
)
from utils.file_logger import FileLogger
from utils.arch_linter import check_architecture_violations


def _collect_imports_in_directory(rel_dir: str):
    """Phân tích AST tất cả file .py trong thư mục để lấy danh sách module được import."""
    abs_dir = os.path.join(PROJECT_ROOT, rel_dir)
    results = {}
    for root, _dirs, files in os.walk(abs_dir):
        if "__pycache__" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, PROJECT_ROOT)
            with open(fpath, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel_path)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    imported.add(mod)
                    for alias in node.names:
                        imported.add(f"{mod}.{alias.name}" if mod else alias.name)
            results[rel_path] = imported
    return results


class TestThreeTierArchitecture(unittest.TestCase):
    """Kiểm tra ranh giới kiến trúc 3 lớp và các thành phần đã chuẩn hóa."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance()
        if cls.app is None:
            cls.app = QApplication(sys.argv)

    def test_01_presentation_layer_no_bypass_imports(self):
        """Tầng Presentation không được vượt rào import infrastructure, psutil, socket, winreg, FileLogger."""
        pres_imports = _collect_imports_in_directory("presentation")
        forbidden_prefixes = ("infrastructure", "psutil", "socket", "winreg", "utils.file_logger")
        for rel_file, modules in pres_imports.items():
            for mod in modules:
                for bad in forbidden_prefixes:
                    self.assertFalse(
                        mod == bad or mod.startswith(f"{bad}."),
                        f"Vi phạm kiến trúc 3 lớp tại {rel_file}: Không được phép import '{mod}' ở tầng Presentation!",
                    )

    def test_02_application_layer_no_ui_or_raw_os_imports(self):
        """Tầng Application không được import presentation/PyQt6 và không gọi thẳng psutil/socket/winreg."""
        app_imports = _collect_imports_in_directory("application")
        forbidden_prefixes = ("presentation", "PyQt6", "psutil", "socket", "winreg")
        for rel_file, modules in app_imports.items():
            for mod in modules:
                for bad in forbidden_prefixes:
                    self.assertFalse(
                        mod == bad or mod.startswith(f"{bad}."),
                        f"Vi phạm kiến trúc 3 lớp tại {rel_file}: Không được phép import '{mod}' ở tầng Application!",
                    )

    def test_03_infrastructure_layer_no_upward_imports(self):
        """Tầng Infrastructure không được phép phụ thuộc ngược lên Application hoặc Presentation."""
        infra_imports = _collect_imports_in_directory("infrastructure")
        forbidden_prefixes = ("application", "presentation", "PyQt6")
        for rel_file, modules in infra_imports.items():
            for mod in modules:
                for bad in forbidden_prefixes:
                    self.assertFalse(
                        mod == bad or mod.startswith(f"{bad}."),
                        f"Vi phạm kiến trúc 3 lớp tại {rel_file}: Không được phép import '{mod}' ở tầng Infrastructure!",
                    )

    def test_04_extracted_views_log_analysis_and_network_scanner(self):
        """Kiểm tra LogAnalysisView và NetworkScannerView hoạt động độc lập và lọc dữ liệu chính xác."""
        sample_logs = [
            FileLogger.parse_policy_log_entry(
                "[2026-11-17 21:00:00] [POLICY_BLOCK] Loại: FIREWALL_BLOCK | Tiến trình: chrome.exe | Trạng thái: SUCCESS"
            ),
            FileLogger.parse_policy_log_entry(
                "[2026-11-17 21:05:00] [POLICY_APPLY] Loại: BANDWIDTH_LIMIT | Tiến trình: discord.exe | Trạng thái: SUCCESS"
            ),
        ]
        log_view = LogAnalysisView(
            fetch_policy_logs=lambda limit: sample_logs,
            fetch_connection_logs=lambda limit: "[2026-11-17] [TCP] chrome.exe | 127.0.0.1:5000 -> 8.8.8.8:443 [ESTABLISHED]",
            get_logs_dir=lambda: tempfile.gettempdir(),
        )
        log_view.refresh_logs()
        self.assertEqual(log_view.policy_log_table.rowCount(), 2)
        self.assertIn("chrome.exe", log_view.txt_conn_log.toPlainText())

        log_view.filter_policy_logs("discord")
        self.assertEqual(log_view.policy_log_table.rowCount(), 1)
        log_view.filter_policy_logs("")
        self.assertEqual(log_view.policy_log_table.rowCount(), 2)

        sample_conns = [
            {
                "pid": 1234,
                "process_name": "chrome.exe",
                "protocol": "TCP",
                "local_addr": "192.168.1.10:54321",
                "remote_addr": "142.250.199.14:443",
                "state": "ESTABLISHED",
            }
        ]
        scanner_view = NetworkScannerView(fetch_connections=lambda: sample_conns)
        scanner_view.refresh_connections()
        self.assertEqual(scanner_view.conn_table.rowCount(), 1)
        self.assertEqual(scanner_view.conn_table.item(0, 1).text(), "chrome.exe")

    def test_05_infrastructure_network_collector_capabilities(self):
        """Kiểm tra NetworkCollector cung cấp đầy đủ các hàm thu thập hệ thống cho tầng Application."""
        collector = NetworkCollector()
        stats = collector.collect_system_stats()
        self.assertIn("cpu_percent", stats)
        self.assertIn("ram_percent", stats)

        dev_uuid, reg_data = collector.generate_device_info()
        self.assertTrue(len(dev_uuid) > 8)
        self.assertEqual(dev_uuid, reg_data.device_uuid)

        nic_info = collector.get_active_nic_info(default_ip=reg_data.ip_address, mac_address=reg_data.mac_address)
        self.assertIn("name", nic_info)
        self.assertIn("ip", nic_info)

    def test_06_arch_linter_and_data_source_strategy(self):
        """Kiểm tra công cụ arch_linter báo 0 lỗi và mẫu Strategy DataSource khởi tạo đúng."""
        violations = check_architecture_violations()
        self.assertEqual(violations, [], f"Arch linter phát hiện vi phạm: {violations}")

        ds_local = create_data_source(monitor_service=object())
        self.assertIsInstance(ds_local, LocalMonitorDataSource)
        self.assertEqual(ds_local.mode_name, "local")

        ds_client = create_data_source(monitor_service=object(), agent_service=object())
        self.assertIsInstance(ds_client, ClientAgentDataSource)
        self.assertEqual(ds_client.mode_name, "client")

        ds_remote = create_data_source(fleet_service=object())
        self.assertIsInstance(ds_remote, RemoteFleetDataSource)
        self.assertEqual(ds_remote.mode_name, "remote")
        self.assertTrue(ds_remote.has_fleet_management)


if __name__ == "__main__":
    unittest.main()
