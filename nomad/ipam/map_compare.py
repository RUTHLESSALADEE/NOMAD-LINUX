"""What a network map has that IPAM should know: the addresses of its devices (each interface's, and the address
it's managed by) and of the hosts the switches see, compared with an IPAM network, so they can be recorded there.

Each address gets a suggested IPAM name: a device's management address its name (R1S1), an interface's the device
and the port (R1S1 Vl100), a host's its name. Addresses outside the network's subnets aren't judged against it (as
with sweeps: they may be another network's).
"""
import ipaddress
from dataclasses import dataclass

from .reconcile import compare

HOST, DEVICE = "host", "device"


@dataclass
class MapAddress:
    ip: str
    kind: str  # HOST or DEVICE
    name: str  # What to call it in IPAM
    mac: str = ""
    where: str = ""  # "R1S1 Vl100" (a device's interface) or "sw1 Gi1/0/5" (where a host is plugged in)
    device: str = ""  # The map's device key: the device, or the switch a host is on
    finding: object = None  # reconcile.Finding against the network, or None when it's outside its subnets


def short_name(label):
    """A device's name without its domain (R1S1.home.lab -> R1S1); an address stays as it is."""
    try:
        ipaddress.ip_address(label)
        return label
    except ValueError:
        return label.split(".")[0] if label else label


def usable(address):
    """Whether an address is worth recording: not 0.0.0.0, a loopback (127.x), link-local or multicast one."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_multicast)


def map_addresses(network_map):
    """[MapAddress] of every device and host address on the map, each once (a device's before a host's)."""
    found = {}
    for key, device in sorted(network_map.devices.items(), key=lambda item: item[1].label.lower()):
        name = short_name(device.label)
        for address, _, port in device.interfaces_l3:
            if usable(address) and address not in found:
                own = address == device.mgmt_ip  # How it's managed: called by its name
                found[address] = MapAddress(address, DEVICE, name if own else f"{name} {port}", "",
                                            f"{name} {port}", key)
        if device.mgmt_ip and usable(device.mgmt_ip) and device.mgmt_ip not in found:
            found[device.mgmt_ip] = MapAddress(device.mgmt_ip, DEVICE, name, "", f"{name} (management)", key)
    for host in network_map.hosts:
        if host.ip and usable(host.ip) and host.ip not in found:
            switch = network_map.devices.get(host.device)
            where = f"{short_name(switch.label) if switch else host.device} {host.port}".strip()
            found[host.ip] = MapAddress(host.ip, HOST, host.name or "", host.mac or "", where, host.device)
    return sorted(found.values(), key=lambda item: (ipaddress.ip_address(item.ip).version,
                                                    ipaddress.ip_address(item.ip)))


def compare_map(network_map, store, network_id):
    """map_addresses with each judged against an IPAM network (finding None: outside its subnets)."""
    addresses = map_addresses(network_map)
    findings = compare({item.ip: (item.mac, item.name) for item in addresses}, store, network_id)
    for item in addresses:
        item.finding = findings.get(item.ip)
    return addresses
