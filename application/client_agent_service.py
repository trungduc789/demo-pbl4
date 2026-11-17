"""
Dịch vụ Agent chạy trên máy con (Client Agent Service - Application Layer):
- Quản lý định danh phần cứng (Device UUID, MAC, OS, Hostname).
- Thu thập số liệu mạng thời gian thực (psutil, ETW Kernel Consumer, TCP Connections).
- Tự động đóng gói và truyền Telemetry định kỳ lên máy chủ Admin.
- Tiếp nhận và trực tiếp thực thi các lệnh can thiệp Kernel (Firewall, QoS Throttling) qua WinDivertPacketShaper.
- Hỗ trợ cơ chế Phân tầng Chính sách 2 lớp (2-Tier Policy):
  + Lớp 1 (Admin Enforced 🔒): Do Admin Server áp đặt, khóa cứng, máy con không được tự ý gỡ bỏ.
  + Lớp 2 (Local Self-Protect 🔓): Do người dùng máy con tự thiết lập cho các tiến trình chưa bị Admin khóa.
"""

import os
import sys
import json
import time
import threading
from typing import Dict, List, Optional, Tuple, Callable, Any

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

        # 2. Định danh máy cố định (Đồng bộ với DatabaseManager.get_or_create_device)
        self.device_uuid, self.registration_data = self._generate_device_info()

        # 3. Quản lý trạng thái và kết nối mạng
        self._is_running = threading.Event()
        self._telemetry_thread: Optional[threading.Thread] = None
        self.prev_sample: RawNetworkSample = self.collector.collect_io_counters()
        self.total_tx_bytes: int = 0
        self.total_rx_bytes: int = 0
        self.proc_info_cache: Dict[int, Tuple[str, str]] = {}
        # {app_lower: {action, app, limit_kbps, direction, time, source: "ADMIN"|"LOCAL", locked_by_admin: bool}}
        self.active_policies: Dict[str, Dict] = {}
        self.latest_down_kbps: float = 0.0
        self.latest_up_kbps: float = 0.0
        self.latest_cpu_pct: float = 0.0
        self.latest_ram_pct: float = 0.0
        self.latest_top_procs: List[ProcessMetricData] = []

        # Bộ đệm kết quả đo độ trễ bất đồng bộ (không bao giờ gây block GUI thread)
        self.cached_server_latency: Optional[float] = None
        self.cached_server_ok: bool = False
        self.cached_server_msg: str = "Đang đo..."
        self.cached_internet_latency: Optional[float] = None
        self.cached_internet_ok: bool = False
        self.cached_internet_msg: str = "Đang đo..."
        self._latency_probe_busy: bool = False

        # 4. Nhật ký kiểm toán thao tác từ Admin & Client (Audit Log Buffer)
        self._audit_lock = threading.Lock()
        self.audit_logs: List[Dict[str, Any]] = []
        self._init_initial_audit_log()

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

    def _init_initial_audit_log(self):
        """Khởi tạo mục nhật ký ban đầu khi khởi động Agent."""
        self.add_audit_log(
            source="Client Agent",
            action="STARTUP",
            target=self.registration_data.hostname,
            status="SUCCESS",
            details=f"Khởi động tiến trình Agent Node thành công (UUID: {self.device_uuid[:8]}...)",
        )

    def add_audit_log(self, source: str, action: str, target: str, status: str, details: str):
        """Ghi nhận sự kiện kiểm toán nghiệp vụ an ninh vào bộ đệm."""
        with self._audit_lock:
            entry = {
                "timestamp": time.time(),
                "time_str": time.strftime("%H:%M:%S"),
                "source": source,
                "action": action,
                "target": target,
                "status": status,
                "details": details,
            }
            self.audit_logs.insert(0, entry)
            if len(self.audit_logs) > 200:
                self.audit_logs.pop()

    def get_audit_logs(self) -> List[Dict[str, Any]]:
        """Lấy danh sách nhật ký kiểm toán cho giao diện."""
        with self._audit_lock:
            return list(self.audit_logs)

    def _generate_device_info(self) -> Tuple[str, DeviceRegistrationData]:
        """Tạo UUID phần cứng ổn định và thu thập thông tin hệ thống của máy con thông qua tầng Infrastructure."""
        if isinstance(self.collector, NetworkCollector):
            return self.collector.generate_device_info(agent_version="1.0.0")
        return NetworkCollector().generate_device_info(agent_version="1.0.0")

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

        # Dừng ETW Consumer nếu là tự tạo
        if self.etw_consumer and self.monitor_service is None:
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

                # 1. Thu thập tiến trình mạng
                top_procs: List[ProcessMetricData] = []
                if self.monitor_service is not None:
                    # Tận dụng dữ liệu mẫu vừa được MonitorService thu thập mà không làm gián đoạn ETW
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

                # Sắp xếp top tiến trình tiêu thụ nhiều nhất
                top_procs.sort(key=lambda p: (p.down_kbps + p.up_kbps), reverse=True)
                top_procs = top_procs[:15]  # Lấy 15 tiến trình cao nhất để gói tin gọn nhẹ

                # 2. Thu thập CPU, RAM thông qua tầng Infrastructure
                collector_obj = self.collector if isinstance(self.collector, NetworkCollector) else NetworkCollector()
                sys_stats = collector_obj.collect_system_stats()
                cpu_pct = sys_stats.get("cpu_percent", 0.0)
                ram_pct = sys_stats.get("ram_percent", 0.0)

                # 3. Thu thập số lượng Socket kết nối
                try:
                    active_conns = self.collector.collect_tcp_connections()
                    active_conns_count = len(active_conns)
                except Exception:
                    active_conns_count = 0

                # 4. Đóng gói TelemetryPacket
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
        Xử lý lệnh can thiệp từ xa do Admin gửi tới (Lớp 1 - Admin Enforced 🔒):
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
                    existing_pol = self._find_policy_entry(app_name)
                    if existing_pol and existing_pol.get("action") == "THROTTLE":
                        try:
                            self.packet_shaper.remove_limit(app_name)
                        except TypeError:
                            self.packet_shaper.remove_limit()
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
                    existing_pol = self._find_policy_entry(app_name)
                    if existing_pol and existing_pol.get("action") == "BLOCK":
                        self.packet_shaper.unblock_app(app_name)
                    success, message = self.packet_shaper.apply_limit(app_name, limit_kbps=limit_kbps, direction=direction)

            elif action == CommandAction.REMOVE_LIMIT.value:
                try:
                    success, message = self.packet_shaper.remove_limit(app_name if app_name else None)
                except TypeError:
                    success, message = self.packet_shaper.remove_limit()

            elif action == CommandAction.PING.value:
                success, message = True, f"Pong from Client '{self.registration_data.hostname}' at {time.strftime('%H:%M:%S')}"

            else:
                success, message = False, f"Hành động không xác định: {action}"

        except Exception as e:
            success, message = False, f"Ngoại lệ khi thực thi lệnh: {e}"

        status_str = "SUCCESS" if success else "FAILED"
        print(f"[ClientAgent Command Finished] {cmd.command_id} -> {status_str}: {message}")

        # Ghi nhận sự kiện kiểm toán nghiệp vụ (Audit Log)
        audit_act = action
        if action == CommandAction.BLOCK_APP.value:
            audit_act = "BLOCK"
            audit_desc = f"Chặn hoàn toàn lưu lượng của ứng dụng '{app_name}' ({direction}) [🔒 Admin Locked]"
        elif action == CommandAction.THROTTLE_SET.value:
            audit_act = "QoS"
            audit_desc = f"Giới hạn tốc độ '{app_name}' mức {limit_kbps:.1f} KB/s ({direction}) [🔒 Admin Locked]"
        elif action == CommandAction.UNBLOCK_APP.value:
            audit_act = "UNBLOCK"
            audit_desc = f"Gỡ bỏ chặn cho ứng dụng '{app_name}'"
        elif action == CommandAction.UNBLOCK_ALL.value:
            audit_act = "UNBLOCK_ALL"
            audit_desc = "Gỡ bỏ toàn bộ danh sách ứng dụng bị chặn"
        elif action == CommandAction.REMOVE_LIMIT.value:
            audit_act = "REMOVE_LIMIT"
            audit_desc = f"Gỡ bỏ hạn mức QoS cho '{app_name or 'toàn bộ'}'"
        elif action == CommandAction.PING.value:
            audit_act = "PING"
            audit_desc = "Admin Server gửi gói tin kiểm tra kết nối (Ping)"
        else:
            audit_desc = message

        self.add_audit_log(
            source="Admin Server",
            action=audit_act,
            target=app_name or "*",
            status=status_str,
            details=audit_desc,
        )

        # Cập nhật danh sách chính sách hiện hành nếu thực thi thành công (Lớp 1: Admin Enforced 🔒)
        if success:
            app_key = app_name.lower()
            if action == CommandAction.BLOCK_APP.value:
                self._remove_policy_entry(app_key)
                self.active_policies[app_key] = {
                    "action": "BLOCK",
                    "app": app_name,
                    "limit_kbps": 0.0,
                    "direction": direction,
                    "time": time.time(),
                    "source": "ADMIN",
                    "locked_by_admin": True,
                }
            elif action == CommandAction.THROTTLE_SET.value:
                self._remove_policy_entry(app_key)
                self.active_policies[app_key] = {
                    "action": "THROTTLE",
                    "app": app_name,
                    "limit_kbps": limit_kbps,
                    "direction": direction,
                    "time": time.time(),
                    "source": "ADMIN",
                    "locked_by_admin": True,
                }
            elif action == CommandAction.UNBLOCK_APP.value:
                self._remove_policy_entry(app_key)
            elif action == CommandAction.UNBLOCK_ALL.value:
                for k in list(self.active_policies.keys()):
                    if self.active_policies[k].get("action") == "BLOCK":
                        del self.active_policies[k]
            elif action == CommandAction.REMOVE_LIMIT.value:
                if app_key:
                    pol = self._find_policy_entry(app_key)
                    if pol and pol.get("action") == "THROTTLE":
                        self._remove_policy_entry(app_key)
                else:
                    for k in list(self.active_policies.keys()):
                        if self.active_policies[k].get("action") == "THROTTLE":
                            del self.active_policies[k]

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

    # =========================================================================
    # QUẢN LÝ CHÍNH SÁCH 2 LỚP (2-TIER POLICY: ADMIN LOCK + CLIENT SELF-PROTECT)
    # =========================================================================

    def _find_policy_entry(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Tìm chính sách đang áp dụng cho ứng dụng (khớp cả tên có/không có .exe)."""
        if not app_name:
            return None
        raw = app_name.lower().strip()
        base = raw[:-4] if raw.endswith(".exe") else raw
        for c in (raw, f"{base}.exe", base):
            if c in self.active_policies:
                return self.active_policies[c]
        return None

    def _remove_policy_entry(self, app_name: str):
        """Xóa chính sách của ứng dụng khỏi bộ nhớ đệm theo mọi biến thể tên."""
        if not app_name:
            return
        raw = app_name.lower().strip()
        base = raw[:-4] if raw.endswith(".exe") else raw
        for c in (raw, f"{base}.exe", base):
            self.active_policies.pop(c, None)

    def get_policy_for_app(self, app_name: str) -> Optional[Dict[str, Any]]:
        """Trả về thông tin chính sách hiện hành của một ứng dụng (nếu có)."""
        return self._find_policy_entry(app_name)

    def is_locked_by_admin(self, app_name: str) -> bool:
        """Kiểm tra ứng dụng có đang bị khóa cứng bởi chính sách từ Admin Server hay không."""
        pol = self._find_policy_entry(app_name)
        if not pol:
            return False
        return bool(pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN"))

    def apply_local_policy(
        self,
        app_name: str,
        action: str = "BLOCK",
        limit_kbps: float = 0.0,
        direction: str = "both",
    ) -> Tuple[bool, str]:
        """
        Người dùng tại máy con tự áp dụng chính sách bảo vệ cục bộ (Lớp 2 - Local Self-Protect):
        - Được phép Chặn hoặc Giới hạn băng thông các tiến trình trên máy mình.
        - KHÔNG được phép ghi đè hoặc nới lỏng nếu tiến trình đó đang bị Admin Server khóa (🔒 Admin Locked).
        """
        app_name = app_name.strip()
        if not app_name:
            return False, "Tên ứng dụng không được để trống."

        if self.is_locked_by_admin(app_name):
            msg = (
                f"Từ chối thao tác: Ứng dụng '{app_name}' đang chịu sự kiểm soát bắt buộc từ Admin Server (🔒 Admin Locked). "
                "Máy con không được phép thay đổi hoặc ghi đè chính sách của Quản trị viên!"
            )
            self.add_audit_log(
                source="Local User",
                action=f"DENIED_{action.upper()}",
                target=app_name,
                status="DENIED",
                details=msg,
            )
            return False, msg

        act_upper = action.upper()
        existing_local = self._find_policy_entry(app_name)
        if act_upper == "BLOCK":
            if existing_local and existing_local.get("action") == "THROTTLE":
                try:
                    self.packet_shaper.remove_limit(app_name)
                except TypeError:
                    self.packet_shaper.remove_limit()
            ok, msg = self.packet_shaper.block_app(app_name, direction=direction)
            if ok:
                self._remove_policy_entry(app_name)
                self.active_policies[app_name.lower()] = {
                    "action": "BLOCK",
                    "app": app_name,
                    "limit_kbps": 0.0,
                    "direction": direction,
                    "time": time.time(),
                    "source": "LOCAL",
                    "locked_by_admin": False,
                }
                self.add_audit_log(
                    source="Local User",
                    action="LOCAL_BLOCK",
                    target=app_name,
                    status="SUCCESS",
                    details=f"Người dùng máy con chủ động chặn cục bộ '{app_name}' ({direction})",
                )
                if self.on_policy_applied_locally:
                    try:
                        self.on_policy_applied_locally("LOCAL_BLOCK", app_name, "SUCCESS", 0.0)
                    except Exception:
                        pass
            return ok, msg

        elif act_upper in ("THROTTLE", "QOS"):
            if limit_kbps <= 0:
                return False, "Hạn mức băng thông phải lớn hơn 0 KB/s."
            if existing_local and existing_local.get("action") == "BLOCK":
                self.packet_shaper.unblock_app(app_name)
            ok, msg = self.packet_shaper.apply_limit(app_name, limit_kbps=limit_kbps, direction=direction)
            if ok:
                self._remove_policy_entry(app_name)
                self.active_policies[app_name.lower()] = {
                    "action": "THROTTLE",
                    "app": app_name,
                    "limit_kbps": float(limit_kbps),
                    "direction": direction,
                    "time": time.time(),
                    "source": "LOCAL",
                    "locked_by_admin": False,
                }
                self.add_audit_log(
                    source="Local User",
                    action="LOCAL_QoS",
                    target=app_name,
                    status="SUCCESS",
                    details=f"Người dùng máy con tự giới hạn tốc độ '{app_name}' mức {limit_kbps:.1f} KB/s ({direction})",
                )
                if self.on_policy_applied_locally:
                    try:
                        self.on_policy_applied_locally("LOCAL_THROTTLE", app_name, "SUCCESS", float(limit_kbps))
                    except Exception:
                        pass
            return ok, msg

        return False, f"Hành động cục bộ không hợp lệ: {action}"

    def remove_local_policy(self, app_name: str) -> Tuple[bool, str]:
        """
        Người dùng tại máy con tự gỡ bỏ chính sách cục bộ do chính họ tạo:
        - Nếu chính sách thuộc Lớp 1 (Admin Server) -> Từ chối tuyệt đối và ghi Audit Log.
        - Nếu chính sách thuộc Lớp 2 (Local User) -> Cho phép gỡ bỏ.
        """
        app_name = app_name.strip()
        if not app_name:
            return False, "Tên ứng dụng không hợp lệ."

        if self.is_locked_by_admin(app_name):
            msg = (
                f"Từ chối gỡ bỏ: Chính sách đối với '{app_name}' được áp đặt bởi Admin Server (🔒 Admin Locked). "
                "Chỉ Quản trị viên mới có quyền mở khóa ứng dụng này!"
            )
            self.add_audit_log(
                source="Local User",
                action="DENIED_UNBLOCK",
                target=app_name,
                status="DENIED",
                details=msg,
            )
            return False, msg

        pol = self._find_policy_entry(app_name)
        if pol and pol.get("action") == "THROTTLE":
            try:
                ok, msg = self.packet_shaper.remove_limit(app_name)
            except TypeError:
                ok, msg = self.packet_shaper.remove_limit()
        else:
            ok, msg = self.packet_shaper.unblock_app(app_name)

        self._remove_policy_entry(app_name)
        self.add_audit_log(
            source="Local User",
            action="LOCAL_UNBLOCK",
            target=app_name,
            status="SUCCESS" if ok else "FAILED",
            details=f"Người dùng máy con đã gỡ bỏ chính sách tự vệ cục bộ cho '{app_name}'",
        )
        if self.on_policy_applied_locally:
            try:
                self.on_policy_applied_locally("LOCAL_UNBLOCK", app_name, "SUCCESS" if ok else "FAILED", 0.0)
            except Exception:
                pass
        return ok, msg

    def clear_all_local_policies(self) -> Tuple[int, int]:
        """
        Chỉ gỡ bỏ các chính sách cục bộ do máy con tự tạo (LOCAL),
        giữ nguyên 100% các chính sách bắt buộc từ Admin Server (ADMIN).
        Trả về: (số_lượng_đã_gỡ, số_lượng_admin_giữ_nguyên)
        """
        removed = 0
        kept_admin = 0
        for key, pol in list(self.active_policies.items()):
            if pol.get("locked_by_admin", pol.get("source", "ADMIN") == "ADMIN"):
                kept_admin += 1
                continue
            app_name = pol.get("app", key)
            if pol.get("action") == "THROTTLE":
                try:
                    self.packet_shaper.remove_limit(app_name)
                except TypeError:
                    self.packet_shaper.remove_limit()
            else:
                self.packet_shaper.unblock_app(app_name)
            del self.active_policies[key]
            removed += 1

        if removed > 0:
            self.add_audit_log(
                source="Local User",
                action="LOCAL_CLEAR_ALL",
                target="*",
                status="SUCCESS",
                details=f"Đã gỡ {removed} chính sách cục bộ (Giữ nguyên {kept_admin} chính sách khóa bởi Admin).",
            )
            if self.on_policy_applied_locally:
                try:
                    self.on_policy_applied_locally("LOCAL_CLEAR_ALL", "*", "SUCCESS", 0.0)
                except Exception:
                    pass
        return removed, kept_admin

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

    # =========================================================================
    # CÁC DỊCH VỤ CHẨN ĐOÁN MẠNG (NETWORK DIAGNOSTICS & SYSTEM HELPERS)
    # =========================================================================

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

    def _get_infra_collector(self) -> NetworkCollector:
        if isinstance(self.collector, NetworkCollector):
            return self.collector
        return NetworkCollector()

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

    def check_internet_latency(self, target_host: str = "8.8.8.8", target_port: int = 53, timeout: float = 1.0) -> Tuple[Optional[float], bool, str]:
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

        # 1. Driver WinDivert
        shaper_active = getattr(self.packet_shaper, "is_active", False)
        if shaper_active:
            results.append("✓ [Driver] WinDivert Packet Filtering: ĐANG HOẠT ĐỘNG (Bảo vệ tích cực)")
        else:
            results.append("✓ [Driver] WinDivert Packet Filtering: SẴN SÀNG (Standby)")

        # 2. ETW Consumer
        etw_real = bool(
            self.etw_consumer
            and hasattr(self.etw_consumer, 'is_etw_active')
            and self.etw_consumer.is_etw_active()
        )
        if etw_real:
            results.append("✓ [ETW] Kernel Event Tracing Consumer: ĐANG THU THẬP BĂNG THÔNG")
        elif self.etw_consumer and (getattr(self.etw_consumer, 'is_running', False) or (hasattr(self.etw_consumer, 'is_alive') and self.etw_consumer.is_alive())):
            results.append("⚠ [ETW] Thiếu quyền Administrator — tạm tắt đo lưu lượng theo từng tiến trình (chỉ đo tổng card mạng)")
        else:
            results.append("ℹ [ETW] Chưa bật Kernel ETW (chỉ đo tổng card mạng)")

        # 3. Kết nối Admin Server
        srv_lat, srv_ok, srv_msg = self.check_server_latency()
        if srv_ok:
            results.append(f"✓ [Server] Máy chủ {self.server_host}:{self.server_port} -> Phản hồi {srv_lat} ms ({srv_msg})")
        else:
            results.append(f"⚠ [Server] Không kết nối được {self.server_host}:{self.server_port} ({srv_msg})")

        # 4. Kết nối Internet Gateway
        net_lat, net_ok, net_msg = self.check_internet_latency()
        if net_ok:
            results.append(f"✓ [Internet] Cổng DNS 8.8.8.8 -> Phản hồi {net_lat} ms ({net_msg})")
        else:
            results.append(f"ℹ [Internet] Kết nối mạng ngoài: {net_msg}")

        # 5. Tổng kết
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
