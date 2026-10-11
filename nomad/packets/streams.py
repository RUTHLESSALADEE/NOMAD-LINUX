"""Following a TCP or UDP conversation: what each side sent, in order, as Wireshark's Follow Stream shows it. TCP data
sent twice is shown once, data that arrived out of order is put back in place, and gaps (data never captured) are
marked."""
from dataclasses import dataclass, field

from .stats import duration_text, size_text


@dataclass
class Chunk:
    direction: int  # 0: from the side that spoke first (the client), 1: the other way
    time: float
    data: bytes = b""
    missing: int = 0  # Bytes never captured before this one


@dataclass
class Followed:
    kind: str  # "tcp" or "udp"
    index: int
    client: str
    server: str
    chunks: list = field(default_factory=list)
    packets: int = 0
    first: float = 0.0
    last: float = 0.0
    truncated: bool = False  # Some packets were only partly captured
    numbers: list = field(default_factory=list)

    def sent(self, direction):
        return sum(len(chunk.data) for chunk in self.chunks if chunk.direction == direction)

    def describe(self):
        return (f"{self.client} → {self.server}: {size_text(self.sent(0))}; {self.server} → {self.client}: "
                f"{size_text(self.sent(1))}. {self.packets:,} packets over {duration_text(self.last - self.first)}.")

    def data(self, direction=None):
        return b"".join(chunk.data for chunk in self.chunks if direction is None or chunk.direction == direction)


def follow(view, kind, index):
    """The Followed conversation kind ("tcp"/"udp") number index of a capfile.View, or None."""
    stream = view.stream(kind, index)
    if stream is None:
        return None
    from .capfile import endpoint
    followed = Followed(kind, index, endpoint(stream.a), endpoint(stream.b), first=stream.first, last=stream.last)
    expected = [None, None]  # Next sequence number (raw) wanted from each side
    pending = [{}, {}]  # Data that came early: sequence -> (time, bytes)
    for position in stream.packets:
        packet = view.packets[position]
        d = packet.dissect()
        if d.payload is None or d.addresses is None or d.ports is None:
            continue
        followed.packets += 1
        followed.numbers.append(packet.number)
        if len(packet.data) < packet.wire_length:
            followed.truncated = True
        direction = 0 if (d.addresses[0], d.ports[1]) == stream.a else 1
        start, end = d.payload
        data = d.data[start:end]
        if kind == "udp":
            if data:
                followed.chunks.append(Chunk(direction, packet.time, data))
            continue
        sequence, _, flags, _, _, _ = d.tcp
        if flags & 0x002:  # SYN: data starts after it
            expected[direction] = (sequence + 1) & 0xFFFFFFFF
            continue
        if not data:
            continue
        if expected[direction] is None:
            expected[direction] = sequence
        add_segment(followed, expected, pending, direction, sequence, data, packet.time)
    for direction in (0, 1):  # Data after a gap that was never filled
        while pending[direction]:
            sequence = min(pending[direction], key=lambda item: distance(expected[direction], item))
            time_, data = pending[direction].pop(sequence)
            missing = distance(expected[direction], sequence)
            followed.chunks.append(Chunk(direction, time_, data, missing))
            expected[direction] = (sequence + len(data)) & 0xFFFFFFFF
            take_pending(followed, expected, pending, direction, time_)
    followed.chunks.sort(key=lambda chunk: chunk.time)  # Stable: what came together stays in order
    return followed


def distance(start, sequence):
    """How far sequence is after start, allowing for wrapping round (negative: before it)."""
    gap = (sequence - start) & 0xFFFFFFFF
    return gap - 0x100000000 if gap >= 0x80000000 else gap


def add_segment(followed, expected, pending, direction, sequence, data, time_):
    gap = distance(expected[direction], sequence)
    if gap > 0:  # Came early (or what's before it never came): hold on to it
        pending[direction].setdefault(sequence, (time_, data))
        return
    if gap < 0:  # Some or all of it was sent before
        data = data[-gap:]
        if not data:
            return
    followed.chunks.append(Chunk(direction, time_, data))
    expected[direction] = (expected[direction] + len(data)) & 0xFFFFFFFF
    take_pending(followed, expected, pending, direction, time_)


def take_pending(followed, expected, pending, direction, now):
    """Data that came early, now that what's before it has come: shown at now, after it."""
    while True:
        ready = [sequence for sequence in pending[direction] if distance(expected[direction], sequence) <= 0]
        if not ready:
            return
        for sequence in sorted(ready, key=lambda item: distance(expected[direction], item)):
            _, data = pending[direction].pop(sequence)
            overlap = -distance(expected[direction], sequence)
            data = data[overlap:]
            if data:
                followed.chunks.append(Chunk(direction, now, data))
                expected[direction] = (expected[direction] + len(data)) & 0xFFFFFFFF


def as_text(data):
    """Bytes as text: printable characters and line breaks kept, anything else shown as a dot."""
    return "".join(chr(byte) if 32 <= byte < 127 or byte in (9, 10, 13) else "." for byte in data).replace("\r\n", "\n")


def hex_dump(data, offset=0):
    lines = []
    for start in range(0, len(data), 16):
        row = data[start:start + 16]
        hex_part = " ".join(f"{byte:02x}" for byte in row[:8]) + "  " + " ".join(f"{byte:02x}" for byte in row[8:])
        text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in row)
        lines.append(f"{offset + start:08x}  {hex_part:<49} {text}")
    return "\n".join(lines)
