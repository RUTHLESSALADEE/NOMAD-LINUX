"""Comparing what's on the network (a sweep, or the ARP table) with what IPAM has recorded.

Each device found is either recorded (with its name), not recorded, recorded with a different MAC address, or
answering on an address IPAM holds as reserved; and each address IPAM records as used that didn't answer is listed
as silent (it may be off, gone, or firewalled: worth checking, not proof it's free).

Several IPAM networks can hold the same range (separate, air-gapped networks), so the caller picks which network
to compare with; candidate_networks suggests them, best match first.
"""
import ipaddress
from dataclasses import dataclass

from ..oui import normalize_mac
from .store import RESERVED

RECORDED, NOT_RECORDED, MAC_DIFFERS, RESERVED_IN_USE = "recorded", "not recorded", "mac differs", "reserved in use"


@dataclass
class Finding:
    ip: str
    state: str
    text: str  # For the IPAM column
    record: object = None  # The Address, when IPAM has one


def candidate_networks(stores, addresses):
    """Networks whose subnets hold any of `addresses`: [(source, network, how many they hold)], most first."""
    addresses = [ipaddress.ip_address(address) for address in addresses]
    candidates = []
    for source, store in stores:
        for network in store.networks():
            blocks = [subnet.network for subnet in store.subnets(network.id)]
            held = sum(1 for address in addresses if any(address in block for block in blocks))
            if held:
                candidates.append((source, network, held))
    return sorted(candidates, key=lambda candidate: (-candidate[2], candidate[1].name.lower()))


def compare(found, store, network_id):
    """found: {ip: (mac, name)} from the network. Returns {ip: Finding} for the addresses in the IPAM network's
    subnets (or recorded in it); others (another network's, link-local...) aren't judged against it."""
    blocks = [subnet.network for subnet in store.subnets(network_id)]
    findings = {}
    for ip, (mac, _name) in found.items():
        record = store.address(network_id, ip)
        if record is None and not any(ipaddress.ip_address(ip) in block for block in blocks):
            continue
        if record is None:
            findings[ip] = Finding(ip, NOT_RECORDED, "Not in IPAM")
            continue
        label = record.name or "no name"
        if mac and record.mac and normalize_mac(mac) != normalize_mac(record.mac):
            findings[ip] = Finding(ip, MAC_DIFFERS, f"MAC differs (IPAM has {record.mac} for {label})", record)
        elif record.status == RESERVED and not record.name:
            findings[ip] = Finding(ip, RESERVED_IN_USE, "Reserved in IPAM, but a device answers", record)
        else:
            findings[ip] = Finding(ip, RECORDED, f"In IPAM: {label}", record)
    return findings


def silent(store, network_id, answered, within):
    """Addresses IPAM records as used in the `within` range(s) (Blocks or networks) that didn't answer."""
    answered = {ipaddress.ip_address(address) for address in answered}
    quiet = []
    seen = set()
    for block in within:
        for record in store.addresses(network_id, block):
            if record.status != RESERVED and record.address not in answered and record.ip not in seen:
                seen.add(record.ip)
                quiet.append(record)
    return sorted(quiet, key=lambda record: (record.address.version, record.address))
