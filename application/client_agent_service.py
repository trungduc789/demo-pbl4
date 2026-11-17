"""
Dịch vụ Agent chạy trên máy con (Client Agent Service - Application Layer):
- Quản lý định danh phần cứng (Device UUID, MAC, OS, Hostname) qua tầng Infrastructure.
- Thu thập số liệu mạng thời gian thực và truyền Telemetry định kỳ lên máy chủ Admin.
- Kế thừa `ClientPolicyMixin` (Chính sách 2 lớp & Audit Log) và `ClientDiagnosticsMixin` (Chẩn đoán mạng & Cấu hình).
"""

import time
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.settings import (
    SERVER_DEFAULT_PORT,
    UPDATE_INTERVAL,
)
from models.network_protocol import (
    DeviceRegistrationData,
    SpeedMetricsData,
    ProcessMetricData,
    TelemetryPacket,
)
from models.network_sample import RawNetworkSample, BandwidthSpeed
from infrastructure.network_collector import NetworkCollector
from infrastructure.packet_shaper import WinDivertPacketShaper
from infrastructure.etw_consumer import ETWNetworkConsumer
from infrastructure.network_client import NetworkClient
from application.bandwidth_calculator import BandwidthCalculator
from application.client_policy_manager import ClientPolicyMixin
from application.client_diagnostics import ClientDiagnosticsMixin
from utils.safe_print import safe_print as print


