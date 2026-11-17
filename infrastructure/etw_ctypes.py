"""
Cấu trúc dữ liệu Ctypes, Hằng số & Khai báo hàm Win32 cho ETW / TDH (Event Tracing for Windows):
- Cung cấp các struct Win32 chuẩn 64-bit/32-bit cho StartTraceW, EnableTraceEx2, OpenTraceW, ProcessTrace.
- Cung cấp các struct TDH (Trace Data Helper) để giải mã động schema sự kiện Microsoft-Windows-Kernel-Network.
"""

import ctypes
from ctypes import wintypes

WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_TRACE_CONTROL_QUERY = 0
EVENT_TRACE_CONTROL_STOP = 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
TRACE_LEVEL_INFORMATION = 4
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183

# GUID của Network Provider trên Windows: Microsoft-Windows-Kernel-Network
GUID_KERNEL_NETWORK = "{7DD42A49-5329-4832-8DFD-43D979153A88}"
KERNEL_NETWORK_SEND_IDS = frozenset((10, 26, 42, 58))
KERNEL_NETWORK_RECV_IDS = frozenset((11, 27, 43, 59))

TDH_PROPERTY_FLAG_STRUCT = 0x1


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


def str_to_guid(guid_str: str) -> GUID:
    clean = guid_str.strip("{}")
    parts = clean.split("-")
    g = GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    b = bytes.fromhex(parts[3] + parts[4])
    for i in range(8):
        g.Data4[i] = b[i]
    return g


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [
        ("BufferSize", wintypes.ULONG),
        ("ProviderId", wintypes.ULONG),
        ("HistoricalContext", ctypes.c_uint64),
        ("TimeStamp", wintypes.LARGE_INTEGER),
        ("Guid", GUID),
        ("ClientContext", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
    ]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("Wnode", WNODE_HEADER),
        ("BufferSize", wintypes.ULONG),
        ("MinimumBuffers", wintypes.ULONG),
        ("MaximumBuffers", wintypes.ULONG),
        ("MaximumFileSize", wintypes.ULONG),
        ("LogFileMode", wintypes.ULONG),
        ("FlushTimer", wintypes.ULONG),
        ("EnableFlags", wintypes.ULONG),
        ("AgeLimit", wintypes.LONG),
        ("NumberOfBuffers", wintypes.ULONG),
        ("FreeBuffers", wintypes.ULONG),
        ("EventsLost", wintypes.ULONG),
        ("BuffersWritten", wintypes.ULONG),
        ("LogBuffersLost", wintypes.ULONG),
        ("RealTimeBuffersLost", wintypes.ULONG),
        ("LoggerThreadId", wintypes.HANDLE),
        ("LogFileNameOffset", wintypes.ULONG),
        ("LoggerNameOffset", wintypes.ULONG),
    ]


class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Id", wintypes.USHORT),
        ("Version", ctypes.c_uint8),
        ("Channel", ctypes.c_uint8),
        ("Level", ctypes.c_uint8),
        ("Opcode", ctypes.c_uint8),
        ("Task", wintypes.USHORT),
        ("Keyword", ctypes.c_uint64),
    ]


class EVENT_HEADER(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.USHORT),
        ("HeaderType", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("EventProperty", wintypes.USHORT),
        ("ThreadId", wintypes.ULONG),
        ("ProcessId", wintypes.ULONG),
        ("TimeStamp", wintypes.LARGE_INTEGER),
        ("ProviderId", GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR),
        ("KernelTime", wintypes.ULONG),
        ("UserTime", wintypes.ULONG),
        ("ActivityId", GUID),
    ]


class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("ProcessorNumber", ctypes.c_uint8),
        ("Alignment", ctypes.c_uint8),
        ("LoggerId", wintypes.USHORT),
    ]


class EVENT_RECORD(ctypes.Structure):
    pass


PEVENT_RECORD = ctypes.POINTER(EVENT_RECORD)
EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, PEVENT_RECORD)

EVENT_RECORD._fields_ = [
    ("EventHeader", EVENT_HEADER),
    ("BufferContext", ETW_BUFFER_CONTEXT),
    ("ExtendedDataCount", wintypes.USHORT),
    ("UserDataLength", wintypes.USHORT),
    ("ExtendedData", ctypes.c_void_p),
    ("UserData", ctypes.c_void_p),
    ("UserContext", ctypes.c_void_p),
]


class EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.USHORT),
        ("HeaderType", ctypes.c_uint8),
        ("Flags", ctypes.c_uint8),
        ("ClassType", ctypes.c_uint8),
        ("ClassLevel", ctypes.c_uint8),
        ("ClassVersion", wintypes.USHORT),
        ("ThreadId", wintypes.ULONG),
        ("ProcessId", wintypes.ULONG),
        ("TimeStamp", wintypes.LARGE_INTEGER),
        ("Guid", GUID),
        ("KernelTime", wintypes.ULONG),
        ("UserTime", wintypes.ULONG),
    ]


