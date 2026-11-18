"""
Bộ Giải mã Schema & Trích xuất Thuộc tính Sự kiện ETW qua TDH API (ETW TDH Parser - Infrastructure Layer):
- TdhGetEventInformation: Tra cứu offset và độ dài thật của trường PID và size từ manifest hệ điều hành.
- TdhGetProperty: Trích xuất động khi layout sự kiện không cố định offset.
- unpack_uint: Giải nén số nguyên 2 / 4 / 8 byte Little-Endian từ vùng nhớ UserData.
"""

import struct
import ctypes
from ctypes import wintypes
from typing import Dict, Optional, Tuple

from infrastructure.etw_ctypes import (
    ERROR_SUCCESS,
    ERROR_INSUFFICIENT_BUFFER,
    TDH_PROPERTY_FLAG_STRUCT,
    PEVENT_RECORD,
    EVENT_PROPERTY_INFO,
    TRACE_EVENT_INFO,
    PROPERTY_DATA_DESCRIPTOR,
    _configure_tdh,
)


def unpack_uint(raw_bytes: bytes, offset: int, length: int) -> int:
    """Giải nén số nguyên không dấu (2, 4, hoặc 8 bytes) từ buffer nhị phân."""
    if offset + length > len(raw_bytes):
        return 0
    if length == 4:
        return struct.unpack_from("<I", raw_bytes, offset)[0]
    if length == 2:
        return struct.unpack_from("<H", raw_bytes, offset)[0]
    if length == 8:
        return struct.unpack_from("<Q", raw_bytes, offset)[0]
    return 0


class ETWTdhSchemaParser:
    """Bộ phân tích schema TDH và bóc tách (PID, packet_size) từ PEVENT_RECORD."""

    def __init__(self):
        self.schema_cache: Dict[Tuple[int, int], Optional[Tuple[int, int, int, int]]] = {}
        self._tdh_pid_name = ctypes.create_unicode_buffer("PID")
        self._tdh_size_name = ctypes.create_unicode_buffer("size")
        self._tdh_pid_desc = PROPERTY_DATA_DESCRIPTOR(ctypes.addressof(self._tdh_pid_name), 0xFFFFFFFF, 0)
        self._tdh_size_desc = PROPERTY_DATA_DESCRIPTOR(ctypes.addressof(self._tdh_size_name), 0xFFFFFFFF, 0)
        try:
            self._tdh = ctypes.WinDLL("tdh", use_last_error=True)
            _configure_tdh(self._tdh)
        except Exception:
            self._tdh = None

    def resolve_tdh_schema(self, p_record: PEVENT_RECORD) -> Optional[Tuple[int, int, int, int]]:
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

    def get_event_layout(
        self, p_record: PEVENT_RECORD, event_id: int, version: int
    ) -> Optional[Tuple[int, int, int, int]]:
        key = (event_id, version)
        if key in self.schema_cache:
            return self.schema_cache[key]

        resolved = self.resolve_tdh_schema(p_record)
        self.schema_cache[key] = resolved
        return resolved

    def extract_via_tdh_property(self, p_record: PEVENT_RECORD) -> Tuple[int, int]:
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

    def parse_pid_and_size(
        self,
        p_record: PEVENT_RECORD,
        event_id: int,
        version: int,
        user_data_ptr: int,
        user_data_len: int,
    ) -> Optional[Tuple[int, int]]:
        """Giải mã (pid, packet_size) từ bản ghi sự kiện ETW."""
        layout = self.get_event_layout(p_record, event_id, version)
        if layout is not None:
            pid_off, pid_len, size_off, size_len = layout
            needed = max(pid_off + pid_len, size_off + size_len)
            if user_data_len < needed:
                return None
            try:
                raw_bytes = ctypes.string_at(user_data_ptr, needed)
                pid = unpack_uint(raw_bytes, pid_off, pid_len)
                packet_size = unpack_uint(raw_bytes, size_off, size_len)
                return pid, packet_size
            except Exception:
                return None
        return self.extract_via_tdh_property(p_record)
