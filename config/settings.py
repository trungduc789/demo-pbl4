# Cấu hình chu kỳ lấy mẫu và giao diện hiển thị
UPDATE_INTERVAL = 1  # Thời gian lấy mẫu t (giây)[cite: 2]
MAX_DATA_POINTS = 60   # Số lượng điểm hiển thị trên biểu đồ lịch sử
UNIT_DIVIDER = 1024    # Quy đổi Bytes sang KB

# Cấu hình kết nối MySQL database
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "0078",
    "database": "demo",
    "charset": "utf8mb4",
    "autocommit": True,
    "use_pure": True,
}