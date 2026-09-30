"""What a network map holds: devices, the links between them, and the hosts on switch ports. Saved as JSON."""
import json
import re
from dataclasses import asdict, dataclass, field, fields

FORMAT_VERSION = 1

# Kinds of device
SWITCH, ROUTER, FIREWALL, AP, PHONE, HOST, UNKNOWN = "switch", "router", "firewall", "ap", "phone", "host", "unknown"
NETWORK_KINDS = {SWITCH, ROUTER, FIREWALL}  # Worth asking over SNMP, and whose links carry other devices' traffic
KIND_NAMES = {SWITCH: "Switch", ROUTER: "Router", FIREWALL: "Firewall", AP: "Access point", PHONE: "Phone",
              HOST: "Host", UNKNOWN: "Unknown"}

# How a device was found
SNMP = "snmp"  # Answered SNMP: its neighbors, MAC and ARP tables were read
NEIGHBOR = "neighbor"  # Only in another device's CDP/LLDP (not asked: out of scope, too far, or not a network device)
NO_SNMP = "no-snmp"  # Answers ping but not SNMP (wrong community or an ACL)
UNREACHABLE = "unreachable"  # Answered neither SNMP nor ping
SOURCE_NAMES = {SNMP: "SNMP", NEIGHBOR: "Seen as a neighbor", NO_SNMP: "Pings, no SNMP", UNREACHABLE: "Unreachable"}

SHARED_PORT_HOSTS = 8  # More MACs than this on a port with no neighbor: probably an unmanaged switch or a hypervisor

PORT_PREFIXES = [  # Longest first, so TenGigabitEthernet isn't taken for GigabitEthernet
    ("hundredgigabitethernet", "Hu"), ("hundredgige", "Hu"), ("fortygigabitethernet", "Fo"),
    ("twentyfivegigabitethernet", "Twe"), ("twentyfivegige", "Twe"), ("tengigabitethernet", "Te"),
    ("fivegigabitethernet", "Fi"), ("twogigabitethernet", "Tw"), ("appgigabitethernet", "Ap"),
    ("gigabitethernet", "Gi"), ("fastethernet", "Fa"), ("port-channel", "Po"), ("ethernet", "Eth"),
    ("management", "Mgmt"), ("vlan", "Vl"),
]


def short_port(name):
    """Cisco's short interface names: GigabitEthernet1/0/1 -> Gi1/0/1, Ethernet1/1 -> Eth1/1. Others unchanged."""
    name = (name or "").strip()
    lower = name.lower()
    for prefix, short in PORT_PREFIXES:
        if lower.startswith(prefix) and len(name) > len(prefix) and (name[len(prefix)].isdigit()
                                                                    or name[len(prefix)] == " "):
            return short + name[len(prefix):].strip()
    return name


def port_key(name):
    """For matching the same port written two ways (Gi1/0/1, GigabitEthernet1/0/1, gi1/0/1)."""
    return short_port(name).lower().replace(" ", "")


def display_name(name):
    """A device name as CDP gives it, without the serial number some devices add: "sw1(FOC1234X0YZ)" -> "sw1"."""
    return re.sub(r"\(.*\)\s*$", "", (name or "").strip()).strip()


def normalize_name(name):
    """A device's name as a key: CDP's "core-sw1.corp.example(FOC1234X0YZ)" and LLDP's "core-sw1" are the same."""
    name = display_name(name)
    if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", name):
        return name.lower()  # An address, not a host name
    return name.split(".")[0].lower()


@dataclass
class Device:
    key: str
    name: str = ""
    mgmt_ip: str = ""
    addresses: list = field(default_factory=list)
    kind: str = UNKNOWN
    platform: str = ""
    sys_descr: str = ""
    sys_object_id: str = ""
    source: str = NEIGHBOR
    hops: int = 0
    error: str = ""
    interfaces_l3: list = field(default_factory=list)  # [[ip, prefix length, port]]
    routes: list = field(default_factory=list)  # [[destination, next hop ("" if connected), port, protocol]]
    routes_truncated: bool = False

    @property
    def label(self):
        return self.name or self.mgmt_ip or self.key


@dataclass
class Link:
    a: str
    a_port: str
    b: str
    b_port: str
    protocols: list = field(default_factory=list)  # ["cdp"], ["lldp"] or both

    @property
    def key(self):
        return frozenset([(self.a, port_key(self.a_port)), (self.b, port_key(self.b_port))])

    def port_on(self, device):
        return self.a_port if device == self.a else self.b_port

    def other(self, device):
        return self.b if device == self.a else self.a


