"""What tells the Map Watcher to read a switch straight away: syslog messages and SNMP traps the switches send.

Switches are set up to send to the computer watching (logging host / snmp-server host). A port coming up, a new
CDP neighbor or a MAC address learned means something may have been plugged in, so that switch is read again a short
while later (TRIGGER_DELAY, so CDP has time to see the new neighbor). Several messages from one switch in that time
are one reading.
"""
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field

from ..snmp import SNMP_TRAPS, TRAP_PORT, inform_response, oid_text, parse_trap
from .watch import TRIGGER_DELAY

log = logging.getLogger(__name__)

LINK_UP = SNMP_TRAPS + (4,)
COLD_START, WARM_START = SNMP_TRAPS + (1,), SNMP_TRAPS + (2,)
CISCO_MAC_CHANGED = (1, 3, 6, 1, 4, 1, 9, 9, 215, 2, 0, 1)  # cmnMacChangedNotification
CISCO_MAC_MOVE = (1, 3, 6, 1, 4, 1, 9, 9, 215, 2, 0, 3)  # cmnMacMoveNotification
LLDP_REM_TABLES_CHANGE = (1, 0, 8802, 1, 1, 2, 0, 0, 1)
TRAP_REASONS = {LINK_UP: "port up (trap)", COLD_START: "restarted (trap)", WARM_START: "restarted (trap)",
                CISCO_MAC_CHANGED: "MAC address learned (trap)", CISCO_MAC_MOVE: "MAC address moved (trap)",
                LLDP_REM_TABLES_CHANGE: "LLDP neighbors changed (trap)"}

# Cisco syslog mnemonics worth reading the switch for
SYSLOG_PATTERNS = [
    (re.compile(r"%LINK-\d-UPDOWN: Interface ([^,]+), changed state to up", re.I), "port {0} up"),
    (re.compile(r"%LINEPROTO-\d-UPDOWN: Line protocol on Interface ([^,]+), changed state to up", re.I),
     "port {0} up"),
    (re.compile(r"%CDP-\d-\w+", re.I), "CDP message"),
    (re.compile(r"%LLDP-\d-\w+", re.I), "LLDP message"),
    (re.compile(r"%SW_MATM-\d-MACFLAP_NOTIF:.*?Host (\S+)", re.I), "MAC {0} moving"),
    (re.compile(r"%ILPOWER-\d-POWER_GRANTED: Interface (\S+)", re.I), "PoE granted on {0}"),
    (re.compile(r"%SYS-5-RESTART", re.I), "restarted"),
]


def syslog_reason(text):
    """Why a syslog message (its text) means the switch is worth reading, or "" if it doesn't."""
    for pattern, reason in SYSLOG_PATTERNS:
        match = pattern.search(text)
        if match:
            return reason.format(*[group.strip() for group in match.groups()])
    return ""


def trap_reason(trap):
    """Why a trap means the switch is worth reading, or "" if it doesn't."""
    return TRAP_REASONS.get(tuple(trap.trap_oid), "")


def trap_text(trap):
    return TRAP_REASONS.get(tuple(trap.trap_oid)) or f"trap {oid_text(trap.trap_oid)}"


@dataclass
class Pending:
    first: float
    reasons: list = field(default_factory=list)


class TriggerQueue:
    """Switch addresses to read soon, each once however many messages it sends meanwhile. Thread-safe: receivers
    add from their threads, the watcher takes due ones from its own."""

    def __init__(self, delay=TRIGGER_DELAY, clock=time.monotonic):
        self.delay, self.clock = delay, clock
        self.pending = {}  # Address -> Pending
        self.lock = threading.Lock()

    def add(self, address, reason):
        with self.lock:
            item = self.pending.setdefault(address, Pending(self.clock()))
            if reason and reason not in item.reasons and len(item.reasons) < 10:
                item.reasons.append(reason)

    def due(self):
        """[(address, [reasons])] whose wait is over, taken off the queue."""
        now = self.clock()
        with self.lock:
            ready = [address for address, item in self.pending.items() if now - item.first >= self.delay]
            return [(address, self.pending.pop(address).reasons) for address in ready]

    def __len__(self):
        return len(self.pending)


class TrapReceiver:
    """Listens for SNMP traps on UDP 162 and calls on_trap(Trap, sender address) from its thread. Informs are
    answered so the switch stops resending them."""

    def __init__(self, on_trap, address="0.0.0.0", port=TRAP_PORT):
        self.on_trap, self.address, self.port = on_trap, address, port
        self.stopping = threading.Event()
        self.sock, self.thread = None, None

    def start(self):
        """Raises OSError if the port can't be used."""
        self.stopping.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.address, self.port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)
        self.sock = sock
        self.thread = threading.Thread(target=self.receive, name="SNMP traps", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(2)
        if self.sock is not None:
            self.sock.close()
        self.sock, self.thread = None, None

    @property
    def listening(self):
        return self.sock is not None

    def receive(self):
        while not self.stopping.is_set():
            try:
                data, sender = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                continue
            try:
                trap = parse_trap(data)
            except ValueError as error:
                log.debug("Not a trap from %s: %s", sender[0], error)
                continue
            if trap.inform:
                try:
                    self.sock.sendto(inform_response(data), sender)
                except (OSError, ValueError, IndexError):
                    pass
            try:
                self.on_trap(trap, sender[0])
            except Exception:  # A bug handling one trap shouldn't stop the listener
                log.exception("Handling a trap from %s failed", sender[0])


def device_for_address(network_map, address):
    """The key of the map device that has address, or None."""
    for key, device in network_map.devices.items():
        if device.owns(address):
            return key
    return None


def switch_config(address, community="public"):
    """Lines to paste into a Cisco switch so it tells this computer when something's plugged in."""
    return [
        f"logging host {address}",
        "logging trap notifications",
        f"snmp-server host {address} version 2c {community}",
        "snmp-server enable traps snmp linkup coldstart warmstart",
        "snmp-server enable traps mac-notification change move",
        "mac address-table notification change",
        "! and on each access port: snmp trap mac-notification change added",
    ]
