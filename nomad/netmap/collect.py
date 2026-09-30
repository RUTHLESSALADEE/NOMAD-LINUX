"""Reading a device's SNMP tables for the map: its identity, CDP/LLDP neighbors, VLANs, MAC table and ARP table.

The parsers take the (oid, Value) pairs a walk returns, so they can be tested without a network.
"""
import ipaddress
import socket
from dataclasses import dataclass, field

from ..oui import format_mac
from ..snmp import IP_ADDRESS, OBJECT_ID, oid_text, parse_oid
from .model import AP, FIREWALL, HOST, PHONE, ROUTER, SWITCH, UNKNOWN

SYS_DESCR, SYS_OBJECT_ID, SYS_NAME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.5.0"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_STACK_STATUS = "1.3.6.1.2.1.31.1.2.1.3"  # Index: higher layer ifIndex, lower layer ifIndex (port-channel members)
LAG_ATTACHED = "1.2.840.10006.300.43.1.2.1.1.13"  # dot3adAggPortAttachedAggID: member ifIndex -> aggregate ifIndex
IP_ADDR_ENTRY = "1.3.6.1.2.1.4.20.1"
ARP_PHYS_ADDRESS = "1.3.6.1.2.1.4.22.1.2"
FDB_ENTRY = "1.3.6.1.2.1.17.4.3.1"
BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
CDP_CACHE_ENTRY = "1.3.6.1.4.1.9.9.23.1.2.1.1"
VTP_VLAN_STATE = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"
LLDP_LOC_PORT_ENTRY = "1.0.8802.1.1.2.1.3.7.1"
LLDP_REM_ENTRY = "1.0.8802.1.1.2.1.4.1.1"
LLDP_REM_MAN_ADDR_IF_SUBTYPE = "1.0.8802.1.1.2.1.4.2.1.3"
CIDR_ROUTE_ENTRY = "1.3.6.1.2.1.4.24.4.1"  # ipCidrRouteTable: index destination, mask, TOS, next hop
IP_ROUTE_ENTRY = "1.3.6.1.2.1.4.21.1"  # The older ipRouteTable, for devices without the one above
ROUTE_PROTOCOLS = {1: "other", 2: "connected", 3: "static", 4: "icmp", 8: "rip", 9: "is-is", 11: "igrp", 13: "ospf",
                   14: "bgp", 16: "eigrp"}

CISCO = "1.3.6.1.4.1.9"
PALO_ALTO = "1.3.6.1.4.1.25461"
RESERVED_VLANS = range(1002, 1006)  # FDDI/Token Ring defaults every Catalyst lists
FDB_LEARNED = 3
AP_WORDS = ("air-", "access point", "c9105", "c9115", "c9120", "c9130", "c9136", "c9162", "c9164", "c9166", "cw916")
ROUTER_WORDS = ("isr", "asr1", "asr9", "csr1000", "c8200", "c8300", "c8500", "c8000", "c1100", "c1111", "c1121",
                "c1161", "router")
SWITCH_WORDS = ("catalyst", "nexus", "nx-os", "switch", "c9200", "c9300", "c9400", "c9500", "c9600", "c2960",
                "c3560", "c3650", "c3750", "c3850", "c4500", "c6500", "c6800", "ws-c", "ie-", "cat9k", "cat3k")

# CDP capability bits (cdpCacheCapabilities, a 4-byte bitmask)
CDP_CAPABILITIES = {0x01: "router", 0x02: "bridge", 0x04: "bridge", 0x08: "switch", 0x10: "host", 0x80: "phone"}
# LLDP system capabilities (a BITS value: the first byte's top bit is bit 0)
LLDP_CAPABILITIES = {2: "bridge", 3: "ap", 4: "router", 5: "phone", 7: "station"}


@dataclass
class Neighbor:
    local_port: str
    name: str
    port: str = ""
    address: str = ""
    platform: str = ""
    capabilities: frozenset = frozenset()
    protocol: str = "cdp"
    chassis_mac: str = ""  # LLDP chassis ID when it's a MAC address


@dataclass
class SystemInfo:
    name: str = ""
    descr: str = ""
    object_id: str = ""


