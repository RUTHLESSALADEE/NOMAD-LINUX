"""Unwrapping ERSPAN: a capture of the GRE packets a switch sent this PC (Port Mirroring's "Mirror to this PC"),
turned into a capture of the frames it mirrored, as if this PC had been plugged into the mirrored port.

Reads pcapng (what pktmon writes) or classic pcap, and writes pcapng with the inner Ethernet frames, each at the time
its GRE packet arrived. ERSPAN types I, II and III are understood; GRE packets split in two (ERSPAN adds about 50
bytes, so a full-size frame doesn't fit a 1500-byte MTU) are put back together first. The same packet captured twice
(pktmon sees some at more than one point) is kept once.
"""
import struct
from collections import Counter, deque
from dataclasses import dataclass, field

from .packets.reader import CaptureError, read_file, records  # noqa: F401 (CaptureError: for callers)

ERSPAN_II = 0x88BE  # GRE protocol type for ERSPAN types I and II
ERSPAN_III = 0x22EB
ETHERNET, RAW_IP = 1, 101
RAW_IP_TYPES = {12, 14, 101, 228}
VLAN_TYPES = {0x8100, 0x88A8, 0x9100}
GRE = 47
SHB, IDB, EPB = 0x0A0D0D0A, 0x00000001, 0x00000006
BYTE_ORDER_MAGIC = 0x1A2B3C4D
DUPLICATE_WINDOW = 16  # Recent packets compared, to drop one captured twice
DUPLICATE_SECONDS = 0.005


@dataclass
class Result:
    frames: int = 0  # Mirrored frames written
    other: int = 0  # Packets that weren't ERSPAN (left out)
    duplicates: int = 0
    incomplete: int = 0  # Pieces of GRE packets whose other piece never came
    sessions: Counter = field(default_factory=Counter)  # ERSPAN session ID -> frames (type I has none: 0)
    senders: Counter = field(default_factory=Counter)  # Switch address -> frames
    left_out: int = 0  # ERSPAN for another session or from another switch than asked for

    def summary(self):
        if not self.frames:
            text = "No ERSPAN packets were in the capture"
            if self.left_out:
                text += f" for that session (there were {self.left_out} for others)"
            return text + "."
        senders = ", ".join(sorted(self.senders))
        text = f"{self.frames} mirrored frame{'' if self.frames == 1 else 's'} from {senders}"
        if self.other:
            text += f"; {self.other} other packet{'' if self.other == 1 else 's'} left out"
        if self.incomplete:
            text += f"; {self.incomplete} split packet{'' if self.incomplete == 1 else 's'} missing a piece"
        return text + "."


# ----------------------------------------------------------------- Reading captures

def read_packets(path):
    """(link type, timestamp in seconds, bytes) for each packet of a pcapng or pcap file."""
    return [(record.link_type, record.time, record.data) for record in records(read_file(path))]


# ----------------------------------------------------------------- Writing pcapng

def pad(data):
    return data + b"\x00" * (-len(data) % 4)


def block(block_type, body):
    body = pad(body)
    length = len(body) + 12
    return struct.pack("<II", block_type, length) + body + struct.pack("<I", length)


def header_blocks(comment="Unwrapped from ERSPAN by NOMAD"):
    text = comment.encode()
    options = struct.pack("<HH", 1, len(text)) + pad(text) + struct.pack("<HH", 0, 0)
    section = block(SHB, struct.pack("<IHHq", BYTE_ORDER_MAGIC, 1, 0, -1) + options)
    interface = block(IDB, struct.pack("<HHI", ETHERNET, 0, 0))  # Microsecond timestamps (the default)
    return section + interface


def packet_block(timestamp, frame):
    units = int(round(timestamp * 1e6))
    return block(EPB, struct.pack("<IIIII", 0, units >> 32, units & 0xFFFFFFFF, len(frame), len(frame)) + frame)


# ----------------------------------------------------------------- Unwrapping

def ip_packet(link_type, packet):
    """The IPv4 packet in a captured one, or None."""
    if link_type in RAW_IP_TYPES:
        return packet if packet[:1] and packet[0] >> 4 == 4 else None
    if link_type != ETHERNET or len(packet) < 14:
        return None
    ether_type, offset = struct.unpack("!H", packet[12:14])[0], 14
    while ether_type in VLAN_TYPES and len(packet) >= offset + 4:
        ether_type, offset = struct.unpack("!H", packet[offset + 2:offset + 4])[0], offset + 4
    return packet[offset:] if ether_type == 0x0800 else None


class Reassembler:
    """Puts IPv4 packets split into fragments back together."""

    def __init__(self):
        self.pending = {}  # (source, destination, id, protocol) -> {offset: (data, more)}

    def add(self, packet):
        """The whole IPv4 packet (header and payload) once every piece has come, else None."""
        header_length = (packet[0] & 0x0F) * 4
        total = struct.unpack("!H", packet[2:4])[0]
        flags_offset = struct.unpack("!H", packet[6:8])[0]
        more, offset = bool(flags_offset & 0x2000), (flags_offset & 0x1FFF) * 8
        payload = packet[header_length:total]
        if not more and offset == 0:
            return packet[:total]
        key = (bytes(packet[12:16]), bytes(packet[16:20]), bytes(packet[4:6]), packet[9])
        pieces = self.pending.setdefault(key, {})
        pieces[offset] = (payload, more, packet[:header_length])
        if not any(not item[1] for item in pieces.values()):
            return None
        joined, position = b"", 0
        for start in sorted(pieces):
            if start != position:
                return None
            joined += pieces[start][0]
            position += len(pieces[start][0])
        header = bytearray(pieces[0][2])
        del self.pending[key]
        header[6:8] = b"\x00\x00"
        header[2:4] = struct.pack("!H", len(header) + len(joined))
        return bytes(header) + joined

    @property
    def incomplete(self):
        return len(self.pending)


