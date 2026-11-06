"""
Dịch vụ Agent chạy trên máy con (Client Agent Service - Application Layer):
- Quản lý định danh phần cứng (Device UUID, MAC, OS, Hostname).
- Thu thập số liệu mạng thời gian thực (psutil, ETW Kernel Consumer, TCP Connections).
- Tự động đóng gói và truyền Telemetry định kỳ lên máy chủ Admin.
- Tiếp nhận và trực tiếp thực thi các lệnh can thiệp Kernel (Firewall, QoS Throttling) qua WinDivertPacketShaper.
"""

import os
import sys
import time
import uuid
import platform
import threading
from typing import Dict, List, Optional, Tuple, Callable, Any

import psutil

from config.settings import (
    SERVER_DEFAULT_HOST,
    SERVER_DEFAULT_PORT,
    UPDATE_INTERVAL,
)
from models.network_protocol import (
    CommandAction,
    DeviceRegistrationData,
    SpeedMetricsData,
    ProcessMetricData,
    TelemetryPacket,
    CommandPacket,
    CommandResultPacket,
)
from models.network_sample import RawNetworkSample, BandwidthSpeed
from infrastructure.network_collector import NetworkCollector
from infrastructure.packet_shaper import WinDivertPacketShaper
from infrastructure.etw_consumer import ETWNetworkConsumer
from infrastructure.network_client import NetworkClient
from application.bandwidth_calculator import BandwidthCalculator
from utils.safe_print import safe_print as print