@dataclass
class DeviceTables:
    """Everything read from one device."""
    info: SystemInfo = field(default_factory=SystemInfo)
    interfaces: dict = field(default_factory=dict)  # ifIndex -> name
    addresses: list = field(default_factory=list)  # [(ip, ifIndex, mask)]
    neighbors: list = field(default_factory=list)  # [Neighbor]
    arp: dict = field(default_factory=dict)  # MAC -> [ip]
    fdb: list = field(default_factory=list)  # [(MAC, ifIndex, vlan)]
    own_macs: set = field(default_factory=set)
    lag_parents: dict = field(default_factory=dict)  # Member ifIndex -> aggregate ifIndex
    routes: list = field(default_factory=list)  # [(destination, next hop, ifIndex, protocol)]
    routes_truncated: bool = False
    warnings: list = field(default_factory=list)


def text(value):
    """An OCTET STRING as text."""
    if value is None or value.is_exception:
        return ""
    if isinstance(value.value, bytes):
        return value.value.decode("utf-8", "replace").rstrip("\0").strip()
    return "" if value.value is None else str(value.value)


def mac_text(raw):
    return format_mac(raw.hex()) if isinstance(raw, bytes) and len(raw) == 6 else ""


def is_printable(raw):
    try:
        decoded = raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return False
    return bool(decoded.strip("\0")) and all(character.isprintable() for character in decoded.rstrip("\0"))


def columns(rows, entry):
    """Split a walked table into {index: {column: Value}}. Index is the tuple of OID numbers after the column."""
    entry = parse_oid(entry) if isinstance(entry, str) else tuple(entry)
    table = {}
    for oid, value in rows:
        if oid[:len(entry)] != entry or len(oid) <= len(entry) + 1 or value.is_exception:
            continue
        table.setdefault(oid[len(entry) + 1:], {})[oid[len(entry)]] = value
    return table


def column(rows, root):
    """One walked column as {index: Value}."""
    root = parse_oid(root) if isinstance(root, str) else tuple(root)
    return {oid[len(root):]: value for oid, value in rows
            if oid[:len(root)] == root and len(oid) > len(root) and not value.is_exception}


def system_info(results):
    """From a get of SYS_DESCR, SYS_OBJECT_ID and SYS_NAME."""
    info = SystemInfo()
    for oid, value in results:
        name = oid_text(oid)
        if name == SYS_NAME:
            info.name = text(value)
        elif name == SYS_DESCR:
            info.descr = text(value)
        elif name == SYS_OBJECT_ID and value.tag == OBJECT_ID:
            info.object_id = oid_text(value.value)
    return info


def interface_names(name_rows, descr_rows=()):
    """ifIndex -> ifName, falling back to ifDescr for interfaces without one."""
    names = {index[0]: text(value) for index, value in column(descr_rows, IF_DESCR).items() if len(index) == 1}
    for index, value in column(name_rows, IF_NAME).items():
        if len(index) == 1 and text(value):
            names[index[0]] = text(value)
    return names


def own_macs(rows):
    return {mac for mac in (mac_text(value.value) for value in column(rows, IF_PHYS_ADDRESS).values()) if mac}


def cdp_capabilities(value):
    if value is None or not isinstance(value.value, bytes):
        return frozenset()
    bits = int.from_bytes(value.value[-4:], "big")
    return frozenset(name for bit, name in CDP_CAPABILITIES.items() if bits & bit)


def lldp_capabilities(value):
    if value is None or not isinstance(value.value, bytes):
        return frozenset()
    raw = value.value
    found = set()
    for bit, name in LLDP_CAPABILITIES.items():
        byte, offset = divmod(bit, 8)
        if byte < len(raw) and raw[byte] & (0x80 >> offset):
            found.add(name)
    return frozenset(found)


def cdp_neighbors(rows, interfaces):
    """cdpCacheTable: index is (local ifIndex, device index)."""
    neighbors = []
    for index, row in sorted(columns(rows, CDP_CACHE_ENTRY).items()):
        name = text(row.get(6))
        if not name or len(index) != 2:
            continue
        address = ""
        raw = row.get(4)
        if raw is not None and isinstance(raw.value, bytes) and len(raw.value) == 4 \
                and (row.get(3) is None or row[3].value == 1):
            address = socket.inet_ntoa(raw.value)
        neighbors.append(Neighbor(local_port=interfaces.get(index[0], f"ifIndex {index[0]}"), name=name,
                                  port=text(row.get(7)), address=address, platform=text(row.get(8)),
                                  capabilities=cdp_capabilities(row.get(9)), protocol="cdp"))
    return neighbors


