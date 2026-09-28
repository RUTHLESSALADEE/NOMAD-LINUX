"""Syslog receiver: collect the log messages switches, firewalls and access points send (UDP, and optionally TCP).

Understands both the classic BSD format (RFC 3164) and the newer one (RFC 5424), and anything else as plain text.
"""
import datetime
import logging
import re
import socket
import threading
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SYSLOG_PORT = 514
SEVERITIES = ["Emergency", "Alert", "Critical", "Error", "Warning", "Notice", "Info", "Debug"]
FACILITIES = ["kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news", "uucp", "cron", "authpriv", "ftp",
              "ntp", "security", "console", "solaris-cron"] + [f"local{number}" for number in range(8)]
PRI_PATTERN = re.compile(r"^<(\d{1,3})>")
RFC5424_PATTERN = re.compile(r"^1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|(?:\[.*?\])+) ?(.*)$", re.DOTALL)
BSD_TIME_PATTERN = re.compile(r"^((?:[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}(?:\.\d+)?)|(?:\d{4}-\d\d-\d\dT\S+)) (.*)$",
                              re.DOTALL)
BSD_TAG_PATTERN = re.compile(r"^([\w\-./]{1,48})(?:\[(\d+)\])?: ?(.*)$", re.DOTALL)
MAX_MESSAGE_BYTES = 65535
MAX_TCP_FRAME = 1024 * 1024


@dataclass
class SyslogMessage:
    received: datetime.datetime
    source: str  # Sender's IP address
    facility: str = ""
    severity: int = 6  # Index into SEVERITIES; messages without a priority count as Info
    timestamp: str = ""  # As the device wrote it
    host: str = ""
    app: str = ""
    message: str = ""
    raw: str = ""
    protocol: str = "UDP"

    @property
    def severity_name(self):
        return SEVERITIES[self.severity] if 0 <= self.severity < len(SEVERITIES) else str(self.severity)

    def search_text(self):
        return " ".join((self.source, self.facility, self.severity_name, self.host, self.app, self.message)).lower()


def parse_message(raw, source, received=None, protocol="UDP"):
    """Parse one syslog message (text). Never fails: anything unrecognised is kept as the message."""
    received = received or datetime.datetime.now()
    text = raw.rstrip("\r\n\0")
    message = SyslogMessage(received, source, raw=text, protocol=protocol)
    match = PRI_PATTERN.match(text)
    if not match or int(match.group(1)) > 191:
        message.message = text
        return message
    priority = int(match.group(1))
    facility = priority // 8
    message.facility = FACILITIES[facility] if facility < len(FACILITIES) else str(facility)
    message.severity = priority % 8
    rest = text[match.end():]

    modern = RFC5424_PATTERN.match(rest)
    if modern:
        timestamp, host, app, _procid, _msgid, _data, body = modern.groups()
        message.timestamp = "" if timestamp == "-" else timestamp
        message.host = "" if host == "-" else host
        message.app = "" if app == "-" else app
        message.message = body.lstrip("﻿")  # RFC 5424 allows a byte order mark before UTF-8 text
        return message

    classic = BSD_TIME_PATTERN.match(rest)
    if classic:
        message.timestamp, rest = classic.groups()
        # After the time comes the host name, unless the device left it out and went straight to the tag
        first, _, remainder = rest.partition(" ")
        if remainder and not first.endswith(":") and "[" not in first:
            message.host, rest = first, remainder
    tag = BSD_TAG_PATTERN.match(rest)
    if tag and not tag.group(1).isdigit():  # Cisco puts a sequence number ("53:") where the tag would be
        message.app, _pid, rest = tag.groups()
    message.message = rest.strip()
    return message


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def split_tcp_frames(buffer):
    """Split a TCP stream into messages (RFC 6587): "123 <34>..." octet counting, or one message per line.

    Returns (messages, leftover bytes still waiting for the rest of their message).
    """
    messages = []
    while buffer:
        count = re.match(rb"^(\d{1,7}) ", buffer)
        if count:
            length = int(count.group(1))
            start = count.end()
            if length > MAX_TCP_FRAME:
                buffer = b""  # Nonsense length: drop what we have rather than wait forever
                break
            if len(buffer) < start + length:
                break
            messages.append(buffer[start:start + length])
            buffer = buffer[start + length:]
            continue
        line_end = buffer.find(b"\n")
        if line_end < 0:
            if len(buffer) > MAX_TCP_FRAME:
                messages.append(buffer)
                buffer = b""
            break
        line = buffer[:line_end].rstrip(b"\r\0")
        if line:
            messages.append(line)
        buffer = buffer[line_end + 1:]
    return messages, buffer


@dataclass
class SyslogReceiver:
    """Listens for syslog messages and calls on_message(SyslogMessage) from its threads."""
    on_message: object
    address: str = "0.0.0.0"
    port: int = SYSLOG_PORT
    tcp: bool = False
    stopping: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.sockets = []
        self.threads = []

    def start(self):
        """Raises OSError if the port can't be used (for example, another syslog server has it)."""
        self.stopping.clear()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.bind((self.address, self.port))
        except OSError:
            udp.close()
            raise
        udp.settimeout(0.5)
        self.sockets.append(udp)
        self._thread(self.receive_udp, udp)
        if self.tcp:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                server.bind((self.address, self.port))
                server.listen(16)
            except OSError:
                server.close()
                self.stop()
                raise
            server.settimeout(0.5)
            self.sockets.append(server)
            self._thread(self.accept_tcp, server)

    def _thread(self, target, *args):
        thread = threading.Thread(target=target, args=args, name="syslog", daemon=True)
        self.threads.append(thread)
        thread.start()

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(2)
        for sock in self.sockets:
            sock.close()
        self.sockets, self.threads = [], []

    def receive_udp(self, sock):
        while not self.stopping.is_set():
            try:
                data, sender = sock.recvfrom(MAX_MESSAGE_BYTES)
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                continue
            self.on_message(parse_message(decode(data), sender[0]))

    def accept_tcp(self, server):
        while not self.stopping.is_set():
            try:
                connection, sender = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self.receive_tcp, args=(connection, sender), name="syslog-tcp",
                                      daemon=True)
            thread.start()

    def receive_tcp(self, connection, sender):
        buffer = b""
        with connection:
            connection.settimeout(0.5)
            while not self.stopping.is_set():
                try:
                    data = connection.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    break
                messages, buffer = split_tcp_frames(buffer + data)
                for raw in messages:
                    self.on_message(parse_message(decode(raw), sender[0], protocol="TCP"))
        if buffer.strip():
            self.on_message(parse_message(decode(buffer), sender[0], protocol="TCP"))


def format_line(message):
    """One line for a saved log file."""
    parts = [message.received.strftime("%Y-%m-%d %H:%M:%S"), message.source, message.severity_name.upper()]
    if message.facility:
        parts.append(message.facility)
    if message.host:
        parts.append(message.host)
    if message.app:
        parts.append(message.app + ":")
    return " ".join(parts + [message.message])
