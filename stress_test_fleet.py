"""
Kịch bản Mô phỏng & Kiểm thử Tải Quy mô lớn với Dữ liệu Sát Thực Tế (Realistic Fleet Load & Stress Simulator):
- Giả lập 50 - 150+ máy trạm thực tế thuộc 5 phân khu (Phòng Lab CNTT, Máy trạm AI/Dev, Studio Thiết kế, Gaming/Tải nặng, Văn phòng).
- Mỗi máy chạy 10 - 14 tiến trình Windows thực tế (chrome.exe, Code.exe, Teams.exe, steam.exe, idm.exe, Docker Desktop.exe, zalo.exe, svchost.exe...)
  với đường cong lưu lượng mượt theo thời gian (Organic Sine + Burst Envelope) thay vì nhiễu ngẫu nhiên.
- Tích hợp Bộ Thực Thi Chính Sách Ảo (Stateful Policy Engine) trên từng máy trạm:
  + Khi Admin bấm CHẶN (BLOCK_APP) trên giao diện: Tiến trình đó trên máy ảo lập tức tụt về 0 KB/s (cả trên đồ thị và bảng Protect).
  + Khi Admin bấm GIỚI HẠN BĂNG THÔNG (THROTTLE_SET): Tiến trình đó lập tức bị ghìm tốc độ đúng bằng mức trần QoS (Token Bucket).
  + Khi Admin bấm GỠ CHẶN / GỠ GIỚI HẠN (UNBLOCK_APP / REMOVE_LIMIT): Lưu lượng tự nhiên phục hồi.

Cách sử dụng:
    python stress_test_fleet.py --port 9988 --clients 60 --interval 1.0
"""

import argparse
import math
import os
import random
import sys
import threading
import time
from typing import Dict, List, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from config.settings import SERVER_DEFAULT_PORT
from infrastructure.network_client import NetworkClient
from models.network_protocol import (
    CommandAction,
    CommandPacket,
    CommandResultPacket,
    DeviceRegistrationData,
    ProcessMetricData,
    SpeedMetricsData,
    TelemetryPacket,
)
from utils.safe_print import safe_print as print


