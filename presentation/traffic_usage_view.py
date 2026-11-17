"""
Giao diện hiển thị thống kê lưu lượng ứng dụng (Usage View) chuẩn phong cách GlassWire:
Bao gồm 4 cột phân tích:
1. Apps: Danh sách ứng dụng tiêu thụ mạng với icon và thanh dung lượng
2. Hosts: Danh sách máy chủ/IP từ xa kết nối
3. Traffic Type: Phân bổ theo giao thức (HTTPS, HTTP, DNS, TCP, UDP...)
4. Countries: Phân bổ lưu lượng theo quốc gia / mạng nội bộ
Hỗ trợ lọc theo chiều lưu lượng (Both, Incoming only, Outgoing only) và sắp xếp linh hoạt.
"""

from typing import Dict, List, Optional
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QProgressBar, QSizePolicy
)
from application.traffic_analytics import (
    format_bytes as format_bytes_human,
    aggregate_connections_usage,
)
from utils.safe_print import safe_print as print


class UsageItemWidget(QFrame):
    """Một dòng hiển thị mục trong cột Usage của GlassWire."""
    def __init__(self, icon: Optional[QtGui.QIcon], title: str, subtitle: str,
                 byte_val: float, max_byte: float, flag_icon: Optional[QtGui.QIcon] = None):
        super().__init__()
        self.setFixedHeight(36)
        self.setStyleSheet("""
            QFrame {
                background-color: transparent;
                border-radius: 4px;
            }
            QFrame:hover {
                background-color: #F8FAFC;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(8)

        # Icon chính (App icon hoặc protocol icon)
        if icon and not icon.isNull():
            lbl_icon = QLabel()
            lbl_icon.setFixedSize(20, 20)
            lbl_icon.setPixmap(icon.pixmap(20, 20))
            layout.addWidget(lbl_icon)

        # Tiêu đề
        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet("font-size: 12px; font-weight: 500; color: #1E293B;")
        self.lbl_title.setToolTip(title)
        layout.addWidget(self.lbl_title, stretch=1)

        # Dung lượng byte
        val_str = format_bytes_human(byte_val)
        self.lbl_val = QLabel(val_str)
        self.lbl_val.setStyleSheet("font-size: 12px; font-weight: bold; color: #334155;")
        self.lbl_val.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.lbl_val)

        # Flag / badge nếu có
        if flag_icon and not flag_icon.isNull():
            lbl_flag = QLabel()
            lbl_flag.setFixedSize(16, 12)
            lbl_flag.setPixmap(flag_icon.pixmap(16, 12))
            layout.addWidget(lbl_flag)


class UsageColumnCard(QFrame):
    """Một cột trong 4 cột của GlassWire Usage View."""
    def __init__(self, title: str):
        super().__init__()
        self.setStyleSheet("""
            QFrame#usageColFrame {
                background-color: #FFFFFF;
                border-right: 1px solid #F1F5F9;
            }
        """)
        self.setObjectName("usageColFrame")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # Header cột
        self.lbl_header = QLabel(title)
        self.lbl_header.setStyleSheet("font-size: 13px; font-weight: bold; color: #64748B; padding-bottom: 4px; border-bottom: 1px solid #F1F5F9;")
        main_layout.addWidget(self.lbl_header)

        # Vùng cuộn danh sách mục
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background-color: transparent;")

        self.list_container = QWidget()
        self.list_container.setStyleSheet("background-color: transparent;")
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 4, 0, 4)
        self.list_layout.setSpacing(2)
        self.list_layout.addStretch()

        self.scroll.setWidget(self.list_container)
        main_layout.addWidget(self.scroll, stretch=1)

    def clear_items(self):
        """Xóa toàn bộ các item cũ để vẽ lại."""
        while self.list_layout.count() > 1:
            child = self.list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def add_item(self, widget: QWidget):
        """Thêm item vào danh sách trước stretch."""
        self.list_layout.insertWidget(self.list_layout.count() - 1, widget)


class GlassWireUsageView(QWidget):
    """
    Khu vực hiển thị Usage đa cột chuẩn mực GlassWire.
    Tự động cập nhật từ dữ liệu lưu lượng và kết nối socket thời gian thực.
    """
    def __init__(self, icon_manager=None):
        super().__init__()
        self.icon_manager = icon_manager
        self.filter_direction = "both"  # "both", "incoming", "outgoing"
        self.sort_order = "high_to_low"  # "high_to_low", "low_to_high", "alpha"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 4 Cột hiển thị: Apps | Hosts | Traffic Type | Countries
        self.col_apps = UsageColumnCard("Apps")
        self.col_hosts = UsageColumnCard("Hosts")
        self.col_traffic_type = UsageColumnCard("Traffic Type")
        self.col_countries = UsageColumnCard("Countries")

        layout.addWidget(self.col_apps, stretch=1)
        layout.addWidget(self.col_hosts, stretch=1)
        layout.addWidget(self.col_traffic_type, stretch=1)
        layout.addWidget(self.col_countries, stretch=1)

    def set_filter_direction(self, direction: str):
        """Thiết lập chiều lọc: both, incoming, outgoing."""
        self.filter_direction = direction

    def set_sort_order(self, sort_order: str):
        """Thiết lập kiểu sắp xếp: high_to_low, low_to_high, alpha."""
        self.sort_order = sort_order

    def update_usage_data(self, app_accumulated: Dict[str, Dict[str, float]],
                          active_conns: List,
                          app_metadata: Dict[str, Dict],
                          total_window_bytes: float = 0.0):
        """Cập nhật dữ liệu cho cả 4 cột theo khung thời gian đã chọn (30s, 1m, 5m, 10m, 1h, 24h)."""
        # 1. XỬ LÝ CỘT APPS
        self.col_apps.clear_items()
        apps_list = []
        for name, data in app_accumulated.items():
            if name == "All apps":
                continue
            if self.filter_direction == "incoming":
                b_val = data.get("down", 0.0)
            elif self.filter_direction == "outgoing":
                b_val = data.get("up", 0.0)
            else:
                b_val = data.get("down", 0.0) + data.get("up", 0.0)
            if b_val > 0:
                apps_list.append((name, b_val))

        # Sắp xếp
        if self.sort_order == "high_to_low":
            apps_list.sort(key=lambda x: x[1], reverse=True)
        elif self.sort_order == "low_to_high":
            apps_list.sort(key=lambda x: x[1])
        elif self.sort_order == "alpha":
            apps_list.sort(key=lambda x: x[0].lower())

        # Nếu chưa truyền total_window_bytes thì tính tổng từ apps_list
        if total_window_bytes <= 0.0:
            total_window_bytes = sum(b for _, b in apps_list)

        max_app_val = max([x[1] for x in apps_list], default=1.0)
        for name, b_val in apps_list[:25]:
            icon = None
            if self.icon_manager:
                exe = app_metadata.get(name, {}).get("exe", "")
                icon = self.icon_manager.get_icon(exe, name)
            item_w = UsageItemWidget(icon, name, "", b_val, max_app_val)
            self.col_apps.add_item(item_w)

        # 2. XỬ LÝ CỘT HOSTS, TRAFFIC TYPE & KHU VỰC MẠNG TỪ ACTIVE CONNECTIONS THỰC TẾ
        self.col_hosts.clear_items()
        self.col_traffic_type.clear_items()
        self.col_countries.clear_items()

        host_counts, proto_counts, region_counts, total_conns = aggregate_connections_usage(active_conns)

        def _sort_items(items_list):
            if self.sort_order == "high_to_low":
                items_list.sort(key=lambda x: x[1], reverse=True)
            elif self.sort_order == "low_to_high":
                items_list.sort(key=lambda x: x[1])
            elif self.sort_order == "alpha":
                items_list.sort(key=lambda x: x[0].lower())
            return items_list

        # Render cột Hosts
        if host_counts:
            sorted_hosts = _sort_items([
                (h_name, total_window_bytes * (cnt / total_conns))
                for h_name, cnt in host_counts.items()
            ])[:20]
            max_host_val = max([x[1] for x in sorted_hosts], default=1.0)
            for host_str, b_val in sorted_hosts:
                self.col_hosts.add_item(UsageItemWidget(None, host_str, "", b_val, max_host_val))
        else:
            self.col_hosts.add_item(UsageItemWidget(None, "Chưa có kết nối ngoài", "", 0.0, 1.0))

        # Render cột Traffic Type
        if proto_counts:
            sorted_proto = _sort_items([
                (p_name, total_window_bytes * (cnt / total_conns))
                for p_name, cnt in proto_counts.items()
            ])
            max_p_val = max([x[1] for x in sorted_proto], default=1.0)
            for p_name, b_val in sorted_proto:
                self.col_traffic_type.add_item(UsageItemWidget(None, p_name, "", b_val, max_p_val))
        else:
            self.col_traffic_type.add_item(UsageItemWidget(None, "Chưa ghi nhận giao thức", "", 0.0, 1.0))

        # Render cột Khu vực mạng (Countries / Scope)
        if region_counts:
            sorted_regions = _sort_items([
                (c_name, total_window_bytes * (cnt / total_conns))
                for c_name, cnt in region_counts.items()
            ])
            max_c_val = max([x[1] for x in sorted_regions], default=1.0)
            for c_name, b_val in sorted_regions:
                self.col_countries.add_item(UsageItemWidget(None, c_name, "", b_val, max_c_val))
        else:
            self.col_countries.add_item(UsageItemWidget(None, "Chưa ghi nhận vùng mạng", "", 0.0, 1.0))
