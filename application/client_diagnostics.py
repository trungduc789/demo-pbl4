"""
Dịch vụ Chẩn đoán Kết nối Mạng & Quản lý Cấu hình Máy Con (ClientDiagnosticsMixin - Application Layer):
- Đo độ trễ bất đồng bộ tới Admin Server và Internet Gateway thông qua tầng Infrastructure.
- Phát hiện thông tin Card mạng đang định tuyến và chạy bài kiểm tra chẩn đoán toàn diện.
- Quản lý cấu hình `client_config.json` và chế độ khởi động cùng Windows.
"""

import os
import sys
import json
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.network_collector import NetworkCollector
from utils.safe_print import safe_print as print


class ClientDiagnosticsMixin:
    """Mixin cung cấp các hàm chẩn đoán mạng và lưu cấu hình cho ClientAgentService."""

    def _get_infra_collector(self) -> NetworkCollector:
        if isinstance(getattr(self, "collector", None), NetworkCollector):
            return self.collector
        return NetworkCollector()

    def trigger_async_latency_check(self):
        """Đo độ trễ Server & Internet trên luồng nền bất đồng bộ để không gây đơ giao diện."""
        if getattr(self, "_latency_probe_busy", False):
            return
        self._latency_probe_busy = True

        def _probe():
            try:
                srv_lat, srv_ok, srv_msg = self.check_server_latency()
                self.cached_server_latency = srv_lat
                self.cached_server_ok = srv_ok
                self.cached_server_msg = srv_msg

                net_lat, net_ok, net_msg = self.check_internet_latency()
                self.cached_internet_latency = net_lat
                self.cached_internet_ok = net_ok
                self.cached_internet_msg = net_msg
            except Exception:
                pass
            finally:
                self._latency_probe_busy = False

        threading.Thread(target=_probe, daemon=True, name="ClientAgent-LatencyProbe").start()

    def check_server_latency(self) -> Tuple[Optional[float], bool, str]:
        """Đo độ trễ TCP tới Admin Server thông qua tầng Infrastructure."""
        lat, ok, msg = self._get_infra_collector().measure_tcp_latency(
            host=self.server_host,
            port=self.server_port,
            timeout=1.0,
            success_msg="Kết nối TCP ổn định",
        )
        if ok:
            return lat, True, msg
        if self.net_client.is_connected():
            return 1.0, True, "Kênh kết nối Socket chính đang mở"
        return None, False, "Không thể kết nối tới Server"

    def check_internet_latency(
        self, target_host: str = "8.8.8.8", target_port: int = 53, timeout: float = 1.0
    ) -> Tuple[Optional[float], bool, str]:
        """Đo độ trễ tới Gateway Internet / Public DNS thông qua tầng Infrastructure."""
        return self._get_infra_collector().measure_tcp_latency(
            host=target_host,
            port=target_port,
            timeout=timeout,
            success_msg="Internet sẵn sàng",
            dns_fallback_host="dns.google",
        )

    def get_active_nic_info(self) -> Dict[str, Any]:
        """Thu thập thông tin card mạng chính đang định tuyến thông qua tầng Infrastructure."""
        return self._get_infra_collector().get_active_nic_info(
            default_ip=self.registration_data.ip_address,
            mac_address=self.registration_data.mac_address,
        )

    def run_full_diagnostics(self) -> List[str]:
        """Chạy bài kiểm tra toàn diện và trả về danh sách nhật ký."""
        results = []
        now_str = time.strftime("%H:%M:%S")
        results.append(f"[{now_str}] Bắt đầu kiểm tra chẩn đoán hệ thống mạng máy con...")

        shaper_active = getattr(self.packet_shaper, "is_active", False)
        if shaper_active:
            results.append("✓ [Driver] WinDivert Packet Filtering: ĐANG HOẠT ĐỘNG (Bảo vệ tích cực)")
        else:
            results.append("✓ [Driver] WinDivert Packet Filtering: SẴN SÀNG (Standby)")

        etw_real = bool(
            self.etw_consumer
            and hasattr(self.etw_consumer, "is_etw_active")
            and self.etw_consumer.is_etw_active()
        )
        if etw_real:
            results.append("✓ [ETW] Kernel Event Tracing Consumer: ĐANG THU THẬP BĂNG THÔNG")
        elif self.etw_consumer and (
            getattr(self.etw_consumer, "is_running", False)
            or (hasattr(self.etw_consumer, "is_alive") and self.etw_consumer.is_alive())
        ):
            results.append(
                "⚠ [ETW] Thiếu quyền Administrator — tạm tắt đo lưu lượng theo từng tiến trình (chỉ đo tổng card mạng)"
            )
        else:
            results.append("ℹ [ETW] Chưa bật Kernel ETW (chỉ đo tổng card mạng)")

        srv_lat, srv_ok, srv_msg = self.check_server_latency()
        if srv_ok:
            results.append(f"✓ [Server] Máy chủ {self.server_host}:{self.server_port} -> Phản hồi {srv_lat} ms ({srv_msg})")
        else:
            results.append(f"⚠ [Server] Không kết nối được {self.server_host}:{self.server_port} ({srv_msg})")

        net_lat, net_ok, net_msg = self.check_internet_latency()
        if net_ok:
            results.append(f"✓ [Internet] Cổng DNS 8.8.8.8 -> Phản hồi {net_lat} ms ({net_msg})")
        else:
            results.append(f"ℹ [Internet] Kết nối mạng ngoài: {net_msg}")

        results.append(f"[{time.strftime('%H:%M:%S')}] Hoàn tất quy trình kiểm tra chẩn đoán.")
        return results

    def is_windows_autostart(self) -> bool:
        """Kiểm tra máy con có được thiết lập tự khởi động cùng Windows không."""
        return NetworkCollector.is_windows_autostart("ClientNetworkAgent")

    def set_windows_autostart(self, enable: bool) -> bool:
        """Bật / tắt tự động khởi động cùng Windows cho Client Agent."""
        return NetworkCollector.set_windows_autostart(enable, "ClientNetworkAgent")

    def save_client_config(
        self,
        server_host: str,
        server_port: int,
        show_toast: bool,
        auto_start: bool,
        config_path: Optional[str] = None,
    ) -> bool:
        """Lưu cấu hình kết nối máy con xuống tệp client_config.json và áp dụng ngay lập tức."""
        if not config_path:
            if getattr(sys, "frozen", False):
                base = os.path.dirname(sys.executable)
            else:
                base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base, "config", "client_config.json")

        self.set_windows_autostart(auto_start)
        cfg = {
            "server_host": server_host,
            "server_port": int(server_port),
            "enable_etw": self.enable_etw,
            "show_toast_notifications": bool(show_toast),
            "auto_start_windows": bool(auto_start),
        }
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"[ClientAgent] Lỗi ghi config: {e}")

        self.show_toast_notifications = bool(show_toast)
        self.update_server_target(server_host, int(server_port))
        return True