@dataclass
class Host:
    mac: str
    device: str  # The switch it was learned on
    port: str
    ip: str = ""
    vendor: str = ""
    vlan: int = 0
    name: str = ""  # From CDP/LLDP for phones and other end devices that announce themselves
    platform: str = ""
    manual: bool = False  # Added by hand (a device that's off or unplugged while mapping), kept when mapping again
    note: str = ""

    def same_as(self, other):
        """The same machine: by MAC, or by IP when either has no MAC."""
        if self.mac and other.mac:
            return self.mac == other.mac
        return bool(self.ip) and self.ip == other.ip


@dataclass
class Trace:
    """A traceroute from this computer: the address that answered at each hop ("" where none did)."""
    target: str
    hops: list = field(default_factory=list)
    reached: bool = False
    reason: str = ""  # Why it was traced: an unreachable device, a next hop, a static route


@dataclass
class NetworkMap:
    devices: dict = field(default_factory=dict)  # key -> Device
    links: list = field(default_factory=list)
    hosts: list = field(default_factory=list)
    seeds: list = field(default_factory=list)
    started: str = ""
    finished: str = ""
    stopped: bool = False
    positions: dict = field(default_factory=dict)  # Device key -> [x, y] where the user left it
    root: str = ""  # Device laid out at the top, when the user chose one
    traces: list = field(default_factory=list)  # [Trace]
    l3_positions: dict = field(default_factory=dict)  # Node key -> [x, y] on the logical (L3) view

    def add_link(self, link):
        """Add a link, merging it with the same link seen from the other end (or by the other protocol)."""
        for existing in self.links:
            if existing.key == link.key:
                for protocol in link.protocols:
                    if protocol not in existing.protocols:
                        existing.protocols.append(protocol)
                return existing
        self.links.append(link)
        return link

    def carry_manual_hosts(self, older):
        """Bring the hosts added by hand to an earlier map of the network over to this one. One that has since
        been found for real is left to the crawl, which gets its name and note. Returns the ones left out because
        their switch isn't on this map."""
        dropped = []
        for manual in (host for host in older.hosts if host.manual):
            found = next((host for host in self.hosts if not host.manual and host.same_as(manual)), None)
            if found is not None:
                found.name = found.name or manual.name
                found.note = found.note or manual.note
            elif manual.device in self.devices:
                self.hosts.append(manual)
            else:
                dropped.append(manual)
        self.hosts.sort(key=lambda host: (self.devices[host.device].label.lower(), port_key(host.port), host.mac))
        return dropped

    def links_of(self, key):
        return [link for link in self.links if key in (link.a, link.b)]

    def hosts_by_port(self, key):
        """{port: [Host]} for one switch, in port order."""
        ports = {}
        for host in self.hosts:
            if host.device == key:
                ports.setdefault(host.port, []).append(host)
        return dict(sorted(ports.items(), key=lambda item: port_sort_key(item[0])))

    def to_json(self):
        data = asdict(self)
        data["devices"] = list(data["devices"].values())
        data["format"] = FORMAT_VERSION
        return json.dumps(data, indent=1)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        if not isinstance(data, dict) or "devices" not in data:
            raise ValueError("This isn't a NOMAD network map.")
        if data.get("format", 1) > FORMAT_VERSION:
            raise ValueError("This map was saved by a newer version of NOMAD.")
        network_map = cls(**{name: data[name] for name in ("seeds", "started", "finished", "stopped", "root")
                             if name in data})
        network_map.devices = {item["key"]: _build(Device, item) for item in data["devices"]}
        network_map.links = [_build(Link, item) for item in data.get("links", [])]
        network_map.hosts = [_build(Host, item) for item in data.get("hosts", [])]
        network_map.positions = {key: tuple(value) for key, value in data.get("positions", {}).items()
                                 if key in network_map.devices}
        network_map.traces = [_build(Trace, item) for item in data.get("traces", [])]
        network_map.l3_positions = {key: tuple(value) for key, value in data.get("l3_positions", {}).items()}
        return network_map


def _build(cls, data):
    """A dataclass from saved fields, ignoring any it doesn't have (from a later version)."""
    names = {item.name for item in fields(cls)}
    return cls(**{key: value for key, value in data.items() if key in names})


def port_sort_key(name):
    """Gi1/0/2 before Gi1/0/10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name or "")]
