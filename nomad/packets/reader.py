"""Reading pcapng (what pktmon and Wireshark write) and classic pcap files. Packets are slices of the file's bytes
(memoryviews), so a big capture isn't copied packet by packet."""
import gzip
import struct
import zlib
from dataclasses import dataclass

ETHERNET = 1
SHB, IDB, EPB, SPB, OPB = 0x0A0D0D0A, 0x00000001, 0x00000006, 0x00000003, 0x00000002
BYTE_ORDER_MAGIC = 0x1A2B3C4D
GZIP_MAGIC = bytes((0x1F, 0x8B))
PCAP_MAGICS = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
               b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}


class CaptureError(Exception):
    """Not a capture file this can read."""


@dataclass
class Record:
    link_type: int
    time: float  # Seconds since 1970
    data: object  # bytes or memoryview: what was captured
    wire_length: int  # How long the packet was (more than len(data) when only its start was kept)
    interface: str = ""


def read_file(path):
    """The file's bytes, checked to be a capture (gzipped ones, as Wireshark can save, unzipped)."""
    with open(path, "rb") as file:
        data = file.read()
    if data[:2] == GZIP_MAGIC:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error) as error:
            raise CaptureError(f"It's gzipped, but couldn't be unzipped ({error}).") from None
    if len(data) < 24:
        raise CaptureError("The capture file is empty.")
    if bytes(data[:4]) not in PCAP_MAGICS and struct.unpack("<I", data[:4])[0] != SHB:
        raise CaptureError("It isn't a pcapng or pcap capture.")
    return data


def records(data):
    """Each packet of a pcapng or pcap file's bytes, as a Record."""
    view = memoryview(data)
    if bytes(view[:4]) in PCAP_MAGICS:
        return read_pcap(view)
    if len(view) >= 4 and struct.unpack("<I", view[:4])[0] == SHB:
        return read_pcapng(view)
    raise CaptureError("It isn't a pcapng or pcap capture.")


def read_pcap(data):
    order, units = PCAP_MAGICS[bytes(data[:4])]
    link_type = struct.unpack(order + "I", data[20:24])[0] & 0xFFFF
    offset = 24
    while offset + 16 <= len(data):
        seconds, fraction, captured, original = struct.unpack(order + "IIII", data[offset:offset + 16])
        offset += 16
        if offset + captured > len(data):
            break
        yield Record(link_type, seconds + fraction / units, data[offset:offset + captured], max(original, captured))
        offset += captured


def read_pcapng(data):
    order = "<"
    interfaces = []  # (link type, timestamp units per second, name)
    offset = 0
    while offset + 12 <= len(data):
        block_type = struct.unpack(order + "I", data[offset:offset + 4])[0]
        if block_type == SHB:
            magic = data[offset + 8:offset + 12]
            order = "<" if struct.unpack("<I", magic)[0] == BYTE_ORDER_MAGIC else ">"
            interfaces = []
        length = struct.unpack(order + "I", data[offset + 4:offset + 8])[0]
        if length < 12 or offset + length > len(data):
            break
        body = data[offset + 8:offset + length - 4]
        if block_type == IDB and len(body) >= 8:
            link_type = struct.unpack(order + "H", body[:2])[0]
            units, name = interface_options(body[8:], order)
            interfaces.append((link_type, units, name))
        elif block_type in (EPB, OPB) and len(body) >= 20:
            if block_type == EPB:
                interface, high, low, captured, original = struct.unpack(order + "IIIII", body[:20])
            else:
                interface, _, high, low, captured, original = struct.unpack(order + "HHIIII", body[:20])
            link_type, units, name = interfaces[interface] if interface < len(interfaces) else (ETHERNET, 1e6, "")
            yield Record(link_type, ((high << 32) | low) / units, body[20:20 + captured], max(original, captured),
                         name)
        elif block_type == SPB and len(body) >= 4 and interfaces:
            original = struct.unpack(order + "I", body[:4])[0]
            packet = body[4:4 + original]
            yield Record(interfaces[0][0], 0.0, packet, original, interfaces[0][2])
        offset += length


def interface_options(options, order):
    """(timestamp units per second, name) from an interface's options: 10^6 and "" when it doesn't say."""
    units, name = 1e6, ""
    offset = 0
    while offset + 4 <= len(options):
        code, length = struct.unpack(order + "HH", options[offset:offset + 4])
        if code == 0:
            break
        value = options[offset + 4:offset + 4 + length]
        if code == 9 and length >= 1:
            units = 2 ** (value[0] & 0x7F) if value[0] & 0x80 else 10 ** value[0]
        elif code in (2, 3) and not name:  # if_name, if_description
            name = bytes(value).decode("utf-8", "replace").rstrip("\0")
        offset += 4 + (length + 3) // 4 * 4
    return units, name
