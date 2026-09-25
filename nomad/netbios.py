"""NetBIOS node status queries (what nbtstat -A does): a host's computer name and MAC address.

Windows machines, Samba servers and many NAS boxes and printers answer these on UDP port 137,
which finds names on networks without a DNS server.
"""
import os
import socket
import struct
from dataclasses import dataclass

NETBIOS_PORT = 137
NBSTAT_TYPE = 0x21
IN_CLASS = 0x01
GROUP_NAME_FLAG = 0x8000
WORKSTATION_SUFFIX = 0x00


@dataclass
class NodeStatus:
    name: str = ""  # Computer name
    group: str = ""  # Workgroup or domain
    mac: str = ""  # AA-BB-CC-DD-EE-FF, or "" if the host didn't report one


def encode_name(name, suffix=0x00):
    """First-level encode a NetBIOS name: each nibble of the padded 16 bytes becomes a letter A-P."""
    raw = name.encode("ascii")[:15].ljust(15, b"\0" if name == "*" else b" ") + bytes([suffix])
    return bytes([32]) + bytes(ord("A") + (byte >> shift & 0x0F) for byte in raw for shift in (4, 0)) + b"\0"


def build_query(transaction_id):
    """A node status request for the wildcard name "*"."""
    header = struct.pack(">HHHHHH", transaction_id, 0, 1, 0, 0, 0)
    return header + encode_name("*") + struct.pack(">HH", NBSTAT_TYPE, IN_CLASS)


def _skip_name(data, offset):
    """Offset just past an encoded (or compressed) name."""
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:  # Compression pointer
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length


def parse_response(data, transaction_id=None):
    """Parse a node status response. Raises ValueError if it isn't one."""
    try:
        response_id, flags, _, answers = struct.unpack_from(">HHHH", data)
        if transaction_id is not None and response_id != transaction_id:
            raise ValueError("Response is for a different query.")
        if not flags & 0x8000 or answers < 1:
            raise ValueError("Not a node status response.")
        offset = _skip_name(data, 12)
        record_type, _, _, length = struct.unpack_from(">HHIH", data, offset)
        if record_type != NBSTAT_TYPE:
            raise ValueError("Not a node status response.")
        offset += 10
        end = offset + length
        count = data[offset]
        offset += 1
        status = NodeStatus()
        for _ in range(count):
            raw_name, suffix, name_flags = data[offset:offset + 15], data[offset + 15], \
                struct.unpack_from(">H", data, offset + 16)[0]
            offset += 18
            name = raw_name.decode("ascii", "replace").rstrip(" \0")
            if suffix == WORKSTATION_SUFFIX:
                if name_flags & GROUP_NAME_FLAG:
                    status.group = status.group or name
                else:
                    status.name = status.name or name
        if offset + 6 <= min(end, len(data)):
            mac = data[offset:offset + 6]
            if any(mac):
                status.mac = "-".join(f"{byte:02X}" for byte in mac)
        return status
    except (IndexError, struct.error):
        raise ValueError("Response is too short.") from None


def node_status(address, timeout=1000):
    """Ask an IPv4 host for its NetBIOS names. Returns a NodeStatus, or None if it didn't answer."""
    transaction_id = struct.unpack(">H", os.urandom(2))[0]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout / 1000)
        try:
            sock.sendto(build_query(transaction_id), (str(address), NETBIOS_PORT))
            while True:
                data, sender = sock.recvfrom(2048)
                if sender[0] != str(address):
                    continue
                try:
                    return parse_response(data, transaction_id)
                except ValueError:
                    continue
        except OSError:  # Timeout, or the host said the port is closed
            return None
