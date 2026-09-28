"""The IPAM database: networks, subnets and addresses in SQLite.

Each network is separate (such as an air-gapped network), so the same ranges can appear in more than one. Within a
network a subnet's CIDR is unique; an address written with a mask stands for the subnet holding it (172.28.101.0/16
is 172.28.0.0/16). Subnets may nest (a /20 block holding /24s), and an address belongs to the most specific subnet
containing it. Only addresses in use or reserved are stored; the rest of a subnet is free.

Rows are never removed: deleting marks them deleted (a tombstone) and every change bumps the row's version and is
written to the change log, so a later sync can send local changes to the server and apply everyone else's.
"""
import contextlib
import datetime
import getpass
import ipaddress
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field

from ..system import app_data_dir

log = logging.getLogger(__name__)

FILE_NAME = "ipam.db"
SCHEMA_VERSION = 1
USED, RESERVED = "used", "reserved"
STATUSES = {USED: "Used", RESERVED: "Reserved"}
MAX_NEXT_FREE_SCAN = 1 << 20  # Stop looking for a free address after this many (a /12's worth)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS networks (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', fields TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL, modified TEXT NOT NULL, modified_by TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS subnets (
    id TEXT PRIMARY KEY, network_id TEXT NOT NULL, cidr TEXT NOT NULL, sort_key TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '', gateway TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    fields TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL, modified TEXT NOT NULL, modified_by TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS subnets_unique ON subnets (network_id, sort_key) WHERE deleted = 0;
CREATE TABLE IF NOT EXISTS addresses (
    id TEXT PRIMARY KEY, network_id TEXT NOT NULL, ip TEXT NOT NULL, sort_key TEXT NOT NULL,
    status TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', mac TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', fields TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL, modified TEXT NOT NULL, modified_by TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS addresses_unique ON addresses (network_id, sort_key) WHERE deleted = 0;
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL, version INTEGER NOT NULL,
    op TEXT NOT NULL, data TEXT NOT NULL, modified TEXT NOT NULL, modified_by TEXT NOT NULL,
    pushed INTEGER NOT NULL DEFAULT 0);
"""


class IpamError(Exception):
    """A change that can't be made, with a message for the user."""


def ip_key(address):
    """Text that sorts addresses numerically, IPv4 before IPv6, so ranges can be found with BETWEEN."""
    return f"{address.version}{int(address):032x}"


def subnet_key(network):
    """Sort key for a subnet: its first address, then its prefix length (a /20 before the /24s inside it)."""
    return f"{ip_key(network.network_address)}/{network.prefixlen:03d}"


class Block:
    """A subnet: ipaddress's network plus its first and last address and a sort key, which the IPAM code uses."""

    def __init__(self, network):
        self.first, self.last = network.network_address, network.broadcast_address
        self.prefixlen, self.version, self.num_addresses = network.prefixlen, network.version, network.num_addresses
        self.netmask = network.netmask

    network_address = property(lambda self: self.first)
    broadcast_address = property(lambda self: self.last)

    @property
    def sort_key(self):
        return self.version, int(self.first), self.prefixlen

    def __contains__(self, address):
        return address.version == self.version and self.first <= address <= self.last

    def subnet_of(self, other):
        return other.version == self.version and other.first <= self.first and self.last <= other.last

    def __eq__(self, other):
        return isinstance(other, Block) and (self.first, self.prefixlen) == (other.first, other.prefixlen)

    def __hash__(self):
        return hash((self.first, self.prefixlen))

    def __lt__(self, other):
        return self.sort_key < other.sort_key

    def __str__(self):
        return f"{self.first}/{self.prefixlen}"

    def __repr__(self):
        return f"Block({self})"


def _address(value, version):
    return ipaddress.IPv4Address(value) if version == 4 else ipaddress.IPv6Address(value)


def parse_address(text):
    try:
        return ipaddress.ip_address(str(text).strip())
    except ValueError:
        raise IpamError(f"{text!r} isn't an IP address.") from None


def parse_subnet(text):
    """A Block from "10.1.2.0/24", "10.1.2.0 255.255.255.0" or "10.1.2.0/255.255.255.0". An address inside the
    subnet stands for the subnet ("172.28.101.0/16" is 172.28.0.0/16)."""
    text = " ".join(str(text).split())
    if " " in text:
        text = text.replace(" ", "/", 1)
    address_text, _, mask = text.partition("/")
    try:
        address = ipaddress.ip_address(address_text)
        return Block(ipaddress.ip_network(f"{address}/{mask or address.max_prefixlen}", strict=False))
    except ValueError:
        raise IpamError(f"{text!r} isn't a subnet (use CIDR like 10.1.2.0/24).") from None


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def current_user():
    try:
        return getpass.getuser()
    except Exception:  # No user name in the environment
        return "unknown"


