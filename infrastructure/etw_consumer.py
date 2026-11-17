import os
import sys
import time
import struct
import ctypes
import threading
from ctypes import wintypes
from collections import defaultdict
from typing import Dict, Optional, Tuple
from datetime import datetime
from utils.safe_print import safe_print as print
from infrastructure.etw_ctypes import (
    WNODE_FLAG_TRACED_GUID,
    EVENT_TRACE_REAL_TIME_MODE,
    EVENT_TRACE_CONTROL_QUERY,
    EVENT_TRACE_CONTROL_STOP,
    EVENT_CONTROL_CODE_ENABLE_PROVIDER,
    TRACE_LEVEL_INFORMATION,
    PROCESS_TRACE_MODE_REAL_TIME,
    PROCESS_TRACE_MODE_EVENT_RECORD,
    INVALID_PROCESSTRACE_HANDLE,
    ERROR_SUCCESS,
    ERROR_ACCESS_DENIED,
    ERROR_INSUFFICIENT_BUFFER,
    ERROR_ALREADY_EXISTS,
    GUID_KERNEL_NETWORK,
    KERNEL_NETWORK_SEND_IDS,
    KERNEL_NETWORK_RECV_IDS,
    TDH_PROPERTY_FLAG_STRUCT,
    GUID,
    str_to_guid,
    WNODE_HEADER,
    EVENT_TRACE_PROPERTIES,
    EVENT_DESCRIPTOR,
    EVENT_HEADER,
    ETW_BUFFER_CONTEXT,
    EVENT_RECORD,
    PEVENT_RECORD,
    EVENT_RECORD_CALLBACK,
    EVENT_TRACE_HEADER,
    EVENT_TRACE,
    EVENT_TRACE_LOGFILEW,
    TDH_NONSTRUCT_TYPE,
    EVENT_PROPERTY_INFO,
    TRACE_EVENT_INFO,
    PROPERTY_DATA_DESCRIPTOR,
    _configure_advapi32,
    _configure_tdh,
)


