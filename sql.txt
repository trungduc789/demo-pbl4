USE demo;

-- 1. DEVICES
CREATE TABLE devices (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_uuid     CHAR(36) NOT NULL UNIQUE,
    hostname        VARCHAR(255) NOT NULL,
    os              VARCHAR(50) NOT NULL,
    os_version      VARCHAR(100),
    agent_version   VARCHAR(50),
    status          ENUM('online', 'offline') NOT NULL DEFAULT 'offline',
    last_seen       DATETIME(6),
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),

    INDEX idx_devices_status (status),
    INDEX idx_devices_last_seen (last_seen)
) ENGINE=InnoDB;


-- 2. NETWORK INTERFACES
CREATE TABLE network_interfaces (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id       BIGINT UNSIGNED NOT NULL,
    name            VARCHAR(100) NOT NULL,
    interface_type  VARCHAR(30),
    mac_address     VARCHAR(17),
    speed           BIGINT UNSIGNED,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),

    CONSTRAINT fk_interfaces_device
        FOREIGN KEY (device_id)
        REFERENCES devices(id)
        ON DELETE CASCADE,

    CONSTRAINT uq_interfaces_device_name
        UNIQUE (device_id, name),

    INDEX idx_interfaces_device (device_id)
) ENGINE=InnoDB;


-- 3. INTERFACE TRAFFIC
CREATE TABLE interface_traffic (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    interface_id    BIGINT UNSIGNED NOT NULL,
    timestamp       DATETIME(6) NOT NULL,
    rx_bytes        BIGINT UNSIGNED NOT NULL,
    tx_bytes        BIGINT UNSIGNED NOT NULL,

    CONSTRAINT fk_interface_traffic_interface
        FOREIGN KEY (interface_id)
        REFERENCES network_interfaces(id)
        ON DELETE CASCADE,

    INDEX idx_interface_traffic_interface_time
        (interface_id, timestamp DESC)
) ENGINE=InnoDB;


-- 4. PROCESSES
CREATE TABLE processes (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id       BIGINT UNSIGNED NOT NULL,
    pid             INT UNSIGNED NOT NULL,
    process_name    VARCHAR(255) NOT NULL,
    executable_path TEXT,
    first_seen      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    status          ENUM('running', 'terminated') NOT NULL DEFAULT 'running',

    CONSTRAINT fk_processes_device
        FOREIGN KEY (device_id)
        REFERENCES devices(id)
        ON DELETE CASCADE,

    INDEX idx_processes_device (device_id),
    INDEX idx_processes_device_pid (device_id, pid),
    INDEX idx_processes_last_seen (last_seen)
) ENGINE=InnoDB;


-- 5. PROCESS TRAFFIC
CREATE TABLE process_traffic (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    process_id      BIGINT UNSIGNED NOT NULL,
    timestamp       DATETIME(6) NOT NULL,
    rx_bytes        BIGINT UNSIGNED NOT NULL,
    tx_bytes        BIGINT UNSIGNED NOT NULL,

    CONSTRAINT fk_process_traffic_process
        FOREIGN KEY (process_id)
        REFERENCES processes(id)
        ON DELETE CASCADE,

    INDEX idx_process_traffic_process_time
        (process_id, timestamp DESC)
) ENGINE=InnoDB;


-- 6. NETWORK CONNECTIONS
CREATE TABLE network_connections (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    device_id       BIGINT UNSIGNED NOT NULL,
    process_id      BIGINT UNSIGNED,

    protocol        VARCHAR(10) NOT NULL,

    local_ip        VARCHAR(45) NOT NULL,
    local_port      INT UNSIGNED,

    remote_ip       VARCHAR(45),
    remote_port     INT UNSIGNED,

    state           VARCHAR(30),

    first_seen      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    CONSTRAINT fk_connections_device
        FOREIGN KEY (device_id)
        REFERENCES devices(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_connections_process
        FOREIGN KEY (process_id)
        REFERENCES processes(id)
        ON DELETE SET NULL,

    INDEX idx_connections_device (device_id),
    INDEX idx_connections_process (process_id),
    INDEX idx_connections_last_seen (last_seen),
    INDEX idx_connections_remote_ip (remote_ip)
) ENGINE=InnoDB;