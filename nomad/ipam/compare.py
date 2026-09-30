"""Comparing a network with a page of the addressing workbook, to bring in what changed without importing it again.

compare_network lists every difference as a Change; the user ticks the ones to make and apply_changes makes them
through the store (so, for tribe networks, through the IPAM server, with its usual conflict checks). Changes made in
NOMAD since the import are protected: where the workbook differs from something edited or deleted in NOMAD since,
the change is listed but not ticked, as NOMAD probably has the newer answer.
"""
from dataclasses import dataclass, field

from .store import STATUSES, IpamError, parse_address, parse_subnet

NETWORK, SUBNET, ADDRESS = "network", "subnet", "address"
ADD, UPDATE, REMOVE = "add", "update", "remove"
ACTION_LABELS = {ADD: "Add", UPDATE: "Change", REMOVE: "Remove"}
SKIPPED_DETAILS = {"Imported from"}  # Set by the import itself, not the workbook
SUBNET_FIELDS = (("name", "Name"), ("gateway", "Gateway"), ("description", "Description"),
                 ("loopbacks", "Loopbacks"), ("fields", "Details"))


@dataclass
class Change:
    kind: str  # NETWORK, SUBNET or ADDRESS
    action: str  # ADD, UPDATE or REMOVE
    key: str  # The detail's name, subnet CIDR or address
    ipam: object = None  # The Subnet or Address as IPAM has it (None when adding), or the detail's value
    sheet: object = None  # What the workbook has: a dict for subnets and addresses, the value for a detail
    differences: list = field(default_factory=list)  # [(label, IPAM's value, the workbook's)] for UPDATE
    chosen: bool = True  # Ticked to be made
    note: str = ""  # Why it isn't ticked, when it isn't

    def describe(self):
        if self.action == UPDATE:
            return "; ".join(f"{label}: {_show(before)} → {_show(after)}" for label, before, after in self.differences)
        if self.kind == NETWORK:
            return f"{self.key}: {_show(self.sheet if self.action == ADD else self.ipam)}"
        if self.kind == SUBNET:
            subnet = self.sheet if self.action == ADD else vars(self.ipam)
            return ", ".join(part for part in (subnet.get("name") or "(no name)",
                                               f"gateway {subnet['gateway']}" if subnet.get("gateway") else "",
                                               "loopbacks" if subnet.get("loopbacks") else "") if part)
        address = self.sheet if self.action == ADD else vars(self.ipam)
        status = STATUSES.get(address.get("status"), address.get("status", ""))
        return f"{status}: {address.get('name') or '(no name)'}"


