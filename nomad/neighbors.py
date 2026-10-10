"""The ARP / IPv6 neighbor table: which MAC address answers for each IP on the local networks.

Also asks for a single host's MAC with an ARP request (SendARP), which finds hosts that don't answer ping,
and spots signs of duplicate IP addresses and ARP spoofing.
"""
import ctypes
import ipaddress
import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field

from .oui import format_mac, is_multicast_mac, normalize_mac, vendor
from .snapshot import CIM_AF_INET, CIM_AF_INET6, CIM_FUNCTIONS, _as_list
from .system import ps_quote, run_command, run_powershell, run_powershell_json

log = logging.getLogger(__name__)

# MSFT_NetNeighbor State values
NEIGHBOR_STATES = {0: "Unreachable", 1: "Incomplete", 2: "Probe", 3: "Delay", 4: "Stale", 5: "Reachable",
                   6: "Permanent"}
PERMANENT = "Permanent"
UNRESOLVED_STATES = {"Unreachable", "Incomplete"}  # No MAC address yet (or any more)
ADDRESS_STATE_DUPLICATE = 2  # MSFT_NetIPAddress AddressState: Windows saw another host using this address

NEIGHBORS_SCRIPT = CIM_FUNCTIONS + r"""
$result = [ordered]@{
    neighbors = @(Get-Instances 'MSFT_NetNeighbor' @('InterfaceIndex', 'InterfaceAlias', 'IPAddress',
        'LinkLayerAddress', 'State', 'AddressFamily'))
    addresses = @(Get-Instances 'MSFT_NetIPAddress' @('InterfaceIndex', 'InterfaceAlias', 'IPAddress',
        'AddressState'))
}
$result | ConvertTo-Json -Depth 4 -Compress
"""


@dataclass
class Neighbor:
    interface: str  # Interface index
    interface_name: str
    address: str
    mac: str  # AA-BB-CC-DD-EE-FF, or "" while unresolved
    state: str
    family: int  # 4 or 6

    @property
    def vendor(self):
        return vendor(self.mac)

    @property
    def sort_key(self):
        address = ipaddress.ip_address(self.address.partition("%")[0])
        return self.family, int(address)

    @property
    def is_multicast(self):
        """Multicast and broadcast entries, which Windows fills in by itself."""
        address = ipaddress.ip_address(self.address.partition("%")[0])
        return (address.is_multicast or self.address == "255.255.255.255" or is_multicast_mac(self.mac)
                or (self.family == 4 and self.mac == "FF-FF-FF-FF-FF-FF"))


@dataclass
class NeighborTable:
    neighbors: list = field(default_factory=list)
    duplicate_addresses: list = field(default_factory=list)  # (interface name, address) Windows flagged


def parse_neighbors(data):
    """Build a NeighborTable from the JSON written by NEIGHBORS_SCRIPT."""
    data = data or {}
    families = {CIM_AF_INET: 4, CIM_AF_INET6: 6}
    neighbors = []
    for row in _as_list(data.get("neighbors")):
        family = families.get(row.get("AddressFamily"))
        address = row.get("IPAddress") or ""
        try:
            ipaddress.ip_address(address.partition("%")[0])
        except ValueError:
            continue
        mac = format_mac(row.get("LinkLayerAddress"))
        if mac == "00-00-00-00-00-00":
            mac = ""
        neighbors.append(Neighbor(str(row.get("InterfaceIndex")), row.get("InterfaceAlias") or "", address, mac,
                                  NEIGHBOR_STATES.get(row.get("State"), "Unknown"), family or 4))
    duplicates = [(row.get("InterfaceAlias") or "", row.get("IPAddress") or "")
                  for row in _as_list(data.get("addresses")) if row.get("AddressState") == ADDRESS_STATE_DUPLICATE]
    return NeighborTable(neighbors, duplicates)


def load_neighbors():
    """Read the neighbor table. Slow (about a second); call it off the UI thread."""
    if os.name == "nt":
        return parse_neighbors(run_powershell_json(NEIGHBORS_SCRIPT))
    return load_linux_neighbors()


def load_linux_neighbors():
    """Build a NeighborTable natively from ip -j neigh."""
    import subprocess
    import json
    try:
        raw = subprocess.run(["ip", "-j", "neigh"], capture_output=True, text=True, check=True).stdout
        data = json.loads(raw)
    except Exception as e:
        log.warning("Failed to run ip -j neigh: %s", e)
        data = []

    from nomad.neighbors import Neighbor, NeighborTable
    neighbors = []
    for item in data:
        dst = item.get("dst")
        lladdr = item.get("lladdr")
        dev = item.get("dev", "")
        state_list = item.get("state", [])
        state = state_list[0].lower() if state_list else "unknown"

        if not dst or not lladdr:
            continue

        family = 6 if ":" in dst else 4
        mac = lladdr.replace(":", "-").upper()
        neighbors.append(Neighbor(
            interface=dev,
            interface_name=dev,
            address=dst,
            mac=mac,
            state=state,
            family=family,
        ))

    return NeighborTable(neighbors, [])


