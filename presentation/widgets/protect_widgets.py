"""
Các Widget Nút Bấm Điều Khiển Tường Lửa & QoS (GlassWire Protect Widgets):
- FirewallSwitchButton: Công tắc gạt Firewall ON / OFF.
- ConnectionPillButton: Nút Pill chuyển trạng thái Allowed / Blocked / Throttled.
"""

from typing import Callable, Optional
from PyQt6 import QtCore
from PyQt6.QtWidgets import QPushButton, QMenu


class FirewallSwitchButton(QPushButton):
    """Công tắc gạt Firewall chuẩn phong cách iOS / GlassWire (OFF / ON)."""

    state_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setFixedSize(58, 28)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._update_appearance()
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked: bool):
        self._update_appearance()
        self.state_changed.emit(checked)

    def _update_appearance(self):
        if self.isChecked():
            self.setText("ON")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #10B981;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 14px;
                    border: none;
                    text-align: center;
                }
                QPushButton:hover {
                    background-color: #059669;
                }
            """)
        else:
            self.setText("OFF")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #94A3B8;
                    color: #FFFFFF;
                    font-size: 11px;
                    font-weight: bold;
                    border-radius: 14px;
                    border: none;
                    text-align: center;
                }
                QPushButton:hover {
                    background-color: #64748B;
                }
            """)


class ConnectionPillButton(QPushButton):
    """Nút bấm dạng Pill chuyển trạng thái Allowed / Blocked / Throttled trong bảng."""

    status_changed = QtCore.pyqtSignal(str, str, str)  # (app_name, direction, new_status)

    def __init__(
        self,
        app_name: str,
        direction: str,
        initial_status: str = "allowed",
        next_status_resolver: Optional[Callable[[str], str]] = None,
    ):
        super().__init__()
        self.app_name = app_name
        self.direction = direction  # "in" hoặc "out"
        self.status = initial_status  # "allowed", "blocked", "throttled"
        self._next_status_resolver = next_status_resolver
        self.setFixedHeight(24)
        self.setMinimumWidth(72)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._update_style()
        self.clicked.connect(self._toggle_status)

    def set_status(self, new_status: str):
        if self.status == new_status:
            return
        self.status = new_status
        self._update_style()

    def _toggle_status(self):
        if self._next_status_resolver:
            new_st = self._next_status_resolver(self.status)
        else:
            new_st = "blocked" if self.status == "allowed" else "allowed"
        self.status = new_st
        self._update_style()
        app_name = self.app_name
        direction = self.direction
        status = self.status
        QtCore.QTimer.singleShot(0, lambda: self.status_changed.emit(app_name, direction, status))

    def _show_context_menu(self, pos: QtCore.QPoint):
        """Menu chuột phải cho phép chọn nhanh Allowed / Blocked / Throttled."""
        menu = QMenu(self)
        act_allow = menu.addAction("✅ Allowed (Cho phép)")
        act_block = menu.addAction("🚫 Blocked (Chặn kết nối)")
        act_throttle = menu.addAction("⚡ Throttled (Giới hạn QoS)")
        chosen = menu.exec(self.mapToGlobal(pos))
        if not chosen:
            return
        target_status = self.status
        if chosen == act_allow:
            target_status = "allowed"
        elif chosen == act_block:
            target_status = "blocked"
        elif chosen == act_throttle:
            target_status = "throttled"
        if target_status != self.status or target_status == "throttled":
            self.status = target_status
            self._update_style()
            app_name = self.app_name
            direction = self.direction
            status = self.status
            QtCore.QTimer.singleShot(0, lambda: self.status_changed.emit(app_name, direction, status))

    def _update_style(self):
        if self.status == "allowed":
            self.setText("Allowed")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #ECFDF5;
                    color: #059669;
                    font-size: 11px;
                    font-weight: bold;
                    border: 1px solid #A7F3D0;
                    border-radius: 12px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #D1FAE5;
                }
            """)
        elif self.status == "blocked":
            self.setText("Blocked")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #FEF2F2;
                    color: #DC2626;
                    font-size: 11px;
                    font-weight: bold;
                    border: 1px solid #FECACA;
                    border-radius: 12px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #FEE2E2;
                }
            """)
        else:
            self.setText("Throttled")
            self.setStyleSheet("""
                QPushButton {
                    background-color: #FFFBEB;
                    color: #D97706;
                    font-size: 11px;
                    font-weight: bold;
                    border: 1px solid #FDE68A;
                    border-radius: 12px;
                    padding: 2px 8px;
                }
                QPushButton:hover {
                    background-color: #FEF3C7;
                }
            """)