def _show(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return ", ".join(f"{name} {text}" for name, text in value.items()) or "(none)"
    return str(value) if value not in (None, "") else "(none)"


def compare_network(store, network_id, plan):
    """Every way the network differs from a workbook page (plan from spreadsheet.import_plan), network details
    first, then subnets and addresses in address order."""
    network = store.network(network_id)
    deleted_subnets, deleted_addresses = store.deleted_since(network_id)
    changes = []

    sheet_details = {name: value for name, value in plan["fields"].items() if name not in SKIPPED_DETAILS}
    ipam_details = {name: value for name, value in network.fields.items() if name not in SKIPPED_DETAILS}
    for name in list(sheet_details) + [name for name in ipam_details if name not in sheet_details]:
        before, after = ipam_details.get(name), sheet_details.get(name)
        if before == after:
            continue
        if before is None:
            changes.append(Change(NETWORK, ADD, name, None, after))
        elif after is None:
            changes.append(Change(NETWORK, REMOVE, name, before, None, chosen=False,
                                  note="Not in the workbook: kept unless you tick it."))
        else:
            changes.append(Change(NETWORK, UPDATE, name, before, after, [(name, before, after)]))

    ipam_subnets = {subnet.cidr: subnet for subnet in store.subnets(network_id)}
    sheet_subnets = {str(parse_subnet(subnet["cidr"])): subnet for subnet in plan["subnets"]}
    for cidr in sorted(set(ipam_subnets) | set(sheet_subnets), key=lambda text: parse_subnet(text).sort_key):
        ipam, sheet = ipam_subnets.get(cidr), sheet_subnets.get(cidr)
        if ipam is None:
            deleted = cidr in deleted_subnets
            changes.append(Change(SUBNET, ADD, cidr, None, sheet, chosen=not deleted,
                                  note="Deleted in NOMAD since it was imported." if deleted else ""))
        elif sheet is None:
            changes.append(Change(SUBNET, REMOVE, cidr, ipam, None, chosen=False,
                                  note="Not in the workbook: kept unless you tick it."))
        else:
            differences = [(label, getattr(ipam, name), _subnet_value(sheet, name)) for name, label in SUBNET_FIELDS
                           if getattr(ipam, name) != _subnet_value(sheet, name)]
            if differences:
                edited = ipam.version > 1
                changes.append(Change(SUBNET, UPDATE, cidr, ipam, sheet, differences, chosen=not edited,
                                      note=f"Changed in NOMAD since it was imported (by {ipam.modified_by})."
                                      if edited else ""))

    ipam_addresses = {address.ip: address for address in store.addresses(network_id)}
    sheet_addresses = {str(parse_address(address["ip"])): address for address in plan["addresses"]}
    for ip in sorted(set(ipam_addresses) | set(sheet_addresses), key=lambda text: _sort_key(text)):
        ipam, sheet = ipam_addresses.get(ip), sheet_addresses.get(ip)
        if ipam is None:
            deleted = ip in deleted_addresses
            changes.append(Change(ADDRESS, ADD, ip, None, sheet, chosen=not deleted,
                                  note="Marked free in NOMAD since it was imported." if deleted else ""))
        elif sheet is None:
            changes.append(Change(ADDRESS, REMOVE, ip, ipam, None, chosen=False,
                                  note="Not in the workbook (perhaps recorded in NOMAD): kept unless you tick it."))
        else:
            differences = [(label, getattr(ipam, name), sheet.get(name, "")) for name, label in
                           (("status", "Status"), ("name", "Name")) if getattr(ipam, name) != sheet.get(name, "")]
            if differences:
                edited = ipam.version > 1
                changes.append(Change(ADDRESS, UPDATE, ip, ipam, sheet,
                                      [(label, STATUSES.get(before, before) if label == "Status" else before,
                                        STATUSES.get(after, after) if label == "Status" else after)
                                       for label, before, after in differences],
                                      chosen=not edited,
                                      note=f"Changed in NOMAD since it was imported (by {ipam.modified_by})."
                                      if edited else ""))
    return changes


def _subnet_value(sheet, name):
    value = sheet.get(name, False if name == "loopbacks" else {} if name == "fields" else "")
    return bool(value) if name == "loopbacks" else ("" if value is None else value)


def _sort_key(ip):
    address = parse_address(ip)
    return address.version, int(address)


def apply_changes(store, network_id, changes):
    """Make the ticked changes: network details, then new subnets, changed subnets, addresses, and removals last.
    Carries on past a change that can't be made. Returns (made, [(Change, error message)])."""
    chosen = [change for change in changes if change.chosen]
    made, failed = 0, []

    details = [change for change in chosen if change.kind == NETWORK]
    if details:
        fields = dict(store.network(network_id).fields)
        for change in details:
            if change.action == REMOVE:
                fields.pop(change.key, None)
            else:
                fields[change.key] = change.sheet
        try:
            store.update_network(network_id, fields=fields)
            made += len(details)
        except IpamError as error:
            failed += [(change, str(error)) for change in details]

    order = [(SUBNET, ADD), (SUBNET, UPDATE), (ADDRESS, ADD), (ADDRESS, UPDATE), (ADDRESS, REMOVE), (SUBNET, REMOVE)]
    for kind, action in order:
        for change in [change for change in chosen if (change.kind, change.action) == (kind, action)]:
            try:
                _apply(store, network_id, change)
                made += 1
            except IpamError as error:
                failed.append((change, str(error)))
    return made, failed


def _apply(store, network_id, change):
    sheet = change.sheet
    if change.kind == SUBNET:
        if change.action == ADD:
            store.add_subnet(network_id, sheet["cidr"], sheet.get("name", ""), sheet.get("gateway", ""),
                             sheet.get("description", ""), sheet.get("fields"), bool(sheet.get("loopbacks")))
        elif change.action == UPDATE:
            store.update_subnet(change.ipam.id, **{name: _subnet_value(sheet, name) for name, label in SUBNET_FIELDS
                                                   if any(label == difference[0]
                                                          for difference in change.differences)})
        else:
            store.delete_subnet(change.ipam.id)  # Its addresses stay, under any subnet holding them
    elif change.action == REMOVE:
        store.free_address(network_id, change.key)
    else:
        current = change.ipam
        store.set_address(network_id, change.key, sheet.get("status", "used"), sheet.get("name", ""),
                          current.mac if current else "", current.description if current else "",
                          current.fields if current else None)