@dataclass
class Network:
    id: str
    name: str
    description: str = ""
    fields: dict = field(default_factory=dict)
    version: int = 1
    modified: str = ""
    modified_by: str = ""


@dataclass
class Subnet:
    id: str
    network_id: str
    cidr: str
    name: str = ""
    gateway: str = ""
    description: str = ""
    fields: dict = field(default_factory=dict)
    version: int = 1
    modified: str = ""
    modified_by: str = ""

    @property
    def network(self):
        return parse_subnet(self.cidr)

    def special_addresses(self):
        """Addresses in the subnet that can't be handed out: {address: label}."""
        network = self.network
        special = {}
        if network.version == 4 and network.num_addresses > 2:
            special[network.network_address] = "Network"
            special[network.broadcast_address] = "Broadcast"
        elif network.version == 6 and network.num_addresses > 2:
            special[network.network_address] = "Subnet router anycast"
        if self.gateway:
            with contextlib.suppress(ValueError):
                gateway = ipaddress.ip_address(self.gateway)
                if gateway in network:
                    special[gateway] = "Gateway"
        return special


@dataclass
class Address:
    id: str
    network_id: str
    ip: str
    status: str = USED
    name: str = ""
    mac: str = ""
    description: str = ""
    fields: dict = field(default_factory=dict)
    version: int = 1
    modified: str = ""
    modified_by: str = ""

    @property
    def address(self):
        return ipaddress.ip_address(self.ip)


ENTITIES = {"networks": Network, "subnets": Subnet, "addresses": Address}
EDITABLE = {
    "networks": {"name", "description", "fields"},
    "subnets": {"name", "gateway", "description", "fields"},
    "addresses": {"status", "name", "mac", "description", "fields"},
}


def _from_row(cls, row):
    values = {name: row[name] for name in row.keys() if name in cls.__dataclass_fields__}
    values["fields"] = json.loads(values.get("fields") or "{}")
    return cls(**values)


def default_path():
    return str(app_data_dir() / FILE_NAME)