# ============================================================================
# DANH MỤC HỒ SƠ MÁY TRẠM & TIẾN TRÌNH WINDOWS THỰC TẾ
# ============================================================================
# Định dạng mỗi tiến trình mẫu: (proc_name, base_down_kb, peak_down_kb, base_up_kb, peak_up_kb, base_conns)
PERSONA_PROFILES = {
    "LAB-CNTT": {
        "os_choices": [("Windows 11 Pro", "23H2 (22631.4317)"), ("Windows 10 Pro", "22H2 (19045.5011)")],
        "subnet": "192.168.10",
        "apps": [
            ("chrome.exe", 45.0, 1450.0, 12.0, 220.0, 18),
            ("Code.exe", 8.0, 320.0, 4.0, 95.0, 8),
            ("python.exe", 5.0, 680.0, 2.0, 140.0, 6),
            ("Teams.exe", 65.0, 380.0, 55.0, 310.0, 12),
            ("Discord.exe", 25.0, 240.0, 18.0, 120.0, 7),
            ("zalo.exe", 10.0, 180.0, 8.0, 150.0, 5),
            ("git.exe", 0.0, 450.0, 0.0, 380.0, 3),
            ("OneDrive.exe", 4.0, 290.0, 10.0, 420.0, 6),
            ("SearchApp.exe", 1.0, 25.0, 0.5, 12.0, 2),
            ("svchost.exe", 2.0, 65.0, 1.5, 30.0, 14),
            ("explorer.exe", 0.5, 15.0, 0.2, 8.0, 3),
        ],
    },
    "AI-WORKSTATION": {
        "os_choices": [("Windows 11 Enterprise", "23H2 (22631.4460)"), ("Windows 11 Pro for Workstations", "24H2 (26100.2033)")],
        "subnet": "192.168.20",
        "apps": [
            ("Docker Desktop.exe", 80.0, 3200.0, 30.0, 850.0, 28),
            ("python.exe", 120.0, 4500.0, 25.0, 620.0, 16),
            ("chrome.exe", 60.0, 1800.0, 15.0, 260.0, 24),
            ("Code.exe", 15.0, 420.0, 8.0, 180.0, 10),
            ("node.exe", 10.0, 540.0, 12.0, 310.0, 9),
            ("mysqld.exe", 18.0, 390.0, 22.0, 410.0, 15),
            ("wslservice.exe", 12.0, 600.0, 6.0, 210.0, 8),
            ("OneDrive.exe", 5.0, 350.0, 15.0, 680.0, 7),
            ("AnyDesk.exe", 20.0, 280.0, 35.0, 450.0, 4),
            ("svchost.exe", 3.0, 85.0, 2.0, 40.0, 18),
        ],
    },
    "DESIGN-MEDIA": {
        "os_choices": [("Windows 11 Pro", "23H2 (22631.4317)")],
        "subnet": "192.168.30",
        "apps": [
            ("idm.exe", 250.0, 6400.0, 15.0, 180.0, 32),
            ("chrome.exe", 180.0, 3800.0, 25.0, 340.0, 26),
            ("Spotify.exe", 35.0, 320.0, 3.0, 25.0, 6),
            ("GoogleDriveFS.exe", 40.0, 1600.0, 90.0, 2400.0, 14),
            ("Photoshop.exe", 5.0, 140.0, 2.0, 85.0, 4),
            ("PremierePro.exe", 8.0, 210.0, 4.0, 120.0, 5),
            ("zalo.exe", 15.0, 340.0, 18.0, 380.0, 6),
            ("msedge.exe", 20.0, 620.0, 6.0, 95.0, 10),
            ("svchost.exe", 2.5, 70.0, 1.5, 35.0, 12),
            ("explorer.exe", 1.0, 20.0, 0.5, 10.0, 3),
        ],
    },
    "GAMING-HIEND": {
        "os_choices": [("Windows 11 Pro", "23H2 (22631.4317)"), ("Windows 10 Pro", "22H2 (19045.5011)")],
        "subnet": "192.168.40",
        "apps": [
            ("steam.exe", 150.0, 5800.0, 12.0, 160.0, 28),
            ("qBittorrent.exe", 120.0, 4200.0, 85.0, 1900.0, 65),
            ("chrome.exe", 90.0, 2600.0, 18.0, 240.0, 20),
            ("Discord.exe", 45.0, 380.0, 35.0, 290.0, 9),
            ("OBS64.exe", 8.0, 65.0, 450.0, 2800.0, 6),
            ("Spotify.exe", 30.0, 280.0, 2.0, 20.0, 5),
            ("nvcontainer.exe", 1.0, 45.0, 0.5, 15.0, 3),
            ("svchost.exe", 3.0, 90.0, 2.0, 40.0, 15),
            ("SearchApp.exe", 1.0, 20.0, 0.5, 10.0, 2),
        ],
    },
    "VAN-PHONG": {
        "os_choices": [("Windows 10 Pro", "22H2 (19045.5011)"), ("Windows 11 Pro", "23H2 (22631.4317)")],
        "subnet": "192.168.50",
        "apps": [
            ("msedge.exe", 35.0, 980.0, 10.0, 160.0, 16),
            ("Teams.exe", 80.0, 520.0, 75.0, 480.0, 14),
            ("zalo.exe", 25.0, 420.0, 20.0, 390.0, 8),
            ("OUTLOOK.EXE", 12.0, 350.0, 8.0, 280.0, 6),
            ("OneDrive.exe", 15.0, 640.0, 25.0, 720.0, 9),
            ("EXCEL.EXE", 2.0, 85.0, 1.5, 60.0, 3),
            ("UltraViewer_Service.exe", 10.0, 240.0, 15.0, 310.0, 4),
            ("spoolsv.exe", 0.5, 35.0, 0.5, 25.0, 2),
            ("svchost.exe", 2.0, 55.0, 1.0, 25.0, 12),
            ("explorer.exe", 0.5, 12.0, 0.2, 6.0, 2),
        ],
    },
}


