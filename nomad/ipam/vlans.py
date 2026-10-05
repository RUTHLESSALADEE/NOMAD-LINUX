"""VLANs: each VLAN domain's VLANs (number, name, status, the subnets they carry), kept in the IPAM database beside
the networks.

A domain is where VLAN numbers are unique: usually a VTP domain, or one site's switches. It can belong to one IPAM
network, whose subnets its VLANs carry (named by CIDR, so a network imported again keeps its links). A subnet is in
at most one VLAN of a domain; a VLAN can carry several (secondary addresses). Nothing here changes the networks,
subnets or addresses themselves: IPAM, and its export to the addressing workbook, are as they were.

VlanStore works on an IpamStore: this computer's database, the server's, or a laptop's copy of the server's. On a
laptop, TeamVlanStore (vlan_team.py) sends changes to the server, or keeps VLAN changes made offline as pending, as
addresses are.
"""
import ipaddress
import json
import re
import uuid

from .store import IpamError, Vlan, VlanDomain, _from_row, parse_subnet, vlan_key

ACTIVE, RESERVED, PLANNED = "active", "reserved", "planned"
STATUSES = {ACTIVE: "Active", RESERVED: "Reserved", PLANNED: "Planned"}
MAX_VLAN = 4094
MAX_NAME = 32  # What Cisco IOS takes for a VLAN's name
SET_VLAN, DELETE_VLAN = "set_vlan", "delete_vlan"
VLAN_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS vlan_pending (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, domain_id TEXT NOT NULL, vlan INTEGER NOT NULL, sort_key TEXT NOT NULL,
    action TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', expected_version INTEGER, original TEXT,
    made TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '');
"""


def check_number(number):
    try:
        number = int(str(number).strip())
    except ValueError:
        raise IpamError(f"{number!r} isn't a VLAN number.") from None
    if not 1 <= number <= MAX_VLAN:
        raise IpamError(f"VLAN {number} isn't between 1 and {MAX_VLAN}.")
    return number


def clean_subnets(subnets):
    """CIDRs as IPAM writes them, in address order, without repeats. Raises IpamError naming one that isn't."""
    blocks = {}
    for text in subnets or ():
        if str(text).strip():
            block = parse_subnet(text)
            blocks[str(block)] = block
    return [str(block) for block in sorted(blocks.values())]


def clean_ranges(ranges):
    """A domain's ranges as [{"first", "last", "name"}], in order. Raises IpamError for one that isn't."""
    cleaned = []
    for item in ranges or ():
        first, last = check_number(item.get("first")), check_number(item.get("last"))
        if first > last:
            raise IpamError(f"The range {first}-{last} goes backwards.")
        cleaned.append({"first": first, "last": last, "name": str(item.get("name", "")).strip()})
    return sorted(cleaned, key=lambda item: (item["first"], item["last"]))


def range_for(domain, number):
    """The domain's range a VLAN number is in (the narrowest), or None."""
    holding = [item for item in domain.ranges if item["first"] <= number <= item["last"]]
    return min(holding, key=lambda item: item["last"] - item["first"], default=None)


def name_problem(name):
    """Why a switch may not take a VLAN name, or "": IOS takes up to 32 characters, without spaces unless quoted."""
    if len(name) > MAX_NAME:
        return f"Cisco switches take VLAN names of up to {MAX_NAME} characters."
    if re.search(r"\s", name):
        return "Cisco switches take VLAN names with spaces only in quotes (an underscore is safer)."
    return ""