def erspan_frame(ip):
    """(session ID or 0, inner frame) from an IPv4 packet carrying ERSPAN, or None."""
    header_length = (ip[0] & 0x0F) * 4
    if ip[9] != GRE or len(ip) < header_length + 4:
        return None
    gre = ip[header_length:]
    flags, protocol = struct.unpack("!HH", gre[:4])
    if flags & 0x0007:  # GRE version 0 only
        return None
    offset = 4 + (4 if flags & 0x8000 else 0) + (4 if flags & 0x2000 else 0)
    sequenced = bool(flags & 0x1000)
    offset += 4 if sequenced else 0
    if protocol == ERSPAN_II:
        if not sequenced:  # Type I: the frame straight after GRE
            return 0, gre[offset:]
        if len(gre) < offset + 8:
            return None
        session = struct.unpack("!H", gre[offset + 2:offset + 4])[0] & 0x03FF
        return session, gre[offset + 8:]
    if protocol == ERSPAN_III:
        if len(gre) < offset + 12:
            return None
        session = struct.unpack("!H", gre[offset + 2:offset + 4])[0] & 0x03FF
        frame_type = (gre[offset + 10] >> 2) & 0x1F
        has_subheader = gre[offset + 11] & 0x01
        inner = gre[offset + 12 + (8 if has_subheader else 0):]
        if frame_type != 0:  # IP without its Ethernet header: give it one
            inner = b"\x00" * 12 + b"\x08\x00" + inner
        return session, inner
    return None


def mirrored(packets, result, erspan_id=0, sender=""):
    """(timestamp, frame) for each frame mirrored in packets ((link type, timestamp, bytes) each), counting what
    happened in result (a Result). erspan_id, sender: only that session's (the ERSPAN ID) and switch's (its origin
    address)."""
    unwrapper = Unwrapper(result, erspan_id, sender)
    for link_type, timestamp, packet in packets:
        frame = unwrapper.add(link_type, timestamp, packet)
        if frame is not None:
            yield timestamp, frame


class Unwrapper:
    """The frames mirrored in packets given one at a time (add()), counting what happened in result (a Result).
    erspan_id, sender: only that session's (the ERSPAN ID) and switch's (its origin address)."""

    def __init__(self, result, erspan_id=0, sender=""):
        import ipaddress
        self.result, self.erspan_id = result, erspan_id
        self.reassembler = Reassembler()
        self.recent = deque(maxlen=DUPLICATE_WINDOW)
        self.wanted = ipaddress.IPv4Address(sender).packed if sender else None

    def add(self, link_type, timestamp, packet):
        """The frame mirrored in packet, or None (not ERSPAN, a piece of one still to come, or left out)."""
        import ipaddress
        result = self.result
        ip = ip_packet(link_type, packet)
        if ip is None or len(ip) < 20:
            result.other += 1
            return None
        if any(stamp >= timestamp - DUPLICATE_SECONDS and earlier == ip for stamp, earlier in self.recent):
            result.duplicates += 1
            return None
        self.recent.append((timestamp, ip))
        if ip[9] != GRE:
            result.other += 1
            return None
        whole = self.reassembler.add(ip)
        result.incomplete = self.reassembler.incomplete
        if whole is None:
            return None
        found = erspan_frame(whole)
        if found is None:
            result.other += 1
            return None
        session, frame = found
        if (self.erspan_id and session and session != self.erspan_id) or                 (self.wanted and whole[12:16] != self.wanted):
            result.left_out += 1
            return None
        result.frames += 1
        result.sessions[session] += 1
        result.senders[str(ipaddress.IPv4Address(bytes(whole[12:16])))] += 1
        return frame


def unwrap(source_path, output_path, erspan_id=0, sender=""):
    """Write the frames mirrored to source_path's ERSPAN packets to output_path (pcapng). erspan_id, sender: only
    that session's (the ERSPAN ID) and switch's (its origin address). Returns a Result."""
    result = Result()
    packets = read_packets(source_path)
    with open(output_path, "wb") as output:
        output.write(header_blocks())
        for timestamp, frame in mirrored(packets, result, erspan_id, sender):
            output.write(packet_block(timestamp, frame))
    return result


def valid_sender(text):
    """Whether text is one IPv4 address (a switch's origin address), not a subnet or blank."""
    import ipaddress
    try:
        ipaddress.IPv4Address((text or "").strip())
        return True
    except ValueError:
        return False


def unwrapped_path(path):
    """Where the unwrapped capture of path goes: beside it, "... (unwrapped).pcapng"."""
    base = str(path)
    if base.lower().endswith(".pcapng"):
        base = base[:-7]
    elif base.lower().endswith(".pcap"):
        base = base[:-5]
    return f"{base} (unwrapped).pcapng"
