"""Checking a network's data for likely mistakes: the kind a spreadsheet collects over the years.

Each finding names the subnet or address it's about, so the IP Addresses page can go straight to it. Nothing here
changes anything; it only points things out.
"""
import collections
import contextlib
import ipaddress
import re
from dataclasses import dataclass

from .store import RESERVED

ERROR, WARNING, INFO = "error", "warning", "info"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}
SEVERITY_LABELS = {ERROR: "Error", WARNING: "Warning", INFO: "Note"}
LEADING_NUMBER = re.compile(r"^\s*(\d{3,})\b")  # "68900 MAIN TCN IN-CT" -> 68900
ODD_NAME = re.compile(r"[a-z][a-z.]*|.*\.")  # All lowercase (like a user name) or ending in a full stop: "rojason."


@dataclass
class Finding:
    severity: str  # ERROR, WARNING or INFO
    check: str  # What kind of problem, such as "Name and Telephony Rng differ"
    message: str
    subnet: object = None  # The Subnet it's about, or the one holding the address
    ip: str = ""  # The address it's about, or ""

    @property
    def where(self):
        if self.ip:
            return self.ip
        return f"{self.subnet.cidr} {self.subnet.name}".strip() if self.subnet is not None else ""


def _telephony(subnet):
    """The subnet's telephony range detail (Telephony Rng, or any detail named like it), or ("", "")."""
    for name, value in subnet.fields.items():
        if "telephony" in name.casefold() and value.strip():
            return name, value.strip()
    return "", ""


def _holding(subnets, address):
    """The most specific subnet holding an address, or None."""
    holding = [subnet for subnet in subnets if address in subnet.network]
    return max(holding, key=lambda subnet: (subnet.network.prefixlen, subnet.network.first), default=None)


def check_network(store, network_id):
    """Every finding for a network, most serious first."""
    subnets = store.subnets(network_id)
    addresses = store.addresses(network_id)
    findings = []
    findings += _check_subnet_names(subnets)
    findings += _check_loopbacks(subnets)
    findings += _check_gateways(subnets)
    findings += _check_addresses(subnets, addresses)
    findings.sort(key=lambda finding: (SEVERITY_ORDER[finding.severity], finding.check,
                                       finding.subnet.network.sort_key if finding.subnet is not None else (9,),
                                       finding.ip))
    return findings


def _check_subnet_names(subnets):
    findings = []
    for subnet in subnets:
        number = LEADING_NUMBER.match(subnet.name)
        detail, telephony = _telephony(subnet)
        if number and telephony.isdigit() and number.group(1) != telephony:
            findings.append(Finding(WARNING, "Name and Telephony Rng differ",
                                    f"The name starts with {number.group(1)} but {detail} is {telephony}.", subnet))
        if not subnet.name.strip():
            findings.append(Finding(INFO, "Subnet has no name", "This subnet has no name.", subnet))
        elif ODD_NAME.fullmatch(subnet.name.strip()):
            findings.append(Finding(WARNING, "Name looks out of place",
                                    f"{subnet.name!r} doesn't look like a subnet name (all lower case, or ending in a "
                                    "full stop): perhaps a user name or note typed over the real one.", subnet))
    return findings


def _check_loopbacks(subnets):
    findings = []
    for subnet in subnets:
        named = "loopback" in subnet.name.casefold()
        if named and not subnet.loopbacks:
            findings.append(Finding(WARNING, "Named loopback but not a loopback subnet",
                                    "The name says Loopback, but the subnet isn't marked Loopbacks, so its first and "
                                    "last addresses count as network and broadcast. Tick Loopbacks in Edit... if "
                                    "every address is a /32 of its own (or check the mask, if the name is wrong).",
                                    subnet))
    return findings


def _check_gateways(subnets):
    findings = []
    for subnet in subnets:
        if not subnet.gateway or subnet.loopbacks:
            continue
        network = subnet.network
        with contextlib.suppress(ValueError):
            gateway = ipaddress.ip_address(subnet.gateway)
            if network.num_addresses <= 4:
                continue  # Point-to-point links: either end is normal
            first_usable, last_usable = int(network.first) + 1, int(network.last) - 1
            if int(gateway) not in (first_usable, last_usable):
                findings.append(Finding(INFO, "Gateway in an unusual place",
                                        f"The gateway {gateway} is neither the first nor the last usable address "
                                        f"({ipaddress.ip_address(first_usable)} or "
                                        f"{ipaddress.ip_address(last_usable)}).", subnet))
    return findings


def _check_addresses(subnets, addresses):
    findings = []
    macs = collections.defaultdict(list)
    for address in addresses:
        subnet = _holding(subnets, address.address)
        if subnet is None:
            findings.append(Finding(WARNING, "Address outside every subnet",
                                    f"{address.ip} ({address.name or 'no name'}) isn't in any subnet of this network.",
                                    None, address.ip))
        else:
            special = subnet.special_addresses().get(address.address)
            if special in ("Network", "Broadcast"):
                findings.append(Finding(ERROR, "Address on the network or broadcast address",
                                        f"{address.ip} ({address.name or 'no name'}) is recorded on {subnet.cidr}'s "
                                        f"{special.lower()} address, which no device can use. Is the mask right, or "
                                        "should it be a loopback subnet?", subnet, address.ip))
        if address.status == RESERVED and not address.name.strip() and not address.description.strip():
            findings.append(Finding(INFO, "Reserved without a name",
                                    f"{address.ip} is reserved, but not for anything named.", subnet, address.ip))
        if address.mac.strip():
            macs[address.mac.strip().replace(":", "-").casefold()].append((address, subnet))
    for entries in macs.values():
        if len(entries) > 1:
            listing = ", ".join(address.ip for address, _ in entries)
            for address, subnet in entries:
                findings.append(Finding(WARNING, "Same MAC on more than one address",
                                        f"{address.mac} is recorded on {listing}.", subnet, address.ip))
    return findings
