import os

# Cấu hình chu kỳ lấy mẫu và giao diện hiển thị
UPDATE_INTERVAL = 1  # Thời gian lấy mẫu t (giây)[cite: 2]
MAX_DATA_POINTS = 60   # Số lượng điểm hiển thị trên biểu đồ lịch sử
UNIT_DIVIDER = 1024    # Quy đổi Bytes sang KB

# Cấu hình kết nối truyền thông Client - Server
SERVER_DEFAULT_HOST = "0.0.0.0"       # Server lắng nghe trên mọi card mạng
SERVER_DEFAULT_PORT = 9988          # Cổng TCP chính của hệ thống quản trị
CLIENT_HEARTBEAT_INTERVAL = 2.0     # Chu kỳ gửi tin Heartbeat (giây)
SERVER_CLIENT_TIMEOUT = 6.0         # Thời gian tối đa không thấy tin trước khi coi máy Offline (giây)
CLIENT_RECONNECT_DELAY = 3.0        # Thời gian chờ giữa các lần tự động kết nối lại (giây)

# Cấu hình bảo mật giao thức truyền thông (PSK + HMAC + Session Key)
NETWORK_PSK = os.environ.get("PBL4_PSK", "PBL4-NetworkMonitor-PSK-2026")
AUTH_TIMEOUT = 5.0                  # Hạn chót bắt tay xác thực kết nối (giây)
MAX_PENDING_AUTH = 32               # Giới hạn số kết nối chưa xác thực đồng thời
RECV_POLL_TIMEOUT = 2.0             # Thời gian timeout đọc socket định kỳ (giây)
