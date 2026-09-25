"""Find the switch and port an adapter is plugged into, from the LLDP and CDP announcements switches send.

Switches announce themselves every 30 (LLDP) or 60 (CDP) seconds. Windows' built-in packet monitor
(pktmon, Windows 10 1809 and later) captures them, so nothing needs installing; it does need
administrator rights.
"""
import ipaddress
import logging
import os
import shutil
import socket
import struct
import tempfile
import time
from dataclasses import dataclass, field

from .system import CommandError, run_command

log = logging.getLogger(__name__)

LLDP_ETHERTYPE = 0x88CC
VLAN_ETHERTYPES = {0x8100, 0x88A8}
CDP_MAC = "01-00-0C-CC-CC-CC"
CDP_SNAP = bytes.fromhex("AAAA0300000C2000")
LINKTYPE_ETHERNET = 1
SEGMENT_SECONDS = 15  # Capture in short pieces so results show up without waiting for the whole time
FILTER_PREFIX = "NOMAD"

LLDP_CAPABILITIES = ["Other", "Repeater", "Bridge", "WLAN access point", "Router", "Telephone", "DOCSIS", "Station",
                     "C-VLAN", "S-VLAN", "Two-port MAC relay"]
CDP_CAPABILITIES = {0x01: "Router", 0x02: "Bridge", 0x04: "Source route bridge", 0x08: "Switch", 0x10: "Host",
                    0x20: "IGMP", 0x40: "Repeater", 0x80: "Phone", 0x100: "Remote", 0x200: "CVTA",
                    0x400: "MAC relay"}


@dataclass
class Neighbor:
    protocol: str  # LLDP or CDP
    source_mac: str = ""
    interface: str = ""  # This computer's adapter that heard it, if known
    device_id: str = ""  # LLDP chassis ID / CDP device ID
    system_name: str = ""
    port_id: str = ""
    port_description: str = ""
    native_vlan: str = ""
    voice_vlan: str = ""
    vlan_names: list = field(default_factory=list)
    management_addresses: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)
    description: str = ""  # System description / software version
    platform: str = ""  # CDP platform (model)
    duplex: str = ""
    ttl: int = 0

    @property
    def name(self):
        return self.system_name or self.device_id

    @property
    def key(self):
        return self.protocol, self.device_id, self.port_id, self.interface


def _mac(raw):
    return "-".join(f"{byte:02X}" for byte in raw)


def _text(raw):
    """Printable text as-is, anything else as hex."""
    try:
        text = raw.decode("utf-8").rstrip("\0")
    except UnicodeDecodeError:
        return raw.hex(":")
    return text if text.isprintable() else raw.hex(":")


def _address(family, raw):
    if family == 1 and len(raw) == 4:
        return socket.inet_ntoa(raw)
    if family == 2 and len(raw) == 16:
        return str(ipaddress.IPv6Address(raw))
    return raw.hex(":")


# ----------------------------------------------------------------- LLDP

def _lldp_id(subtype, value):
    if subtype == 4 and len(value) == 6:  # MAC address
        return _mac(value)
    if subtype == 5 and value:  # Network address (chassis) / 4 is network address for ports
        return _address(value[0], value[1:])
    return _text(value)


def parse_lldp(payload, neighbor):
    """Fill neighbor from an LLDP PDU (the frame after the EtherType)."""
    offset = 0
    while offset + 2 <= len(payload):
        header, = struct.unpack_from(">H", payload, offset)
        kind, length = header >> 9, header & 0x1FF
        value = payload[offset + 2:offset + 2 + length]
        offset += 2 + length
        if kind == 0:
            break
        if kind == 1 and value:
            neighbor.device_id = _lldp_id(value[0], value[1:])
        elif kind == 2 and value:
            port_subtype = value[0]
            neighbor.port_id = _mac(value[1:]) if port_subtype == 3 and len(value) == 7 else \
                _address(value[1], value[2:]) if port_subtype == 4 and len(value) > 2 else _text(value[1:])
        elif kind == 3 and len(value) >= 2:
            neighbor.ttl, = struct.unpack(">H", value[:2])
        elif kind == 4:
            neighbor.port_description = _text(value)
        elif kind == 5:
            neighbor.system_name = _text(value)
        elif kind == 6:
            neighbor.description = _text(value)
        elif kind == 7 and len(value) >= 4:
            _, enabled = struct.unpack(">HH", value[:4])
            neighbor.capabilities = [name for bit, name in enumerate(LLDP_CAPABILITIES) if enabled & (1 << bit)]
        elif kind == 8 and len(value) >= 2:
            address_length = value[0]
            if address_length >= 2:
                neighbor.management_addresses.append(_address(value[1], value[2:1 + address_length]))
        elif kind == 127 and len(value) >= 4:
            _parse_lldp_org(value[:3], value[3], value[4:], neighbor)


