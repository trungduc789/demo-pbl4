import os
import sys

# Đảm bảo đường dẫn gốc của project có trong sys.path khi chạy trực tiếp file này
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import threading
import platform
import uuid
from datetime import datetime
from typing import Dict, Optional, Set

import mysql.connector
import psutil
from mysql.connector import Error

from config.settings import DB_CONFIG
from infrastructure.network_collector import NetworkCollector
from utils.safe_print import safe_print as print


class DatabaseManager:
    """Quản lý kết nối và ghi dữ liệu vào MySQL (Thread-Safe hoàn toàn)."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or DB_CONFIG
        self.connection = None
        self._lock = threading.RLock()
        self.device_id: Optional[int] = None
        self.interface_ids: Dict[str, int] = {}
        self.collector = NetworkCollector()
        with self._lock:
            self._connect()
            # Tự động kiểm tra và đăng ký máy/card mạng ngay khi khởi tạo
            if self.connection and self.connection.is_connected():
                self.get_or_create_device()
                self.ensure_interfaces()


    def _connect(self):
        try:
            base_config = {
                "host": self.config.get("host", "127.0.0.1"),
                "port": self.config.get("port", 3306),
                "user": self.config.get("user", "root"),
                "password": self.config.get("password", ""),
                "charset": self.config.get("charset", "utf8mb4"),
                "autocommit": True,
                "use_pure": True,
            }

            temp_conn = mysql.connector.connect(**base_config)
            cursor = temp_conn.cursor()
            cursor.execute("CREATE DATABASE IF NOT EXISTS demo CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
            temp_conn.commit()
            cursor.close()
            temp_conn.close()

            full_config = dict(self.config)
            full_config["use_pure"] = True
            self.connection = mysql.connector.connect(**full_config)
            if self.connection is not None:
                self.connection.autocommit = True
            self._ensure_schema()
            return self.connection
        except Error as exc:
            print(f"[DB] Không kết nối được MySQL: {exc}")
            return None

    def _ensure_schema(self):
        if self.connection is None or not self.connection.is_connected():
            return

        cursor = self.connection.cursor()
        try:
            cursor.execute("USE demo")
            schema = """
            CREATE TABLE IF NOT EXISTS devices (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                device_uuid CHAR(36) NOT NULL UNIQUE,
                hostname VARCHAR(255) NOT NULL,
                os VARCHAR(50) NOT NULL,
                os_version VARCHAR(100),
                agent_version VARCHAR(50),
                status ENUM('online', 'offline') NOT NULL DEFAULT 'offline',
                last_seen DATETIME(6),
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                INDEX idx_devices_status (status),
                INDEX idx_devices_last_seen (last_seen)
            ) ENGINE=InnoDB;

            CREATE TABLE IF NOT EXISTS network_interfaces (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                device_id BIGINT UNSIGNED NOT NULL,
                name VARCHAR(100) NOT NULL,
                interface_type VARCHAR(30),
                mac_address VARCHAR(17),
                speed BIGINT UNSIGNED,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                CONSTRAINT fk_interfaces_device
                    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                CONSTRAINT uq_interfaces_device_name UNIQUE (device_id, name),
                INDEX idx_interfaces_device (device_id)
            ) ENGINE=InnoDB;

            CREATE TABLE IF NOT EXISTS interface_traffic (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                interface_id BIGINT UNSIGNED NOT NULL,
                timestamp DATETIME(6) NOT NULL,
                rx_bytes BIGINT UNSIGNED NOT NULL,
                tx_bytes BIGINT UNSIGNED NOT NULL,
                CONSTRAINT fk_interface_traffic_interface
                    FOREIGN KEY (interface_id) REFERENCES network_interfaces(id) ON DELETE CASCADE,
                INDEX idx_interface_traffic_interface_time (interface_id, timestamp DESC)
            ) ENGINE=InnoDB;

            CREATE TABLE IF NOT EXISTS processes (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                device_id BIGINT UNSIGNED NOT NULL,
                pid INT UNSIGNED NOT NULL,
                process_name VARCHAR(255) NOT NULL,
                executable_path TEXT,
                first_seen DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                last_seen DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                status ENUM('running', 'terminated') NOT NULL DEFAULT 'running',
                CONSTRAINT fk_processes_device
                    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                INDEX idx_processes_device (device_id),
                INDEX idx_processes_device_pid (device_id, pid),
                INDEX idx_processes_last_seen (last_seen)
            ) ENGINE=InnoDB;

            CREATE TABLE IF NOT EXISTS process_traffic (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                process_id BIGINT UNSIGNED NOT NULL,
                timestamp DATETIME(6) NOT NULL,
                rx_bytes BIGINT UNSIGNED NOT NULL,
                tx_bytes BIGINT UNSIGNED NOT NULL,
                CONSTRAINT fk_process_traffic_process
                    FOREIGN KEY (process_id) REFERENCES processes(id) ON DELETE CASCADE,
                INDEX idx_process_traffic_process_time (process_id, timestamp DESC)
            ) ENGINE=InnoDB;

            CREATE TABLE IF NOT EXISTS network_connections (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                device_id BIGINT UNSIGNED NOT NULL,
                process_id BIGINT UNSIGNED,
                protocol VARCHAR(10) NOT NULL,
                local_ip VARCHAR(45) NOT NULL,
                local_port INT UNSIGNED,
                remote_ip VARCHAR(45),
                remote_port INT UNSIGNED,
                state VARCHAR(30),
                first_seen DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                last_seen DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                CONSTRAINT fk_connections_device
                    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                CONSTRAINT fk_connections_process
                    FOREIGN KEY (process_id) REFERENCES processes(id) ON DELETE SET NULL,
                INDEX idx_connections_device (device_id),
                INDEX idx_connections_process (process_id),
                INDEX idx_connections_last_seen (last_seen),
                INDEX idx_connections_remote_ip (remote_ip),
                INDEX idx_connections_lookup (device_id, protocol, local_ip, local_port)
            ) ENGINE=InnoDB;
            """
            for statement in schema.split(";"):
                if statement.strip():
                    cursor.execute(statement)
            self.connection.commit()
        except Error as exc:
            print(f"[DB] Không tạo được schema: {exc}")
        finally:
            cursor.close()

    def ensure_connection(self):
        with self._lock:
            if self.connection is None or not self.connection.is_connected():
                self.connection = self._connect()
            return self.connection

    def get_or_create_device(self) -> Optional[int]:
        """Kiểm tra máy hiện tại, nếu chưa có trong DB thì tự động đăng ký mới."""
        conn = self.ensure_connection()
        if conn is None:
            return None

        hostname = platform.node() or "unknown-host"
        os_name = platform.system() or "UnknownOS"
        os_version = platform.version() or "unknown"
        stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{hostname}-{uuid.getnode()}"))

        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id FROM devices WHERE device_uuid = %s LIMIT 1",
                (stable_uuid,),
            )
            row = cursor.fetchone()
            if row:
                self.device_id = int(row[0])
                cursor.execute(
                    "UPDATE devices SET hostname = %s, os = %s, os_version = %s, status = 'online', last_seen = NOW() WHERE id = %s",
                    (hostname, os_name, os_version, self.device_id),
                )
                conn.commit()
                return self.device_id

            # Thiết bị chưa được đăng ký -> Thêm mới vào bảng devices
            cursor.execute(
                """
                INSERT INTO devices (device_uuid, hostname, os, os_version, agent_version, status, last_seen, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'online', NOW(), NOW(), NOW())
                """,
                (stable_uuid, hostname, os_name, os_version, "demo-agent-v1.0"),
            )
            self.device_id = cursor.lastrowid
            conn.commit()
            print(f"[DB] Đã đăng ký thiết bị mới thành công: {hostname} (ID={self.device_id}, UUID={stable_uuid})")
            return self.device_id
        finally:
            cursor.close()

    def ensure_interfaces(self, device_id: Optional[int] = None) -> Dict[str, int]:
        """Đăng ký các card mạng của thiết bị nếu chưa tồn tại trong bảng network_interfaces."""
        conn = self.ensure_connection()
        if conn is None:
            return {}

        target_device_id = device_id if device_id is not None else self.device_id
        if target_device_id is None:
            target_device_id = self.get_or_create_device()
        if target_device_id is None:
            return {}

        nic_map = psutil.net_io_counters(pernic=True)
        addrs_map = psutil.net_if_addrs()
        stats_map = psutil.net_if_stats()

        cursor = conn.cursor()
        try:
            for name in nic_map:
                cursor.execute(
                    "SELECT id FROM network_interfaces WHERE device_id = %s AND name = %s LIMIT 1",
                    (target_device_id, name),
                )
                row = cursor.fetchone()
                if row:
                    self.interface_ids[name] = int(row[0])
                    continue

                # Lấy địa chỉ MAC và trạng thái card mạng nếu có
                mac_address = None
                if name in addrs_map:
                    for addr in addrs_map[name]:
                        # AF_LINK đại diện cho địa chỉ MAC trên hầu hết các OS
                        if getattr(addr, 'family', None) is not None and getattr(addr.family, 'name', '') == 'AF_LINK':
                            mac_address = addr.address
                            break

                speed = None
                is_active = True
                if name in stats_map:
                    stat = stats_map[name]
                    is_active = bool(stat.isup)
                    speed = int(stat.speed) if stat.speed else None

                # Xác định loại card mạng cơ bản
                interface_type = "loopback" if "loopback" in name.lower() else ("wireless" if "wi-fi" in name.lower() or "wireless" in name.lower() else "ethernet")

                cursor.execute(
                    """
                    INSERT INTO network_interfaces (device_id, name, interface_type, mac_address, speed, is_active, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (target_device_id, name, interface_type, mac_address, speed, is_active),
                )
                self.interface_ids[name] = int(cursor.lastrowid)
                print(f"[DB] Đã đăng ký card mạng mới: {name} (ID={self.interface_ids[name]})")

            conn.commit()
            return self.interface_ids
        finally:
            cursor.close()

    def save_interface_traffic(self, timestamp: float):
        """Lưu vết lưu lượng mạng của từng card mạng vào interface_traffic."""
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return

            device_id = self.device_id if self.device_id is not None else self.get_or_create_device()
            if device_id is None:
                return

            self.ensure_interfaces(device_id)
            nic_map = psutil.net_io_counters(pernic=True)

            if not self.interface_ids:
                return

            cursor = conn.cursor()
            try:
                record_time = datetime.fromtimestamp(timestamp)
                for interface_name, counters in nic_map.items():
                    interface_id = self.interface_ids.get(interface_name)
                    if interface_id is None:
                        continue

                    cursor.execute(
                        """
                        INSERT INTO interface_traffic (interface_id, timestamp, rx_bytes, tx_bytes)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (interface_id, record_time, int(counters.bytes_recv), int(counters.bytes_sent)),
                    )

                conn.commit()
            finally:
                cursor.close()

    def save_or_update_process(
        self,
        cursor,
        device_id: int,
        pid: int,
        proc_name: str,
        exe_path: Optional[str],
        record_time: datetime
    ) -> Optional[int]:
        """
        Dùng Windows Process API / psutil để ghi tiến trình vào bảng processes.
        Nếu cùng PID đang chạy nhưng khác tên -> đánh dấu tiến trình cũ đã terminated và tạo mới.
        """
        cursor.execute(
            """
            SELECT id, process_name, executable_path 
            FROM processes 
            WHERE device_id = %s AND pid = %s AND status = 'running' 
            LIMIT 1
            """,
            (device_id, pid),
        )
        row = cursor.fetchone()
        if row:
            proc_id, existing_name, existing_exe = row[0], row[1], row[2]
            # Nếu trùng tên -> cập nhật thời gian hoạt động gần nhất last_seen và đường dẫn thực thi
            if existing_name.lower() == proc_name.lower():
                resolved_exe = exe_path or existing_exe
                cursor.execute(
                    """
                    UPDATE processes 
                    SET last_seen = %s, executable_path = %s 
                    WHERE id = %s
                    """,
                    (record_time, resolved_exe, proc_id),
                )
                return int(proc_id)
            else:
                # PID đã bị hệ điều hành cấp lại cho một tiến trình khác -> kết thúc tiến trình cũ
                cursor.execute(
                    """
                    UPDATE processes 
                    SET status = 'terminated', last_seen = %s 
                    WHERE id = %s
                    """,
                    (record_time, proc_id),
                )

        # Ghi mới tiến trình vào bảng processes
        cursor.execute(
            """
            INSERT INTO processes (device_id, pid, process_name, executable_path, first_seen, last_seen, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'running')
            """,
            (device_id, pid, proc_name, exe_path, record_time, record_time),
        )
        return int(cursor.lastrowid)

    def save_or_update_connection(
        self,
        cursor,
        device_id: int,
        process_id: Optional[int],
        protocol: str,
        local_ip: str,
        local_port: Optional[int],
        remote_ip: Optional[str],
        remote_port: Optional[int],
        state: str,
        record_time: datetime
    ) -> int:
        """
        Ghi hoặc cập nhật kết nối trong network_connections.
        Dùng toán tử <=> (NULL-safe equal) của MySQL để so sánh IP và cổng remote.
        """
        cursor.execute(
            """
            SELECT id, state 
            FROM network_connections 
            WHERE device_id = %s 
              AND protocol = %s 
              AND local_ip = %s 
              AND local_port = %s 
              AND remote_ip <=> %s 
              AND remote_port <=> %s 
            LIMIT 1
            """,
            (device_id, protocol, local_ip, local_port, remote_ip, remote_port),
        )
        row = cursor.fetchone()
        if row:
            conn_id = int(row[0])
            cursor.execute(
                """
                UPDATE network_connections 
                SET process_id = COALESCE(%s, process_id),
                    state = %s,
                    last_seen = %s 
                WHERE id = %s
                """,
                (process_id, state, record_time, conn_id),
            )
            return conn_id
        else:
            cursor.execute(
                """
                INSERT INTO network_connections (
                    device_id, process_id, protocol,
                    local_ip, local_port, remote_ip, remote_port,
                    state, first_seen, last_seen
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    device_id, process_id, protocol,
                    local_ip, local_port, remote_ip, remote_port,
                    state, record_time, record_time
                ),
            )
            return int(cursor.lastrowid)

    def track_and_update_terminated_processes(
        self,
        cursor,
        device_id: int,
        record_time: datetime
    ) -> int:
        """
        Theo dõi thời điểm tiến trình kết thúc:
        - Quét các tiến trình trong DB có status = 'running'.
        - Dùng psutil kiểm tra xem tiến trình có còn sống hay đã dừng.
        - Cập nhật status = 'terminated', ghi nhận last_seen là thời điểm kết thúc.
        - Chuyển trạng thái các kết nối mạng thuộc về tiến trình sang 'CLOSED'.
        """
        cursor.execute(
            """
            SELECT id, pid, process_name 
            FROM processes 
            WHERE device_id = %s AND status = 'running'
            """,
            (device_id,),
        )
        running_procs = cursor.fetchall()
        if not running_procs:
            return 0

        terminated_count = 0
        for proc_id, pid, proc_name in running_procs:
            is_ended = False
            if not psutil.pid_exists(pid):
                is_ended = True
            else:
                try:
                    p = psutil.Process(pid)
                    if p.name().lower() != proc_name.lower():
                        is_ended = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    is_ended = True

            if is_ended:
                cursor.execute(
                    """
                    UPDATE processes 
                    SET status = 'terminated', last_seen = %s 
                    WHERE id = %s
                    """,
                    (record_time, proc_id),
                )
                cursor.execute(
                    """
                    UPDATE network_connections 
                    SET state = 'CLOSED', last_seen = %s 
                    WHERE process_id = %s AND state NOT IN ('CLOSED', 'TIME_WAIT')
                    """,
                    (record_time, proc_id),
                )
                terminated_count += 1
                print(f"[DB] Tiến trình kết thúc: {proc_name} (PID={pid}, ID={proc_id}) tại {record_time}")

        return terminated_count

    def save_processes_and_connections(self, timestamp: float):
        """
        Quy trình xử lý theo dõi tiến trình và kết nối:
        1. Dùng Windows Process API / psutil lấy PID, tên và đường dẫn.
        2. Dùng API kết nối TCP đối chiếu PID, IP và cổng.
        3. Ghi tiến trình vào processes.
        4. Ghi hoặc cập nhật kết nối trong network_connections.
        5. Theo dõi thời điểm tiến trình kết thúc.
        """
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return

            device_id = self.device_id if self.device_id is not None else self.get_or_create_device()
            if device_id is None:
                return

            record_time = datetime.fromtimestamp(timestamp)
            cursor = conn.cursor()
            try:
                # 1 & 2. Thu thập kết nối TCP và đối chiếu IP/cổng/PID
                tcp_connections = self.collector.collect_tcp_connections()
                proc_id_cache: Dict[int, int] = {}

                for conn_info in tcp_connections:
                    pid = conn_info.pid
                    db_proc_id: Optional[int] = None

                    # 3. Ghi/cập nhật tiến trình vào bảng processes
                    if pid and pid > 0:
                        if pid in proc_id_cache:
                            db_proc_id = proc_id_cache[pid]
                        else:
                            proc_info = self.collector.get_process_info(pid)
                            if proc_info:
                                db_proc_id = self.save_or_update_process(
                                    cursor=cursor,
                                    device_id=device_id,
                                    pid=proc_info.pid,
                                    proc_name=proc_info.name,
                                    exe_path=proc_info.executable_path,
                                    record_time=record_time
                                )
                                if db_proc_id:
                                    proc_id_cache[pid] = db_proc_id

                                    # Lưu vết lưu lượng I/O bytes của tiến trình nếu có
                                    try:
                                        p = psutil.Process(pid)
                                        proc_io = p.io_counters()
                                        cursor.execute(
                                            """
                                            INSERT INTO process_traffic (process_id, timestamp, rx_bytes, tx_bytes)
                                            VALUES (%s, %s, %s, %s)
                                            """,
                                            (db_proc_id, record_time, int(proc_io.read_bytes), int(proc_io.write_bytes)),
                                        )
                                    except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess):
                                        pass

                    # 4. Ghi hoặc cập nhật kết nối vào network_connections
                    self.save_or_update_connection(
                        cursor=cursor,
                        device_id=device_id,
                        process_id=db_proc_id,
                        protocol=conn_info.protocol,
                        local_ip=conn_info.local_ip,
                        local_port=conn_info.local_port,
                        remote_ip=conn_info.remote_ip,
                        remote_port=conn_info.remote_port,
                        state=conn_info.state,
                        record_time=record_time
                    )

                # 5. Theo dõi thời điểm tiến trình kết thúc
                self.track_and_update_terminated_processes(cursor, device_id, record_time)

                conn.commit()
            except Exception as exc:
                print(f"[DB] Lỗi khi xử lý tiến trình và kết nối TCP: {exc}")
            finally:
                cursor.close()

    def save_process_traffic_samples(self, traffic_samples: Dict[int, Dict[str, int]], timestamp: float):
        """
        Lưu các mẫu lưu lượng mạng thu thập từ ETW Consumer vào bảng process_traffic.
        - traffic_samples: { pid: {'rx_bytes': ..., 'tx_bytes': ...} }
        - timestamp: Mốc thời gian mẫu
        """
        if not traffic_samples:
            return

        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return

            device_id = self.device_id if self.device_id is not None else self.get_or_create_device()
            if device_id is None:
                return

            record_time = datetime.fromtimestamp(timestamp)
            cursor = conn.cursor()
            try:
                for pid, bytes_info in traffic_samples.items():
                    rx = bytes_info.get("rx_bytes", 0)
                    tx = bytes_info.get("tx_bytes", 0)
                    if rx <= 0 and tx <= 0:
                        continue

                    # 1. Tra cứu hoặc tự động tạo tiến trình trong bảng processes
                    cursor.execute(
                        """
                        SELECT id FROM processes 
                        WHERE device_id = %s AND pid = %s AND status = 'running' 
                        LIMIT 1
                        """,
                        (device_id, pid),
                    )
                    row = cursor.fetchone()
                    db_proc_id = None
                    if row:
                        db_proc_id = int(row[0])
                    else:
                        proc_info = self.collector.get_process_info(pid)
                        if proc_info:
                            db_proc_id = self.save_or_update_process(
                                cursor=cursor,
                                device_id=device_id,
                                pid=proc_info.pid,
                                proc_name=proc_info.name,
                                exe_path=proc_info.executable_path,
                                record_time=record_time
                            )

                    # 2. Lưu mẫu lưu lượng mạng vào bảng process_traffic
                    if db_proc_id:
                        cursor.execute(
                            """
                            INSERT INTO process_traffic (process_id, timestamp, rx_bytes, tx_bytes)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (db_proc_id, record_time, rx, tx),
                        )

                conn.commit()
            except Exception as exc:
                print(f"[DB] Lỗi khi lưu process_traffic từ ETW: {exc}")
            finally:
                cursor.close()

    def get_database_stats(self) -> dict:
        """Kiểm tra việc cập nhật và lưu lịch sử: đếm số lượng bản ghi và mốc thời gian mới nhất của các bảng."""
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return {"connected": False, "error": "Chưa kết nối MySQL"}
            cursor = conn.cursor()
            stats = {"connected": True}
            try:
                tables = ["devices", "network_interfaces", "interface_traffic", "processes", "process_traffic", "network_connections"]
                for tbl in tables:
                    cursor.execute(f"SELECT COUNT(*) FROM {tbl}")
                    stats[tbl] = cursor.fetchone()[0]

                cursor.execute("SELECT MAX(timestamp) FROM interface_traffic")
                stats["latest_interface_traffic"] = cursor.fetchone()[0]
                cursor.execute("SELECT MAX(timestamp) FROM process_traffic")
                stats["latest_process_traffic"] = cursor.fetchone()[0]
                cursor.execute("SELECT MAX(last_seen) FROM network_connections")
                stats["latest_connection"] = cursor.fetchone()[0]
            except Exception as e:
                stats["error"] = str(e)
            finally:
                cursor.close()
            return stats

    def get_recent_history(self, limit: int = 15) -> list:
        """Lấy danh sách các bản ghi lưu lượng gần nhất từ database để kiểm tra tính toàn vẹn."""
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return []
            cursor = conn.cursor()
            history = []
            try:
                query = """
                SELECT pt.timestamp, p.pid, p.process_name, pt.rx_bytes, pt.tx_bytes
                FROM process_traffic pt
                JOIN processes p ON pt.process_id = p.id
                ORDER BY pt.timestamp DESC
                LIMIT %s
                """
                cursor.execute(query, (limit,))
                for row in cursor.fetchall():
                    history.append({
                        "timestamp": row[0],
                        "pid": row[1],
                        "name": row[2],
                        "rx_bytes": row[3],
                        "tx_bytes": row[4]
                    })
            except Exception as e:
                print(f"[DB] Lỗi khi đọc lịch sử: {e}")
            finally:
                cursor.close()
            return history

    def get_recorded_dates(self) -> list:
        """Lấy danh sách các ngày có dữ liệu đo đạc trong interface_traffic."""
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return []
            cursor = conn.cursor()
            dates = []
            try:
                query = "SELECT DISTINCT DATE(timestamp) FROM interface_traffic ORDER BY DATE(timestamp) DESC LIMIT 30"
                cursor.execute(query)
                for row in cursor.fetchall():
                    if row[0]:
                        dates.append(str(row[0]))
            except Exception as e:
                print(f"[DB] Lỗi lấy danh sách ngày: {e}")
            finally:
                cursor.close()
            return dates

    def get_daily_traffic_series(self, target_date_str: str) -> list:
        """
        Lấy chuỗi dữ liệu lưu lượng theo ngày từ interface_traffic và tính toán tốc độ KB/s.
        Trả về danh sách [{'timestamp': float, 'datetime_str': str, 'down_kb': float, 'up_kb': float}].
        """
        import time as _t
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return []
            cursor = conn.cursor()
            series = []
            try:
                query = """
                SELECT timestamp, SUM(rx_bytes) AS total_rx, SUM(tx_bytes) AS total_tx
                FROM interface_traffic
                WHERE DATE(timestamp) = %s
                GROUP BY timestamp
                ORDER BY timestamp ASC
                """
                cursor.execute(query, (target_date_str,))
                rows = cursor.fetchall()
                if len(rows) < 2:
                    return []

                prev_t = None
                prev_rx = None
                prev_tx = None

                for row in rows:
                    dt = row[0]
                    t_val = dt.timestamp() if hasattr(dt, 'timestamp') else _t.mktime(dt.timetuple())
                    cur_rx = float(row[1])
                    cur_tx = float(row[2])

                    if prev_t is not None:
                        dt_sec = max(0.2, t_val - prev_t)
                        delta_rx = max(0.0, cur_rx - prev_rx)
                        delta_tx = max(0.0, cur_tx - prev_tx)
                        down_kb = delta_rx / (dt_sec * 1024.0)
                        up_kb = delta_tx / (dt_sec * 1024.0)

                        series.append({
                            "timestamp": t_val,
                            "datetime_str": dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "down_kb": down_kb,
                            "up_kb": up_kb
                        })

                    prev_t = t_val
                    prev_rx = cur_rx
                    prev_tx = cur_tx

            except Exception as e:
                print(f"[DB] Lỗi lấy chuỗi lưu lượng ngày {target_date_str}: {e}")
            finally:
                cursor.close()
            return series

    def get_processes_around_time(self, target_datetime, tolerance_seconds: float = 3.0) -> list:
        """
        Lấy các tiến trình có phát sinh lưu lượng mạng xung quanh một mốc thời gian cụ thể.
        """
        with self._lock:
            conn = self.ensure_connection()
            if conn is None:
                return []
            cursor = conn.cursor()
            procs = []
            try:
                query = """
                SELECT p.pid, p.process_name, pt.rx_bytes, pt.tx_bytes
                FROM process_traffic pt
                JOIN processes p ON pt.process_id = p.id
                WHERE pt.timestamp BETWEEN DATE_SUB(%s, INTERVAL %s SECOND) AND DATE_ADD(%s, INTERVAL %s SECOND)
                ORDER BY (pt.rx_bytes + pt.tx_bytes) DESC
                LIMIT 10
                """
                cursor.execute(query, (target_datetime, int(tolerance_seconds), target_datetime, int(tolerance_seconds)))
                for row in cursor.fetchall():
                    procs.append({
                        "pid": row[0],
                        "name": row[1],
                        "rx_bytes": row[2],
                        "tx_bytes": row[3]
                    })
            except Exception as e:
                print(f"[DB] Lỗi lấy tiến trình tại mốc thời gian: {e}")
            finally:
                cursor.close()
            return procs

    def close(self):
        with self._lock:
            if self.connection is not None and self.connection.is_connected():
                self.connection.close()





if __name__ == "__main__":
    import time

    print("[INFO] Dang khoi tao DatabaseManager...")
    db = DatabaseManager()
    if db.connection and db.connection.is_connected():
        print("[INFO] Ket noi MySQL thanh cong!")
        print(f"[INFO] Device ID: {db.device_id}")
        print(f"[INFO] Danh sach card mang: {len(db.interface_ids)}")
        now_ts = time.time()
        db.save_interface_traffic(now_ts)
        print("[INFO] Da luu interface_traffic thanh cong!")
        db.save_processes_and_connections(now_ts)
        print("[INFO] Da luu processes va network_connections thanh cong!")
        db.close()
    else:
        print("[ERROR] Khong the ket noi toi MySQL.")
