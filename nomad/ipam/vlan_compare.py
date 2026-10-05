"""Comparing a VLAN domain with the VLANs a network map found on its switches, to bring them in.

compare_with_map lists the differences as VlanChanges; the user ticks the ones to make and apply_vlan_changes makes
them (for the tribe's VLANs, through the IPAM server, with its conflict checks). What the database says on purpose
is protected: a VLAN deleted from it isn't offered back ticked, and where it names a VLAN differently from the
switches, its name may be the one intended (the switches may be what needs changing), so that isn't ticked either.

A VLAN interface (an SVI, or a router's subinterface) links the IPAM subnet holding its address (the most specific
one, in the domain's network), whatever mask either is written with. Subnets are only ever linked when they're
already in IPAM: an interface no IPAM subnet holds is pointed out, never added to IPAM.
"""
from dataclasses import dataclass, field

from .store import IpamError
from .vlans import ACTIVE, holding_subnet

ADD, RENAME, LINK, MISSING = "add", "rename", "link", "missing"
ACTION_LABELS = {ADD: "Add", RENAME: "Rename", LINK: "Link subnets", MISSING: "Not on the map"}


@dataclass
class VlanChange:
    action: str  # ADD, RENAME, LINK, or MISSING (only pointed out)
    vlan: int
    name: str = ""  # What the switches call it
    recorded: object = None  # The Vlan as the database has it, or None
    subnets: list = field(default_factory=list)  # CIDRs to link (ADD and LINK)
    detail: str = ""  # Where it was seen
    chosen: bool = True
    note: str = ""  # Why it isn't ticked, or what else to know

    @property
    def can_apply(self):
        """Whether ticking it does anything (some are only pointed out)."""
        return self.action != MISSING and (self.action != LINK or bool(self.subnets))

    def describe(self):
        if self.action == ADD:
            parts = [self.name or "(no name)"] + ([f"carrying {', '.join(self.subnets)}"] if self.subnets else [])
            return ", ".join(parts)
        if self.action == RENAME:
            return f"Name: {self.recorded.name or '(none)'} → {self.name or '(none)'}"
        if self.action == LINK:
            if not self.subnets:
                return "Its VLAN interface's subnet can't be linked"
            return f"Carries {', '.join(self.subnets)} (from its VLAN interface)"
        return f"{self.recorded.name or '(no name)'}: in the database, not on any switch the map read"


def compare_with_map(vlan_store, ipam_store, domain, found):
    """How a domain differs from a map's VLANs (found: [netmap.vlans.MapVlan], those of the VTP domain chosen).
    Returns [VlanChange] by VLAN number."""
    recorded = {vlan.vlan: vlan for vlan in vlan_store.vlans(domain.id)} if domain.id else {}
    deleted = vlan_store.deleted_numbers(domain.id) if domain.id else set()
    network_subnets = []
    if domain.network_id:
        try:
            network_subnets = ipam_store.subnets(domain.network_id)
        except IpamError:
            pass
    linked = {cidr: vlan.vlan for vlan in recorded.values() for cidr in vlan.subnets}
    changes = []
    seen = set()
    for item in sorted(found, key=lambda item: item.vlan):
        seen.add(item.vlan)
        subnets, notes = [], []
        for gateway in item.gateways:
            interface = f"{gateway.address}/{gateway.prefix}"
            if not domain.network_id:
                notes.append(f"its VLAN interface {interface} can't be linked: the domain has no IPAM network")
                continue
            subnet = holding_subnet(network_subnets, gateway.address)
            if subnet is None:
                notes.append(f"no IPAM subnet holds its VLAN interface {interface}")
            elif linked.get(subnet.cidr) == item.vlan or subnet.cidr in subnets:
                continue
            elif subnet.cidr in linked:
                notes.append(f"{subnet.cidr} (holding {interface}) is VLAN {linked[subnet.cidr]}'s in the database")
            else:
                subnets.append(subnet.cidr)
        notes = list(dict.fromkeys(notes))
        where = f"on {len(item.switches)} switch{'es' if len(item.switches) != 1 else ''}" if item.switches else \
            "only seen in ports or VLAN interfaces"
        if len(item.names) > 1:
            where += f"; the switches name it {', '.join(repr(name) for name in item.names)}"
        current = recorded.get(item.vlan)
        if current is None:
            again = item.vlan in deleted
            note = "Deleted from the database: tick it to add it again." if again else ""
            changes.append(VlanChange(ADD, item.vlan, item.name, None, subnets, where, not again,
                                      "; ".join(([note] if note else []) + notes)))
            continue
        if item.name and item.name != current.name:
            chosen = not current.name
            note = "" if chosen else "The database's name may be the one intended (rename the VLAN on the switches " \
                                     "instead); tick it to take the switches' name."
            changes.append(VlanChange(RENAME, item.vlan, item.name, current, [], where, chosen, note))
        if subnets:
            changes.append(VlanChange(LINK, item.vlan, item.name, current, subnets, where, True, "; ".join(notes)))
        elif notes:
            changes.append(VlanChange(LINK, item.vlan, item.name, current, [], where, False, "; ".join(notes)))
    for number, vlan in sorted(recorded.items()):
        if number not in seen and vlan.status == ACTIVE:
            changes.append(VlanChange(MISSING, number, vlan.name, vlan, [], "", False,
                                      "Only pointed out: perhaps it's planned, or on switches the map didn't read."))
    return changes


def apply_vlan_changes(vlans, domain_id, changes):
    """Make the ticked changes (vlans: a VlanStore or TeamVlanStore), in one go. Returns how many VLANs changed."""
    items = {}
    for change in changes:
        if not change.chosen or not change.can_apply:
            continue
        current = vlans.vlan(domain_id, change.vlan)
        item = items.get(change.vlan)
        if item is None:
            item = {"vlan": change.vlan, "name": current.name if current else "",
                    "status": current.status if current else ACTIVE,
                    "subnets": list(current.subnets) if current else [],
                    "description": current.description if current else "",
                    "fields": dict(current.fields) if current else {}}
            items[change.vlan] = item
        if change.action in (ADD, RENAME):
            item["name"] = change.name
        item["subnets"] = list(dict.fromkeys(item["subnets"] + change.subnets))
    if items:
        vlans.set_vlans(domain_id, [items[number] for number in sorted(items)])
    return len(items)