class VlanStore:
    """VLAN domains and VLANs in an IpamStore. Changes go through the store's change log (so they sync and have
    history like everything else in it)."""

    def __init__(self, store):
        self.store = store

    @property
    def db(self):
        return self.store.db

    @property
    def user(self):
        return self.store.user

    # ----------------------------------------------------------------- Domains

    def domains(self):
        rows = self.db.execute("SELECT * FROM vlan_domains WHERE deleted = 0 ORDER BY name COLLATE NOCASE")
        return [self._domain(row) for row in rows]

    @staticmethod
    def _domain(row):
        return _from_row(VlanDomain, row)

    def domain(self, domain_id):
        return self.store._get("vlan_domains", domain_id)

    def domain_named(self, name):
        row = self.db.execute("SELECT * FROM vlan_domains WHERE deleted = 0 AND name = ? COLLATE NOCASE",
                              (name.strip(),)).fetchone()
        return None if row is None else self._domain(row)

    def domains_for_vtp(self, vtp_domain):
        """Domains matching a VTP domain's name (what a map found), best first."""
        wanted = (vtp_domain or "").strip().lower()
        if not wanted:
            return []
        return [domain for domain in self.domains()
                if domain.vtp_domain.lower() == wanted or domain.name.lower() == wanted]

    def _check_domain(self, name, network_id, domain_id=None):
        name = name.strip()
        if not name:
            raise IpamError("Give the VLAN domain a name.")
        existing = self.domain_named(name)
        if existing is not None and existing.id != domain_id:
            raise IpamError(f"There's already a VLAN domain called {existing.name}.")
        if network_id:
            self.store.network(network_id)  # Raises if it's gone
        return name

    def add_domain(self, name, network_id="", vtp_domain="", description="", ranges=None, fields=None):
        with self.store.transaction():
            name = self._check_domain(name, network_id)
            return self.store._insert("vlan_domains", VlanDomain(
                uuid.uuid4().hex, name, network_id or "", (vtp_domain or "").strip(), description,
                clean_ranges(ranges), dict(fields or {})))

    def update_domain(self, domain_id, **changes):
        with self.store.transaction():
            domain = self.domain(domain_id)
            if "name" in changes or "network_id" in changes:
                changes["name"] = self._check_domain(changes.get("name", domain.name),
                                                     changes.get("network_id", domain.network_id), domain_id)
            if "ranges" in changes:
                changes["ranges"] = clean_ranges(changes["ranges"])
            if "vtp_domain" in changes:
                changes["vtp_domain"] = (changes["vtp_domain"] or "").strip()
            return self.store._update("vlan_domains", domain, changes)

    def delete_domain(self, domain_id):
        """Delete a domain and its VLANs."""
        with self.store.transaction():
            domain = self.domain(domain_id)
            for vlan in self.vlans(domain_id):
                self.store._delete("vlans", vlan)
            self.store._delete("vlan_domains", domain)

    # ----------------------------------------------------------------- VLANs

    def vlans(self, domain_id):
        rows = self.db.execute("SELECT * FROM vlans WHERE domain_id = ? AND deleted = 0 ORDER BY sort_key",
                               (domain_id,))
        return [self._vlan(row) for row in rows]

    @staticmethod
    def _vlan(row):
        return _from_row(Vlan, row)

    def vlan(self, domain_id, number):
        row = self.db.execute("SELECT * FROM vlans WHERE domain_id = ? AND sort_key = ? AND deleted = 0",
                              (domain_id, vlan_key(check_number(number)))).fetchone()
        return None if row is None else self._vlan(row)

    def vlan_with_subnet(self, domain_id, cidr):
        """The VLAN in the domain carrying a subnet, or None."""
        cidr = str(parse_subnet(cidr))
        return next((vlan for vlan in self.vlans(domain_id) if cidr in vlan.subnets), None)

    def set_vlan(self, domain_id, number, name="", status=ACTIVE, subnets=(), description="", fields=None):
        """Record a VLAN, adding it or changing what's recorded."""
        if status not in STATUSES:
            raise IpamError(f"Unknown VLAN status {status!r}.")
        number = check_number(number)
        values = dict(name=name.strip(), status=status, subnets=clean_subnets(subnets), description=description,
                      fields=dict(fields or {}))
        with self.store.transaction():
            self.domain(domain_id)
            for cidr in values["subnets"]:
                other = self.vlan_with_subnet(domain_id, cidr)
                if other is not None and other.vlan != number:
                    raise IpamError(f"{cidr} is already in VLAN {other.vlan}{f' ({other.name})' if other.name else ''}: "
                                    "a subnet can be in one VLAN of a domain.")
            existing = self.vlan(domain_id, number)
            if existing is not None:
                return self.store._update("vlans", existing, values)
            return self.store._insert("vlans", Vlan(uuid.uuid4().hex, domain_id, number, **values))

    def delete_vlan(self, domain_id, number):
        with self.store.transaction():
            existing = self.vlan(domain_id, number)
            if existing is not None:
                self.store._delete("vlans", existing)

    def next_free(self, domain_id, first=1, last=MAX_VLAN):
        """The lowest VLAN number from first to last that the domain hasn't recorded, or None."""
        taken = {vlan.vlan for vlan in self.vlans(domain_id)}
        return next((number for number in range(max(1, first), min(last, MAX_VLAN) + 1) if number not in taken), None)

    def deleted_numbers(self, domain_id):
        """VLAN numbers deleted from a domain (and not recorded again): removed on purpose, so a comparison with a
        network map doesn't offer to put them back as though they were new."""
        rows = self.db.execute(
            "SELECT vlan FROM vlans AS old WHERE domain_id = ? AND deleted = 1 AND NOT EXISTS (SELECT 1 FROM vlans "
            "WHERE domain_id = old.domain_id AND sort_key = old.sort_key AND deleted = 0)", (domain_id,))
        return {row[0] for row in rows}

    def search(self, text, limit=500):
        """VLANs whose number, name, description or subnets match: [(domain, vlan)]."""
        text = text.strip()
        if not text:
            return []
        domains = {domain.id: domain for domain in self.domains()}
        like = f"%{text.lower()}%"
        number = int(text) if text.isdigit() else -1
        rows = self.db.execute("SELECT * FROM vlans WHERE deleted = 0 AND (vlan = ? OR lower(name) LIKE ? OR "
                               "lower(description) LIKE ? OR subnets LIKE ? OR lower(fields) LIKE ?) "
                               "ORDER BY domain_id, sort_key LIMIT ?", (number, like, like, f"%{text}%", like, limit))
        return [(domains[row["domain_id"]], self._vlan(row)) for row in rows if row["domain_id"] in domains]

    def set_vlans(self, domain_id, items):
        """Record several VLANs at once ([{"vlan", "name", "status", "subnets", "description", "fields"}]), all or
        none."""
        with self.store.transaction():
            return [self.set_vlan(domain_id, item["vlan"], **{name: value for name, value in item.items()
                                                              if name not in ("vlan", "expected_version")})
                    for item in items]


