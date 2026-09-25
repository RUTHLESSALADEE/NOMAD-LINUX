"""Wake-on-LAN: send the "magic packet" that wakes a computer whose network card is set to listen for it."""
import ipaddress
import json
import socket
from dataclasses import dataclass

from .oui import format_mac, normalize_mac

WOL_PORTS = (9, 7)  # Discard and echo; network cards accept the packet on any port, these are customary
LIMITED_BROADCAST = "255.255.255.255"


@dataclass
class WakeTarget:
    name: str
    mac: str  # AA-BB-CC-DD-EE-FF
    broadcast: str = ""  # A subnet's broadcast address, to wake a computer on a routed subnet


def magic_packet(mac):
    """Six 0xFF bytes followed by the MAC address sixteen times."""
    digits = normalize_mac(mac)
    if not digits:
        raise ValueError(f"'{mac}' is not a MAC address. Use a form like 00-11-22-33-44-55.")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


def validate_broadcast(text):
    """Returns the broadcast address to use (or "" for none). Raises ValueError if it isn't an IPv4 address."""
    text = text.strip()
    if not text:
        return ""
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        raise ValueError(f"'{text}' is not an IPv4 address. Use a subnet's broadcast address, "
                         "such as 192.168.20.255.") from None


def destinations(adapter_addresses, broadcast="", port=WOL_PORTS[0]):
    """Where to send the packet: every local subnet's broadcast address, plus an optional routed one.

    adapter_addresses are ipaddress.IPv4Interface values for the adapters that are up. Returns
    [(source address or None, destination address, port)].
    """
    targets = []
    for interface in adapter_addresses:
        if interface.network.prefixlen >= 31:
            continue
        source = str(interface.ip)
        targets.append((source, LIMITED_BROADCAST, port))
        targets.append((source, str(interface.network.broadcast_address), port))
    if broadcast:
        targets.append((None, broadcast, port))
    if not targets:
        targets.append((None, LIMITED_BROADCAST, port))
    return list(dict.fromkeys(targets))  # Drop duplicates, keeping the order


def send_magic_packet(mac, targets):
    """Send the packet to each (source, destination, port). Returns how many were sent; raises OSError if none."""
    packet = magic_packet(mac)
    sent, last_error = 0, None
    for source, destination, port in targets:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                if source:
                    sock.bind((source, 0))  # Picks the adapter the broadcast leaves from
                sock.sendto(packet, (destination, port))
                sent += 1
            except OSError as error:
                last_error = error
    if not sent:
        raise last_error or OSError("No network to send the packet on.")
    return sent


def targets_to_json(targets):
    return json.dumps([{"name": target.name, "mac": target.mac, "broadcast": target.broadcast}
                       for target in targets])


def targets_from_json(text):
    """Saved devices; anything unreadable is skipped rather than stopping the app."""
    try:
        items = json.loads(text or "[]")
    except ValueError:
        return []
    targets = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and format_mac(item.get("mac", "")):
            targets.append(WakeTarget(str(item.get("name", "")), format_mac(item["mac"]),
                                      str(item.get("broadcast", ""))))
    return targets