def lldp_local_ports(rows):
    """lldpLocPortTable: local port number -> interface name."""
    ports = {}
    for index, row in columns(rows, LLDP_LOC_PORT_ENTRY).items():
        port_id, description = row.get(3), text(row.get(4))
        subtype = row[2].value if 2 in row else 0
        name = text(port_id) if port_id is not None and subtype in (5, 7) and is_printable(port_id.value) else ""
        ports[index[0]] = name or description or text(port_id)
    return ports


def lldp_management_addresses(rows):
    """lldpRemManAddrTable, walked by one column: {(local port, remote index): IPv4 address}. The address is in the
    index (time mark, local port, remote index, address subtype, length, address bytes)."""
    addresses = {}
    for index in column(rows, LLDP_REM_MAN_ADDR_IF_SUBTYPE):
        if len(index) >= 9 and index[3] == 1 and index[4] == 4:
            addresses.setdefault((index[1], index[2]), ".".join(str(number) for number in index[5:9]))
    return addresses


def lldp_neighbors(rows, local_ports, interfaces, management_addresses):
    """lldpRemTable: index is (time mark, local port number, remote index)."""
    neighbors = []
    for index, row in sorted(columns(rows, LLDP_REM_ENTRY).items()):
        if len(index) != 3:
            continue
        _, local, remote = index
        chassis = row.get(5)
        chassis_subtype = row[4].value if 4 in row else 0
        chassis_mac = mac_text(chassis.value) if chassis is not None and chassis_subtype == 4 else ""
        name = text(row.get(9)) or chassis_mac or (text(chassis) if chassis is not None
                                                    and is_printable(chassis.value) else "")
        if not name:
            continue
        port_id, port_subtype = row.get(7), (row[6].value if 6 in row else 0)
        if port_id is not None and port_subtype in (5, 7) and is_printable(port_id.value):
            port = text(port_id)
        elif port_id is not None and port_subtype == 3:
            port = text(row.get(8)) or mac_text(port_id.value)
        else:
            port = text(row.get(8)) or (text(port_id) if port_id is not None and is_printable(port_id.value) else "")
        local_port = local_ports.get(local) or interfaces.get(local, f"port {local}")
        descr = text(row.get(10))
        neighbors.append(Neighbor(local_port=local_port, name=name, port=port,
                                  address=management_addresses.get((local, remote), ""),
                                  platform=descr.splitlines()[0][:80] if descr else "",
                                  capabilities=lldp_capabilities(row.get(12)), protocol="lldp",
                                  chassis_mac=chassis_mac))
    return neighbors


def vlans(rows):
    """VLAN numbers from CISCO-VTP-MIB vtpVlanState (index: domain, VLAN), operational ones only."""
    found = set()
    for index, value in column(rows, VTP_VLAN_STATE).items():
        if len(index) == 2 and value.value == 1 and index[1] not in RESERVED_VLANS:
            found.add(index[1])
    return sorted(found)


def fdb(entry_rows, base_port_rows, vlan=0):
    """Learned MACs from dot1dTpFdbTable as [(MAC, ifIndex, vlan)]; the switch's own MACs (status self) are left
    out."""
    base_ports = {index[0]: value.value for index, value in column(base_port_rows, BASE_PORT_IFINDEX).items()
                  if len(index) == 1}
    entries = []
    for index, row in columns(entry_rows, FDB_ENTRY).items():
        if len(index) != 6 or 2 not in row or (3 in row and row[3].value != FDB_LEARNED):
            continue
        if_index = base_ports.get(row[2].value)
        if if_index:
            entries.append((format_mac(bytes(index).hex()), if_index, vlan))
    return entries


def arp(rows):
    """ipNetToMediaPhysAddress (index: ifIndex, IPv4 address) as {MAC: [ip]}."""
    table = {}
    for index, value in column(rows, ARP_PHYS_ADDRESS).items():
        mac = mac_text(value.value)
        if len(index) == 5 and mac:
            table.setdefault(mac, []).append(".".join(str(number) for number in index[1:]))
    for addresses in table.values():
        addresses.sort(key=lambda address: ipaddress.ip_address(address))
    return table