# --------------------------------------------------------------------- The subnets a domain's VLANs can carry

def subnet_choices(ipam_store, domain):
    """The subnets of the domain's network as [(CIDR, name)], or [] when it has none."""
    if not domain.network_id:
        return []
    try:
        ipam_store.network(domain.network_id)
    except IpamError:
        return []
    return [(subnet.cidr, subnet.name) for subnet in ipam_store.subnets(domain.network_id)]


VLAN_NAME_PATTERN = re.compile(r"^\s*vlan\s*[-_ ]?\s*(\d{1,4})\b", re.IGNORECASE)


def vlan_named_in(text):
    """The VLAN a subnet's name or detail says it's in ("Vlan 6", "VLAN-10"), or 0."""
    match = VLAN_NAME_PATTERN.match(text or "")
    number = int(match.group(1)) if match else 0
    return number if 1 <= number <= MAX_VLAN else 0


def suggested_links(ipam_store, vlan_store, domain, skip=()):
    """Subnets of the domain's network that say which VLAN they're in, by name ("Vlan 6") or in a detail (such as
    the "Vlan 10" column some workbook pages have), and aren't in a VLAN of the domain yet: [(VLAN, CIDR, why)].
    skip: CIDRs not to suggest (subnets whose role isn't a VLAN's). Only read: the subnets are left exactly as they
    are."""
    linked = {cidr for vlan in vlan_store.vlans(domain.id) for cidr in vlan.subnets}
    found = []
    for subnet in (ipam_store.subnets(domain.network_id) if domain.network_id else []):
        if subnet.cidr in linked or subnet.cidr in skip:
            continue
        number, why = vlan_named_in(subnet.name), f"its name is {subnet.name}"
        if not number:
            for key, value in subnet.fields.items():
                number = vlan_named_in(str(value))
                if number:
                    why = f"its {key} says {value}"
                    break
        if number:
            found.append((number, subnet.cidr, why))
    return found


