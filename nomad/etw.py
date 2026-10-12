"""A real-time Event Tracing for Windows (ETW) session: NOMAD's own listener on a Windows event provider, handed
each event as it's logged. Packet Capture listens to pktmon's provider with one to show packets while it captures.

Built on advapi32 through ctypes (nothing to install). Starting a session needs administrator rights. A session
outlives the process that started it, so one left by a crash is stopped before starting again.
"""
import ctypes
import logging
import os
import sys
import threading
try:
    from ctypes import wintypes
except (ImportError, ValueError):
    wintypes = None

log = logging.getLogger(__name__)

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
ERROR_MORE_DATA = 234
ERROR_CANCELLED = 1223
ERROR_WMI_INSTANCE_NOT_FOUND = 4201
EVENT_TRACE_CONTROL_QUERY, EVENT_TRACE_CONTROL_STOP = 0, 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_TRACE_USE_MS_FLUSH_TIMER = 0x00000010
WNODE_FLAG_TRACED_GUID = 0x00020000
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF
TRACE_LEVEL_VERBOSE = 5
MAX_NAME = 1024  # Characters of room for the session's name
QPC_CLOCK = 1
FILETIME_EPOCH = 11644473600  # Seconds from 1601 (FILETIME's start) to 1970
STOP_WAIT_SECONDS = 5.0

TRACEHANDLE = ctypes.c_uint64


class EtwError(OSError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        """A GUID from its text, like "4d4f80d9-c8bd-4d73-bb5b-19c90402c5ac" (braces optional)."""
        parts = text.strip("{}").split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return cls(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16), (ctypes.c_ubyte * 8)(*tail))


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", wintypes.ULONG), ("ProviderId", wintypes.ULONG), ("HistoricalContext", ctypes.c_uint64),
                ("TimeStamp", ctypes.c_int64), ("Guid", GUID), ("ClientContext", wintypes.ULONG),
                ("Flags", wintypes.ULONG)]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", wintypes.ULONG), ("MinimumBuffers", wintypes.ULONG),
                ("MaximumBuffers", wintypes.ULONG), ("MaximumFileSize", wintypes.ULONG),
                ("LogFileMode", wintypes.ULONG), ("FlushTimer", wintypes.ULONG), ("EnableFlags", wintypes.ULONG),
                ("AgeLimit", wintypes.LONG), ("NumberOfBuffers", wintypes.ULONG), ("FreeBuffers", wintypes.ULONG),
                ("EventsLost", wintypes.ULONG), ("BuffersWritten", wintypes.ULONG),
                ("LogBuffersLost", wintypes.ULONG), ("RealTimeBuffersLost", wintypes.ULONG),
                ("LoggerThreadId", wintypes.HANDLE), ("LogFileNameOffset", wintypes.ULONG),
                ("LoggerNameOffset", wintypes.ULONG)]


class PropertiesBuffer(ctypes.Structure):
    """EVENT_TRACE_PROPERTIES with room after it for the session's name, as StartTrace and ControlTrace want."""
    _fields_ = [("properties", EVENT_TRACE_PROPERTIES), ("name", ctypes.c_wchar * MAX_NAME),
                ("file", ctypes.c_wchar * MAX_NAME)]


class EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [("Size", wintypes.USHORT), ("FieldTypeFlags", wintypes.USHORT), ("Version", wintypes.ULONG),
                ("ThreadId", wintypes.ULONG), ("ProcessId", wintypes.ULONG), ("TimeStamp", ctypes.c_int64),
                ("Guid", GUID), ("ProcessorTime", ctypes.c_uint64)]


class EVENT_TRACE(ctypes.Structure):
    _fields_ = [("Header", EVENT_TRACE_HEADER), ("InstanceId", wintypes.ULONG), ("ParentInstanceId", wintypes.ULONG),
                ("ParentGuid", GUID), ("MofData", ctypes.c_void_p), ("MofLength", wintypes.ULONG),
                ("ClientContext", wintypes.ULONG)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(name, wintypes.WORD) for name in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute",
                                                   "wSecond", "wMilliseconds")]


class TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [("Bias", wintypes.LONG), ("StandardName", ctypes.c_wchar * 32), ("StandardDate", SYSTEMTIME),
                ("StandardBias", wintypes.LONG), ("DaylightName", ctypes.c_wchar * 32), ("DaylightDate", SYSTEMTIME),
                ("DaylightBias", wintypes.LONG)]


class TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", wintypes.ULONG), ("Version", wintypes.ULONG), ("ProviderVersion", wintypes.ULONG),
                ("NumberOfProcessors", wintypes.ULONG), ("EndTime", ctypes.c_int64),
                ("TimerResolution", wintypes.ULONG), ("MaximumFileSize", wintypes.ULONG),
                ("LogFileMode", wintypes.ULONG), ("BuffersWritten", wintypes.ULONG), ("LogInstanceGuid", GUID),
                ("LoggerName", ctypes.c_void_p), ("LogFileName", ctypes.c_void_p),
                ("TimeZone", TIME_ZONE_INFORMATION), ("BootTime", ctypes.c_int64), ("PerfFreq", ctypes.c_int64),
                ("StartTime", ctypes.c_int64), ("ReservedFlags", wintypes.ULONG), ("BuffersLost", wintypes.ULONG)]


class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Id", wintypes.USHORT), ("Version", ctypes.c_ubyte), ("Channel", ctypes.c_ubyte),
                ("Level", ctypes.c_ubyte), ("Opcode", ctypes.c_ubyte), ("Task", wintypes.USHORT),
                ("Keyword", ctypes.c_uint64)]


class EVENT_HEADER(ctypes.Structure):
    _fields_ = [("Size", wintypes.USHORT), ("HeaderType", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("EventProperty", wintypes.USHORT), ("ThreadId", wintypes.ULONG), ("ProcessId", wintypes.ULONG),
                ("TimeStamp", ctypes.c_int64), ("ProviderId", GUID), ("EventDescriptor", EVENT_DESCRIPTOR),
                ("ProcessorTime", ctypes.c_uint64), ("ActivityId", GUID)]


class EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", EVENT_HEADER), ("ProcessorNumber", ctypes.c_ubyte), ("Alignment", ctypes.c_ubyte),
                ("LoggerId", wintypes.USHORT), ("ExtendedDataCount", wintypes.USHORT),
                ("UserDataLength", wintypes.USHORT), ("ExtendedData", ctypes.c_void_p), ("UserData", ctypes.c_void_p),
                ("UserContext", ctypes.c_void_p)]


EVENT_RECORD_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(None, ctypes.POINTER(EVENT_RECORD))


class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [("LogFileName", ctypes.c_wchar_p), ("LoggerName", ctypes.c_wchar_p), ("CurrentTime", ctypes.c_int64),
                ("BuffersRead", wintypes.ULONG), ("ProcessTraceMode", wintypes.ULONG), ("CurrentEvent", EVENT_TRACE),
                ("LogfileHeader", TRACE_LOGFILE_HEADER), ("BufferCallback", ctypes.c_void_p),
                ("BufferSize", wintypes.ULONG), ("Filled", wintypes.ULONG), ("EventsLost", wintypes.ULONG),
                ("EventRecordCallback", EVENT_RECORD_CALLBACK), ("IsKernelTrace", wintypes.ULONG),
                ("Context", ctypes.c_void_p)]


