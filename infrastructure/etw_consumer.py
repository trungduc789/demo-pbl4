"""
ETW Consumer thu thập sự kiện gửi/nhận TCP/UDP từ kernel (Microsoft-Windows-Kernel-Network):
- Quản lý vòng đời ETW session thời gian thực (StartTraceW, EnableTraceEx2, OpenTraceW, ProcessTrace).
- Ủy quyền giải mã schema TDH và trích xuất trường (PID, size) cho `infrastructure.etw_tdh_parser`.
- Tổng hợp lưu lượng theo PID và tự động fallback an toàn nếu thiếu quyền Administrator.
"""

import time
import ctypes
import threading
from collections import defaultdict
from typing import Dict, Optional, Tuple

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
from infrastructure.etw_tdh_parser import ETWTdhSchemaParser, unpack_uint


class ETWNetworkConsumer:
    """
    ETW Consumer thu thập sự kiện gửi/nhận TCP/UDP từ kernel (Microsoft-Windows-Kernel-Network).
    """

    SESSION_NAME = "NetworkFlow_ETW_Session"

    def __init__(self):
        self.is_running = False
        self.session_handle = ctypes.c_uint64(0)
        self.trace_handle = ctypes.c_uint64(0)
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Bảng tổng hợp dữ liệu: pid -> {'rx_bytes': int, 'tx_bytes': int}
        self._traffic_accumulator: Dict[int, Dict[str, int]] = defaultdict(
            lambda: {"rx_bytes": 0, "tx_bytes": 0}
        )

        # Bộ phân tích TDH Schema
        self._kernel_net_guid_bytes = bytes(str_to_guid(GUID_KERNEL_NETWORK))
        self._tdh_parser = ETWTdhSchemaParser()
        self._tdh_schema_cache = self._tdh_parser.schema_cache

        # Trạng thái fallback khi không có quyền Administrator
        self._use_fallback = False

        # Giữ tham chiếu callback để GC không thu hồi
        self._c_callback = EVENT_RECORD_CALLBACK(self._event_callback)

    @staticmethod
    def _unpack_uint(raw_bytes: bytes, offset: int, length: int) -> int:
        return unpack_uint(raw_bytes, offset, length)

    def _event_callback(self, p_record: PEVENT_RECORD):
        """Callback được gọi từ kernel mỗi khi có sự kiện mạng TCP/UDP phát sinh."""
        if not p_record:
            return

        header = p_record.contents.EventHeader
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

        parsed = self._tdh_parser.parse_pid_and_size(
            p_record,
            event_id,
            header.EventDescriptor.Version,
            user_data_ptr,
            user_data_len,
        )
        if parsed is None:
            return
        pid, packet_size = parsed

        if pid <= 0 or pid >= 0xFFFFFFFF:
            pid = header.ProcessId

        if pid <= 0 or pid >= 0xFFFFFFFF or packet_size <= 0:
            return

        with self._lock:
            if is_send:
                self._traffic_accumulator[pid]["tx_bytes"] += packet_size
            elif is_recv:
                self._traffic_accumulator[pid]["rx_bytes"] += packet_size

    def start(self):
        """Khởi động ETW consumer chạy ngầm trên một background thread."""
        if self.is_running:
            return

        self.is_running = True
        self._worker_thread = threading.Thread(
            target=self._run_consumer, daemon=True, name="ETW-Consumer-Thread"
        )
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
                print(
                    f"[ETW Warning] EnableTraceEx2(Kernel-Network) thất bại (mã lỗi={enable_status}). Chuyển sang chế độ fallback psutil."
                )
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
                print(
                    f"[ETW Warning] ProcessTrace kết thúc bất thường (mã lỗi={proc_status}). Chuyển sang chế độ fallback psutil."
                )
                self._cleanup_etw_handles(advapi32)
                self._use_fallback = True
                self._run_fallback_loop()
        except Exception as exc:
            print(f"[ETW Error] Ngoại lệ luồng consumer: {exc}. Chuyển sang chế độ fallback psutil.")
            self._use_fallback = True
            if self.is_running:
                self._run_fallback_loop()

    def _run_fallback_loop(self):
        """Chế độ chờ an toàn khi không thể bật Kernel ETW (không dùng Disk I/O làm sai lệch số liệu)."""
        while self.is_running:
            time.sleep(0.5)

    def flush_and_aggregate(self) -> Dict[int, Dict[str, int]]:
        """Lấy ra mẫu dữ liệu tổng hợp byte (rx_bytes, tx_bytes) theo từng PID và reset bộ đếm."""
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