def _parse_lldp_org(oui, subtype, value, neighbor):
    if oui == b"\x00\x80\xc2":  # IEEE 802.1
        if subtype == 1 and len(value) >= 2:
            vlan, = struct.unpack(">H", value[:2])
            if vlan:
                neighbor.native_vlan = str(vlan)
        elif subtype == 3 and len(value) >= 3:
            vlan, name_length = struct.unpack(">HB", value[:3])
            neighbor.vlan_names.append(f"{vlan} {_text(value[3:3 + name_length])}")
    elif oui == b"\x00\x12\xbb" and subtype == 2 and len(value) >= 4:  # LLDP-MED network policy
        application = value[0]
        policy, = struct.unpack(">I", b"\0" + value[1:4])
        vlan = (policy >> 9) & 0xFFF
        if application == 1 and vlan:  # Voice
            neighbor.voice_vlan = str(vlan)


# ----------------------------------------------------------------- CDP

def _cdp_addresses(value):
    addresses = []
    if len(value) < 4:
        return addresses
    count, = struct.unpack(">I", value[:4])
    offset = 4
    for _ in range(count):
        if offset + 2 > len(value):
            break
        protocol_length = value[offset + 1]
        protocol = value[offset + 2:offset + 2 + protocol_length]
        offset += 2 + protocol_length
        address_length, = struct.unpack_from(">H", value, offset)
        raw = value[offset + 2:offset + 2 + address_length]
        offset += 2 + address_length
        if protocol == b"\xcc" and len(raw) == 4:
            addresses.append(socket.inet_ntoa(raw))
        elif len(raw) == 16:
            addresses.append(str(ipaddress.IPv6Address(raw)))
    return addresses


def parse_cdp(payload, neighbor):
    """Fill neighbor from a CDP packet (after the LLC/SNAP header)."""
    if len(payload) < 4:
        return
    neighbor.ttl = payload[1]
    offset = 4
    while offset + 4 <= len(payload):
        kind, length = struct.unpack_from(">HH", payload, offset)
        if length < 4:
            break
        value = payload[offset + 4:offset + length]
        offset += length
        if kind == 0x0001:
            neighbor.device_id = _text(value)
        elif kind == 0x0002:
            neighbor.management_addresses.extend(_cdp_addresses(value))
        elif kind == 0x0003:
            neighbor.port_id = _text(value)
        elif kind == 0x0004 and len(value) >= 4:
            bits, = struct.unpack(">I", value[:4])
            neighbor.capabilities = [name for bit, name in CDP_CAPABILITIES.items() if bits & bit]
        elif kind == 0x0005:
            neighbor.description = _text(value)
        elif kind == 0x0006:
            neighbor.platform = _text(value)
        elif kind == 0x000A and len(value) >= 2:
            neighbor.native_vlan = str(struct.unpack(">H", value[:2])[0])
        elif kind == 0x000B and value:
            neighbor.duplex = "Full" if value[0] else "Half"
        elif kind == 0x000E and len(value) >= 3:
            neighbor.voice_vlan = str(struct.unpack(">H", value[1:3])[0])
        elif kind == 0x0014:
            neighbor.system_name = _text(value)
        elif kind == 0x0016:
            for address in _cdp_addresses(value):
                if address not in neighbor.management_addresses:
                    neighbor.management_addresses.insert(0, address)
    neighbor.management_addresses = list(dict.fromkeys(neighbor.management_addresses))


def parse_frame(frame, interface=""):
    """Return a Neighbor if an Ethernet frame is an LLDP or CDP announcement, else None."""
    if len(frame) < 14:
        return None
    source = _mac(frame[6:12])
    ethertype, = struct.unpack_from(">H", frame, 12)
    offset = 14
    while ethertype in VLAN_ETHERTYPES and offset + 4 <= len(frame):
        ethertype, = struct.unpack_from(">H", frame, offset + 2)
        offset += 4
    if ethertype == LLDP_ETHERTYPE:
        neighbor = Neighbor("LLDP", source, interface)
        parse_lldp(frame[offset:], neighbor)
        return neighbor
    if ethertype <= 1500 and frame[offset:offset + 8] == CDP_SNAP:  # 802.3 length field, then LLC/SNAP
        neighbor = Neighbor("CDP", source, interface)
        parse_cdp(frame[offset + 8:], neighbor)
        return neighbor
    return None


# ----------------------------------------------------------------- pcapng