def advapi32():
    """advapi32 with the ETW functions' types set (once)."""
    global _advapi32
    if _advapi32 is None:
        dll = ctypes.WinDLL("advapi32")
        dll.StartTraceW.argtypes = [ctypes.POINTER(TRACEHANDLE), wintypes.LPCWSTR, ctypes.POINTER(PropertiesBuffer)]
        dll.StartTraceW.restype = wintypes.ULONG
        dll.ControlTraceW.argtypes = [TRACEHANDLE, wintypes.LPCWSTR, ctypes.POINTER(PropertiesBuffer),
                                      wintypes.ULONG]
        dll.ControlTraceW.restype = wintypes.ULONG
        dll.EnableTraceEx2.argtypes = [TRACEHANDLE, ctypes.POINTER(GUID), wintypes.ULONG, ctypes.c_ubyte,
                                       ctypes.c_uint64, ctypes.c_uint64, wintypes.ULONG, ctypes.c_void_p]
        dll.EnableTraceEx2.restype = wintypes.ULONG
        dll.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
        dll.OpenTraceW.restype = TRACEHANDLE
        dll.ProcessTrace.argtypes = [ctypes.POINTER(TRACEHANDLE), wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p]
        dll.ProcessTrace.restype = wintypes.ULONG
        dll.CloseTrace.argtypes = [TRACEHANDLE]
        dll.CloseTrace.restype = wintypes.ULONG
        _advapi32 = dll
    return _advapi32


_advapi32 = None


def properties(name, buffer_kb=0, minimum_buffers=0, maximum_buffers=0, flush_ms=0):
    """A PropertiesBuffer for a real-time session called name."""
    buffer = PropertiesBuffer()
    props = buffer.properties
    props.Wnode.BufferSize = ctypes.sizeof(PropertiesBuffer)
    props.Wnode.Flags = WNODE_FLAG_TRACED_GUID
    props.Wnode.ClientContext = QPC_CLOCK
    props.LogFileMode = EVENT_TRACE_REAL_TIME_MODE | (EVENT_TRACE_USE_MS_FLUSH_TIMER if flush_ms else 0)
    props.BufferSize, props.MinimumBuffers, props.MaximumBuffers = buffer_kb, minimum_buffers, maximum_buffers
    props.FlushTimer = flush_ms
    props.LoggerNameOffset = PropertiesBuffer.name.offset
    props.LogFileNameOffset = 0
    buffer.name = name[:MAX_NAME - 1]
    return buffer


def stop_session(name):
    """Stop the session called name, if there is one. True if one was running."""
    status = advapi32().ControlTraceW(0, name, ctypes.byref(properties(name)), EVENT_TRACE_CONTROL_STOP)
    if status not in (ERROR_SUCCESS, ERROR_WMI_INSTANCE_NOT_FOUND):
        log.warning("Stopping the ETW session %s: error %s", name, status)
    return status == ERROR_SUCCESS


def filetime_seconds(timestamp):
    """Seconds since 1970 from an event's timestamp (100 ns units since 1601)."""
    return timestamp / 1e7 - FILETIME_EPOCH


