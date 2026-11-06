"""
Định nghĩa giao thức mạng (Network Protocol Specification) và các đối tượng truyền dữ liệu (DTOs)
cho kiến trúc phân lớp Client - Server (Network Traffic Monitor & Packet Shaper).
"""

from __future__ import annotations
import time
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any


class MessageType(str, Enum):
    """Phân loại các loại gói tin truyền thông trong hệ thống."""
    REGISTER = "REGISTER"                # Client đăng ký thông tin máy với Server
    HEARTBEAT = "HEARTBEAT"              # Giữ kết nối (Keep-Alive)
    TELEMETRY = "TELEMETRY"              # Số liệu đo đạc thời gian thực từ Client gửi lên
    COMMAND = "COMMAND"                  # Lệnh can thiệp từ Admin gửi xuống Client
    COMMAND_RESULT = "COMMAND_RESULT"    # Báo cáo kết quả thực thi lệnh từ Client về Admin
    DISCONNECT = "DISCONNECT"            # Thông báo ngắt kết nối chủ động


class CommandAction(str, Enum):
    """Các hành động can thiệp từ xa trên máy con."""
    BLOCK_APP = "BLOCK_APP"              # Chặn hoàn toàn truy cập mạng của ứng dụng (Firewall + WinDivert drop)
    UNBLOCK_APP = "UNBLOCK_APP"          # Gỡ chặn cho ứng dụng
    UNBLOCK_ALL = "UNBLOCK_ALL"          # Gỡ chặn cho toàn bộ ứng dụng
    THROTTLE_SET = "THROTTLE_SET"        # Bóp băng thông cho ứng dụng theo hạn mức KB/s
    REMOVE_LIMIT = "REMOVE_LIMIT"        # Gỡ bỏ giới hạn băng thông
    PING = "PING"                        # Kiểm tra độ trễ phản hồi


@dataclass
class DeviceRegistrationData:
    """Thông tin định danh phần cứng và hệ điều hành của máy con khi vừa kết nối."""
    device_uuid: str
    hostname: str
    os_name: str
    os_version: str
    agent_version: str = "1.0.0"
    mac_address: str = ""
    ip_address: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DeviceRegistrationData:
        return cls(
            device_uuid=data.get("device_uuid", ""),
            hostname=data.get("hostname", "Unknown"),
            os_name=data.get("os_name", "Unknown"),
            os_version=data.get("os_version", ""),
            agent_version=data.get("agent_version", "1.0.0"),
            mac_address=data.get("mac_address", ""),
            ip_address=data.get("ip_address", "")
        )


@dataclass
class SpeedMetricsData:
    """Số liệu tốc độ truyền tải tức thời và tổng lưu lượng tích lũy."""
    upload_kbps: float = 0.0
    download_kbps: float = 0.0
    total_up_bytes: int = 0
    total_down_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpeedMetricsData:
        return cls(
            upload_kbps=float(data.get("upload_kbps", 0.0)),
            download_kbps=float(data.get("download_kbps", 0.0)),
            total_up_bytes=int(data.get("total_up_bytes", 0)),
            total_down_bytes=int(data.get("total_down_bytes", 0))
        )


@dataclass
class ProcessMetricData:
    """Số liệu lưu lượng tiêu thụ của một tiến trình trên máy con."""
    name: str
    pid: int
    down_kbps: float = 0.0
    up_kbps: float = 0.0
    connections_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProcessMetricData:
        return cls(
            name=data.get("name", ""),
            pid=int(data.get("pid", 0)),
            down_kbps=float(data.get("down_kbps", 0.0)),
            up_kbps=float(data.get("up_kbps", 0.0)),
            connections_count=int(data.get("connections_count", 0))
        )


@dataclass
class TelemetryPacket:
    """Gói tin số liệu đo lường định kỳ gửi từ Client lên Server."""
    device_uuid: str
    timestamp: float
    speeds: SpeedMetricsData
    system_stats: Dict[str, float] = field(default_factory=dict)
    top_processes: List[ProcessMetricData] = field(default_factory=list)
    active_conns_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_uuid": self.device_uuid,
            "timestamp": self.timestamp,
            "speeds": self.speeds.to_dict(),
            "system_stats": self.system_stats,
            "top_processes": [p.to_dict() for p in self.top_processes],
            "active_conns_count": self.active_conns_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TelemetryPacket:
        raw_speeds = data.get("speeds", {})
        raw_procs = data.get("top_processes", [])
        return cls(
            device_uuid=data.get("device_uuid", ""),
            timestamp=float(data.get("timestamp", time.time())),
            speeds=SpeedMetricsData.from_dict(raw_speeds),
            system_stats=data.get("system_stats", {}),
            top_processes=[ProcessMetricData.from_dict(p) for p in raw_procs],
            active_conns_count=int(data.get("active_conns_count", 0)),
        )


@dataclass
class CommandPacket:
    """Lệnh điều khiển do Server/Admin gửi xuống máy con cụ thể."""
    command_id: str
    target_device_uuid: str
    action: str  # Tương ứng với CommandAction
    target_app: str = ""
    limit_kbps: float = 0.0
    direction: str = "both"  # "both", "upload", "download"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CommandPacket:
        return cls(
            command_id=data.get("command_id", ""),
            target_device_uuid=data.get("target_device_uuid", ""),
            action=data.get("action", CommandAction.PING.value),
            target_app=data.get("target_app", ""),
            limit_kbps=float(data.get("limit_kbps", 0.0)),
            direction=data.get("direction", "both"),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class CommandResultPacket:
    """Kết quả phản hồi của máy con sau khi thực thi lệnh can thiệp."""
    command_id: str
    device_uuid: str
    status: str  # "SUCCESS" hoặc "FAILED"
    message: str
    applied_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CommandResultPacket:
        return cls(
            command_id=data.get("command_id", ""),
            device_uuid=data.get("device_uuid", ""),
            status=data.get("status", "FAILED"),
            message=data.get("message", ""),
            applied_at=float(data.get("applied_at", time.time())),
        )


@dataclass
class ProtocolMessage:
    """Khung vỏ đóng gói tổng quát cho mọi thông điệp trên đường truyền socket."""
    msg_type: str  # Tương ứng MessageType
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProtocolMessage:
        return cls(
            msg_type=data.get("msg_type", ""),
            payload=data.get("payload", {}),
            timestamp=float(data.get("timestamp", time.time()))
        )