class ClientAgentService:
    """Tầng nghiệp vụ trung tâm của máy con (Agent Node)."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = SERVER_DEFAULT_PORT,
        update_interval: float = UPDATE_INTERVAL,
        enable_etw: bool = True,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.update_interval = update_interval
        self.enable_etw = enable_etw

        # 1. Khởi tạo các module hạ tầng kỹ thuật
        self.collector = NetworkCollector()
        self.calculator = BandwidthCalculator()
        self.packet_shaper = WinDivertPacketShaper()
        self.etw_consumer: Optional[ETWNetworkConsumer] = None

        if self.enable_etw:
            try:
                self.etw_consumer = ETWNetworkConsumer()
                self.etw_consumer.start()
            except Exception as e:
                print(f"[ClientAgent Warning] Không thể khởi động ETW Consumer: {e}")
                self.etw_consumer = None

        # 2. Định danh máy cố định (Đồng bộ với DatabaseManager.get_or_create_device)
        self.device_uuid, self.registration_data = self._generate_device_info()

        # 3. Quản lý trạng thái và kết nối mạng
        self._is_running = threading.Event()
        self._telemetry_thread: Optional[threading.Thread] = None
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()
        self.total_tx_bytes: int = 0
        self.total_rx_bytes: int = 0
        self.proc_info_cache: Dict[int, Tuple[str, str]] = {}
        self.active_policies: Dict[str, Dict] = {}  # {app_lower: {action, app, limit_kbps, direction, time}}

        # Callback thông báo cho GUI khay hệ thống (System Tray Notification)
        # on_policy_applied_locally(action, app_name, status, limit_kbps)
        self.on_policy_applied_locally: Optional[Any] = None

        self.show_toast_notifications: bool = True
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

        self.net_client = NetworkClient(
            server_host=self.server_host,
            server_port=self.server_port,
            reg_data=self.registration_data,
        )
        self.net_client.on_command_received = self.handle_remote_command
        self.net_client.on_connected = self._handle_client_connected
        self.net_client.on_disconnected = self._handle_client_disconnected

    def _handle_client_connected(self):
        if self.on_connected:
            try:
                self.on_connected()
            except Exception as e:
                print(f"[ClientAgent] on_connected callback error: {e}")

    def _handle_client_disconnected(self):
        if self.on_disconnected:
            try:
                self.on_disconnected()
            except Exception as e:
                print(f"[ClientAgent] on_disconnected callback error: {e}")


    def _generate_device_info(self) -> Tuple[str, DeviceRegistrationData]:
        """Tạo UUID phần cứng ổn định và thu thập thông tin hệ thống của máy con."""
        hostname = platform.node() or "unknown-host"
        os_name = platform.system() or "UnknownOS"
        os_version = platform.version() or "unknown"
        mac_num = uuid.getnode()
        mac_str = ":".join(f"{(mac_num >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))
        stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{hostname}-{mac_num}"))

        # Tìm IP LAN thực tế
        local_ip = "127.0.0.1"
        try:
            for iface_name, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family == psutil.AF_LINK or not hasattr(addr, "address"):
                        continue
                    # Bỏ qua loopback
                    if addr.address.startswith("127.") or addr.address == "::1":
                        continue
                    if "." in addr.address:  # IPv4
                        local_ip = addr.address
                        break
                if local_ip != "127.0.0.1":
                    break
        except Exception:
            pass

        reg_data = DeviceRegistrationData(
            device_uuid=stable_uuid,
            hostname=hostname,
            os_name=os_name,
            os_version=os_version,
            agent_version="1.0.0",
            mac_address=mac_str,
            ip_address=local_ip,
        )
        return stable_uuid, reg_data

    def start(self):
        """Khởi động toàn bộ dịch vụ Agent và bắt đầu gửi Telemetry."""
        if self._is_running.is_set():
            return
        self._is_running.set()

        # Bắt đầu kết nối mạng Client
        self.net_client.start()

        # Bắt đầu luồng đo lường và phát Telemetry
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_loop, daemon=True, name="ClientAgent-Telemetry"
        )
        self._telemetry_thread.start()

        print(f"[ClientAgent] Agent Service đã khởi chạy thành công cho máy '{self.registration_data.hostname}' (UUID: {self.device_uuid})")

    def stop(self):
        """Dừng Agent Service và giải phóng toàn bộ tài nguyên."""
        self._is_running.clear()

        # Dừng mạng
        self.net_client.stop()

        # Dừng ETW Consumer
        if self.etw_consumer:
            try:
                self.etw_consumer.stop()
            except Exception:
                pass
            self.etw_consumer = None

        # Gỡ bỏ giới hạn bóp băng thông hoặc chặn mạng nếu đang áp dụng
        try:
            self.packet_shaper.unblock_all()
            self.packet_shaper.remove_limit()
        except Exception:
            pass

        print("[ClientAgent] Agent Service đã dừng an toàn.")

    def _telemetry_loop(self):
        """Vòng lặp thu thập telemetry định kỳ và đẩy lên Server."""
        while self._is_running.is_set():
            time.sleep(self.update_interval)
            if not self._is_running.is_set():
                break

            try:
                curr_sample = self.collector.collect_io_counters()
                speed: BandwidthSpeed = self.calculator.calculate_speed(self.prev_sample, curr_sample)
                time_delta = max(0.1, curr_sample.timestamp - self.prev_sample.timestamp)
                self.prev_sample = curr_sample

                # Tích lũy dung lượng tổng
                self.total_tx_bytes = curr_sample.bytes_sent
                self.total_rx_bytes = curr_sample.bytes_recv

                # 1. Thu thập tiến trình mạng từ ETW Consumer
                top_procs: List[ProcessMetricData] = []
                if self.etw_consumer:
                    try:
                        etw_samples = self.etw_consumer.flush_and_aggregate()
                        if etw_samples:
                            for pid, b_info in etw_samples.items():
                                rx = b_info.get("rx_bytes", 0)
                                tx = b_info.get("tx_bytes", 0)
                                down_kb = rx / (time_delta * 1024.0)
                                up_kb = tx / (time_delta * 1024.0)

                                if pid in self.proc_info_cache:
                                    p_name, _ = self.proc_info_cache[pid]
                                else:
                                    p_info = self.collector.get_process_info(pid)
                                    p_name = p_info.name if p_info else f"PID {pid}"
                                    p_exe = p_info.executable_path if p_info and p_info.executable_path else ""
                                    self.proc_info_cache[pid] = (p_name, p_exe)

                                top_procs.append(
                                    ProcessMetricData(
                                        name=p_name,
                                        pid=pid,
                                        down_kbps=round(down_kb, 2),
                                        up_kbps=round(up_kb, 2),
                                        connections_count=0,
                                    )
                                )
                    except Exception as e:
                        print(f"[ClientAgent ETW Loop Warning] {e}")

                # Sắp xếp top tiến trình tiêu thụ nhiều nhất
                top_procs.sort(key=lambda p: (p.down_kbps + p.up_kbps), reverse=True)
                top_procs = top_procs[:15]  # Lấy 15 tiến trình cao nhất để gói tin gọn nhẹ

                # 2. Thu thập CPU, RAM
                cpu_pct = psutil.cpu_percent(interval=None)
                ram_pct = psutil.virtual_memory().percent

                # 3. Thu thập số lượng Socket kết nối
                try:
                    active_conns = self.collector.collect_tcp_connections()
                    active_conns_count = len(active_conns)
                except Exception:
                    active_conns_count = 0

                # 4. Đóng gói TelemetryPacket
                telem = TelemetryPacket(
                    device_uuid=self.device_uuid,
                    timestamp=time.time(),
                    speeds=SpeedMetricsData(
                        upload_kbps=round(speed.upload_speed_kb, 2),
                        download_kbps=round(speed.download_speed_kb, 2),
                        total_up_bytes=self.total_tx_bytes,
                        total_down_bytes=self.total_rx_bytes,
                    ),
                    system_stats={
                        "cpu_percent": float(cpu_pct),
                        "ram_percent": float(ram_pct),
                    },
                    top_processes=top_procs,
                    active_conns_count=active_conns_count,
                )

                # 5. Gửi lên Server nếu đang kết nối
                if self.net_client.is_connected():
                    self.net_client.send_telemetry(telem)

            except Exception as e:
                print(f"[ClientAgent Telemetry Error] {e}")

    def handle_remote_command(self, cmd: CommandPacket) -> Optional[CommandResultPacket]:
        """
        Xử lý lệnh can thiệp từ xa do Admin gửi tới:
        - BLOCK_APP: Chặn hoàn toàn qua Windows Defender Firewall & WinDivert Dropper
        - UNBLOCK_APP: Gỡ chặn cho ứng dụng
        - UNBLOCK_ALL: Gỡ bỏ chặn toàn bộ
        - THROTTLE_SET: Bóp băng thông theo hạn mức KB/s bằng Token Bucket
        - REMOVE_LIMIT: Gỡ bỏ giới hạn băng thông
        - PING: Phản hồi độ trễ
        """
        action = cmd.action
        app_name = cmd.target_app.strip()
        direction = cmd.direction or "both"
        limit_kbps = cmd.limit_kbps

        print(f"[ClientAgent Command Executing] Hành động: '{action}' | App: '{app_name}' | Limit: {limit_kbps} KB/s")

        success = False
        message = ""

        try:
            if action == CommandAction.BLOCK_APP.value:
                if not app_name:
                    success, message = False, "Tên ứng dụng cần chặn không được để trống."
                else:
                    success, message = self.packet_shaper.block_app(app_name, direction=direction)

            elif action == CommandAction.UNBLOCK_APP.value:
                if not app_name:
                    success, message = False, "Tên ứng dụng cần gỡ chặn không được để trống."
                else:
                    success, message = self.packet_shaper.unblock_app(app_name)

            elif action == CommandAction.UNBLOCK_ALL.value:
                success, message = self.packet_shaper.unblock_all()

            elif action == CommandAction.THROTTLE_SET.value:
                if not app_name:
                    success, message = False, "Tên ứng dụng cần bóp băng thông không được để trống."
                elif limit_kbps <= 0:
                    success, message = False, "Hạn mức băng thông phải lớn hơn 0 KB/s."
                else:
                    success, message = self.packet_shaper.apply_limit(app_name, limit_kbps=limit_kbps, direction=direction)

            elif action == CommandAction.REMOVE_LIMIT.value:
                success, message = self.packet_shaper.remove_limit()

            elif action == CommandAction.PING.value:
                success, message = True, f"Pong from Client '{self.registration_data.hostname}' at {time.strftime('%H:%M:%S')}"

            else:
                success, message = False, f"Hành động không xác định: {action}"

        except Exception as e:
            success, message = False, f"Ngoại lệ khi thực thi lệnh: {e}"

        status_str = "SUCCESS" if success else "FAILED"
        print(f"[ClientAgent Command Finished] {cmd.command_id} -> {status_str}: {message}")

        # Cập nhật danh sách chính sách hiện hành nếu thực thi thành công
        if success:
            app_key = app_name.lower()
            if action == CommandAction.BLOCK_APP.value:
                self.active_policies[app_key] = {
                    "action": "BLOCK",
                    "app": app_name,
                    "limit_kbps": 0.0,
                    "direction": direction,
                    "time": time.time(),
                }
            elif action == CommandAction.THROTTLE_SET.value:
                self.active_policies[app_key] = {
                    "action": "THROTTLE",
                    "app": app_name,
                    "limit_kbps": limit_kbps,
                    "direction": direction,
                    "time": time.time(),
                }
            elif action == CommandAction.UNBLOCK_APP.value:
                self.active_policies.pop(app_key, None)
            elif action == CommandAction.UNBLOCK_ALL.value:
                self.active_policies.clear()
            elif action == CommandAction.REMOVE_LIMIT.value:
                if app_key:
                    self.active_policies.pop(app_key, None)
                else:
                    self.active_policies.clear()

            # Bắn tín hiệu để hiển thị Toast thông báo trên màn hình máy con
            if self.on_policy_applied_locally:
                try:
                    self.on_policy_applied_locally(action, app_name, status_str, limit_kbps)
                except Exception as ex:
                    print(f"[ClientAgent Toast Error] {ex}")

        return CommandResultPacket(
            command_id=cmd.command_id,
            device_uuid=self.device_uuid,
            status=status_str,
            message=message,
            applied_at=time.time(),
        )

    def update_server_target(self, new_host: str, new_port: int):
        """Đổi địa chỉ Server mục tiêu và tự động kết nối lại."""
        print(f"[ClientAgent] Chuyển đổi kết nối tới Server mới: {new_host}:{new_port}")
        self.server_host = new_host
        self.server_port = new_port

        self.net_client.stop()
        self.net_client = NetworkClient(
            server_host=self.server_host,
            server_port=self.server_port,
            reg_data=self.registration_data,
        )
        self.net_client.on_command_received = self.handle_remote_command
        self.net_client.on_connected = self._handle_client_connected
        self.net_client.on_disconnected = self._handle_client_disconnected
        if self._is_running.is_set():
            self.net_client.start()