def read_pcapng(data):
    """Yield (interface name, frame) for each Ethernet packet in a pcapng file."""
    offset = 0
    endian = "<"
    interfaces = []  # (link type, name) per interface in the current section
    while offset + 12 <= len(data):
        block_type, = struct.unpack_from(endian + "I", data, offset)
        if block_type == 0x0A0D0D0A:  # Section header: byte order may change
            magic = data[offset + 8:offset + 12]
            endian = "<" if magic == b"\x4d\x3c\x2b\x1a" else ">"
            interfaces = []
        length, = struct.unpack_from(endian + "I", data, offset + 4)
        if length < 12 or offset + length > len(data):
            break
        body = data[offset + 8:offset + length - 4]
        if block_type == 1 and len(body) >= 8:  # Interface description
            link_type, = struct.unpack_from(endian + "H", body, 0)
            interfaces.append((link_type, _pcapng_interface_name(body[8:], endian)))
        elif block_type == 6 and len(body) >= 20:  # Enhanced packet
            interface_id, _, _, captured, _ = struct.unpack_from(endian + "IIIII", body, 0)
            if interface_id < len(interfaces) and interfaces[interface_id][0] == LINKTYPE_ETHERNET:
                yield interfaces[interface_id][1], body[20:20 + captured]
        elif block_type == 3 and len(body) >= 4 and interfaces and interfaces[0][0] == LINKTYPE_ETHERNET:
            yield interfaces[0][1], body[4:]  # Simple packet
        offset += length


def _pcapng_interface_name(options, endian):
    name = description = ""
    offset = 0
    while offset + 4 <= len(options):
        code, length = struct.unpack_from(endian + "HH", options, offset)
        value = options[offset + 4:offset + 4 + length]
        offset += 4 + (length + 3) // 4 * 4
        if code == 0:
            break
        if code == 2:
            name = value.decode("utf-8", "replace")
        elif code == 3:
            description = value.decode("utf-8", "replace")
    return description or name


def neighbors_from_pcapng(data):
    found = {}
    for interface, frame in read_pcapng(data):
        neighbor = parse_frame(frame, interface)
        if neighbor is not None:
            found[neighbor.key] = neighbor  # Keep the latest announcement from each
    return list(found.values())


# ----------------------------------------------------------------- Capture with pktmon

def find_pktmon():
    return shutil.which("pktmon.exe")


def _pktmon(*arguments):
    return run_command(["pktmon", *arguments], timeout=60)


def _add_filters():
    try:
        _pktmon("filter", "add", f"{FILTER_PREFIX}-LLDP", "-d", f"0x{LLDP_ETHERTYPE:04X}")
    except CommandError:  # Some versions only take decimal protocol numbers
        _pktmon("filter", "add", f"{FILTER_PREFIX}-LLDP", "-d", str(LLDP_ETHERTYPE))
    _pktmon("filter", "add", f"{FILTER_PREFIX}-CDP", "-m", CDP_MAC)


def discover(duration, should_stop=lambda: False, found=lambda neighbor: None,
             progress=lambda elapsed, duration: None, segment=SEGMENT_SECONDS):
    """Listen for switch announcements on every wired adapter for up to `duration` seconds.

    Stops early once a segment has heard at least one switch. found(neighbor) is called for each new one.
    Raises CommandError (for example, without administrator rights) or OSError.
    Note: this replaces any packet filters set in pktmon, and removes them when done.
    """
    if not find_pktmon():
        raise OSError("pktmon isn't available. It comes with Windows 10 version 1809 and later.")
    folder = tempfile.mkdtemp(prefix="nomad-lldp-")
    etl, pcap = os.path.join(folder, "capture.etl"), os.path.join(folder, "capture.pcapng")
    seen = {}
    started = time.monotonic()
    try:
        for arguments in (("stop",), ("filter", "remove")):  # A capture left running would make start fail
            try:
                _pktmon(*arguments)
            except CommandError:
                pass
        _add_filters()
        while not should_stop():
            elapsed = time.monotonic() - started
            if elapsed >= duration:
                break
            _pktmon("start", "--capture", "--comp", "nics", "--pkt-size", "0", "--file-name", etl)
            try:
                segment_end = time.monotonic() + min(segment, duration - elapsed)
                while time.monotonic() < segment_end and not should_stop():
                    progress(time.monotonic() - started, duration)
                    time.sleep(0.25)
            finally:
                _pktmon("stop")
            _pktmon("etl2pcap", etl, "--out", pcap)
            with open(pcap, "rb") as file:
                for neighbor in neighbors_from_pcapng(file.read()):
                    if neighbor.key not in seen:
                        found(neighbor)
                    seen[neighbor.key] = neighbor
            if seen:
                break
        progress(duration, duration)
        return list(seen.values())
    finally:
        for arguments in (("stop",), ("filter", "remove")):
            try:
                _pktmon(*arguments)
            except CommandError:
                pass
        shutil.rmtree(folder, ignore_errors=True)