def holding_subnet(subnets, address):
    """The most specific of subnets (IPAM Subnets) holding an address, or None: where a VLAN interface's address
    belongs, even when IPAM writes the block bigger or smaller than the interface's mask."""
    try:
        address = ipaddress.ip_address(address)
    except ValueError:
        return None
    holding = [subnet for subnet in subnets if not subnet.loopbacks and address in subnet.network]
    return max(holding, key=lambda subnet: subnet.network.prefixlen, default=None)


def network_for_gateways(ipam_store, gateways):
    """The IPAM network holding the most of these VLAN interfaces' addresses (netmap.vlans.Gateways), or None."""
    best, best_count = None, 0
    addresses = [gateway.address for gateway in gateways]
    for network in ipam_store.networks():
        subnets = ipam_store.subnets(network.id)
        count = sum(1 for address in addresses if holding_subnet(subnets, address) is not None)
        if count > best_count:
            best, best_count = network, count
    return best


# --------------------------------------------------------------------- Pending VLAN changes on a laptop's copy

def keep_pending_on_top(copy, items):
    """The server's rows just replaced some VLANs that have changes waiting (in the copy's vlan_pending). Remember
    the server's row as what to go back to if the change is refused, and show the waiting change on top again."""
    arrived = {(item["row"]["domain_id"], item["row"]["sort_key"]): item["row"] for item in items
               if item["entity"] == "vlans"}
    if not arrived:
        return
    store = VlanStore(copy)
    for entry in copy.db.execute("SELECT * FROM vlan_pending WHERE state = 'pending'").fetchall():
        row = arrived.get((entry["domain_id"], entry["sort_key"]))
        if row is None:
            continue
        with copy.transaction():
            copy.db.execute("UPDATE vlan_pending SET original = ? WHERE seq = ?",
                            (None if row["deleted"] else json.dumps(row), entry["seq"]))
            try:
                if entry["action"] == SET_VLAN:
                    store.set_vlan(entry["domain_id"], entry["vlan"], **json.loads(entry["data"]))
                else:
                    store.delete_vlan(entry["domain_id"], entry["vlan"])
            except IpamError:
                pass  # The domain went, or the subnet is now another VLAN's: the server will refuse it


# --------------------------------------------------------------------- History

def vlan_history(store, domain_id, number):
    """Everything that happened to one VLAN number in a domain (each time it was deleted and recorded again is a new
    row), newest first, as history.Events. store: an IpamStore, or a TeamStore (whose copy keeps the history)."""
    from .history import _events_for, source_of
    db, table = source_of(store)
    rows = db.execute(f"SELECT DISTINCT entity_id FROM {table} WHERE entity = 'vlans' AND op = 'create' AND "
                      "json_extract(data, '$.domain_id') = ? AND json_extract(data, '$.vlan') = ?",
                      (domain_id, int(number)))
    return list(reversed(_events_for(db, table, [row[0] for row in rows])))


def domain_history(store, domain_id):
    """Every change to a domain and its VLANs, newest first."""
    from .history import _events_for, source_of
    db, table = source_of(store)
    rows = db.execute(f"SELECT DISTINCT entity_id FROM {table} WHERE entity = 'vlans' AND op = 'create' AND "
                      "json_extract(data, '$.domain_id') = ?", (domain_id,))
    return list(reversed(_events_for(db, table, [domain_id] + [row[0] for row in rows])))
