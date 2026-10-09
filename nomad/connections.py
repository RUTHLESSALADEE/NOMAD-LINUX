"""TCP connections and listening TCP/UDP ports with the program that owns each (what netstat -ano shows).

Reads the tables straight from the IP Helper API on Windows or /proc/net on Linux, so it's quick enough
to refresh every couple of seconds and doesn't depend on the Windows display language.
"""
import ctypes
import glob
import ipaddress
import os
import socket
import struct
import sys
from dataclasses import dataclass

if sys.platform == "win32":
    from ctypes import wintypes
else:
    class _WinTypes:
        DWORD = ctypes.c_uint32
        HANDLE = ctypes.c_void_p
        c_wchar = ctypes.c_wchar
        c_long = ctypes.c_long
        c_size_t = ctypes.c_size_t
        c_ubyte = ctypes.c_ubyte
    wintypes = _WinTypes()

TCP = "TCP"
UDP = "UDP"
LISTENING = "Listening"
TCP_STATES = {1: "Established", 2: "SYN sent", 3: "SYN received", 4: "FIN wait 1",
              5: "FIN wait 2", 6: "Time wait", 7: "Closed", 8: "Close wait",
              9: "Last ACK", 10: LISTENING, 11: "Closing"}
# Note: Windows MIB states have different numeric IDs:
WIN_TCP_STATES = {1: "Closed", 2: LISTENING, 3: "SYN sent", 4: "SYN received", 5: "Established", 6: "FIN wait 1",
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
    if sys.platform != "win32":
        names = {}
        for proc_path in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(os.path.basename(proc_path))
                exe_link = os.path.join(proc_path, "exe")
                if os.path.islink(exe_link):
                    try:
                        exe_name = os.path.basename(os.readlink(exe_link))
                        if exe_name:
                            names[pid] = exe_name
                            continue
                    except OSError:
                        pass
                comm_path = os.path.join(proc_path, "comm")
                with open(comm_path, "r", encoding="utf-8", errors="replace") as f:
                    names[pid] = f.read().strip()
            except (OSError, ValueError):
                pass
        return names

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


def _linux_parse_ipv4(hex_str):
    return socket.inet_ntoa(struct.pack("<I", int(hex_str, 16)))


def _linux_parse_ipv6(hex_str):
    raw = bytes.fromhex(hex_str)
    words = struct.unpack("<4I", raw)
    be = struct.pack(">4I", *words)
    return str(ipaddress.IPv6Address(be))


def _linux_socket_inodes():
    """Map socket inode string to owning PID."""
    inode_to_pid = {}
    for fd_path in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            target = os.readlink(fd_path)
            if target.startswith("socket:["):
                inode = target[8:-1]
                pid = int(fd_path.split("/")[2])
                inode_to_pid[inode] = pid
        except OSError:
            pass
    return inode_to_pid


def _linux_list_connections():
    inode_to_pid = _linux_socket_inodes()
    names = process_names()
    connections = []

    files = [
        ("/proc/net/tcp", TCP, 4),
        ("/proc/net/tcp6", TCP, 6),
        ("/proc/net/udp", UDP, 4),
        ("/proc/net/udp6", UDP, 6),
    ]

    for path, proto, family in files:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        if len(lines) <= 1:
            continue
        for line in lines[1:]:
            parts = line.strip().split()
            if len(parts) < 10:
                continue
            local_addr_str, local_port_str = parts[1].split(":")
            remote_addr_str, remote_port_str = parts[2].split(":")
            st_hex = int(parts[3], 16)
            inode = parts[9]

            local_port = int(local_port_str, 16)
            remote_port = int(remote_port_str, 16)

            if family == 4:
                local_addr = _linux_parse_ipv4(local_addr_str)
                remote_addr = _linux_parse_ipv4(remote_addr_str)
            else:
                local_addr = _linux_parse_ipv6(local_addr_str)
                remote_addr = _linux_parse_ipv6(remote_addr_str)

            if proto == TCP:
                state = TCP_STATES.get(st_hex, str(st_hex))
                if state == LISTENING:
                    remote_addr = ""
                    remote_port = 0
            else:
                state = ""
                remote_addr = ""
                remote_port = 0

            pid = inode_to_pid.get(inode, 0)
            proc_name = names.get(pid, "")

            connections.append(Connection(
                protocol=proto,
                local_address=local_addr,
                local_port=local_port,
                remote_address=remote_addr,
                remote_port=remote_port,
                state=state,
                pid=pid,
                process=proc_name
            ))
    return connections


def list_connections():
    """Every TCP connection and listening socket, and every UDP endpoint, with its owning process."""
    if sys.platform != "win32":
        return _linux_list_connections()

    api = ctypes.WinDLL("iphlpapi")
    connections = []
    for row in _read_table(api.GetExtendedTcpTable, socket.AF_INET, TCP_TABLE_OWNER_PID_ALL, MIB_TCPROW_OWNER_PID):
        state = WIN_TCP_STATES.get(row.dwState, str(row.dwState))
        remote = "" if state == LISTENING else ipv4_from_dword(row.dwRemoteAddr)
        connections.append(Connection(TCP, ipv4_from_dword(row.dwLocalAddr), port_from_dword(row.dwLocalPort),
                                      remote, 0 if state == LISTENING else port_from_dword(row.dwRemotePort),
                                      state, row.dwOwningPid))
    for row in _read_table(api.GetExtendedTcpTable, socket.AF_INET6, TCP_TABLE_OWNER_PID_ALL,
                           MIB_TCP6ROW_OWNER_PID):
        state = WIN_TCP_STATES.get(row.dwState, str(row.dwState))
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