class RealtimeSession:
    """Listen to one provider's events: start() it, and on_event(event id, seconds since 1970, the event's data
    (bytes)) is called for each on a thread of its own until stop(). on_event must be quick: ETW drops events for a
    listener that falls behind (lost() counts them)."""

    def __init__(self, name, provider, keywords, on_event, level=TRACE_LEVEL_VERBOSE, buffer_kb=128,
                 minimum_buffers=16, maximum_buffers=256, flush_ms=250):
        self.name, self.provider, self.keywords, self.level = name, GUID.parse(provider), keywords, level
        self.on_event = on_event
        self.sizes = (buffer_kb, minimum_buffers, maximum_buffers, flush_ms)
        self.session = TRACEHANDLE(0)
        self.trace = TRACEHANDLE(INVALID_PROCESSTRACE_HANDLE)
        self.callback = EVENT_RECORD_CALLBACK(self.event)  # Kept: ETW calls it until the trace is closed
        self.logfile = None
        self.thread = None
        self.provider_data1 = self.provider.Data1
        self.failures = 0  # Events on_event raised on (logged once)
        self.running = False

    def start(self):
        """Raises EtwError (ERROR_ACCESS_DENIED without administrator rights)."""
        dll = advapi32()
        stop_session(self.name)  # One left running by a crash
        buffer = properties(self.name, *self.sizes)
        status = dll.StartTraceW(ctypes.byref(self.session), self.name, ctypes.byref(buffer))
        if status == ERROR_ALREADY_EXISTS:
            stop_session(self.name)
            buffer = properties(self.name, *self.sizes)
            status = dll.StartTraceW(ctypes.byref(self.session), self.name, ctypes.byref(buffer))
        if status != ERROR_SUCCESS:
            raise EtwError(status, f"Couldn't start listening to Windows' events ({describe(status)}).")
        self.running = True
        try:
            status = dll.EnableTraceEx2(self.session, ctypes.byref(self.provider), EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                        self.level, self.keywords, 0, 0, None)
            if status != ERROR_SUCCESS:
                raise EtwError(status, f"Couldn't listen to the packet monitor's events ({describe(status)}).")
            logfile = EVENT_TRACE_LOGFILEW()
            logfile.LoggerName = self.name
            logfile.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
            logfile.EventRecordCallback = self.callback
            self.logfile = logfile
            handle = dll.OpenTraceW(ctypes.byref(logfile))
            if handle == INVALID_PROCESSTRACE_HANDLE:
                error = ctypes.GetLastError()
                raise EtwError(error, f"Couldn't open the event session ({describe(error)}).")
            self.trace = TRACEHANDLE(handle)
        except BaseException:
            self.stop()
            raise
        self.thread = threading.Thread(target=self.process, name=f"ETW {self.name}", daemon=True)
        self.thread.start()
        log.info("ETW session %s listening", self.name)

    def process(self):
        status = advapi32().ProcessTrace(ctypes.byref(self.trace), 1, None, None)
        if status not in (ERROR_SUCCESS, ERROR_CANCELLED):
            log.warning("ETW session %s stopped delivering events: error %s", self.name, status)

    def event(self, record):
        try:
            header = record.contents.EventHeader
            if header.ProviderId.Data1 != self.provider_data1:  # Not the provider's (the session's own header)
                return
            data = record.contents
            payload = ctypes.string_at(data.UserData, data.UserDataLength) if data.UserData else b""
            self.on_event(header.EventDescriptor.Id, filetime_seconds(header.TimeStamp), payload)
        except Exception:  # Raising into ETW would print a traceback per event
            self.failures += 1
            if self.failures == 1:
                log.exception("Handling an event from %s failed", self.name)

    def lost(self):
        """(events lost, buffers lost) by this session so far: what ETW dropped because the listener fell behind."""
        if not self.running:
            return 0, 0
        buffer = properties(self.name)
        status = advapi32().ControlTraceW(self.session, None, ctypes.byref(buffer), EVENT_TRACE_CONTROL_QUERY)
        if status != ERROR_SUCCESS:
            return 0, 0
        return buffer.properties.EventsLost, buffer.properties.RealTimeBuffersLost

    def stop(self):
        dll = advapi32()
        if self.running:
            self.running = False
            status = dll.ControlTraceW(self.session, None, ctypes.byref(properties(self.name)),
                                       EVENT_TRACE_CONTROL_STOP)
            if status not in (ERROR_SUCCESS, ERROR_WMI_INSTANCE_NOT_FOUND, ERROR_MORE_DATA):
                log.warning("Stopping the ETW session %s: error %s", self.name, status)
        if self.trace.value != INVALID_PROCESSTRACE_HANDLE:
            dll.CloseTrace(self.trace)
            self.trace = TRACEHANDLE(INVALID_PROCESSTRACE_HANDLE)
        thread, self.thread = self.thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_WAIT_SECONDS)
            if thread.is_alive():
                log.warning("ETW session %s's listener didn't finish", self.name)


def describe(status):
    if status == ERROR_ACCESS_DENIED:
        return "NOMAD needs to run as administrator"
    try:
        return ctypes.FormatError(status).strip().rstrip(".")
    except (ValueError, OSError):
        return f"error {status}"