def ip_addresses(rows):
    """ipAddrTable as [(ip, ifIndex, mask)]."""
    found = []
    for index, row in columns(rows, IP_ADDR_ENTRY).items():
        if len(index) != 4:
            continue
        address = ".".join(str(number) for number in index)
        mask = row[3].value if 3 in row and row[3].tag == IP_ADDRESS else ""
        found.append((address, row[2].value if 2 in row else 0, mask))
    return sorted(found, key=lambda item: ipaddress.ip_address(item[0]))


def lag_parents(stack_rows, lag_rows):
    """Port-channel members: {member ifIndex: port-channel ifIndex}, from ifStackTable or IEEE8023-LAG-MIB."""
    parents = {}
    for index, value in column(lag_rows, LAG_ATTACHED).items():
        if len(index) == 1 and isinstance(value.value, int) and value.value and value.value != index[0]:
            parents[index[0]] = value.value
    for index, value in column(stack_rows, IF_STACK_STATUS).items():
        if len(index) == 2 and index[0] and index[1] and value.value == 1:
            parents.setdefault(index[1], index[0])
    return parents


def classify(object_id="", descr="", capabilities=frozenset(), platform=""):
    """Switch, router, firewall, access point, phone or host, from what the device says about itself."""
    words = f"{descr} {platform}".lower()
    if object_id.startswith(PALO_ALTO + ".") or "palo alto" in words or "pan-os" in words:
        return FIREWALL
    if "adaptive security appliance" in words or "firepower" in words or platform.lower().startswith(("asa", "ftd")):
        return FIREWALL
    if "phone" in capabilities or "phone" in words:
        return PHONE
    if "ap" in capabilities or any(word in words for word in AP_WORDS):
        return AP
    # The model says more than the capabilities: an ISR with a switch module announces both
    if any(word in words for word in ROUTER_WORDS):
        return ROUTER
    if any(word in words for word in SWITCH_WORDS):
        return SWITCH
    if "switch" in capabilities or "bridge" in capabilities:
        return SWITCH
    if "router" in capabilities:
        return ROUTER
    if "station" in capabilities or "host" in capabilities:
        return HOST
    if object_id.startswith(CISCO + "."):
        return ROUTER  # A Cisco box that answers SNMP but said nothing clearer
    return UNKNOWN


def _dotted(numbers):
    return ".".join(str(number) for number in numbers)


def _network(address, mask):
    try:
        return str(ipaddress.ip_network(f"{address}/{mask}", strict=False))
    except ValueError:
        return ""


def routes(cidr_rows, old_rows=()):
    """The routing table as [(destination, next hop, ifIndex, protocol)], from ipCidrRouteTable, or ipRouteTable
    when a device doesn't have that. Next hop is "" for directly connected networks."""
    found = []
    for index, row in columns(cidr_rows, CIDR_ROUTE_ENTRY).items():
        if len(index) != 13:
            continue
        destination = _network(_dotted(index[:4]), _dotted(index[4:8]))
        next_hop = _dotted(index[9:13])
        kind = row[6].value if 6 in row else 0
        if not destination or kind == 2:  # Reject (null) routes lead nowhere
            continue
        protocol = ROUTE_PROTOCOLS.get(row[7].value, "other") if 7 in row else "other"
        found.append((destination, "" if kind == 3 or next_hop == "0.0.0.0" else next_hop,
                      row[5].value if 5 in row else 0, protocol))
    if not found:
        for index, row in columns(old_rows, IP_ROUTE_ENTRY).items():
            if len(index) != 4 or 11 not in row:
                continue
            destination = _network(_dotted(index), row[11].value)
            next_hop = row[7].value if 7 in row and isinstance(row[7].value, str) else ""
            kind = row[8].value if 8 in row else 0
            if not destination or kind == 2:
                continue
            protocol = ROUTE_PROTOCOLS.get(row[9].value, "other") if 9 in row else "other"
            found.append((destination, "" if kind == 3 or next_hop == "0.0.0.0" else next_hop,
                          row[2].value if 2 in row else 0, protocol))
    return sorted(found, key=lambda route: (ipaddress.ip_network(route[0]).network_address,
                                            ipaddress.ip_network(route[0]).prefixlen, route[1]))