class ETWNetworkConsumer:
    """
    ETW Consumer thu thập sự kiện gửi/nhận TCP/UDP từ kernel (Microsoft-Windows-Kernel-Network):
    - Bắt đầu ETW session thời gian thực.
    - Lọc chặt theo ProviderId và xác minh schema trường (PID, size) bằng TDH API.
    - Tổng hợp lưu lượng theo PID trong khoảng thời gian t.
    - Cơ chế tự phục hồi / fallback nếu chạy dưới quyền Non-Admin.
    """

    SESSION_NAME = "NetworkFlow_ETW_Session"

    def __init__(self):
        self.is_running = False
        self.session_handle = ctypes.c_uint64(0)
        self.trace_handle = ctypes.c_uint64(0)
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Bảng tổng hợp dữ liệu: pid -> {'rx_bytes': int, 'tx_bytes': int}
        self._traffic_accumulator: Dict[int, Dict[str, int]] = defaultdict(lambda: {'rx_bytes': 0, 'tx_bytes': 0})

        # Cache schema từ TdhGetEventInformation theo (event_id, version) -> (pid_off, pid_len, size_off, size_len)
        self._kernel_net_guid_bytes = bytes(str_to_guid(GUID_KERNEL_NETWORK))
        self._tdh_schema_cache: Dict[Tuple[int, int], Optional[Tuple[int, int, int, int]]] = {}
        self._tdh_pid_name = ctypes.create_unicode_buffer("PID")
        self._tdh_size_name = ctypes.create_unicode_buffer("size")
        self._tdh_pid_desc = PROPERTY_DATA_DESCRIPTOR(ctypes.addressof(self._tdh_pid_name), 0xFFFFFFFF, 0)
        self._tdh_size_desc = PROPERTY_DATA_DESCRIPTOR(ctypes.addressof(self._tdh_size_name), 0xFFFFFFFF, 0)
        try:
            self._tdh = ctypes.WinDLL("tdh", use_last_error=True)
            _configure_tdh(self._tdh)
        except Exception:
            self._tdh = None

        # Trạng thái fallback khi không có quyền Administrator
        self._use_fallback = False

        # Giữ tham chiếu callback để GC không thu hồi
        self._c_callback = EVENT_RECORD_CALLBACK(self._event_callback)

    def _resolve_tdh_schema(self, p_record: PEVENT_RECORD) -> Optional[Tuple[int, int, int, int]]:
        """Dùng TdhGetEventInformation lấy offset và độ dài thật của trường PID và size từ manifest."""
        if not self._tdh:
            return None
        try:
            buf_size = wintypes.ULONG(0)
            status = self._tdh.TdhGetEventInformation(p_record, 0, None, None, ctypes.byref(buf_size))
            if status != ERROR_INSUFFICIENT_BUFFER or buf_size.value < ctypes.sizeof(TRACE_EVENT_INFO):
                return None

            buf = ctypes.create_string_buffer(buf_size.value)
            status = self._tdh.TdhGetEventInformation(p_record, 0, None, buf, ctypes.byref(buf_size))
            if status != ERROR_SUCCESS:
                return None

            info = ctypes.cast(buf, ctypes.POINTER(TRACE_EVENT_INFO)).contents
            base = ctypes.addressof(buf)
            prop_count = info.TopLevelPropertyCount
            if prop_count <= 0:
                return None

            props = (EVENT_PROPERTY_INFO * prop_count).from_address(base + ctypes.sizeof(TRACE_EVENT_INFO))
            offset = 0
            pid_spec = None
            size_spec = None

            for i in range(prop_count):
                prop = props[i]
                if prop.Flags & TDH_PROPERTY_FLAG_STRUCT or prop.length == 0:
                    break
                name = ctypes.wstring_at(base + prop.NameOffset).lower() if prop.NameOffset else ""
                if name == "pid" and pid_spec is None:
                    pid_spec = (offset, prop.length)
                elif name == "size" and size_spec is None:
                    size_spec = (offset, prop.length)
                if pid_spec is not None and size_spec is not None:
                    return (pid_spec[0], pid_spec[1], size_spec[0], size_spec[1])
                offset += prop.length
        except Exception:
            return None
        return None

    def _get_event_layout(self, p_record: PEVENT_RECORD, event_id: int, version: int) -> Optional[Tuple[int, int, int, int]]:
        key = (event_id, version)
        if key in self._tdh_schema_cache:
            return self._tdh_schema_cache[key]

        resolved = self._resolve_tdh_schema(p_record)
        self._tdh_schema_cache[key] = resolved
        return resolved

    def _extract_via_tdh_property(self, p_record: PEVENT_RECORD) -> Tuple[int, int]:
        """Trích xuất PID và size qua TdhGetProperty khi layout sự kiện không cố định offset."""
        if not self._tdh:
            return 0, 0
        pid_val = ctypes.c_uint32(0)
        size_val = ctypes.c_uint32(0)
        st_size = self._tdh.TdhGetProperty(
            p_record, 0, None, 1, ctypes.byref(self._tdh_size_desc), 4, ctypes.byref(size_val)
        )
        if st_size != ERROR_SUCCESS or size_val.value == 0:
            return 0, 0
        st_pid = self._tdh.TdhGetProperty(
            p_record, 0, None, 1, ctypes.byref(self._tdh_pid_desc), 4, ctypes.byref(pid_val)
        )
        pid = pid_val.value if st_pid == ERROR_SUCCESS else 0
        return pid, size_val.value
    @staticmethod
    def _unpack_uint(raw_bytes: bytes, offset: int, length: int) -> int:
        if offset + length > len(raw_bytes):
            return 0
        if length == 4:
            return struct.unpack_from("<I", raw_bytes, offset)[0]
        if length == 2:
            return struct.unpack_from("<H", raw_bytes, offset)[0]
        if length == 8:
            return struct.unpack_from("<Q", raw_bytes, offset)[0]
        return 0

    def _event_callback(self, p_record: PEVENT_RECORD):
        """Callback được gọi từ kernel mỗi khi có sự kiện mạng TCP/UDP phát sinh."""
        if not p_record:
            return

        header = p_record.contents.EventHeader
        # Lọc chặt theo ProviderId: chỉ xử lý Microsoft-Windows-Kernel-Network
        if bytes(header.ProviderId) != self._kernel_net_guid_bytes:
            return

        event_id = header.EventDescriptor.Id
        is_send = event_id in KERNEL_NETWORK_SEND_IDS
        is_recv = event_id in KERNEL_NETWORK_RECV_IDS
        if not (is_send or is_recv):
            return

        user_data_len = p_record.contents.UserDataLength
        user_data_ptr = p_record.contents.UserData
        if not user_data_ptr or user_data_len <= 0:
            return

        layout = self._get_event_layout(
            p_record, event_id, header.EventDescriptor.Version
        )
        if layout is not None:
            pid_off, pid_len, size_off, size_len = layout
            needed = max(pid_off + pid_len, size_off + size_len)
            if user_data_len < needed:
                return
            try:
                raw_bytes = ctypes.string_at(user_data_ptr, needed)
                pid = self._unpack_uint(raw_bytes, pid_off, pid_len)
                packet_size = self._unpack_uint(raw_bytes, size_off, size_len)
            except Exception:
                return
        else:
            pid, packet_size = self._extract_via_tdh_property(p_record)

        if pid <= 0 or pid >= 0xFFFFFFFF:
            pid = header.ProcessId

        if pid <= 0 or pid >= 0xFFFFFFFF or packet_size <= 0:
            return

        # Tổng hợp byte vào bộ đếm theo PID
        with self._lock:
            if is_send:
                self._traffic_accumulator[pid]['tx_bytes'] += packet_size
            elif is_recv:
                self._traffic_accumulator[pid]['rx_bytes'] += packet_size

    def start(self):
        """Khởi động ETW consumer chạy ngầm trên một background thread."""
        if self.is_running:
            return

        self.is_running = True
        self._worker_thread = threading.Thread(target=self._run_consumer, daemon=True, name="ETW-Consumer-Thread")
        self._worker_thread.start()

    def is_alive(self) -> bool:
        return bool(self.is_running and self._worker_thread and self._worker_thread.is_alive())

    def is_etw_active(self) -> bool:
        """Trả về True chỉ khi phiên ETW Kernel thực sự đang thu thập (không phải fallback psutil)."""
        return bool(
            self.is_running
            and not self._use_fallback
            and self.session_handle.value != 0
            and self.trace_handle.value not in (0, INVALID_PROCESSTRACE_HANDLE, 0xFFFFFFFF)
        )

    def _build_trace_properties(self):
        prop_size = ctypes.sizeof(EVENT_TRACE_PROPERTIES) + 1024
        prop_buf = ctypes.create_string_buffer(prop_size)
        p_props = ctypes.cast(prop_buf, ctypes.POINTER(EVENT_TRACE_PROPERTIES))
        p_props.contents.Wnode.BufferSize = prop_size
        p_props.contents.Wnode.Flags = WNODE_FLAG_TRACED_GUID
        p_props.contents.Wnode.ClientContext = 1
        p_props.contents.Wnode.Guid = str_to_guid(GUID_KERNEL_NETWORK)
        p_props.contents.BufferSize = 64
        p_props.contents.MinimumBuffers = 4
        p_props.contents.MaximumBuffers = 64
        p_props.contents.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
        p_props.contents.FlushTimer = 1
        p_props.contents.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
        return prop_buf, p_props

    def _cleanup_etw_handles(self, advapi32):
        if self.trace_handle.value not in (0, INVALID_PROCESSTRACE_HANDLE, 0xFFFFFFFF):
            advapi32.CloseTrace(self.trace_handle)
            self.trace_handle = ctypes.c_uint64(0)

        if self.session_handle.value != 0:
            _stop_buf, p_stop_props = self._build_trace_properties()
            advapi32.ControlTraceW(
                self.session_handle,
                self.SESSION_NAME,
                p_stop_props,
                EVENT_TRACE_CONTROL_STOP,
            )
            self.session_handle = ctypes.c_uint64(0)

    def _run_consumer(self):
        """Khởi chạy phiên ETW Kernel thời gian thực; tự động fallback nếu thiếu quyền Admin."""
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            _configure_advapi32(advapi32)

            _prop_buf, p_props = self._build_trace_properties()
            status = advapi32.StartTraceW(
                ctypes.byref(self.session_handle),
                self.SESSION_NAME,
                p_props,
            )

            # Nếu session cũ còn sót lại từ lần chạy trước, dừng và khởi tạo lại
            if status == ERROR_ALREADY_EXISTS:
                _stop_buf, p_stop_props = self._build_trace_properties()
                advapi32.ControlTraceW(
                    0,
                    self.SESSION_NAME,
                    p_stop_props,
                    EVENT_TRACE_CONTROL_STOP,
                )
                self.session_handle = ctypes.c_uint64(0)
                _prop_buf, p_props = self._build_trace_properties()
                status = advapi32.StartTraceW(
                    ctypes.byref(self.session_handle),
                    self.SESSION_NAME,
                    p_props,
                )

            if status != ERROR_SUCCESS or self.session_handle.value == 0:
                reason = "thiếu quyền Administrator" if status == ERROR_ACCESS_DENIED else f"mã lỗi Win32={status}"
                print(f"[ETW Warning] Không thể khởi tạo ETW Session ({reason}). Chuyển sang chế độ fallback psutil.")
                self._use_fallback = True
                self._run_fallback_loop()
                return

            guid_kernel_net = str_to_guid(GUID_KERNEL_NETWORK)
            enable_status = advapi32.EnableTraceEx2(
                self.session_handle,
                ctypes.byref(guid_kernel_net),
                EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                TRACE_LEVEL_INFORMATION,
                0xFFFFFFFFFFFFFFFF,
                0,
                0,
                None,
            )
            if enable_status != ERROR_SUCCESS:
                print(f"[ETW Warning] EnableTraceEx2(Kernel-Network) thất bại (mã lỗi={enable_status}). Chuyển sang chế độ fallback psutil.")
                self._cleanup_etw_handles(advapi32)
                self._use_fallback = True
                self._run_fallback_loop()
                return

            logfile = EVENT_TRACE_LOGFILEW()
            logfile.LoggerName = self.SESSION_NAME
            logfile.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
            logfile.EventRecordCallback = self._c_callback

            trace_handle = advapi32.OpenTraceW(ctypes.byref(logfile))
            self.trace_handle = ctypes.c_uint64(trace_handle)
            if self.trace_handle.value in (0, INVALID_PROCESSTRACE_HANDLE, 0xFFFFFFFF):
                err = ctypes.get_last_error()
                print(f"[ETW Warning] OpenTraceW thất bại (mã lỗi={err}). Chuyển sang chế độ fallback psutil.")
                self._cleanup_etw_handles(advapi32)
                self._use_fallback = True
                self._run_fallback_loop()
                return

            self._use_fallback = False
            print(f"[ETW] Đã kích hoạt phiên Kernel ETW '{self.SESSION_NAME}' thành công.")

            handles = (ctypes.c_uint64 * 1)(self.trace_handle.value)
            proc_status = advapi32.ProcessTrace(handles, 1, None, None)
            if self.is_running and proc_status != ERROR_SUCCESS:
                print(f"[ETW Warning] ProcessTrace kết thúc bất thường (mã lỗi={proc_status}). Chuyển sang chế độ fallback psutil.")
                self._cleanup_etw_handles(advapi32)
                self._use_fallback = True
                self._run_fallback_loop()
        except Exception as exc:
            print(f"[ETW Error] Ngoại lệ luồng consumer: {exc}. Chuyển sang chế độ fallback psutil.")
            self._use_fallback = True
            if self.is_running:
                self._run_fallback_loop()

    def _run_fallback_loop(self):
        """
        Chế độ chờ an toàn khi không thể bật Kernel ETW (ví dụ thiếu quyền Administrator).
        Không dùng psutil.Process.io_counters() vì read_bytes/write_bytes bao gồm cả Disk I/O
        và I/O khác, sẽ gán nhầm tiến trình ghi file lớn xuống đĩa thành upload mạng.
        """
        while self.is_running:
            time.sleep(0.5)

    def flush_and_aggregate(self) -> Dict[int, Dict[str, int]]:
        """
        Lấy ra mẫu dữ liệu tổng hợp byte (rx_bytes, tx_bytes) theo từng PID
        trong khoảng thời gian vừa qua, đồng thời reset bộ đếm cho chu kỳ tiếp theo.
        Khi Kernel ETW không hoạt động (_use_fallback=True), trả về {} thay vì bịa số liệu từ Disk I/O.
        """
        with self._lock:
            snapshot = dict(self._traffic_accumulator)
            self._traffic_accumulator.clear()

        if self._use_fallback:
            return {}

        return snapshot

    def stop(self):
        """Dừng ETW Consumer và dọn dẹp Trace Session."""
        self.is_running = False
        try:
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            _configure_advapi32(advapi32)
            self._cleanup_etw_handles(advapi32)
            print("[ETW] Đã dừng ETW Consumer thành công.")
        except Exception as exc:
            print(f"[ETW] Lỗi khi dừng consumer: {exc}")
