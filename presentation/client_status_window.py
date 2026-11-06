"""
Cửa sổ Quản lý & Trạng thái Máy Con (Client Status & Configuration Window - Presentation Layer):
- Quản lý cấu hình IP/Port kết nối tới máy chủ Admin (Server).
- Hiển thị thông tin định danh máy (Device UUID, MAC, OS, IP).
- Bảng danh sách các chính sách an ninh (Firewall / QoS) đang bị áp dụng từ Admin.
- Giám sát tốc độ truyền nhận tức thời trên máy con.
"""

import os
import sys
import json
import time
from typing import Optional
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QHeaderView, QFrame, QMessageBox, QApplication
)

def get_client_config_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "client_config.json")

CONFIG_PATH = get_client_config_path()


class ClientStatusWindow(QMainWindow):
    """Cửa sổ hiển thị trạng thái và cấu hình kết nối của Client Agent."""

    def __init__(self, agent_service, parent=None):
        super().__init__(parent)
        self.agent = agent_service
        self._init_ui()

        # Timer định kỳ cập nhật trạng thái giao diện
        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self._refresh_ui)
        self.refresh_timer.start()

    def _init_ui(self):
        self.setWindowTitle("Client Agent - Network Monitor & Protect")
        self.resize(680, 520)
        self.setMinimumSize(580, 420)
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #FFFFFF;
                color: #1E293B;
                font-family: 'Segoe UI', system-ui, sans-serif;
            }
            QTabWidget::pane {
                border: 1px solid #E2E8F0;
                background-color: #FFFFFF;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #F8FAFC;
                color: #64748B;
                padding: 8px 18px;
                font-size: 12px;
                font-weight: 600;
                border: 1px solid #E2E8F0;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                background-color: #FFFFFF;
                color: #2563EB;
                font-weight: bold;
                border-top: 2px solid #2563EB;
            }
            QPushButton {
                background-color: #2563EB;
                color: #FFFFFF;
                font-size: 12px;
                font-weight: bold;
                border-radius: 6px;
                padding: 6px 16px;
                border: none;
            }
            QPushButton:hover {
                background-color: #1D4ED8;
            }
            QLineEdit, QSpinBox {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #2563EB;
                background-color: #FFFFFF;
            }
        """)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # 1. Header Banner
        header = QHBoxLayout()
        header_left = QVBoxLayout()
        header_left.setSpacing(2)

        title = QLabel("💻 Client Network Agent")
        title.setStyleSheet("font-size: 17px; font-weight: 800; color: #0F172A;")
        sub = QLabel("Tiến trình giám sát và tiếp nhận chính sách an ninh mạng tập trung")
        sub.setStyleSheet("font-size: 11px; color: #64748B;")
        header_left.addWidget(title)
        header_left.addWidget(sub)
        header.addLayout(header_left)
        header.addStretch()

        self.lbl_conn_badge = QLabel("⚪ Đang ngắt kết nối")
        self.lbl_conn_badge.setStyleSheet("""
            background-color: #F1F5F9;
            color: #64748B;
            font-size: 11px;
            font-weight: bold;
            padding: 4px 12px;
            border-radius: 12px;
        """)
        header.addWidget(self.lbl_conn_badge)
        layout.addLayout(header)

        # 2. Tab Widget
        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._create_conn_tab(), "📡 Kết Nối Server")
        self.tab_widget.addTab(self._create_policy_tab(), "🛡️ Chính Sách An Ninh")
        self.tab_widget.addTab(self._create_device_tab(), "📋 Thông Tin Máy")
        self.tab_widget.addTab(self._create_live_tab(), "📊 Lưu Lượng Trực Tiếp")
        layout.addWidget(self.tab_widget)

        self.setCentralWidget(central)

    # =========================================================================
    # TAB 1: CẤU HÌNH KẾT NỐI SERVER
    # =========================================================================
    def _create_conn_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(18, 16, 18, 16)
        l.setSpacing(14)

        info_box = QFrame()
        info_box.setStyleSheet("background-color: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 8px; padding: 10px;")
        ib_layout = QVBoxLayout(info_box)
        lbl_info = QLabel("ℹ️ Máy con sẽ tự động gửi số liệu đo lường định kỳ và chấp hành lệnh can thiệp Firewall / QoS từ máy chủ Admin được cấu hình dưới đây.")
        lbl_info.setStyleSheet("font-size: 11px; color: #1E3A8A; line-height: 1.4;")
        lbl_info.setWordWrap(True)
        ib_layout.addWidget(lbl_info)
        l.addWidget(info_box)

        # Form cài đặt IP/Port
        form_frame = QFrame()
        form_frame.setStyleSheet("background-color: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px;")
        form = QVBoxLayout(form_frame)
        form.setSpacing(10)

        # IP Server
        h1 = QHBoxLayout()
        lbl_ip = QLabel("Địa chỉ IP Máy Chủ (Admin Server):")
        lbl_ip.setStyleSheet("font-weight: bold; font-size: 12px; min-width: 220px;")
        self.txt_server_ip = QLineEdit(self.agent.server_host)
        h1.addWidget(lbl_ip)
        h1.addWidget(self.txt_server_ip, stretch=1)
        form.addLayout(h1)

        # Port
        h2 = QHBoxLayout()
        lbl_port = QLabel("Cổng TCP Quản Trị:")
        lbl_port.setStyleSheet("font-weight: bold; font-size: 12px; min-width: 220px;")
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(self.agent.server_port)
        h2.addWidget(lbl_port)
        h2.addWidget(self.spin_port, stretch=1)
        form.addLayout(h2)

        # Checkbox Toast
        self.chk_toast = QCheckBox("Hiển thị thông báo góc màn hình khi Admin áp dụng chính sách")
        self.chk_toast.setChecked(getattr(self.agent, "show_toast_notifications", True))
        form.addWidget(self.chk_toast)

        l.addWidget(form_frame)

        # Nút Lưu & Kết nối lại
        btn_box = QHBoxLayout()
        btn_box.addStretch()
        btn_save = QPushButton("💾 Lưu & Kết Nối Lại")
        btn_save.setFixedHeight(34)
        btn_save.clicked.connect(self._on_save_connection)
        btn_box.addWidget(btn_save)
        l.addLayout(btn_box)

        l.addStretch()
        return tab

    def _on_save_connection(self):
        new_host = self.txt_server_ip.text().strip()
        new_port = self.spin_port.value()
        if not new_host:
            QMessageBox.warning(self, "Lỗi", "Địa chỉ IP máy chủ không được để trống!")
            return

        # Lưu file config
        cfg = {
            "server_host": new_host,
            "server_port": new_port,
            "enable_etw": self.agent.enable_etw,
            "show_toast_notifications": self.chk_toast.isChecked(),
        }
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"[ClientWindow] Lỗi lưu config: {e}")

        # Cập nhật kết nối ngay
        self.agent.show_toast_notifications = self.chk_toast.isChecked()
        self.agent.update_server_target(new_host, new_port)
        QMessageBox.information(self, "Thành Công", f"Đã lưu cấu hình và đang kết nối lại tới {new_host}:{new_port}!")

    # =========================================================================
    # TAB 2: CHÍNH SÁCH AN NINH HIỆN HÀNH
    # =========================================================================
    def _create_policy_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(14, 14, 14, 14)
        l.setSpacing(10)

        lbl = QLabel("Danh sách ứng dụng đang bị Quản trị viên can thiệp trên máy con này:")
        lbl.setStyleSheet("font-size: 12px; font-weight: 600; color: #475569;")
        l.addWidget(lbl)

        self.tbl_policies = QTableWidget()
        self.tbl_policies.setColumnCount(5)
        self.tbl_policies.setHorizontalHeaderLabels([
            "Ứng Dụng", "Hành Động Can Thiệp", "Hạn Mức Băng Thông", "Chiều Áp Dụng", "Thời Điểm"
        ])
        self.tbl_policies.verticalHeader().setVisible(False)
        self.tbl_policies.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl_policies.setStyleSheet("""
            QTableWidget {
                border: 1px solid #E2E8F0;
                border-radius: 6px;
                font-size: 12px;
            }
            QHeaderView::section {
                background-color: #F8FAFC;
                color: #475569;
                font-weight: bold;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #E2E8F0;
            }
        """)
        l.addWidget(self.tbl_policies, stretch=1)
        return tab

    # =========================================================================
    # TAB 3: THÔNG TIN THIẾT BỊ
    # =========================================================================
    def _create_device_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(18, 16, 18, 16)
        l.setSpacing(12)

        reg = self.agent.registration_data
        rows = [
            ("Tên máy (Hostname):", reg.hostname),
            ("Device UUID:", reg.device_uuid),
            ("Địa chỉ MAC:", reg.mac_address),
            ("Địa chỉ IP LAN:", reg.ip_address),
            ("Hệ điều hành:", f"{reg.os_name} {reg.os_version}"),
            ("Phiên bản Agent:", reg.agent_version),
        ]

        frame = QFrame()
        frame.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 14px;")
        fl = QVBoxLayout(frame)
        fl.setSpacing(8)

        for label, val in rows:
            h = QHBoxLayout()
            lbl_title = QLabel(label)
            lbl_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #475569; min-width: 150px;")
            lbl_val = QLabel(val)
            lbl_val.setStyleSheet("font-size: 12px; color: #0F172A;")
            h.addWidget(lbl_title)
            h.addWidget(lbl_val, stretch=1)
            fl.addLayout(h)

        l.addWidget(frame)

        # Nút sao chép UUID
        btn_box = QHBoxLayout()
        btn_copy = QPushButton("📋 Sao Chép Device UUID")
        btn_copy.setFixedWidth(180)
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(reg.device_uuid))
        btn_box.addWidget(btn_copy)
        btn_box.addStretch()
        l.addLayout(btn_box)

        l.addStretch()
        return tab

    # =========================================================================
    # TAB 4: LƯU LƯỢNG TRỰC TIẾP
    # =========================================================================
    def _create_live_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setContentsMargins(18, 16, 18, 16)
        l.setSpacing(14)

        cards = QHBoxLayout()
        cards.setSpacing(12)

        self.card_down = self._create_metric_card("⬇️ TỐC ĐỘ TẢI XUỐNG", "0 B/s", "#D97706")
        self.card_up = self._create_metric_card("⬆️ TỐC ĐỘ TẢI LÊN", "0 B/s", "#E11D48")
        cards.addWidget(self.card_down)
        cards.addWidget(self.card_up)
        l.addLayout(cards)

        details = QFrame()
        details.setStyleSheet("background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px;")
        dl = QVBoxLayout(details)
        dl.setSpacing(8)

        self.lbl_cpu = QLabel("CPU: 0%")
        self.lbl_ram = QLabel("RAM: 0%")
        self.lbl_total_down = QLabel("Tổng tải xuống: 0 B")
        self.lbl_total_up = QLabel("Tổng tải lên: 0 B")

        for lbl in (self.lbl_cpu, self.lbl_ram, self.lbl_total_down, self.lbl_total_up):
            lbl.setStyleSheet("font-size: 12px; color: #334155; font-weight: 600;")
            dl.addWidget(lbl)

        l.addWidget(details)
        l.addStretch()
        return tab

    def _create_metric_card(self, title: str, value: str, color: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-left: 4px solid {color};
                border-radius: 8px;
                padding: 8px 14px;
            }}
        """)
        l = QVBoxLayout(card)
        l.setSpacing(2)
        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748B;")
        lbl_v = QLabel(value)
        lbl_v.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {color};")
        card.lbl_val = lbl_v
        l.addWidget(lbl_t)
        l.addWidget(lbl_v)
        return card

    # =========================================================================
    # CẬP NHẬT GIAO DIỆN ĐỊNH KỲ
    # =========================================================================
    def _refresh_ui(self):
        """Cập nhật trạng thái kết nối, số liệu live và bảng chính sách."""
        # 1. Trạng thái kết nối
        is_conn = self.agent.net_client.is_connected()
        if is_conn:
            self.lbl_conn_badge.setText(f"🟢 Đã kết nối: {self.agent.server_host}:{self.agent.server_port}")
            self.lbl_conn_badge.setStyleSheet("""
                background-color: #ECFDF5;
                color: #059669;
                font-size: 11px;
                font-weight: bold;
                padding: 4px 12px;
                border-radius: 12px;
                border: 1px solid #A7F3D0;
            """)
        else:
            self.lbl_conn_badge.setText(f"⚪ Đang tìm kiếm Server ({self.agent.server_host}:{self.agent.server_port})...")
            self.lbl_conn_badge.setStyleSheet("""
                background-color: #F1F5F9;
                color: #64748B;
                font-size: 11px;
                font-weight: bold;
                padding: 4px 12px;
                border-radius: 12px;
            """)

        # 2. Cập nhật bảng chính sách hiện hành
        policies = list(self.agent.active_policies.values())
        self.tbl_policies.setRowCount(len(policies))
        for r, pol in enumerate(policies):
            app_item = QTableWidgetItem(pol.get("app", ""))
            app_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))

            act = pol.get("action", "")
            act_text = "🚫 CHẶN HOÀN TOÀN" if act == "BLOCK" else "⚡ GIỚI HẠN BĂNG THÔNG"
            act_item = QTableWidgetItem(act_text)
            act_item.setForeground(QtGui.QColor("#DC2626" if act == "BLOCK" else "#D97706"))
            act_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Weight.Bold))

            limit = pol.get("limit_kbps", 0.0)
            lim_str = f"{limit:,.1f} KB/s" if limit > 0 else "0 KB/s (Chặn hoàn toàn)"
            lim_item = QTableWidgetItem(lim_str)

            dir_str = pol.get("direction", "both").upper()
            dir_item = QTableWidgetItem(dir_str)

            t_val = pol.get("time", time.time())
            time_str = time.strftime("%H:%M:%S", time.localtime(t_val))
            time_item = QTableWidgetItem(time_str)

            self.tbl_policies.setItem(r, 0, app_item)
            self.tbl_policies.setItem(r, 1, act_item)
            self.tbl_policies.setItem(r, 2, lim_item)
            self.tbl_policies.setItem(r, 3, dir_item)
            self.tbl_policies.setItem(r, 4, time_item)

        # 3. Cập nhật số liệu Live
        rx_b = self.agent.total_rx_bytes
        tx_b = self.agent.total_tx_bytes
        self.lbl_total_down.setText(f"Tổng tải xuống: {self._format_bytes(rx_b)}")
        self.lbl_total_up.setText(f"Tổng tải lên: {self._format_bytes(tx_b)}")

        # Shaper speeds
        shaper = self.agent.packet_shaper
        down_kb = shaper.current_down_kbps if shaper.is_active else 0.0
        up_kb = shaper.current_up_kbps if shaper.is_active else 0.0
        self.card_down.lbl_val.setText(f"{down_kb:,.1f} KB/s" if down_kb >= 1.0 else f"{down_kb * 1024:,.0f} B/s")
        self.card_up.lbl_val.setText(f"{up_kb:,.1f} KB/s" if up_kb >= 1.0 else f"{up_kb * 1024:,.0f} B/s")

    @staticmethod
    def _format_bytes(b: int) -> str:
        if b >= 1024 * 1024 * 1024:
            return f"{b / (1024 * 1024 * 1024):,.2f} GB"
        elif b >= 1024 * 1024:
            return f"{b / (1024 * 1024):,.1f} MB"
        elif b >= 1024:
            return f"{b / 1024:,.1f} KB"
        return f"{b:,} B"

    def closeEvent(self, event):
        """Khi bấm dấu X thì chỉ ẩn cửa sổ xuống khay hệ thống, không thoát Agent."""
        event.ignore()
        self.hide()
