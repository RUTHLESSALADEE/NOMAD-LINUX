"""TCP connections and listening TCP/UDP ports with the program that owns each (what netstat -ano shows).

Reads the tables straight from the IP Helper API, so it's quick enough to refresh every couple of seconds
and doesn't depend on the Windows display language.
"""
import ctypes
import ipaddress
import socket
from ctypes import wintypes
from dataclasses import dataclass

TCP = "TCP"
UDP = "UDP"
LISTENING = "Listening"
TCP_STATES = {1: "Closed", 2: LISTENING, 3: "SYN sent", 4: "SYN received", 5: "Established", 6: "FIN wait 1",
              7: "FIN wait 2", 8: "Close wait", 9: "Closing", 10: "Last ACK", 11: "Time wait", 12: "Delete TCB"}
TCP_TABLE_OWNER_PID_ALL = 5
UDP_TABLE_OWNER_PID = 1
ERROR_INSUFFICIENT_BUFFER = 122
SYSTEM_PROCESSES = {0: "System Idle Process", 4: "System"}


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("ucRemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwState", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD)]


class MIB_UDPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class MIB_UDP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


@dataclass
class Connection:
    protocol: str  # TCP or UDP
    local_address: str
    local_port: int
    remote_address: str  # "" for UDP and listening sockets
    remote_port: int  # 0 for UDP and listening sockets
    state: str  # TCP state; "" for UDP
    pid: int
    process: str = ""

    @property
    def family(self):
        return 6 if ":" in self.local_address else 4

    @property
    def listening(self):
        """Waiting for connections (TCP) or bound to a port (UDP)."""
        return self.state == LISTENING or self.protocol == UDP

    def search_text(self):
        return " ".join(str(value).lower() for value in (
            self.protocol, self.local_address, self.local_port, self.remote_address,
            self.remote_port if self.remote_address else "", self.state, self.pid, self.process))


def port_from_dword(value):
    """Ports are stored in network byte order in the low 16 bits."""
    return socket.ntohs(value & 0xFFFF)


def ipv4_from_dword(value):
    return socket.inet_ntoa(value.to_bytes(4, "little"))


def ipv6_text(raw, scope):
    address = str(ipaddress.IPv6Address(bytes(raw)))
    return f"{address}%{scope}" if scope and address.startswith("fe80") else address


def matches(connection, text):
    """Whether every word of a filter appears somewhere in the connection's columns.

    A word that's a number matches a port or PID exactly, so "443" doesn't find port 4430.
    """
    haystack = connection.search_text()
    numbers = {str(connection.local_port), str(connection.pid)}
    if connection.remote_address:
        numbers.add(str(connection.remote_port))
    for word in text.lower().split():
        if word.isdigit():
            if word not in numbers:
                return False
        elif word not in haystack:
            return False
    return True


def _read_table(function, family, table_class, row_class):
    size = wintypes.DWORD(0)
    buffer = None
    for _ in range(5):  # The table can grow between asking for its size and reading it
        result = function(buffer, ctypes.byref(size), False, family, table_class, 0)
        if result == 0:
            break
        if result != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(result, ctypes.FormatError(result))
        buffer = ctypes.create_string_buffer(size.value)
    else:
        raise OSError(ERROR_INSUFFICIENT_BUFFER, "The connection table kept changing size.")
    if buffer is None:
        return []
    count = wintypes.DWORD.from_buffer(buffer).value
    rows = (row_class * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
    return list(rows)


def process_names():
    """{pid: executable name} for every running process (names are visible without administrator rights)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    TH32CS_SNAPPROCESS = 0x2
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return dict(SYSTEM_PROCESSES)
    names = dict(SYSTEM_PROCESSES)
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.setdefault(entry.th32ProcessID, entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def list_connections():
    """Every TCP connection and listening socket, and every UDP endpoint, with its owning process."""
    api = ctypes.WinDLL("iphlpapi")
    connections = []
    for row in _read_table(api.GetExtendedTcpTable, socket.AF_INET, TCP_TABLE_OWNER_PID_ALL, MIB_TCPROW_OWNER_PID):
        state = TCP_STATES.get(row.dwState, str(row.dwState))
        remote = "" if state == LISTENING else ipv4_from_dword(row.dwRemoteAddr)
        connections.append(Connection(TCP, ipv4_from_dword(row.dwLocalAddr), port_from_dword(row.dwLocalPort),
                                      remote, 0 if state == LISTENING else port_from_dword(row.dwRemotePort),
                                      state, row.dwOwningPid))
    for row in _read_table(api.GetExtendedTcpTable, socket.AF_INET6, TCP_TABLE_OWNER_PID_ALL,
                           MIB_TCP6ROW_OWNER_PID):
        state = TCP_STATES.get(row.dwState, str(row.dwState))
        remote = "" if state == LISTENING else ipv6_text(row.ucRemoteAddr, row.dwRemoteScopeId)
        connections.append(Connection(TCP, ipv6_text(row.ucLocalAddr, row.dwLocalScopeId),
                                      port_from_dword(row.dwLocalPort), remote,
                                      0 if state == LISTENING else port_from_dword(row.dwRemotePort),
                                      state, row.dwOwningPid))
    for row in _read_table(api.GetExtendedUdpTable, socket.AF_INET, UDP_TABLE_OWNER_PID, MIB_UDPROW_OWNER_PID):
        connections.append(Connection(UDP, ipv4_from_dword(row.dwLocalAddr), port_from_dword(row.dwLocalPort),
                                      "", 0, "", row.dwOwningPid))
    for row in _read_table(api.GetExtendedUdpTable, socket.AF_INET6, UDP_TABLE_OWNER_PID, MIB_UDP6ROW_OWNER_PID):
        connections.append(Connection(UDP, ipv6_text(row.ucLocalAddr, row.dwLocalScopeId),
                                      port_from_dword(row.dwLocalPort), "", 0, "", row.dwOwningPid))
    names = process_names()
    for connection in connections:
        connection.process = names.get(connection.pid, "")
    return connections
