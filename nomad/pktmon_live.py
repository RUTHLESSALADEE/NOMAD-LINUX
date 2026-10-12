"""pktmon's packets as it captures them, for Packet Capture's live view. pktmon logs each packet it captures as an
event of its Windows event provider (Microsoft-Windows-PktMon), bytes included; NOMAD listens to those events with a
real-time session of its own (etw.RealtimeSession) while pktmon writes its file as usual. The file stays the complete
record: when the listener falls behind, Windows drops events for it alone.

Events come in batches per processor, a little out of order, so each packet is held back HOLD_SECONDS and handed
over (take()) in time order.
"""
import heapq
import itertools
import logging
import struct
import threading
import time
from collections import deque

from . import etw
from .packets.reader import Record

log = logging.getLogger(__name__)

PROVIDER = "4d4f80d9-c8bd-4d73-bb5b-19c90402c5ac"  # Microsoft-Windows-PktMon
PAYLOAD_KEYWORD = 0x10  # Events carrying the packet's bytes
PACKET_EVENTS = {160, 170}  # A packet captured, and one a component dropped (both with its bytes)
SESSION_NAME = "NOMAD Live Capture"
HOLD_SECONDS = 0.75  # A packet is shown once it's this old: every packet before it has come by then
MAX_WAITING = 200_000  # Packets held for the live view at most; more are dropped (and counted)
PACKET_HEADER = 34  # Bytes before the packet in a packet event (PktGroupId ... LoggedPayloadSize)
TYPE_OFFSET = 14  # Where the packet's media type is in that header
ETHERNET_MEDIA = 1
ETHERNET, RAW_IP = 1, 101


def parse_packet(data, header=PACKET_HEADER):
    """(link type, bytes captured, bytes on the wire, header length) from a packet event's data, or None. The
    header's length is checked against the logged size it ends with, and looked for when it isn't the usual."""
    found = header if fits(data, header) else next((size for size in range(12, 96, 2) if fits(data, size)), None)
    if found is None:
        return None
    original = struct.unpack("<H", data[found - 4:found - 2])[0]
    packet = data[found:]
    media = struct.unpack("<H", data[TYPE_OFFSET:TYPE_OFFSET + 2])[0] if found == PACKET_HEADER else 0
    original = max(original, len(packet))
    link_type = RAW_IP if media != ETHERNET_MEDIA and looks_ip(packet, original) else ETHERNET
    return link_type, packet, original, found


def fits(data, header):
    """Whether data is a header this long ending with the length of what follows it."""
    return len(data) > header >= 4 and struct.unpack("<H", data[header - 2:header])[0] == len(data) - header


def looks_ip(packet, original):
    """Whether packet is an IP packet without an Ethernet header: its length field agrees with its size."""
    if len(packet) >= 20 and packet[0] >> 4 == 4:
        return struct.unpack("!H", packet[2:4])[0] == original
    if len(packet) >= 40 and packet[0] >> 4 == 6:
        return struct.unpack("!H", packet[4:6])[0] + 40 == original
    return False


class LiveFeed:
    """pktmon's packets while it captures: start() before pktmon starts, take() the ones ready every so often, and
    stop() after it stops."""

    def __init__(self, session=None):
        self.session = session  # An etw.RealtimeSession, or None to make one
        self.incoming = deque()  # (seconds, event data), as the events come (ETW's thread adds, take() removes)
        self.held = []  # Heap of (seconds, order, event data) waiting to be old enough
        self.order = itertools.count()
        self.lock = threading.Lock()
        self.overflow = 0  # Packets dropped because too many were waiting
        self.unreadable = 0  # Packet events NOMAD couldn't make sense of
        self.header = PACKET_HEADER
        self.received = 0

    def start(self):
        """Raises etw.EtwError (e.g. without administrator rights)."""
        if self.session is None:
            self.session = etw.RealtimeSession(SESSION_NAME, PROVIDER, PAYLOAD_KEYWORD, self.event)
        self.session.start()

    def stop(self):
        if self.session is not None:
            self.session.stop()

    def event(self, event_id, seconds, data):
        """An event (on ETW's thread): kept quick, so the listener keeps up."""
        if event_id not in PACKET_EVENTS:
            return
        if len(self.incoming) + len(self.held) >= MAX_WAITING:
            self.overflow += 1
            return
        self.incoming.append((seconds, data))

    def take(self, now=None, everything=False):
        """The packets ready to show (reader.Records), oldest first: those at least HOLD_SECONDS old (everything: all
        of them, after the capture stops)."""
        with self.lock:
            incoming, held, order = self.incoming, self.held, self.order
            while incoming:
                seconds, data = incoming.popleft()
                heapq.heappush(held, (seconds, next(order), data))
            cutoff = float("inf") if everything else (time.time() if now is None else now) - HOLD_SECONDS
            ready = []
            while held and held[0][0] <= cutoff:
                seconds, _, data = heapq.heappop(held)
                record = self.record(seconds, data)
                if record is not None:
                    ready.append(record)
            return ready

    def record(self, seconds, data):
        parsed = parse_packet(data, self.header)
        if parsed is None:
            self.unreadable += 1
            if self.unreadable == 1:
                log.warning("A pktmon packet event NOMAD couldn't read (%d bytes): %s", len(data), data[:48].hex())
            return None
        link_type, packet, wire_length, header = parsed
        if header != self.header:
            log.info("pktmon packet events have a %d-byte header (expected %d)", header, self.header)
            self.header = header
        self.received += 1
        if self.received == 1:
            log.info("First live packet: %d-byte event, header %s", len(data), data[:header].hex())
        return Record(link_type, seconds, packet, wire_length)

    @property
    def waiting(self):
        return len(self.incoming) + len(self.held)

    def missed(self):
        """Packets the live view missed: dropped by Windows (the listener fell behind), by NOMAD (too many waiting)
        or unreadable. (count, whether more were missed than counted: whole buffers of events dropped)."""
        events, buffers = self.session.lost() if self.session is not None else (0, 0)
        return events + self.overflow + self.unreadable, buffers > 0