class RealisticVirtualMachine:
    """
    Mô phỏng một máy trạm vật lý hoàn chỉnh với:
    - Danh sách tiến trình cố định PID theo phiên làm việc.
    - Động cơ tạo sóng lưu lượng liên tục (Organic Wave + Burst State).
    - Bộ thực thi chính sách QoS / Firewall có trạng thái (Stateful Policy Enforcement).
    """

    def __init__(self, index: int, server_host: str, server_port: int):
        persona_keys = list(PERSONA_PROFILES.keys())
        self.persona_name = persona_keys[(index - 1) % len(persona_keys)]
        profile = PERSONA_PROFILES[self.persona_name]

        self.device_uuid = f"real-sim-{self.persona_name.lower()}-{index:03d}"
        self.hostname = f"{self.persona_name}-PC{index:03d}"
        os_name, os_ver = random.choice(profile["os_choices"])
        self.ip_address = f"{profile['subnet']}.{10 + (index % 235)}"
        self.mac_address = f"00:1A:2B:{(index >> 8) & 0xFF:02X}:{index & 0xFF:02X}:{(index * 7) & 0xFF:02X}"

        self.reg_data = DeviceRegistrationData(
            device_uuid=self.device_uuid,
            hostname=self.hostname,
            os_name=os_name,
            os_version=os_ver,
            agent_version="2.0.0-Enterprise",
            mac_address=self.mac_address,
            ip_address=self.ip_address,
        )

        # Khởi tạo trạng thái từng tiến trình trên máy
        self.processes: List[Dict] = []
        used_pids = set()
        for app_tuple in profile["apps"]:
            p_name, b_down, pk_down, b_up, pk_up, b_conns = app_tuple
            pid = random.randint(1100, 28000)
            while pid in used_pids:
                pid = random.randint(1100, 28000)
            used_pids.add(pid)

            self.processes.append({
                "name": p_name,
                "pid": pid,
                "base_down": b_down * random.uniform(0.75, 1.25),
                "peak_down": pk_down * random.uniform(0.8, 1.3),
                "base_up": b_up * random.uniform(0.75, 1.25),
                "peak_up": pk_up * random.uniform(0.8, 1.3),
                "base_conns": b_conns,
                # Pha dao động riêng biệt cho từng tiến trình để tạo sóng tự nhiên
                "phase": random.uniform(0.0, math.pi * 2),
                "freq": random.uniform(0.08, 0.28),
                "burst_until": 0.0,
                "burst_intensity": 0.0,
            })

        # Bộ nhớ chính sách do Admin áp đặt xuống máy này: app_name_lower -> {"action": ..., "limit_kbps": ..., "direction": ...}
        self._policy_lock = threading.Lock()
        self.active_policies: Dict[str, Dict] = {}

        # Tổng byte tích lũy như card mạng thật
        self.total_down_bytes = random.randint(50_000_000, 2_500_000_000)
        self.total_up_bytes = random.randint(10_000_000, 600_000_000)
        self.cpu_base = random.uniform(12.0, 45.0)
        self.ram_base = random.uniform(42.0, 78.0)

        self.client = NetworkClient(
            server_host=server_host,
            server_port=server_port,
            reg_data=self.reg_data,
        )
        self.client.on_command_received = self._handle_remote_command

    def start(self):
        self.client.start()

    def stop(self):
        self.client.stop()

    def _handle_remote_command(self, cmd: CommandPacket) -> CommandResultPacket:
        """Thực thi thật các lệnh Chặn / Mở chặn / Bóp băng thông từ Admin trên máy ảo."""
        action = cmd.action
        target_app = (cmd.target_app or "").strip()
        app_key = target_app.lower()
        limit_kbps = float(cmd.limit_kbps or 0.0)
        direction = cmd.direction or "both"

        with self._policy_lock:
            if action == CommandAction.BLOCK_APP.value:
                self.active_policies[app_key] = {
                    "action": CommandAction.BLOCK_APP.value,
                    "limit_kbps": 0.0,
                    "direction": direction,
                }
                msg = f"[{self.hostname}] Đã CHẶN hoàn toàn '{target_app}' (Firewall + WinDivert Drop)."
            elif action == CommandAction.UNBLOCK_APP.value:
                self.active_policies.pop(app_key, None)
                msg = f"[{self.hostname}] Đã MỞ KHÓA mạng cho '{target_app}'."
            elif action == CommandAction.UNBLOCK_ALL.value:
                self.active_policies.clear()
                msg = f"[{self.hostname}] Đã GỠ BỎ toàn bộ chính sách chặn/giới hạn."
            elif action == CommandAction.THROTTLE_SET.value:
                self.active_policies[app_key] = {
                    "action": CommandAction.THROTTLE_SET.value,
                    "limit_kbps": limit_kbps,
                    "direction": direction,
                }
                msg = f"[{self.hostname}] Đã BÓP BĂNG THÔNG '{target_app}' ở mức {limit_kbps:.0f} KB/s ({direction})."
            elif action == CommandAction.REMOVE_LIMIT.value:
                if app_key:
                    self.active_policies.pop(app_key, None)
                    msg = f"[{self.hostname}] Đã gỡ giới hạn tốc độ cho '{target_app}'."
                else:
                    to_del = [k for k, v in self.active_policies.items() if v.get("action") == CommandAction.THROTTLE_SET.value]
                    for k in to_del:
                        self.active_policies.pop(k, None)
                    msg = f"[{self.hostname}] Đã gỡ toàn bộ giới hạn băng thông QoS."
            elif action == CommandAction.PING.value:
                msg = f"Pong từ {self.hostname} ({self.ip_address}) - độ trễ <1ms"
            else:
                msg = f"[{self.hostname}] Đã xử lý lệnh {action}."

        print(f"  ⚡ [LỆNH ADMIN -> {self.hostname}] {msg}")
        return CommandResultPacket(
            command_id=cmd.command_id,
            device_uuid=self.device_uuid,
            status="SUCCESS",
            message=msg,
        )

    def build_next_telemetry(self, now: float, dt: float) -> Tuple[TelemetryPacket, int]:
        """Tính toán mẫu Telemetry tiếp theo với sóng lưu lượng tự nhiên và áp dụng chính sách QoS."""
        proc_metrics: List[ProcessMetricData] = []
        total_down_kb = 0.0
        total_up_kb = 0.0
        total_conns = 0

        with self._policy_lock:
            policies_snapshot = dict(self.active_policies)

        for p in self.processes:
            p_name = p["name"]
            app_key = p_name.lower()

            # 1. Tính sóng nền mượt theo hàm lượng giác kết hợp nhiễu tự nhiên
            wave = 0.5 * (1.0 + math.sin(now * p["freq"] + p["phase"]))
            wave_up = 0.5 * (1.0 + math.cos(now * (p["freq"] * 0.85) + p["phase"]))

            # 2. Kích hoạt xung tải (burst) ngẫu nhiên kéo dài 6 - 18 giây như người dùng mở tab/tải file thật
            if now > p["burst_until"]:
                if random.random() < 0.06:  # 6% cơ hội bắt đầu 1 đợt tải mới mỗi giây
                    p["burst_until"] = now + random.uniform(6.0, 18.0)
                    p["burst_intensity"] = random.uniform(0.45, 1.0)
                else:
                    p["burst_intensity"] = 0.0

            burst_factor = p["burst_intensity"] if now <= p["burst_until"] else 0.0
            jitter = random.uniform(0.92, 1.08)

            down_kb = (p["base_down"] * (0.3 + 0.7 * wave) + (p["peak_down"] - p["base_down"]) * burst_factor * wave) * jitter
            up_kb = (p["base_up"] * (0.3 + 0.7 * wave_up) + (p["peak_up"] - p["base_up"]) * (burst_factor * 0.6) * wave_up) * jitter

            # Một số tiến trình nền lúc rảnh có thể về gần 0
            if burst_factor == 0.0 and wave < 0.15 and p_name in ("git.exe", "Photoshop.exe", "PremierePro.exe", "EXCEL.EXE", "spoolsv.exe"):
                down_kb = 0.0
                up_kb = 0.0

            conns = max(1, int(p["base_conns"] * (0.6 + 0.6 * wave + 0.5 * burst_factor)))

            # 3. Áp dụng chính sách bảo mật / QoS của Admin nếu có
            pol = policies_snapshot.get(app_key)
            if pol:
                act = pol.get("action")
                direction = pol.get("direction", "both")
                if act == CommandAction.BLOCK_APP.value:
                    if direction in ("both", "download", "in"):
                        down_kb = 0.0
                    if direction in ("both", "upload", "out"):
                        up_kb = 0.0
                    if down_kb == 0.0 and up_kb == 0.0:
                        conns = 0
                elif act == CommandAction.THROTTLE_SET.value:
                    limit_val = max(1.0, float(pol.get("limit_kbps", 100.0)))
                    tb_jitter = random.uniform(0.94, 0.995)  # Dao động sát trần Token Bucket thực tế
                    if direction in ("both", "download") and down_kb > limit_val:
                        down_kb = limit_val * tb_jitter
                    if direction in ("both", "upload") and up_kb > limit_val:
                        up_kb = limit_val * tb_jitter

            down_kb = round(max(0.0, down_kb), 2)
            up_kb = round(max(0.0, up_kb), 2)

            total_down_kb += down_kb
            total_up_kb += up_kb
            total_conns += conns

            proc_metrics.append(
                ProcessMetricData(
                    name=p_name,
                    pid=p["pid"],
                    down_kbps=down_kb,
                    up_kbps=up_kb,
                    connections_count=conns,
                )
            )

        # Sắp xếp tiến trình theo lưu lượng giảm dần giống hệt máy thật
        proc_metrics.sort(key=lambda x: (x.down_kbps + x.up_kbps), reverse=True)

        self.total_down_bytes += int(total_down_kb * 1024.0 * dt)
        self.total_up_bytes += int(total_up_kb * 1024.0 * dt)

        # Tính CPU & RAM biến thiên theo lưu lượng tải
        load_boost = min(35.0, (total_down_kb + total_up_kb) / 250.0)
        cpu_pct = round(min(99.0, max(3.0, self.cpu_base + load_boost + random.uniform(-3.0, 3.0))), 1)
        ram_pct = round(min(96.0, max(25.0, self.ram_base + (load_boost * 0.25) + random.uniform(-1.0, 1.0))), 1)

        packet = TelemetryPacket(
            device_uuid=self.device_uuid,
            timestamp=now,
            speeds=SpeedMetricsData(
                upload_kbps=round(total_up_kb, 2),
                download_kbps=round(total_down_kb, 2),
                total_up_bytes=self.total_up_bytes,
                total_down_bytes=self.total_down_bytes,
            ),
            system_stats={
                "cpu_percent": cpu_pct,
                "ram_percent": ram_pct,
            },
            top_processes=proc_metrics,
            active_conns_count=total_conns,
        )
        return packet, len(policies_snapshot)