class EVENT_TRACE(ctypes.Structure):
    _fields_ = [
        ("Header", EVENT_TRACE_HEADER),
        ("InstanceId", wintypes.ULONG),
        ("ParentInstanceId", wintypes.ULONG),
        ("ParentGuid", GUID),
        ("MofData", ctypes.c_void_p),
        ("MofLength", wintypes.ULONG),
        ("ClientContext", wintypes.ULONG),
    ]


class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [
        ("LogFileName", wintypes.LPWSTR),
        ("LoggerName", wintypes.LPWSTR),
        ("CurrentTime", ctypes.c_int64),
        ("BuffersRead", wintypes.ULONG),
        ("ProcessTraceMode", wintypes.ULONG),
        ("CurrentEvent", EVENT_TRACE),
        ("LogfileHeader", ctypes.c_char * 280),
        ("BufferCallback", ctypes.c_void_p),
        ("BufferSize", wintypes.ULONG),
        ("Filled", wintypes.ULONG),
        ("EventsLost", wintypes.ULONG),
        ("EventRecordCallback", EVENT_RECORD_CALLBACK),
        ("IsKernelTrace", wintypes.ULONG),
        ("Context", ctypes.c_void_p),
    ]


class TDH_NONSTRUCT_TYPE(ctypes.Structure):
    _fields_ = [
        ("InType", wintypes.USHORT),
        ("OutType", wintypes.USHORT),
        ("MapNameOffset", wintypes.ULONG),
    ]


class EVENT_PROPERTY_INFO(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.ULONG),
        ("NameOffset", wintypes.ULONG),
        ("nonStructType", TDH_NONSTRUCT_TYPE),
        ("count", wintypes.USHORT),
        ("length", wintypes.USHORT),
        ("Reserved", wintypes.ULONG),
    ]


class TRACE_EVENT_INFO(ctypes.Structure):
    _fields_ = [
        ("ProviderGuid", GUID),
        ("EventGuid", GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR),
        ("DecodingSource", wintypes.ULONG),
        ("ProviderNameOffset", wintypes.ULONG),
        ("LevelNameOffset", wintypes.ULONG),
        ("ChannelNameOffset", wintypes.ULONG),
        ("KeywordsNameOffset", wintypes.ULONG),
        ("TaskNameOffset", wintypes.ULONG),
        ("OpcodeNameOffset", wintypes.ULONG),
        ("EventMessageOffset", wintypes.ULONG),
        ("ProviderMessageOffset", wintypes.ULONG),
        ("BinaryXMLOffset", wintypes.ULONG),
        ("BinaryXMLSize", wintypes.ULONG),
        ("EventNameOffset", wintypes.ULONG),
        ("EventAttributesOffset", wintypes.ULONG),
        ("PropertyCount", wintypes.ULONG),
        ("TopLevelPropertyCount", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
    ]


class PROPERTY_DATA_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("PropertyName", ctypes.c_uint64),
        ("ArrayIndex", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
    ]


def _configure_advapi32(advapi32):
    """Thiết lập argtypes/restype chuẩn cho các hàm Win32 ETW trên x64/x86."""
    TRACEHANDLE = ctypes.c_uint64
    advapi32.StartTraceW.argtypes = [
        ctypes.POINTER(TRACEHANDLE),
        wintypes.LPCWSTR,
        ctypes.POINTER(EVENT_TRACE_PROPERTIES),
    ]
    advapi32.StartTraceW.restype = wintypes.ULONG

    advapi32.ControlTraceW.argtypes = [
        TRACEHANDLE,
        wintypes.LPCWSTR,
        ctypes.POINTER(EVENT_TRACE_PROPERTIES),
        wintypes.ULONG,
    ]
    advapi32.ControlTraceW.restype = wintypes.ULONG

    advapi32.EnableTraceEx2.argtypes = [
        TRACEHANDLE,
        ctypes.POINTER(GUID),
        wintypes.ULONG,
        ctypes.c_uint8,
        ctypes.c_uint64,
        ctypes.c_uint64,
        wintypes.ULONG,
        ctypes.c_void_p,
    ]
    advapi32.EnableTraceEx2.restype = wintypes.ULONG

    advapi32.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
    advapi32.OpenTraceW.restype = TRACEHANDLE

    advapi32.ProcessTrace.argtypes = [
        ctypes.POINTER(TRACEHANDLE),
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.ProcessTrace.restype = wintypes.ULONG

    advapi32.CloseTrace.argtypes = [TRACEHANDLE]
    advapi32.CloseTrace.restype = wintypes.ULONG


def _configure_tdh(tdh):
    """Thiết lập argtypes/restype cho TdhGetEventInformation và TdhGetProperty."""
    tdh.TdhGetEventInformation.argtypes = [
        PEVENT_RECORD,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
    ]
    tdh.TdhGetEventInformation.restype = wintypes.ULONG

    tdh.TdhGetProperty.argtypes = [
        PEVENT_RECORD,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(PROPERTY_DATA_DESCRIPTOR),
        wintypes.ULONG,
        ctypes.c_void_p,
    ]
    tdh.TdhGetProperty.restype = wintypes.ULONG