class ClientAgentService(ClientPolicyMixin, ClientDiagnosticsMixin):
    """Tầng nghiệp vụ trung tâm của máy con (Agent Node)."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = SERVER_DEFAULT_PORT,
        update_interval: float = UPDATE_INTERVAL,
        enable_etw: bool = True,
        monitor_service: Optional[Any] = None,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.update_interval = update_interval
        self.enable_etw = enable_etw
        self.monitor_service = monitor_service

        # 1. Khởi tạo các module hạ tầng kỹ thuật (hoặc dùng chung từ monitor_service)
        if self.monitor_service is not None:
            self.collector = getattr(self.monitor_service, "collector", NetworkCollector())
            self.calculator = getattr(self.monitor_service, "calculator", BandwidthCalculator())
            self.packet_shaper = getattr(self.monitor_service, "packet_shaper", WinDivertPacketShaper())
            self.etw_consumer = getattr(self.monitor_service, "etw_consumer", None)
        else:
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

        # 2. Định danh máy cố định
        self.device_uuid, self.registration_data = self._generate_device_info()

        # 3. Quản lý trạng thái và kết nối mạng
        self._is_running = threading.Event()
        self._telemetry_thread: Optional[threading.Thread] = None
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()
        self.total_tx_bytes: int = 0
        self.total_rx_bytes: int = 0
        self.proc_info_cache: Dict[int, Tuple[str, str]] = {}
        self.active_policies: Dict[str, Dict] = {}
        self.latest_down_kbps: float = 0.0
        self.latest_up_kbps: float = 0.0
        self.latest_cpu_pct: float = 0.0
        self.latest_ram_pct: float = 0.0
        self.latest_top_procs: List[ProcessMetricData] = []

        # Bộ đệm kết quả đo độ trễ bất đồng bộ
        self.cached_server_latency: Optional[float] = None
        self.cached_server_ok: bool = False
        self.cached_server_msg: str = "Đang đo..."
        self.cached_internet_latency: Optional[float] = None
        self.cached_internet_ok: bool = False
        self.cached_internet_msg: str = "Đang đo..."
        self._latency_probe_busy: bool = False

        # 4. Nhật ký kiểm toán thao tác từ Admin & Client
        self._audit_lock = threading.Lock()
        self.audit_logs: List[Dict[str, Any]] = []
        self._init_initial_audit_log()

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
        self.add_audit_log(
            source="Client Agent",
            action="CONNECT",
            target=f"{self.server_host}:{self.server_port}",
            status="SUCCESS",
            details=f"Kết nối thành công tới Admin Server ({self.server_host}:{self.server_port})",
        )
        if self.on_connected:
            try:
                self.on_connected()
            except Exception as e:
                print(f"[ClientAgent] on_connected callback error: {e}")

    def _handle_client_disconnected(self):
        self.add_audit_log(
            source="Client Agent",
            action="DISCONNECT",
            target=f"{self.server_host}:{self.server_port}",
            status="INFO",
            details=f"Mất kết nối với Admin Server ({self.server_host}:{self.server_port}), đang tự động kết nối lại...",
        )
        if self.on_disconnected:
            try:
                self.on_disconnected()
            except Exception as e:
                print(f"[ClientAgent] on_disconnected callback error: {e}")

    def _generate_device_info(self) -> Tuple[str, DeviceRegistrationData]:
        """Tạo UUID phần cứng ổn định và thu thập thông tin hệ thống của máy con thông qua tầng Infrastructure."""
        return self._get_infra_collector().generate_device_info(agent_version="1.0.0")

    def start(self):
        """Khởi động toàn bộ dịch vụ Agent và bắt đầu gửi Telemetry."""
        if self._is_running.is_set():
            return
        self._is_running.set()
        self.net_client.start()
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_loop, daemon=True, name="ClientAgent-Telemetry"
        )
        self._telemetry_thread.start()
        print(
            f"[ClientAgent] Agent Service đã khởi chạy thành công cho máy '{self.registration_data.hostname}' "
            f"(UUID: {self.device_uuid})"
        )

    def stop(self):
        """Dừng Agent Service và giải phóng toàn bộ tài nguyên."""
        self._is_running.clear()
        self.net_client.stop()

        if self.etw_consumer and self.monitor_service is None:
            try:
                self.etw_consumer.stop()
            except Exception:
                pass
            self.etw_consumer = None

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

                self.total_tx_bytes = curr_sample.bytes_sent
                self.total_rx_bytes = curr_sample.bytes_recv

                top_procs: List[ProcessMetricData] = []
                if self.monitor_service is not None:
                    latest_sp = getattr(self.monitor_service, "latest_speed", None)
                    if latest_sp is not None:
                        speed = latest_sp

                    raw_proc_speeds = getattr(self.monitor_service, "latest_proc_speeds", [])
                    for p in raw_proc_speeds[:15]:
                        top_procs.append(
                            ProcessMetricData(
                                name=p.get("name", "Unknown"),
                                pid=p.get("pid", 0),
                                down_kbps=round(float(p.get("down_kb", 0.0)), 2),
                                up_kbps=round(float(p.get("up_kb", 0.0)), 2),
                                connections_count=0,
                            )
                        )
                elif self.etw_consumer:
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

                top_procs.sort(key=lambda p: (p.down_kbps + p.up_kbps), reverse=True)
                top_procs = top_procs[:15]

                sys_stats = self._get_infra_collector().collect_system_stats()
                cpu_pct = sys_stats.get("cpu_percent", 0.0)
                ram_pct = sys_stats.get("ram_percent", 0.0)

                try:
                    active_conns = self.collector.collect_tcp_connections()
                    active_conns_count = len(active_conns)
                except Exception:
                    active_conns_count = 0

                self.latest_down_kbps = round(speed.download_speed_kb, 2)
                self.latest_up_kbps = round(speed.upload_speed_kb, 2)
                self.latest_cpu_pct = float(cpu_pct)
                self.latest_ram_pct = float(ram_pct)
                self.latest_top_procs = top_procs

                telem = TelemetryPacket(
                    device_uuid=self.device_uuid,
                    timestamp=time.time(),
                    speeds=SpeedMetricsData(
                        upload_kbps=round(speed.upload_speed_kb, 2),
                        download_kbps=round(speed.download_speed_kb, 2),
                        total_up_bytes=self.total_tx_bytes,
                        total_down_bytes=self.total_rx_bytes,
                    ),
                    system_stats={"cpu_percent": float(cpu_pct), "ram_percent": float(ram_pct)},
                    top_processes=top_procs,
                    active_conns_count=active_conns_count,
                )

                if self.net_client.is_connected():
                    self.net_client.send_telemetry(telem)

            except Exception as e:
                print(f"[ClientAgent Telemetry Error] {e}")

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
