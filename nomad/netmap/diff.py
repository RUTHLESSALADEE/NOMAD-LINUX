"""Comparing two maps of the same network: devices and links that appeared, went away or changed, and hosts that
moved to another port."""
from dataclasses import dataclass

from .model import KIND_NAMES, SOURCE_NAMES

ADDED, REMOVED, CHANGED, MOVED = "Added", "Gone", "Changed", "Moved"
DEVICE, LINK, HOST = "Device", "Link", "Host"


@dataclass
class Change:
    change: str  # ADDED, REMOVED, CHANGED or MOVED
    what: str  # DEVICE, LINK or HOST
    name: str
    detail: str = ""
    device: str = ""  # Device key to show on the newer map ("" when it's only on the older one)
    mac: str = ""  # For hosts


def link_text(network_map, link):
    devices = network_map.devices
    a = devices[link.a].label if link.a in devices else link.a
    b = devices[link.b].label if link.b in devices else link.b
    return f"{a} {link.a_port} - {b} {link.b_port}"


def compare(old, new):
    """What changed from old to new, as [Change]: devices, then links, then hosts."""
    changes = []
    for key in sorted(new.devices.keys() - old.devices.keys()):
        device = new.devices[key]
        changes.append(Change(ADDED, DEVICE, device.label, " ".join(part for part in (
            KIND_NAMES.get(device.kind, device.kind), device.mgmt_ip, device.platform) if part), key))
    for key in sorted(old.devices.keys() - new.devices.keys()):
        device = old.devices[key]
        changes.append(Change(REMOVED, DEVICE, device.label, " ".join(part for part in (
            KIND_NAMES.get(device.kind, device.kind), device.mgmt_ip) if part)))
    for key in sorted(old.devices.keys() & new.devices.keys()):
        before, after = old.devices[key], new.devices[key]
        details = []
        if before.source != after.source:
            details.append(f"{SOURCE_NAMES.get(before.source, before.source)} -> "
                           f"{SOURCE_NAMES.get(after.source, after.source)}")
        for label, attribute in (("address", "mgmt_ip"), ("model", "platform")):
            old_value, new_value = getattr(before, attribute), getattr(after, attribute)
            if old_value and new_value and old_value != new_value:
                details.append(f"{label} {old_value} -> {new_value}")
        if details:
            changes.append(Change(CHANGED, DEVICE, after.label, "; ".join(details), key))

    old_links = {link.key: link for link in old.links}
    new_links = {link.key: link for link in new.links}
    for key in new_links.keys() - old_links.keys():
        link = new_links[key]
        changes.append(Change(ADDED, LINK, link_text(new, link), device=link.a))
    for key in old_links.keys() - new_links.keys():
        link = old_links[key]
        changes.append(Change(REMOVED, LINK, link_text(old, link)))

    def place(network_map, host):
        device = network_map.devices.get(host.device)
        return f"{device.label if device else host.device} {host.port}"

    old_hosts = {host.mac: host for host in old.hosts if host.mac}
    new_hosts = {host.mac: host for host in new.hosts if host.mac}
    host_changes = []
    for mac in sorted(old_hosts.keys() & new_hosts.keys()):
        before, after = old_hosts[mac], new_hosts[mac]
        name = after.name or after.ip or mac
        if place(old, before) != place(new, after):
            host_changes.append(Change(MOVED, HOST, name, f"{place(old, before)} -> {place(new, after)}",
                                       after.device, mac))
        elif before.ip and after.ip and before.ip != after.ip:
            host_changes.append(Change(CHANGED, HOST, name, f"IP {before.ip} -> {after.ip}", after.device, mac))
    for mac in sorted(new_hosts.keys() - old_hosts.keys()):
        host = new_hosts[mac]
        host_changes.append(Change(ADDED, HOST, host.name or host.ip or mac, f"{place(new, host)} {host.vendor}"
                                   .strip(), host.device, mac))
    for mac in sorted(old_hosts.keys() - new_hosts.keys()):
        host = old_hosts[mac]
        host_changes.append(Change(REMOVED, HOST, host.name or host.ip or mac, f"{place(old, host)} {host.vendor}"
                                   .strip(), mac=mac))
    order = {MOVED: 0, CHANGED: 1, ADDED: 2, REMOVED: 3}
    changes.sort(key=lambda change: (change.what != DEVICE, order.get(change.change, 9), change.name.lower()))
    host_changes.sort(key=lambda change: (order[change.change], change.name.lower()))
    return changes + host_changes