def shared_macs(neighbors, networks=None):
    """MAC addresses that answer for more than one IPv4 address on the same interface.

    That's normal for a router or server with several addresses, but if one of them is the gateway it
    can mean another device is pretending to be the gateway (ARP spoofing). With networks (the local
    subnets), only addresses inside them count: entries for other addresses come from proxy ARP or
    on-link routes, where the gateway answering for them is expected. Returns {(interface, mac): [addresses]}.
    """
    by_mac = {}
    for neighbor in neighbors:
        if networks is not None and neighbor.family == 4 and not any(
                ipaddress.ip_address(neighbor.address) in network for network in networks if network.version == 4):
            continue
        if neighbor.family == 4 and neighbor.mac and not neighbor.is_multicast \
                and neighbor.state not in UNRESOLVED_STATES:
            by_mac.setdefault((neighbor.interface, neighbor.mac), []).append(neighbor.address)
    return {key: sorted(addresses, key=lambda text: int(ipaddress.ip_address(text)))
            for key, addresses in by_mac.items() if len(addresses) > 1}


class MacHistory:
    """Remembers the MAC seen for each address, to notice when a different device starts answering for it.

    A MAC that changes back and forth usually means two devices share the IP address (or one is spoofing it).
    """

    def __init__(self):
        self.seen = {}  # (interface, address) -> MAC
        self.changes = {}  # (interface, address) -> list of earlier MACs, oldest first

    def update(self, neighbors):
        """Record the latest table. Returns the (neighbor, previous MAC) pairs that changed this time."""
        changed = []
        for neighbor in neighbors:
            if not neighbor.mac or neighbor.is_multicast:
                continue
            key = (neighbor.interface, neighbor.address)
            previous = self.seen.get(key)
            if previous and previous != neighbor.mac:
                earlier = self.changes.setdefault(key, [])
                if previous not in earlier:
                    earlier.append(previous)
                changed.append((neighbor, previous))
            self.seen[key] = neighbor.mac
        return changed

    def earlier_macs(self, neighbor):
        return [mac for mac in self.changes.get((neighbor.interface, neighbor.address), []) if mac != neighbor.mac]


def build_delete_neighbor_script(neighbor):
    return (f"Remove-NetNeighbor -InterfaceIndex {int(neighbor.interface)} "
            f"-IPAddress {ps_quote(neighbor.address.partition('%')[0])} -Confirm:$false")


def delete_neighbor(neighbor):
    run_powershell(build_delete_neighbor_script(neighbor))


def clear_neighbor_cache():
    """Empty the ARP and IPv6 neighbor caches (the same as arp -d *)."""
    if os.name != "nt":
        try:
            subprocess.run(["ip", "neigh", "flush", "all"], capture_output=True)
        except Exception as e:
            log.warning("Could not flush neighbor cache: %s", e)
        return
    run_command(["netsh", "interface", "ip", "delete", "arpcache"])
    run_command(["netsh", "interface", "ipv6", "delete", "neighbors"])


# ----------------------------------------------------------------- SendARP

_send_arp = None


def _send_arp_function():
    global _send_arp
    if _send_arp is None:
        if not hasattr(ctypes, "WinDLL"):
            return None
        function = ctypes.WinDLL("iphlpapi", use_last_error=True).SendARP
        function.restype = ctypes.c_uint32
        function.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        _send_arp = function
    return _send_arp


def arp_lookup(address, source=None):
    """Ask for an IPv4 host's MAC address with ARP (answered from the cache if it's there).

    Only works for hosts on a directly connected subnet. Returns the MAC as AA-BB-CC-DD-EE-FF, or None
    if nothing answered.
    """
    if os.name != "nt":
        # Check kernel ARP table from /proc/net/arp or ip neigh
        try:
            target = str(address).strip()
            with open("/proc/net/arp", "r") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == target:
                        mac = parts[3].replace(":", "-").upper()
                        if normalize_mac(mac) not in ("", "000000000000", "00-00-00-00-00-00"):
                            return mac
        except Exception:
            pass
        return None

    func = _send_arp_function()
    if func is None:
        return None
    destination = int.from_bytes(socket.inet_aton(str(address)), "little")
    source_value = int.from_bytes(socket.inet_aton(str(source)), "little") if source else 0
    buffer = (ctypes.c_ubyte * 8)()
    length = ctypes.c_ulong(6)
    if func(destination, source_value, buffer, ctypes.byref(length)) != 0 or length.value != 6:
        return None
    mac = format_mac(bytes(buffer[:6]).hex())
    return mac if normalize_mac(mac) not in ("", "000000000000") else None