def main():
    parser = argparse.ArgumentParser(description="Realistic Enterprise Fleet Load Simulator (Port 9988)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="IP của Admin Server (mặc định: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=SERVER_DEFAULT_PORT, help=f"Cổng TCP của Admin Server (mặc định: {SERVER_DEFAULT_PORT})")
    parser.add_argument("--clients", type=int, default=60, help="Số lượng máy trạm thực tế mô phỏng (mặc định: 60)")
    parser.add_argument("--interval", type=float, default=1.0, help="Chu kỳ gửi Telemetry (giây, mặc định: 1.0)")
    args = parser.parse_args()

    print("=" * 86)
    print(f"🏢 MÔ PHỎNG HỆ THỐNG MẠNG FLEET: {args.clients} MÁY TRẠM -> {args.host}:{args.port}")
    print("=" * 86)
    print(" • Phân khu mô phỏng : LAB-CNTT, AI-WORKSTATION, DESIGN-MEDIA, GAMING-HIEND, VAN-PHONG")
    print(" • Tiến trình / máy  : 9 - 11 tiến trình thực tế (chrome, Code, Docker, idm, steam, Teams...)")
    print(" • Tương tác 2 chiều : Hỗ trợ thực thi thật lệnh Chặn (Block) & Giới hạn QoS (Throttle) từ Admin")
    print("-" * 86)

    machines: List[RealisticVirtualMachine] = []
    for idx in range(1, args.clients + 1):
        vm = RealisticVirtualMachine(idx, args.host, args.port)
        vm.start()
        machines.append(vm)
        if idx % 15 == 0 or idx == args.clients:
            print(f"[*] Đang khởi tạo kết nối: {idx}/{args.clients} máy trạm...")
        time.sleep(0.015)

    total_procs = sum(len(m.processes) for m in machines)
    print(f"\n[✅ SẴN SÀNG] Đang vận hành {len(machines)} máy trạm ({total_procs} tiến trình thời gian thực).")
    print("👉 Mở Admin Dashboard (python admin_main.py --port 9988) để xem đồ thị sóng & test Chặn/QoS.")
    print("👉 Nhấn Ctrl+C tại cửa sổ này để dừng mô phỏng.\n")

    cycle = 0
    try:
        while True:
            t_start = time.perf_counter()
            now = time.time()
            connected_cnt = 0
            fleet_down_kbps = 0.0
            fleet_up_kbps = 0.0
            active_policy_cnt = 0

            for vm in machines:
                if vm.client.is_connected():
                    pkt, pol_cnt = vm.build_next_telemetry(now, args.interval)
                    if vm.client.send_telemetry(pkt):
                        connected_cnt += 1
                        fleet_down_kbps += pkt.speeds.download_kbps
                        fleet_up_kbps += pkt.speeds.upload_kbps
                        active_policy_cnt += pol_cnt

            cycle += 1
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0

            if cycle == 1 or cycle % 5 == 0:
                down_mbs = fleet_down_kbps / 1024.0
                up_mbs = fleet_up_kbps / 1024.0

                print(
                    f"[Chu kỳ #{cycle:04d}] Online: {connected_cnt}/{len(machines)} máy ({total_procs} procs) | "
                    f"Tổng tải: {down_mbs:,.1f} MB/s ↓ / {up_mbs:,.1f} MB/s ↑ | "
                    f"Luật QoS: {active_policy_cnt} | "
                    f"Gửi: {elapsed_ms:.1f} ms"
                )

            sleep_time = max(0.05, args.interval - (time.perf_counter() - t_start))
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[*] Đang ngắt kết nối toàn bộ máy trạm mô phỏng...")
        for vm in machines:
            vm.stop()
        print("[*] Đã dừng bộ mô phỏng an toàn.")


if __name__ == "__main__":
    main()
