"""
Động cơ Bắt Gói Tin WinDivert & Luồng Token Bucket Cô Lập Theo Ứng Dụng (Divert Engine - Infrastructure Layer):
- run_app_throttle_worker: Luồng Token Bucket riêng biệt cho mỗi ứng dụng, loại bỏ Head-of-Line Blocking.
- run_windivert_capture_loop: Luồng bắt gói tin tầng NDIS Kernel với tra cứu O(1) trong bộ nhớ (không gọi psutil).
"""

from __future__ import annotations
import time
import queue
import threading
from typing import Any

try:
    import pydivert
except ImportError:
    pydivert = None

from infrastructure.shaper_models import AppThrottlePolicy
from utils.safe_print import safe_print as print


def run_app_throttle_worker(shaper: Any, policy: AppThrottlePolicy):
    """
    Luồng Token Bucket riêng cho một ứng dụng:
    Lấy các gói tin từ hàng đợi của riêng ứng dụng đó, tính độ trễ rồi bơm lại vào mạng.
    Ứng dụng này ngủ sẽ KHÔNG làm ảnh hưởng bất kỳ ứng dụng nào khác.
    """
    rate_bytes_per_sec = max(1024.0, policy.limit_kbps * 1024.0)
    burst_capacity = max(16384.0, rate_bytes_per_sec * 0.25)
    policy.tokens = burst_capacity
    policy.last_refill = time.perf_counter()
    send_lock = shaper._send_lock

    while not policy.stop_event.is_set() and not shaper._stop_event.is_set():
        try:
            item = policy.queue.get(timeout=0.05)
        except queue.Empty:
            policy.update_speed_stats(0, False)
            continue

        if item is None:
            break

        packet, packet_size, is_inbound = item

        # Chờ handle WinDivert sẵn sàng nếu luồng Capture đang khởi tạo driver
        handle = shaper._divert_handle
        wait_tries = 0
        while (
            handle is None
            and wait_tries < 50
            and not policy.stop_event.is_set()
            and not shaper._stop_event.is_set()
        ):
            time.sleep(0.01)
            wait_tries += 1
            handle = shaper._divert_handle

        if not handle or not getattr(handle, "is_open", True):
            break

        now = time.perf_counter()
        elapsed = max(0.0, now - policy.last_refill)
        tokens = min(burst_capacity, policy.tokens + elapsed * rate_bytes_per_sec)
        policy.last_refill = now

        if tokens < packet_size:
            needed = packet_size - tokens
            sleep_sec = needed / rate_bytes_per_sec
            # Chỉ gọi time.sleep khi thời gian chờ >= 10ms vì bộ lập lịch Windows
            # có độ phân giải ~15.6ms. Với sleep nhỏ hơn, dùng nợ token (negative tokens)
            # để tránh làm tụt băng thông xuống ~90 KB/s và gây đứt kết nối socket.
            if sleep_sec >= 0.010:
                time.sleep(sleep_sec)
                after_sleep = time.perf_counter()
                actual_elapsed = max(0.0, after_sleep - now)
                tokens = min(burst_capacity, tokens + actual_elapsed * rate_bytes_per_sec)
                policy.last_refill = after_sleep
            policy.tokens = tokens - packet_size
        else:
            policy.tokens = tokens - packet_size

        try:
            with send_lock:
                handle.send(packet, recalculate_checksum=False)
            policy.update_speed_stats(packet_size, is_inbound)
        except Exception:
            try:
                with send_lock:
                    handle.send(packet, recalculate_checksum=True)
                policy.update_speed_stats(packet_size, is_inbound)
            except Exception:
                pass


def run_windivert_capture_loop(
    shaper: Any,
    stop_event: threading.Event,
    pydivert_mod: Any = None,
):
    """
    Luồng chính bắt gói tin tầng mạng:
    - Sử dụng tra cứu O(1) trong bộ nhớ, tuyệt đối không gọi psutil.
    - Gói tin của app bị Block: Drop ngay tại driver.
    - Gói tin của app bị Throttle: Phân phối vào đúng hàng đợi của app đó.
    - Gói tin bình thường khác: Cho qua tức thì (Zero latency pass-through).
    """
    divert_lib = pydivert_mod if pydivert_mod is not None else pydivert
    if divert_lib is None:
        print("[PacketShaper Error] Thư viện pydivert chưa được cài đặt!")
        return

    filter_str = "!loopback and (tcp or udp)"
    w = None
    try:
        with divert_lib.WinDivert(filter=filter_str, priority=0) as w:
            # Tăng kích thước hàng đợi trong kernel của WinDivert lên mức tối đa
            # để không bị rớt gói tin khi băng thông cao hoặc đa luồng (Speedtest)
            try:
                w.set_param(divert_lib.Param.QUEUE_LEN, 8192)
                w.set_param(divert_lib.Param.QUEUE_TIME, 2048)
                w.set_param(divert_lib.Param.QUEUE_SIZE, 33554432)
            except Exception:
                pass
            shaper._divert_handle = w
            print("[PacketShaper] Đã kích hoạt WinDivert Kernel Filtering thành công.")

            while not stop_event.is_set():
                try:
                    packet = w.recv()
                    if not packet:
                        continue
                except Exception:
                    if stop_event.is_set():
                        break
                    continue

                try:
                    is_inbound = packet.is_inbound
                    port = packet.dst_port if is_inbound else packet.src_port

                    # Tra cứu O(1)
                    with shaper._lock:
                        if port in shaper.blocked_ports:
                            # 1. Thuộc ứng dụng bị BLOCK -> Hủy gói tại chỗ
                            continue

                        app_key = shaper.port_to_throttle_app.get(port)
                        policy = shaper.throttled_apps.get(app_key) if app_key else None

                    if policy is not None:
                        # 2. Thuộc ứng dụng bị THROTTLE -> Kiểm tra hướng áp dụng
                        direction = policy.direction
                        match_dir = (
                            direction == "both"
                            or (direction == "upload" and not is_inbound)
                            or (direction == "download" and is_inbound)
                        )
                        if match_dir:
                            packet_len = len(packet.raw) if packet.raw else 64
                            try:
                                policy.queue.put_nowait((packet, packet_len, is_inbound))
                                shaper._port_last_seen[port] = time.time()
                                continue
                            except queue.Full:
                                # Tail drop riêng cho app này khi hàng đợi quá đầy
                                continue

                    # 3. Gói tin bình thường -> Cho qua tức thì
                    try:
                        with shaper._send_lock:
                            w.send(packet, recalculate_checksum=False)
                    except Exception:
                        pass

                except Exception:
                    try:
                        with shaper._send_lock:
                            w.send(packet, recalculate_checksum=False)
                    except Exception:
                        pass
    except Exception as e:
        if not stop_event.is_set():
            print(f"[PacketShaper Error] WinDivert capture dừng: {e}")
    finally:
        if shaper._divert_handle is w:
            shaper._divert_handle = None
