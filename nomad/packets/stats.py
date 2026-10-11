"""Summaries of a capture: conversations, top talkers, protocols, VLANs, the devices heard announcing themselves
(CDP/LLDP) and traffic over time. Each row knows the display filter that shows its packets."""
from collections import Counter
from dataclasses import dataclass, field

from .. import oui


@dataclass
class Conversation:
    kind: str  # "ip", "eth", "tcp" or "udp"
    a: str
    b: str
    packets_ab: int = 0
    bytes_ab: int = 0
    packets_ba: int = 0
    bytes_ba: int = 0
    first: float = 0.0
    last: float = 0.0
    filter: str = ""
    note: str = ""  # TCP: how it went (handshake time, reset)

    @property
    def packets(self):
        return self.packets_ab + self.packets_ba

    @property
    def bytes(self):
        return self.bytes_ab + self.bytes_ba

    @property
    def duration(self):
        return self.last - self.first


@dataclass
class Endpoint:
    kind: str  # "ip" or "eth"
    address: str
    name: str = ""
    packets_sent: int = 0
    bytes_sent: int = 0
    packets_received: int = 0
    bytes_received: int = 0
    filter: str = ""

    @property
    def packets(self):
        return self.packets_sent + self.packets_received

    @property
    def bytes(self):
        return self.bytes_sent + self.bytes_received


@dataclass
class ProtocolRow:
    name: str
    depth: int
    packets: int
    bytes: int
    filter: str
    children: list = field(default_factory=list)


@dataclass
class Neighbor:
    protocol: str
    device: str
    port: str
    mac: str
    platform: str = ""
    native_vlan: str = ""
    address: str = ""
    packets: int = 0
    filter: str = ""


@dataclass
class Summary:
    packets: int = 0
    bytes: int = 0
    duration: float = 0.0
    first: float = 0.0  # Seconds since 1970
    conversations: dict = field(default_factory=dict)  # kind -> [Conversation]
    endpoints: dict = field(default_factory=dict)  # kind -> [Endpoint]
    protocols: list = field(default_factory=list)  # Top-level ProtocolRows
    vlans: Counter = field(default_factory=Counter)
    neighbors: list = field(default_factory=list)
    names: dict = field(default_factory=dict)  # IP address -> name learned from the capture (DNS, DHCP)
    broadcasts: int = 0
    multicasts: int = 0

    def headline(self):
        rate = self.bytes * 8 / self.duration if self.duration > 0 else 0
        text = (f"{self.packets:,} packet{'' if self.packets == 1 else 's'}, {size_text(self.bytes)} over "
                f"{duration_text(self.duration)}")
        if rate:
            text += f", averaging {rate_text(rate)}"
        if self.packets:
            text += f". Broadcast {self.broadcasts * 100 / self.packets:.1f}%, multicast " \
                    f"{self.multicasts * 100 / self.packets:.1f}%"
        if self.vlans:
            text += ". VLANs tagged: " + ", ".join(f"{vlan} ({count:,})" for vlan, count in
                                                   sorted(self.vlans.items())[:12])
        return text + "."


