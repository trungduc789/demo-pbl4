import sys
import time
import unittest
from unittest.mock import MagicMock
from PyQt6.QtWidgets import QApplication

from presentation.protect_view import GlassWireProtectView, ConnectionPillButton

_app = QApplication.instance() or QApplication(sys.argv)


class TestFirewallAndProtectStability(unittest.TestCase):
    """Kiểm thử tính ổn định của Tab Protect: không nhảy thứ tự, giữ nguyên tìm kiếm, và không crash khi click liên tục."""

    def test_01_row_order_and_widgets_remain_stable_across_updates(self):
        """Các dòng đã hiển thị phải giữ nguyên vị trí cố định và không tạo lại widget khi tốc độ thay đổi."""
        view = GlassWireProtectView()

        # Chu kỳ 1: svchost.exe, firefox.exe, brave.exe
        view.update_protect_table(
            proc_speeds=[
                {"name": "svchost.exe", "pid": 100, "down_kb": 50.0, "up_kb": 1.0, "exe": ""},
                {"name": "firefox.exe", "pid": 200, "down_kb": 20.0, "up_kb": 2.0, "exe": ""},
                {"name": "brave.exe", "pid": 300, "down_kb": 5.0, "up_kb": 0.5, "exe": ""},
            ],
            app_accumulated={
                "svchost.exe": {"down": 51200, "up": 1024},
                "firefox.exe": {"down": 20480, "up": 2048},
                "brave.exe": {"down": 5120, "up": 512},
            },
            app_metadata={},
        )

        self.assertEqual(view.table.rowCount(), 3)
        initial_order = [view.table.item(r, 1).text().strip() for r in range(3)]
        self.assertEqual(initial_order, ["svchost.exe", "firefox.exe", "brave.exe"])

        # Lưu tham chiếu các nút Pill ở chu kỳ 1
        pill_firefox_in = view._row_pills["firefox.exe"]["in"]
        pill_brave_out = view._row_pills["brave.exe"]["out"]

        # Chu kỳ 2: Tốc độ đảo ngược hoàn toàn (brave lên đầu, svchost về 0, thêm Zalo.exe mới)
        view.update_protect_table(
            proc_speeds=[
                {"name": "brave.exe", "pid": 300, "down_kb": 500.0, "up_kb": 80.0, "exe": ""},
                {"name": "Zalo.exe", "pid": 400, "down_kb": 120.0, "up_kb": 10.0, "exe": ""},
                {"name": "firefox.exe", "pid": 200, "down_kb": 1.0, "up_kb": 0.0, "exe": ""},
            ],
            app_accumulated={
                "svchost.exe": {"down": 51200, "up": 1024},
                "firefox.exe": {"down": 21504, "up": 2048},
                "brave.exe": {"down": 517120, "up": 82432},
                "Zalo.exe": {"down": 122880, "up": 10240},
            },
            app_metadata={},
        )

        # Kiểm tra 3 ứng dụng đầu tiên KHÔNG BỊ NHẢY VỊ TRÍ, ứng dụng mới Zalo.exe nối vào cuối
        second_order = [view.table.item(r, 1).text().strip() for r in range(view.table.rowCount())]
        self.assertEqual(second_order, ["svchost.exe", "firefox.exe", "brave.exe", "Zalo.exe"])

        # Kiểm tra các nút bấm C++ không bị hủy và tạo lại
        self.assertIs(view._row_pills["firefox.exe"]["in"], pill_firefox_in)
        self.assertIs(view._row_pills["brave.exe"]["out"], pill_brave_out)

    def test_02_search_filter_persists_across_realtime_updates(self):
        """Khi đang tìm kiếm, các chu kỳ cập nhật mới không được làm mất bộ lọc hoặc làm nhảy kết quả."""
        view = GlassWireProtectView()
        view.update_protect_table(
            proc_speeds=[
                {"name": "svchost.exe", "pid": 100, "down_kb": 10.0, "up_kb": 0.0, "exe": ""},
                {"name": "firefox.exe", "pid": 200, "down_kb": 20.0, "up_kb": 0.0, "exe": ""},
                {"name": "brave.exe", "pid": 300, "down_kb": 30.0, "up_kb": 0.0, "exe": ""},
            ],
            app_accumulated={},
            app_metadata={},
        )

        # Người dùng gõ tìm kiếm "fire"
        view.search_input.setText("fire")
        _app.processEvents()

        self.assertTrue(view.table.isRowHidden(0))   # svchost.exe ẩn
        self.assertFalse(view.table.isRowHidden(1))  # firefox.exe hiện
        self.assertTrue(view.table.isRowHidden(2))   # brave.exe ẩn

        # 5 chu kỳ cập nhật liên tiếp với thêm tiến trình mới không khớp "fire"
        for i in range(5):
            view.update_protect_table(
                proc_speeds=[
                    {"name": "brave.exe", "pid": 300, "down_kb": 100.0 + i, "up_kb": 5.0, "exe": ""},
                    {"name": f"extra_{i}.exe", "pid": 500 + i, "down_kb": 15.0, "up_kb": 1.0, "exe": ""},
                    {"name": "firefox.exe", "pid": 200, "down_kb": 50.0 + i, "up_kb": 2.0, "exe": ""},
                ],
                app_accumulated={},
                app_metadata={},
            )

        # Kiểm tra chỉ duy nhất dòng firefox.exe (row 1) hiển thị, tất cả dòng khác đều ẩn
        visible_rows = [
            view.table.item(r, 1).text().strip()
            for r in range(view.table.rowCount())
            if not view.table.isRowHidden(r)
        ]
        self.assertEqual(visible_rows, ["firefox.exe"])

    def test_03_rapid_block_unblock_15_times_without_crash(self):
        """Thực hiện 15 thao tác Block / Gỡ Block liên tiếp xen kẽ cập nhật bảng không gây crash."""
        view = GlassWireProtectView()
        commands_sent = []

        def fake_commander(uid, action, app, limit, direction):
            commands_sent.append((uid, action, app, direction))
            return True, "OK"

        view.set_remote_mode("7302f16a-1111-2222-3333", fake_commander)
        view.update_protect_table(
            proc_speeds=[
                {"name": "firefox.exe", "pid": 200, "down_kb": 25.0, "up_kb": 5.0, "exe": ""},
                {"name": "brave.exe", "pid": 300, "down_kb": 15.0, "up_kb": 2.0, "exe": ""},
            ],
            app_accumulated={},
            app_metadata={},
        )

        pill = view._row_pills["firefox.exe"]["in"]

        # Click liên tiếp 15 lần vừa click vừa cập nhật bảng
        for step in range(15):
            pill.click()
            _app.processEvents()
            view.update_protect_table(
                proc_speeds=[
                    {"name": "brave.exe", "pid": 300, "down_kb": float(step * 10), "up_kb": 2.0, "exe": ""},
                    {"name": "firefox.exe", "pid": 200, "down_kb": float(step * 5), "up_kb": 1.0, "exe": ""},
                ],
                app_accumulated={},
                app_metadata={},
            )
            _app.processEvents()

        # Chờ worker xử lý hết hàng đợi lệnh
        view._cmd_queue.join()

        # Sau 15 lần click (lẻ): trạng thái hiện tại phải là "blocked" (vì lần 1: blocked, lần 2: allowed, ..., lần 15: blocked)
        self.assertEqual(pill.status, "blocked")
        self.assertEqual(view._row_pills["firefox.exe"]["out"].status, "blocked")
        self.assertEqual(len(commands_sent), 15)
        self.assertEqual(commands_sent[0][1], "BLOCK_APP")
        self.assertEqual(commands_sent[1][1], "UNBLOCK_APP")
        self.assertEqual(commands_sent[-1][1], "BLOCK_APP")


if __name__ == "__main__":
    unittest.main()
