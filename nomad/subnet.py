"""Subnet calculator: network, broadcast, host range and counts for an IPv4 or IPv6 address and prefix."""
import ipaddress
import re

MAX_SPLIT_ROWS = 4096
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def parse_subnet(text):
    """Accept 192.168.1.10/24, 192.168.1.10 255.255.255.0, 192.168.1.10/255.255.255.0 or an IPv6 prefix.

    A bare IPv4 address is treated as /24 and a bare IPv6 address as /64, the usual sizes.
    Returns (address, network). Raises ValueError with a message suitable for showing to the user.
    """
    text = text.strip()
    if not text:
        raise ValueError("Enter an address and prefix, such as 192.168.1.10/24 or 192.168.1.10 255.255.255.0.")
    parts = [part for part in re.split(r"[\s/]+", text) if part]
    if len(parts) > 2:
        raise ValueError(f"'{text}' has too many parts. Use an address and a prefix length or netmask.")
    try:
        address = ipaddress.ip_address(parts[0].partition("%")[0])
    except ValueError:
        raise ValueError(f"'{parts[0]}' is not an IPv4 or IPv6 address.") from None
    if len(parts) == 1:
        prefix = "24" if address.version == 4 else "64"
    else:
        prefix = parts[1]
    try:
        interface = ipaddress.ip_interface(f"{address}/{prefix}")
    except ValueError:
        kind = "0-32 or a netmask like 255.255.255.0" if address.version == 4 else "0-128"
        raise ValueError(f"'{prefix}' isn't a valid prefix length or netmask (use {kind}).") from None
    return address, interface.network


def address_kind(address):
    if address.version == 4 and address in CGNAT:
        return "Shared (carrier-grade NAT, 100.64.0.0/10)"
    for test, label in ((address.is_loopback, "Loopback"), (address.is_link_local, "Link-local"),
                        (address.is_multicast, "Multicast"), (address.is_unspecified, "Unspecified"),
                        (address.is_reserved, "Reserved"), (address.is_private, "Private"),
                        (address.is_global, "Public")):
        if test:
            return label
    return "Special use"


def host_range(network):
    """(first, last, count) of usable host addresses. /31 and /32 (and IPv6 /127, /128) use every address."""
    if network.version == 4 and network.prefixlen <= 30:
        return network.network_address + 1, network.broadcast_address - 1, network.num_addresses - 2
    if network.version == 6 and network.prefixlen <= 126:
        # The first address is the subnet-router anycast address
        return network.network_address + 1, network.broadcast_address, network.num_addresses - 1
    return network.network_address, network.broadcast_address, network.num_addresses


def describe(address, network):
    """Rows of (label, value) describing the subnet."""
    first, last, count = host_range(network)
    rows = [("Address", str(address)), ("Network", f"{network.network_address}/{network.prefixlen}")]
    if network.version == 4:
        rows += [("Netmask", str(network.netmask)), ("Wildcard mask", str(network.hostmask)),
                 ("Broadcast", str(network.broadcast_address) if network.prefixlen <= 30 else "None (point-to-point)")]
    else:
        rows += [("Last address", str(network.broadcast_address))]
    rows += [("First host", str(first)), ("Last host", str(last)),
             ("Usable hosts", f"{count:,}"), ("Total addresses", f"{network.num_addresses:,}"),
             ("Address type", address_kind(address))]
    if network.version == 4:
        rows += [("Netmask in binary", ".".join(f"{byte:08b}" for byte in network.netmask.packed)),
                 ("Reverse DNS zone", reverse_zone(network))]
        if address == network.network_address and network.prefixlen <= 30:
            rows.append(("Note", "This is the network address itself, which hosts can't use."))
        elif address == network.broadcast_address and network.prefixlen <= 30:
            rows.append(("Note", "This is the broadcast address, which hosts can't use."))
    return rows


def reverse_zone(network):
    """The in-addr.arpa zone covering the network (rounded out to whole octets)."""
    octets = str(network.network_address).split(".")[:max(1, network.prefixlen // 8)]
    return ".".join(reversed(octets)) + ".in-addr.arpa"


def split(network, new_prefix, limit=MAX_SPLIT_ROWS):
    """Divide network into subnets of new_prefix. Returns (first `limit` subnets, total count).

    Raises ValueError if new_prefix isn't longer than the network's prefix.
    """
    if not network.prefixlen < new_prefix <= network.max_prefixlen:
        raise ValueError(f"Choose a prefix between /{network.prefixlen + 1} and /{network.max_prefixlen} "
                         f"to split /{network.prefixlen} into smaller subnets.")
    total = 2 ** (new_prefix - network.prefixlen)
    subnets = []
    for subnet in network.subnets(new_prefix=new_prefix):
        if len(subnets) >= limit:
            break
        subnets.append(subnet)
    return subnets, total


def prefix_for_hosts(hosts, version=4):
    """The longest IPv4 prefix whose subnets hold at least `hosts` usable addresses."""
    if hosts < 1:
        raise ValueError("Enter how many hosts each subnet needs.")
    for prefix in range(32 if version == 4 else 128, -1, -1):
        network = ipaddress.ip_network(("0.0.0.0" if version == 4 else "::", prefix))
        if host_range(network)[2] >= hosts:
            return prefix
    raise ValueError("That's more hosts than fit in any subnet.")