def size_text(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def rate_text(bits_per_second):
    for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if bits_per_second < 1000 or unit == "Gbit/s":
            return f"{bits_per_second:.0f} {unit}" if unit == "bit/s" else f"{bits_per_second:.1f} {unit}"
        bits_per_second /= 1000
    return ""


def duration_text(seconds):
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 120:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def address_filter(address):
    return f"ipv6.addr == {address}" if ":" in address else f"ip.addr == {address}"


class Collector:
    """Fed every packet (and its dissection) while the capture is decoded."""

    def __init__(self):
        self.summary = Summary()
        self.chains = Counter()
        self.chain_bytes = Counter()
        self.ip = {}  # (a, b) -> Conversation
        self.eth = {}
        self.ip_endpoints = {}
        self.eth_endpoints = {}
        self.neighbors = {}
        self.dns_names = {}
        self.mac_names = {}
        self.arp = {}  # IP address -> MAC address (ARP senders)

    def add(self, packet, d):
        summary = self.summary
        size = packet.wire_length
        summary.packets += 1
        summary.bytes += size
        self.chains[packet.chain] += 1
        self.chain_bytes[packet.chain] += size
        values = d.values
        if d.macs:
            source, destination = d.macs
            self.pair(self.eth, self.eth_endpoints, "eth", source, destination, packet)
            if destination == "ff:ff:ff:ff:ff:ff":
                summary.broadcasts += 1
            elif int(destination[1], 16) & 1:
                summary.multicasts += 1
        if d.addresses:
            self.pair(self.ip, self.ip_endpoints, "ip", d.addresses[0], d.addresses[1], packet)
        vlans = values.get("vlan.id")
        if vlans:
            summary.vlans[vlans[0]] += 1
        if "dns.qry.name" in values and ("dns.a" in values or "dns.aaaa" in values):
            name = values["dns.qry.name"][0]
            for address in values.get("dns.a", []) + values.get("dns.aaaa", []):
                self.dns_names.setdefault(str(address), name)
        if "dhcp.option.hostname" in values and "dhcp.hw.mac_addr" in values:
            self.mac_names[values["dhcp.hw.mac_addr"][0]] = values["dhcp.option.hostname"][0]
        if "arp.src.hw_mac" in values:
            address = str(values["arp.src.proto_ipv4"][0])
            if address != "0.0.0.0":
                self.arp.setdefault(address, values["arp.src.hw_mac"][0])
        if "cdp" in packet.chain or "lldp" in packet.chain:
            self.neighbor(packet, d)

    def pair(self, conversations, endpoints, kind, source, destination, packet):
        key = (source, destination) if source <= destination else (destination, source)
        conversation = conversations.get(key)
        if conversation is None:
            conversation = Conversation(kind, source, destination, first=packet.time)
            conversations[key] = conversation
        if source == conversation.a:
            conversation.packets_ab += 1
            conversation.bytes_ab += packet.wire_length
        else:
            conversation.packets_ba += 1
            conversation.bytes_ba += packet.wire_length
        conversation.last = packet.time
        for address, sent in ((source, True), (destination, False)):
            endpoint = endpoints.get(address)
            if endpoint is None:
                endpoint = endpoints[address] = Endpoint(kind, address)
            if sent:
                endpoint.packets_sent += 1
                endpoint.bytes_sent += packet.wire_length
            else:
                endpoint.packets_received += 1
                endpoint.bytes_received += packet.wire_length

    def neighbor(self, packet, d):
        values = d.values
        if "cdp" in packet.chain:
            protocol, device = "CDP", first(values, "cdp.deviceid")
            port, platform = first(values, "cdp.portid"), first(values, "cdp.platform")
            vlan, address = first(values, "cdp.native_vlan"), first(values, "cdp.address")
        else:
            protocol = "LLDP"
            device = first(values, "lldp.tlv.system.name") or first(values, "lldp.chassis.id")
            port = first(values, "lldp.port.id")
            description = first(values, "lldp.port.desc")
            if description and description != port:
                port = f"{port} ({description})"
            platform = first(values, "lldp.tlv.system.desc").splitlines()[0] if first(
                values, "lldp.tlv.system.desc") else ""
            vlan, address = first(values, "lldp.ieee.802_1.port_vlan.id"), first(values, "lldp.mgn.addr.ip4")
        source = d.macs[0] if d.macs else ""
        key = (protocol, device, port)
        neighbor = self.neighbors.get(key)
        if neighbor is None:
            name = protocol.lower()
            id_field = "cdp.deviceid" if protocol == "CDP" else "lldp.port.id"
            id_value = device if protocol == "CDP" else first(values, "lldp.port.id")
            neighbor = self.neighbors[key] = Neighbor(protocol, device, port, source, platform, str(vlan or ""),
                                                      str(address or ""),
                                                      filter=f'{name} && {id_field} == "{id_value}"')
        neighbor.packets += 1
        if device and source:
            self.mac_names.setdefault(source, device)

    def finish(self, view):
        summary = self.summary
        packets = view.packets
        if packets:
            summary.first = packets[0].epoch
            summary.duration = packets[-1].time
        names = dict(self.dns_names)
        for address, mac in self.arp.items():
            if address not in names and mac in self.mac_names:
                names[address] = self.mac_names[mac]
        summary.names = names
        ip_conversations = list(self.ip.values())
        for conversation in ip_conversations:
            conversation.filter = f"{address_filter(conversation.a)} && {address_filter(conversation.b)}"
        eth_conversations = list(self.eth.values())
        for conversation in eth_conversations:
            conversation.filter = f"eth.addr == {conversation.a} && eth.addr == {conversation.b}"
        streams = {"tcp": [], "udp": []}
        for stream in view.tracker.streams:
            a_flow, b_flow = stream.flows
            conversation = Conversation(stream.kind, endpoint_text(stream.a), endpoint_text(stream.b), a_flow.packets,
                                        a_flow.bytes, b_flow.packets, b_flow.bytes, stream.first, stream.last,
                                        f"{stream.kind}.stream == {stream.index}")
            if stream.kind == "tcp":
                conversation.note = tcp_note(stream)
            streams[stream.kind].append(conversation)
        summary.conversations = {"ip": by_bytes(ip_conversations), "tcp": by_bytes(streams["tcp"]),
                                 "udp": by_bytes(streams["udp"]), "eth": by_bytes(eth_conversations)}
        ip_endpoints = list(self.ip_endpoints.values())
        for endpoint in ip_endpoints:
            endpoint.name = names.get(endpoint.address, "")
            endpoint.filter = address_filter(endpoint.address)
        eth_endpoints = list(self.eth_endpoints.values())
        for endpoint in eth_endpoints:
            endpoint.name = self.mac_names.get(endpoint.address) or oui.vendor(endpoint.address)
            endpoint.filter = f"eth.addr == {endpoint.address}"
        summary.endpoints = {"ip": by_bytes(ip_endpoints), "eth": by_bytes(eth_endpoints)}
        summary.protocols = hierarchy(self.chains, self.chain_bytes)
        summary.neighbors = sorted(self.neighbors.values(), key=lambda item: (item.device.lower(), item.port))
        return summary


def first(values, name):
    found = values.get(name)
    return found[0] if found else ""


def endpoint_text(pair):
    address, port = pair
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def tcp_note(stream):
    parts = []
    if stream.handshake_seconds is not None:
        parts.append(f"handshake {stream.handshake_seconds * 1000:.1f} ms")
    elif stream.syn is not None:
        parts.append("no answer to SYN" if stream.reset_by is None else "refused")
    if stream.reset_by is not None and stream.syn_ack is not None:
        parts.append(f"reset by {endpoint_text(stream.a if stream.reset_by == 0 else stream.b)}")
    resent = sum(stream.counts.get(flag, 0) for flag in ("retransmission", "fast_retransmission",
                                                         "spurious_retransmission"))
    if resent:
        parts.append(f"{resent} resent")
    if stream.counts.get("lost_segment"):
        parts.append(f"{stream.counts['lost_segment']} gaps")
    if stream.counts.get("zero_window"):
        parts.append("zero window")
    return ", ".join(parts)


def by_bytes(items):
    return sorted(items, key=lambda item: item.bytes, reverse=True)


def hierarchy(chains, chain_bytes):
    """ProtocolRows from the protocols each packet had (outermost first), as Wireshark's Protocol Hierarchy."""
    root = ProtocolRow("", -1, 0, 0, "")
    for chain, count in chains.items():
        node = root
        seen = []
        for name in chain:
            if name == "_ws.malformed":
                name = "malformed"
            seen.append(name)
            child = next((item for item in node.children if item.name == name), None)
            if child is None:
                child = ProtocolRow(name, node.depth + 1, 0, 0, "_ws.malformed" if name == "malformed" else name)
                node.children.append(child)
            child.packets += count
            child.bytes += chain_bytes[chain]
            node = child
    sort_rows(root.children)
    return root.children


def sort_rows(rows):
    rows.sort(key=lambda row: row.packets, reverse=True)
    for row in rows:
        sort_rows(row.children)


def timeline(packets, buckets=200):
    """(seconds per bucket, [(packets, bytes)] per bucket) for traffic over time."""
    if not packets:
        return 1.0, []
    duration = packets[-1].time
    width = nice_width(duration / buckets if duration > 0 else 1.0)
    count = int(duration // width) + 1
    rows = [[0, 0] for _ in range(count)]
    for packet in packets:
        row = rows[min(int(packet.time // width), count - 1)]
        row[0] += 1
        row[1] += packet.wire_length
    return width, [tuple(row) for row in rows]


def nice_width(seconds):
    """A round bucket size: 1, 2 or 5 times a power of ten seconds, at least 1 ms."""
    seconds = max(seconds, 0.001)
    scale = 0.001
    while True:
        for step in (1, 2, 5):
            if step * scale >= seconds:
                return step * scale
        scale *= 10