class IpamStore:
    """The local IPAM database. Use one per thread (SQLite connections aren't shared between threads)."""

    def __init__(self, path=None, user=None):
        self.path = path or default_path()
        self.user = user or current_user()
        self.db = sqlite3.connect(self.path, isolation_level=None)  # Transactions are begun and ended explicitly
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._depth = 0
        self.db.executescript(SCHEMA)
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('replica_id', ?)", (uuid.uuid4().hex,))

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        """Group changes so they all happen or none do (nested uses join the outer one)."""
        if self._depth == 0:
            self.db.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.db.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self.db.execute("COMMIT")

    # ----------------------------------------------------------------- Writing, with the change log

    def _insert(self, table, item):
        item.version, item.modified, item.modified_by = 1, now(), self.user
        data = {name: getattr(item, name) for name in item.__dataclass_fields__}
        row = dict(data, fields=json.dumps(item.fields))  # Details keep the order they were given in
        if table == "subnets":
            row["sort_key"] = subnet_key(item.network)
        elif table == "addresses":
            row["sort_key"] = ip_key(item.address)
        names = ", ".join(row)
        self.db.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        self._log(table, item.id, item.version, "create", data)
        return item

    def _update(self, table, item, changes):
        unknown = set(changes) - EDITABLE[table]
        if unknown:
            raise ValueError(f"Can't change {', '.join(sorted(unknown))} of {table}")
        changes = {name: value for name, value in changes.items() if getattr(item, name) != value}
        if not changes:
            return item
        for name, value in changes.items():
            setattr(item, name, value)
        item.version, item.modified, item.modified_by = item.version + 1, now(), self.user
        row = dict(changes, version=item.version, modified=item.modified, modified_by=item.modified_by)
        if "fields" in row:
            row["fields"] = json.dumps(row["fields"])
        assignments = ", ".join(f"{name} = ?" for name in row)
        self.db.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", list(row.values()) + [item.id])
        self._log(table, item.id, item.version, "update", changes)
        return item

    def _delete(self, table, item):
        item.version, item.modified, item.modified_by = item.version + 1, now(), self.user
        self.db.execute(f"UPDATE {table} SET deleted = 1, version = ?, modified = ?, modified_by = ? WHERE id = ?",
                        (item.version, item.modified, item.modified_by, item.id))
        self._log(table, item.id, item.version, "delete", {})

    def _log(self, table, entity_id, version, op, data):
        self.db.execute("INSERT INTO changes (entity, entity_id, version, op, data, modified, modified_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (table, entity_id, version, op, json.dumps(data, sort_keys=True), now(), self.user))

    def _get(self, table, item_id):
        row = self.db.execute(f"SELECT * FROM {table} WHERE id = ? AND deleted = 0", (item_id,)).fetchone()
        if row is None:
            raise IpamError("It was deleted (perhaps by someone else).")
        return _from_row(ENTITIES[table], row)

    def pending_changes(self):
        """Changes made here that haven't been sent to a server yet."""
        return self.db.execute("SELECT COUNT(*) FROM changes WHERE pushed = 0").fetchone()[0]

    # ----------------------------------------------------------------- Networks

    def networks(self):
        rows = self.db.execute("SELECT * FROM networks WHERE deleted = 0 ORDER BY name COLLATE NOCASE")
        return [_from_row(Network, row) for row in rows]

    def network(self, network_id):
        return self._get("networks", network_id)

    def network_named(self, name):
        row = self.db.execute("SELECT * FROM networks WHERE deleted = 0 AND name = ? COLLATE NOCASE",
                              (name.strip(),)).fetchone()
        return None if row is None else _from_row(Network, row)

    def _check_network_name(self, name, network_id=None):
        name = name.strip()
        if not name:
            raise IpamError("Give the network a name.")
        existing = self.network_named(name)
        if existing is not None and existing.id != network_id:
            raise IpamError(f"There's already a network called {existing.name}.")
        return name

    def add_network(self, name, description="", fields=None):
        name = self._check_network_name(name)
        with self.transaction():
            return self._insert("networks", Network(uuid.uuid4().hex, name, description, dict(fields or {})))

    def update_network(self, network_id, **changes):
        with self.transaction():
            network = self.network(network_id)
            if "name" in changes:
                changes["name"] = self._check_network_name(changes["name"], network_id)
            return self._update("networks", network, changes)

    def delete_network(self, network_id):
        """Delete a network with all its subnets and addresses."""
        with self.transaction():
            network = self.network(network_id)
            self.clear_network(network_id)
            self._delete("networks", network)

    def clear_network(self, network_id):
        """Delete every subnet and address in a network, keeping the network (before importing over it)."""
        with self.transaction():
            for subnet in self.subnets(network_id):
                self._delete("subnets", subnet)
            for address in self.addresses(network_id):
                self._delete("addresses", address)

    # ----------------------------------------------------------------- Subnets

    def subnets(self, network_id):
        """The network's subnets, in address order (a containing block before the subnets inside it)."""
        rows = self.db.execute("SELECT * FROM subnets WHERE network_id = ? AND deleted = 0 ORDER BY sort_key",
                               (network_id,))
        return [_from_row(Subnet, row) for row in rows]

    def subnet(self, subnet_id):
        return self._get("subnets", subnet_id)

    def add_subnet(self, network_id, cidr, name="", gateway="", description="", fields=None):
        network = parse_subnet(cidr)
        gateway = self._check_gateway(gateway, network)
        with self.transaction():
            self.network(network_id)
            existing = self.db.execute("SELECT name FROM subnets WHERE network_id = ? AND sort_key = ? AND deleted = 0",
                                       (network_id, subnet_key(network))).fetchone()
            if existing is not None:
                raise IpamError(f"{network} is already in this network ({existing['name'] or 'no name'}).")
            return self._insert("subnets", Subnet(uuid.uuid4().hex, network_id, str(network), name.strip(), gateway,
                                                  description, dict(fields or {})))

    def update_subnet(self, subnet_id, **changes):
        with self.transaction():
            subnet = self.subnet(subnet_id)
            if "gateway" in changes:
                changes["gateway"] = self._check_gateway(changes["gateway"], subnet.network)
            if "name" in changes:
                changes["name"] = changes["name"].strip()
            return self._update("subnets", subnet, changes)

    def delete_subnet(self, subnet_id, with_addresses=False):
        """Delete a subnet; its addresses stay (under any containing subnet) unless with_addresses."""
        with self.transaction():
            subnet = self.subnet(subnet_id)
            if with_addresses:
                for address in self.addresses(subnet.network_id, subnet.network):
                    self._delete("addresses", address)
            self._delete("subnets", subnet)

    @staticmethod
    def _check_gateway(gateway, network):
        gateway = (gateway or "").strip()
        if not gateway:
            return ""
        address = parse_address(gateway)
        if address not in network:
            raise IpamError(f"The gateway {address} isn't in {network}.")
        return str(address)

    def subnet_for(self, network_id, address):
        """The most specific subnet holding an address, or None."""
        address = ipaddress.ip_address(address)
        best = None
        for subnet in self.subnets(network_id):
            if address in subnet.network and (best is None or (subnet.network.prefixlen, subnet.network.first) >
                                              (best.network.prefixlen, best.network.first)):
                best = subnet
        return best

    # ----------------------------------------------------------------- Addresses

    def addresses(self, network_id, within=None):
        """Addresses in use or reserved in a network, or in one range of it, in address order."""
        if within is None:
            rows = self.db.execute("SELECT * FROM addresses WHERE network_id = ? AND deleted = 0 ORDER BY sort_key",
                                   (network_id,))
        else:
            rows = self.db.execute("SELECT * FROM addresses WHERE network_id = ? AND deleted = 0 AND sort_key "
                                   "BETWEEN ? AND ? ORDER BY sort_key",
                                   (network_id, ip_key(within.network_address), ip_key(within.broadcast_address)))
        return [_from_row(Address, row) for row in rows]

    def address(self, network_id, ip):
        row = self.db.execute("SELECT * FROM addresses WHERE network_id = ? AND sort_key = ? AND deleted = 0",
                              (network_id, ip_key(parse_address(ip)))).fetchone()
        return None if row is None else _from_row(Address, row)

    def count_addresses(self, network_id, within):
        return self.db.execute("SELECT COUNT(*) FROM addresses WHERE network_id = ? AND deleted = 0 AND sort_key "
                               "BETWEEN ? AND ?", (network_id, ip_key(within.network_address),
                                                   ip_key(within.broadcast_address))).fetchone()[0]

    def set_address(self, network_id, ip, status=USED, name="", mac="", description="", fields=None):
        """Record an address as used or reserved, adding it or changing what's recorded."""
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        ip = parse_address(ip)
        values = dict(status=status, name=name.strip(), mac=mac.strip(), description=description,
                      fields=dict(fields or {}))
        with self.transaction():
            self.network(network_id)
            existing = self.address(network_id, ip)
            if existing is not None:
                return self._update("addresses", existing, values)
            return self._insert("addresses", Address(uuid.uuid4().hex, network_id, str(ip), **values))

    def free_address(self, network_id, ip):
        """Mark an address free again (forgetting what was recorded for it)."""
        with self.transaction():
            existing = self.address(network_id, ip)
            if existing is not None:
                self._delete("addresses", existing)

    def next_free(self, subnet):
        """The lowest address in a subnet that isn't recorded, the gateway, or the network or broadcast address."""
        network = subnet.network
        taken = {address.address for address in self.addresses(subnet.network_id, network)}
        taken.update(subnet.special_addresses())
        # Addresses in a nested subnet belong to that subnet, so don't hand them out here
        nested = [other.network for other in self.subnets(subnet.network_id)
                  if other.id != subnet.id and other.network.subnet_of(network) and other.network != network]
        candidate = int(network.network_address)
        for _ in range(min(network.num_addresses, MAX_NEXT_FREE_SCAN)):
            address = _address(candidate, network.version)
            if address not in taken and not any(address in block for block in nested):
                return address
            candidate += 1
        return None

    # ----------------------------------------------------------------- Finding

    def search(self, text, network_id=None, limit=500):
        """Subnets and addresses matching text: an address (or subnet) by value, or any name or description.

        Returns [(network, subnet or None, address or None)]; a subnet match has address None.
        """
        text = text.strip()
        if not text:
            return []
        networks = {network.id: network for network in self.networks()}
        results = []
        like = f"%{text.lower()}%"
        where_network = "" if network_id is None else "AND network_id = ?"
        extra = [] if network_id is None else [network_id]

        address_value = None
        with contextlib.suppress(ValueError):
            address_value = ipaddress.ip_address(text)
        if address_value is not None:  # Every subnet holding it, and the address itself if recorded
            for current_id in ([network_id] if network_id else list(networks)):
                subnet = self.subnet_for(current_id, address_value)
                address = self.address(current_id, address_value)
                if subnet is not None or address is not None:
                    results.append((networks[current_id], subnet, address or
                                    Address("", current_id, str(address_value), status="")))
            return results

        rows = self.db.execute(f"SELECT * FROM subnets WHERE deleted = 0 {where_network} AND (cidr LIKE ? OR "
                               "lower(name) LIKE ? OR lower(description) LIKE ? OR lower(fields) LIKE ?) "
                               "ORDER BY sort_key LIMIT ?", extra + [f"{text}%", like, like, like, limit])
        for row in rows:
            subnet = _from_row(Subnet, row)
            results.append((networks[subnet.network_id], subnet, None))
        rows = self.db.execute(f"SELECT * FROM addresses WHERE deleted = 0 {where_network} AND (ip LIKE ? OR "
                               "lower(name) LIKE ? OR lower(mac) LIKE ? OR lower(description) LIKE ? OR "
                               "lower(fields) LIKE ?) ORDER BY network_id, sort_key LIMIT ?",
                               extra + [f"{text}%", like, like, like, like, limit])
        subnet_cache = {}
        for row in rows:
            address = _from_row(Address, row)
            subnets = subnet_cache.setdefault(address.network_id, self.subnets(address.network_id))
            holding = [subnet for subnet in subnets if address.address in subnet.network]
            subnet = max(holding, key=lambda subnet: (subnet.network.prefixlen, subnet.network.first), default=None)
            results.append((networks[address.network_id], subnet, address))
        return results[:limit]
