"""Crawling the network: ask each device for its CDP/LLDP neighbors over SNMP, then ask those neighbors, and so on.

Starts from one or more seed addresses and stays within the scope (subnets, hops and a device limit). Devices that
don't answer SNMP are pinged, so the map can tell "wrong community or ACL" from "can't be reached". Switches' MAC
tables (per VLAN on Cisco IOS, where each VLAN has its own table) and routers' ARP tables put hosts on the edge
ports they're plugged into.
"""
import copy
import datetime
import ipaddress
import logging
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field

from ..oui import format_mac, vendor
from ..snmp import V2C, SnmpClient, SnmpError, parse_oid
from . import collect, l3
from .model import AP, HOST, KIND_NAMES, NETWORK_KINDS, NO_SNMP, PHONE, SERVER, SNMP, UNKNOWN, UNREACHABLE, Device, \
    Host, Link, NetworkMap, Trace, display_name, normalize_name, port_key, short_port

log = logging.getLogger(__name__)

WORKERS = 16  # Devices read at once (Scope can change it)
MAX_WORKERS = 64
BULK_ROWS = 50  # Rows asked for in each SNMP GETBULK: fewer round trips than the usual 25
VLAN_WORKERS = 4  # A Catalyst's per-VLAN MAC tables read at once
STOPPED = "Stopped"
PINGS_NO_SNMP = "Answers ping but not SNMP: check the community string and the device's SNMP ACL."
NO_ANSWER = "No answer to SNMP or ping."
LIVE_INTERVAL = 1.5  # Seconds between snapshots of the map for drawing it while it's crawled
ROUTE_ROWS = 20000  # Per column: a core with the full internet table shouldn't take all day
MAX_ROUTES = 5000  # Kept per device in the map
END_DEVICE_KINDS = {PHONE, HOST}  # Shown as hosts on their switch port, not as devices on the map


@dataclass
class CrawlSettings:
    seeds: list
    communities: list = field(default_factory=lambda: ["public"])
    overrides: list = field(default_factory=list)  # [(subnet text, community)]: tried first for addresses in it
    scope: list = field(default_factory=list)  # Subnets the crawl may go into; empty means any private address
    max_hops: int = 6
    max_devices: int = 500
    version: int = V2C
    timeout: int = 2000  # Milliseconds per SNMP request
    retries: int = 1
    collect_hosts: bool = True
    trace: bool = True  # Traceroute to what SNMP couldn't show (for the logical view)
    max_traces: int = l3.MAX_TRACES
    workers: int = WORKERS
    # Device key -> {attribute: value} corrected by hand on the map before: an address to ask it at (CDP gave none,
    # or the wrong one), what kind it is (asked if a network device), its name. Kept over what the crawl finds.
    corrections: dict = field(default_factory=dict)
    # Device key -> [its addresses]: deleted from the map by hand, so left off it and not crawled through (unless
    # it's where the crawl starts)
    deleted: dict = field(default_factory=dict)


def parse_networks(lines):
    """Subnets from text, one per line (or separated by commas or spaces). Raises ValueError naming a bad one."""
    networks = []
    for item in " ".join(lines).replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"'{item}' isn't a subnet or address.") from None
    return networks


def parse_overrides(overrides):
    """[(subnet text, community)] as [(network, community)], the most specific subnet first."""
    return sorted(((ipaddress.ip_network(subnet, strict=False), community) for subnet, community in overrides),
                  key=lambda item: item[0].prefixlen, reverse=True)


def communities_for(address, communities, overrides):
    """The community strings to try for address, in order: those for the subnets it's in (overrides, as
    parse_overrides gives them), then communities."""
    ip = ipaddress.ip_address(address)
    chosen = [community for network, community in overrides if ip.version == network.version and ip in network]
    return list(dict.fromkeys(chosen + list(communities)))


class Crawler:
    def __init__(self, settings, client_factory=SnmpClient, pinger=None, should_stop=lambda: False,
                 progress=lambda message: None, echo=None, events=lambda kind, *details: None, known=None):
        """events(kind, *details) reports progress for a live view, from the crawl's own and its worker threads:
            ("started", address, key or None)  a device is being read
            ("step", address, text)            what it's doing now (the table, the VLAN, the community)
            ("finished", address, outcome)     snmp, no-snmp, unreachable, again (a second address), stopped, traced
            ("log", text)                      something worth keeping in the crawl log
            ("counts", {...})                  read, reading, queued, found, no_snmp, unreachable
            ("map", NetworkMap)                a copy of the map so far (devices and links), every LIVE_INTERVAL
            ("phase", text)                    hosts, traceroute
            ("community", address, community)  the community string it answered to
        known: a map this crawl adds to (Crawl from Here). Devices it read aren't read again, and keep their keys.
        """
        self.settings = settings
        self.events = events
        self.counts = {"read": 0, "reading": 0, "queued": 0, "found": 0, "no_snmp": 0, "unreachable": 0}
        self.limit_logged = False
        self.client_factory = client_factory
        self.pinger = pinger or _ping
        self.echo = echo or l3.icmp_echo
        self.should_stop = should_stop
        self.progress = progress
        self.scope = parse_networks(settings.scope)
        self.overrides = parse_overrides(settings.overrides)
        self.map = NetworkMap(seeds=list(settings.seeds))
        self.aliases = {}  # Address or normalized name -> device key
        self.tables = {}  # Device key -> DeviceTables, for placing hosts at the end
        self.asked = set()  # Addresses already asked (or queued)
        self.capabilities = {}  # Device key -> what its neighbors' CDP/LLDP say it is (router, switch...)
        self.known = known
        if known is not None:
            for key, device in known.devices.items():
                if device.source == SNMP and not device.manual:  # Already read: recognize it, but don't ask again
                    for address in [device.mgmt_ip] + list(device.addresses):
                        if address:
                            self.aliases.setdefault(address, key)
                            self.asked.add(address)
                    if device.name:
                        self.aliases.setdefault(normalize_name(device.name), key)
            self.asked -= set(settings.seeds)  # Except where it starts from
        seeds = set(settings.seeds)
        self.deleted = {key for key, addresses in settings.deleted.items() if not seeds & set(addresses)}
        self.deleted_addresses = {address for key in self.deleted for address in settings.deleted[key]}
        self.left_out = set()  # Deleted devices seen as neighbors (logged once each)

    # ----------------------------------------------------------------- Scope

    def in_scope(self, address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if self.scope:
            return any(ip in network for network in self.scope)
        return ip.is_private and not ip.is_loopback and not ip.is_link_local

    def communities_for(self, address):
        return communities_for(address, self.settings.communities, self.overrides)

    # ----------------------------------------------------------------- Crawl

    def run(self):
        self.map.started = _now()
        queue = []
        for seed in self.settings.seeds:
            if seed not in self.asked:
                self.asked.add(seed)
                queue.append((seed, 0, None))
        done = 0
        last_snapshot, changed = 0.0, False
        self.events("log", f"Starting from {', '.join(self.settings.seeds)}")
        with ThreadPoolExecutor(max_workers=self.settings.workers) as executor:
            running = {}
            while (queue or running) and not self.should_stop():
                while queue and len(running) < self.settings.workers * 2:
                    address, hops, key = queue.pop(0)
                    if key is not None and key not in self.map.devices:
                        key = self.find(address)  # Merged into another device meanwhile
                    if key is not None and self.map.devices[key].source == SNMP:
                        continue  # Reached at another address meanwhile
                    running[executor.submit(self.visit, address)] = (address, hops, key)
                    self.events("started", address, key)
                self.report_counts(len(running), len(queue))
                finished, _ = wait(running, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in finished:
                    address, hops, key = running.pop(future)
                    if key is not None and key not in self.map.devices:
                        key = self.find(address)
                    done += 1
                    try:
                        tables, error = future.result()
                    except Exception as crash:  # A bug reading one device shouldn't lose the whole map
                        log.exception("Reading %s failed", address)
                        tables, error = None, f"Couldn't read it: {crash}"
                    queue.extend(self.record(address, hops, key, tables, error))
                    changed = True
                    self.report_counts(len(running), len(queue))
                    self.progress(f"Read {done} of {done + len(queue) + len(running)} devices "
                                  f"({len(self.map.devices)} found so far): {address}")
                if changed and time.monotonic() - last_snapshot >= LIVE_INTERVAL:
                    self.events("map", self.snapshot())
                    last_snapshot, changed = time.monotonic(), False
            self.map.stopped = self.should_stop()
            if self.map.stopped:
                self.events("log", f"Stopped, with {len(running)} being read and {len(queue)} still to read")
                for future in running:
                    future.cancel()
        self.map.remove_devices([key for key in self.map.devices if key in self.deleted])  # Reached another way
        self.report_counts(0, 0)
        self.events("map", self.snapshot())
        for key, corrections in self.settings.corrections.items():  # Corrected by hand: kept over what was found
            if key in self.map.devices:
                self.map.devices[key].apply_corrections(corrections)
        if self.settings.collect_hosts:
            self.events("phase", "Placing hosts on switch ports")
        self.place_hosts()
        if self.settings.collect_hosts:
            self.events("log", f"Placed {len(self.map.hosts)} hosts on switch ports")
        if self.settings.trace and not self.map.stopped:
            self.trace_paths()
        self.map.finished = _now()
        return self.map

    def report_counts(self, reading, queued):
        self.counts.update(reading=reading, queued=queued, found=len(self.map.devices))
        self.events("counts", dict(self.counts))

    def snapshot(self):
        """A copy of the devices and links found so far, for drawing while the crawl goes on."""
        snapshot = NetworkMap(seeds=list(self.map.seeds), started=self.map.started)
        snapshot.devices = {key: copy.copy(device) for key, device in self.map.devices.items()}
        snapshot.links = [copy.copy(link) for link in self.map.links]
        for link in snapshot.links:
            link.protocols = list(link.protocols)
        return snapshot

    def trace_paths(self):
        """Traceroute from this computer to devices that didn't answer SNMP, unknown next hops and static routes'
        destinations."""
        targets = l3.trace_targets(self.map, self.in_scope, self.settings.max_traces)
        if not targets:
            return
        self.progress(f"Tracing the way to {len(targets)} addresses SNMP couldn't show...")
        self.events("phase", f"Traceroute to {len(targets)} addresses SNMP couldn't show")

        def echo(address, ttl):
            self.events("step", address, f"Traceroute, hop {ttl}")
            return self.echo(address, ttl)

        traces = []
        with ThreadPoolExecutor(max_workers=self.settings.workers) as executor:
            futures = {}
            for address, reason in targets:
                futures[executor.submit(l3.trace, address, echo, should_stop=self.should_stop)] = (address, reason)
                self.events("started", address, None)
            for number, future in enumerate(as_completed(futures), start=1):
                address, reason = futures[future]
                try:
                    hops, reached = future.result()
                except OSError as error:  # No ICMP (a locked-down laptop): the map is still worth having
                    log.warning("Couldn't trace %s: %s", address, error)
                    self.events("finished", address, "traced")
                    self.events("log", f"Traceroute to {address} failed: {error}")
                    continue
                traces.append(Trace(address, hops, reached, reason))
                self.events("finished", address, "traced")
                self.events("log", f"Traced {address} ({reason}): " + " > ".join(hop or "*" for hop in hops)
                            + ("" if reached else " (not reached)"))
                self.progress(f"Traced {number} of {len(targets)}: {address}")
        self.map.traces = sorted(traces, key=lambda item: ipaddress.ip_address(item.target))
        self.map.stopped = self.should_stop()

    def connect(self, address):
        """Find the community string a device answers to. Returns (client, SystemInfo, community), or Nones when it
        answers none of them (or the crawl is stopping)."""
        communities = self.communities_for(address)
        for number, community in enumerate(communities, start=1):
            if self.should_stop():
                break
            self.events("step", address, f"Trying community {number} of {len(communities)}")
            try:
                client = self.client_factory(address, community, self.settings.version,
                                             timeout=self.settings.timeout, retries=self.settings.retries)
                info = collect.system_info(client.get([parse_oid(collect.SYS_DESCR), parse_oid(collect.SYS_OBJECT_ID),
                                                       parse_oid(collect.SYS_NAME)]))
                self.events("community", address, community)
                return client, info, community
            except (SnmpError, OSError) as problem:
                log.debug("SNMP to %s: %s", address, problem)
                self.events("log", f"{address}: no answer with community {number} of {len(communities)}")
        return None, None, None

    def visit(self, address):
        """Read one device. Returns (DeviceTables or None, error). Runs on a worker thread."""
        started = time.monotonic()
        client, info, community = self.connect(address)
        if client is None:
            if self.should_stop():
                return None, STOPPED
            self.events("step", address, "Pinging")
            return None, PINGS_NO_SNMP if self.pinger(address) else NO_ANSWER
        tables = collect.DeviceTables(info=info)
        tables.timings["Finding the community string"] = time.monotonic() - started
        self.read_tables(client, tables, community)
        tables.timings["total"] = time.monotonic() - started
        return tables, ""

    def walk(self, client, root, tables, what, limit=None, timing=None):
        """Walk one table (or column), noting the time it took under timing (or what) for the crawl log."""
        self.events("step", client.host, what)
        started = time.monotonic()
        try:
            options = {"limit": limit} if limit else {}
            return list(client.walk(parse_oid(root), max_repetitions=BULK_ROWS, should_stop=self.should_stop,
                                    **options))
        except (SnmpError, OSError) as problem:
            tables.warnings.append(f"{what}: {problem}")
            return []
        finally:
            step = timing or what
            tables.timings[step] = tables.timings.get(step, 0) + time.monotonic() - started

    def read_tables(self, client, tables, community):
        tables.interfaces = collect.interface_names(
            self.walk(client, collect.IF_NAME, tables, "Interface names", timing="Interfaces"),
            self.walk(client, collect.IF_DESCR, tables, "Interfaces"))
        tables.addresses = collect.ip_addresses(self.walk(client, collect.IP_ADDR_ENTRY, tables, "IP addresses"))
        tables.neighbors = collect.cdp_neighbors(self.walk(client, collect.CDP_CACHE_ENTRY, tables, "CDP",
                                                           timing="Neighbors"), tables.interfaces)
        lldp_rows = self.walk(client, collect.LLDP_REM_ENTRY, tables, "LLDP", timing="Neighbors")
        if lldp_rows:
            local_ports = collect.lldp_local_ports(self.walk(client, collect.LLDP_LOC_PORT_ENTRY, tables, "LLDP",
                                                             timing="Neighbors"))
            addresses = collect.lldp_management_addresses(
                self.walk(client, collect.LLDP_REM_MAN_ADDR_IF_SUBTYPE, tables, "LLDP", timing="Neighbors"))
            tables.neighbors += collect.lldp_neighbors(lldp_rows, local_ports, tables.interfaces, addresses)
        tables.arp = collect.arp(self.walk(client, collect.ARP_PHYS_ADDRESS, tables, "ARP", timing="ARP table"))
        route_rows = []
        for column in (5, 6, 7):  # ifIndex, type, protocol: the index holds destination, mask and next hop
            route_rows += self.walk(client, f"{collect.CIDR_ROUTE_ENTRY}.{column}", tables, "Routes", ROUTE_ROWS,
                                    timing="Routing table")
        old_rows = []
        if not route_rows:
            for column in (2, 7, 8, 9, 11):
                old_rows += self.walk(client, f"{collect.IP_ROUTE_ENTRY}.{column}", tables, "Routes", ROUTE_ROWS,
                                      timing="Routing table")
        tables.routes = collect.routes(route_rows, old_rows)
        tables.routes_truncated = len(route_rows) >= 3 * ROUTE_ROWS or len(old_rows) >= 5 * ROUTE_ROWS
        if not self.settings.collect_hosts:
            return
        tables.own_macs = collect.own_macs(self.walk(client, collect.IF_PHYS_ADDRESS, tables, "Interfaces"))
        tables.lag_parents = collect.lag_parents(
            self.walk(client, collect.IF_STACK_STATUS, tables, "Port-channels", timing="Interfaces"),
            self.walk(client, collect.LAG_ATTACHED, tables, "Port-channels", timing="Interfaces"))
        info = tables.info
        vlan_list = collect.vlans(self.walk(client, collect.VTP_VLAN_STATE, tables, "VLANs", timing="MAC tables")) \
            if info.object_id.startswith(collect.CISCO + ".") else []
        if vlan_list and "nx-os" not in info.descr.lower():
            self.read_vlan_tables(client, tables, community, vlan_list)
        else:
            base_ports = self.walk(client, collect.BASE_PORT_IFINDEX, tables, "MAC table", timing="MAC tables")
            q_rows = []
            for column in (2, 3):  # Port and status: the index holds the VLAN and the MAC
                q_rows += self.walk(client, f"{collect.Q_FDB_ENTRY}.{column}", tables, "MAC table by VLAN",
                                    timing="MAC tables")
            if not collect.fdb_by_vlan(q_rows, base_ports):
                for column in (2, 3):
                    q_rows += self.walk(client, f"{collect.FDB_ENTRY}.{column}", tables, "MAC table",
                                        timing="MAC tables")
            tables.fdb = collect.fdb_by_vlan(q_rows, base_ports) or collect.fdb(q_rows, base_ports)

    def read_vlan_tables(self, client, tables, community, vlan_list):
        """Catalyst IOS keeps a MAC table per VLAN, read with community@vlan. Only the VLANs its ports use (a VTP
        domain can list hundreds the switch doesn't carry), several at once."""
        in_use = collect.vlans_in_use(
            self.walk(client, collect.VM_VLAN, tables, "VLANs in use", timing="MAC tables"),
            self.walk(client, collect.VM_VOICE_VLAN, tables, "VLANs in use", timing="MAC tables"),
            self.walk(client, collect.TRUNK_NATIVE_VLAN, tables, "VLANs in use", timing="MAC tables"))
        chosen = [vlan for vlan in vlan_list if vlan in in_use] if in_use else vlan_list
        if len(chosen) < len(vlan_list):
            tables.notes.append(f"MAC tables for the {len(chosen)} of {len(vlan_list)} VLANs its ports use "
                                f"({', '.join(str(vlan) for vlan in chosen[:12])}{'...' if len(chosen) > 12 else ''})")
        started = time.monotonic()
        done = [0]

        def read(vlan):
            if self.should_stop():
                return []
            try:
                vlan_client = self.client_factory(client.host, f"{community}@{vlan}", self.settings.version,
                                                  timeout=self.settings.timeout, retries=self.settings.retries)
                rows = []
                for column in (2, 3):  # Port and status: the MAC is in the index
                    rows += list(vlan_client.walk(parse_oid(f"{collect.FDB_ENTRY}.{column}"),
                                                  max_repetitions=BULK_ROWS, should_stop=self.should_stop))
                ports = list(vlan_client.walk(parse_oid(collect.BASE_PORT_IFINDEX), max_repetitions=BULK_ROWS,
                                              should_stop=self.should_stop))
            except (SnmpError, OSError):
                return []  # A VLAN with no ports here doesn't answer on some models
            finally:
                done[0] += 1
                self.events("step", client.host, f"MAC tables: {done[0]} of {len(chosen)} VLANs read")
            return collect.fdb(rows, ports, vlan)

        with ThreadPoolExecutor(max_workers=VLAN_WORKERS) as executor:
            for entries in executor.map(read, chosen):
                tables.fdb += entries
        tables.timings["MAC tables"] = tables.timings.get("MAC tables", 0) + time.monotonic() - started

    # ----------------------------------------------------------------- Putting results on the map

    def find(self, address="", name=""):
        return self.aliases.get(address) or (self.aliases.get(normalize_name(name)) if name else None)

    def found_address(self, key, address):
        """The address to note on a device as found: not one it was only asked at because it was corrected by
        hand (that's put back at the end, as a correction over what the crawl found)."""
        return "" if address == self.settings.corrections.get(key, {}).get("mgmt_ip") else address

    def add_device(self, key, **details):
        device = self.map.devices.get(key)
        if device is None:
            device = self.map.devices[key] = Device(key=key)
        for attribute, value in details.items():
            if value and not getattr(device, attribute):
                setattr(device, attribute, value)
        if device.mgmt_ip:
            self.aliases.setdefault(device.mgmt_ip, key)
        if device.name:
            self.aliases.setdefault(normalize_name(device.name), key)
        return device

    def merge(self, old, new):
        """Fold device old into new: its links, addresses and anything new doesn't know yet."""
        gone = self.map.devices.pop(old)
        target = self.map.devices[new]
        for attribute in ("name", "mgmt_ip", "platform"):
            if not getattr(target, attribute):
                setattr(target, attribute, getattr(gone, attribute))
        links, self.map.links = self.map.links, []
        for link in links:
            link.a, link.b = (new if link.a == old else link.a), (new if link.b == old else link.b)
            if link.a != link.b:
                self.map.add_link(link)
        for alias, key in self.aliases.items():
            if key == old:
                self.aliases[alias] = new
        self.capabilities.setdefault(new, set()).update(self.capabilities.pop(old, ()))

    def record(self, address, hops, key, tables, error):
        """Put a visit's results on the map. Returns the neighbors to visit next as [(address, hops, key)]."""
        if tables is None:
            key = key or self.find(address) or f"ip:{address}"
            device = self.add_device(key, mgmt_ip=self.found_address(key, address))
            if error == STOPPED:
                self.events("finished", address, "stopped")
            elif device.source != SNMP:
                device.source = NO_SNMP if error == PINGS_NO_SNMP else UNREACHABLE
                device.error = error
                device.hops = hops
                self.counts["no_snmp" if device.source == NO_SNMP else "unreachable"] += 1
                self.events("finished", address, device.source)
                self.events("log", f"{device.label}: {error}")
            return []

        info = tables.info
        existing = self.find(address, info.name)
        if existing and existing in self.map.devices and self.map.devices[existing].source == SNMP:
            self.aliases[address] = existing
            self.events("finished", address, "again")
            self.events("log", f"{address} is {self.map.devices[existing].label} again (another of its addresses)")
            return []  # Reached the same device at a second address
        if key is None:
            key = existing or normalize_name(info.name) or f"ip:{address}"
        elif existing and existing != key:
            self.merge(key, existing)  # Found under two names (a neighbor's view, and its own sysName)
            key = existing
        device = self.add_device(key, mgmt_ip=self.found_address(key, address))
        device.name = info.name or device.name  # Its own sysName over how a neighbor wrote it
        device.source, device.error, device.hops = SNMP, "", hops
        device.sys_descr, device.sys_object_id = info.descr, info.object_id
        device.addresses = [ip for ip, _, _ in tables.addresses] or [address]
        device.interfaces_l3 = [[ip, prefix_length(mask), tables.interfaces.get(if_index, "")]
                                for ip, if_index, mask in tables.addresses]
        routes = [[destination, next_hop, tables.interfaces.get(if_index, ""), protocol]
                  for destination, next_hop, if_index, protocol in tables.routes]
        device.routes = routes[:MAX_ROUTES]
        device.routes_truncated = len(routes) > MAX_ROUTES or tables.routes_truncated
        for ip in device.addresses:
            self.aliases.setdefault(ip, key)
        device.kind = collect.classify(info.object_id, info.descr, frozenset(self.capabilities.get(key, ())),
                                       device.platform)
        if tables.warnings:
            log.info("%s: %s", device.label, "; ".join(tables.warnings))
            for warning in tables.warnings:
                self.events("log", f"{device.label}: couldn't read {warning}")
        if device.routes_truncated:
            self.events("log", f"{device.label}: only the first {len(device.routes):,} routes were kept")
        self.tables[key] = tables
        self.counts["read"] += 1
        self.events("finished", address, SNMP)
        self.events("log", f"Read {device.label} ({address}){timing_summary(tables.timings)}: "
                           f"{collect_summary(device, tables)}")
        for note in tables.notes:
            self.events("log", f"{device.label}: {note}")

        next_visits = []
        for neighbor in tables.neighbors:
            kind = collect.classify(capabilities=neighbor.capabilities, platform=neighbor.platform)
            if kind in END_DEVICE_KINDS:
                continue  # Placed on its port as a host at the end
            other = self.find(neighbor.address, neighbor.name) or normalize_name(neighbor.name)
            if other == key:
                continue
            if other in self.deleted or (neighbor.address and neighbor.address in self.deleted_addresses):
                if other not in self.left_out:
                    self.left_out.add(other)
                    self.events("log", f"Left out {display_name(neighbor.name) or neighbor.address} (seen on "
                                       f"{device.label}): it was deleted from the map")
                continue
            corrected = self.settings.corrections.get(other, {})  # By hand, on the map before
            found_kind, kind = kind, corrected.get("kind", kind)  # What to note, and what decides whether it's asked
            address = corrected.get("mgmt_ip") or neighbor.address
            fresh = other not in self.map.devices
            on_map = self.known is not None and other in self.known.devices  # From the map being added to
            known = not fresh or on_map
            if address != neighbor.address:
                self.aliases.setdefault(address, other)
            other_device = self.add_device(other, name=display_name(neighbor.name), mgmt_ip=neighbor.address,
                                           platform=neighbor.platform)
            if fresh:  # What the map says it is, if it's on it (an access point's port isn't an uplink)
                other_device.kind = found_kind
                if on_map:  # What the crawl found it to be before (the correction goes back on at the end)
                    before = self.known.devices[other]
                    other_device.kind = before.corrected.get("kind", [before.kind])[0]
                other_device.hops = hops + 1
            self.capabilities.setdefault(other, set()).update(neighbor.capabilities)
            self.map.add_link(Link(key, short_port(neighbor.local_port), other, short_port(neighbor.port),
                                   [neighbor.protocol]))
            if other_device.source == SNMP or (address and address in self.asked):
                continue
            name = f"{other_device.label} ({address})" if address else other_device.label
            if not known:
                self.events("log", f"Found {name} through {neighbor.protocol.upper()} on {device.label} "
                                   f"{short_port(neighbor.local_port)}")
            why_not = ""
            if not address:
                why_not = "it didn't announce a management address"
            elif not self.in_scope(address):
                why_not = "it's outside the scope"
            elif hops + 1 > self.settings.max_hops:
                why_not = f"it's more than {self.settings.max_hops} hops from the start"
            elif kind not in NETWORK_KINDS | {UNKNOWN}:
                why_not = f"it's {'an access point' if kind == AP else 'not a network device'}: shown, not asked"
            elif len(self.asked) >= self.settings.max_devices:
                if not self.limit_logged:
                    self.events("log", f"Reached the limit of {self.settings.max_devices} devices: no more will "
                                       "be asked (change it under Scope)")
                    self.limit_logged = True
                continue
            if why_not:
                if not known:
                    self.events("log", f"Not asking {name}: {why_not}")
                continue
            self.asked.add(address)
            next_visits.append((address, hops + 1, other))
        return next_visits

    def place_hosts(self):
        """Put each MAC on the one edge port it was learned on, with its IP from the ARP tables. Phones and other end
        devices that announce themselves over CDP/LLDP get their names."""
        devices = self.map.devices
        network_macs, end_devices = set(), {}  # end_devices: (switch key, port key) -> Neighbor
        for key, tables in self.tables.items():
            if devices[key].kind in NETWORK_KINDS:
                network_macs |= tables.own_macs
            for neighbor in tables.neighbors:
                kind = collect.classify(capabilities=neighbor.capabilities, platform=neighbor.platform)
                if kind in END_DEVICE_KINDS:
                    end_devices[(key, port_key(neighbor.local_port))] = neighbor
                elif neighbor.chassis_mac and kind in NETWORK_KINDS:
                    network_macs.add(neighbor.chassis_mac)
        arp_table = {}
        for tables in self.tables.values():
            for mac, addresses in tables.arp.items():
                arp_table.setdefault(mac, addresses[0])

        candidates = {}  # MAC -> [(switch key, port name, vlan, MACs on that port)]
        for key, tables in self.tables.items():
            uplinks = self.uplink_ports(key, tables, network_macs)
            learned = [(mac, short_port(tables.interfaces.get(tables.lag_parents.get(if_index, if_index),
                                                              str(if_index))), vlan)
                       for mac, if_index, vlan in tables.fdb]
            port_counts = {}
            for _, port, _ in learned:
                port_counts[port] = port_counts.get(port, 0) + 1
            for mac, port, vlan in learned:
                if port_key(port) not in uplinks and mac not in network_macs and mac not in tables.own_macs:
                    candidates.setdefault(mac, []).append((key, port, vlan, port_counts[port]))

        hosts, named = [], set()
        for mac, places in candidates.items():
            key, port, vlan, _ = min(places, key=lambda place: place[3])  # Nearest: the port with fewest MACs
            host = Host(mac=mac, device=key, port=port, ip=arp_table.get(mac, ""), vendor=vendor(mac), vlan=vlan)
            neighbor = end_devices.get((key, port_key(port)))
            if neighbor is not None and end_device_mac(neighbor) == mac:
                host.name, host.platform = neighbor.name, neighbor.platform
                host.ip = host.ip or neighbor.address
                named.add((key, port_key(port)))
            hosts.append(host)
        for (key, port), neighbor in end_devices.items():
            if (key, port) not in named:  # Its MAC wasn't in the table (or the table couldn't be read)
                mac = end_device_mac(neighbor)
                hosts.append(Host(mac=mac, device=key, port=short_port(neighbor.local_port), ip=neighbor.address,
                                  vendor=vendor(mac) if mac else "", name=neighbor.name, platform=neighbor.platform))
        hosts.sort(key=lambda host: (devices[host.device].label.lower(), port_key(host.port), host.mac))
        self.map.hosts = hosts

    def uplink_ports(self, key, tables, network_macs):
        """Ports (as port keys) leading to other network devices, whose MACs belong further along."""
        uplinks = set()
        for link in self.map.links_of(key):
            other = self.map.devices.get(link.other(key))
            if other is not None and other.kind not in (PHONE, HOST, AP, SERVER):
                uplinks.add(port_key(link.port_on(key)))
        members = {}
        for member, parent in tables.lag_parents.items():
            members.setdefault(parent, []).append(member)
        for parent, member_list in members.items():  # A port-channel is an uplink when its members are
            if any(port_key(tables.interfaces.get(member, "")) in uplinks for member in member_list):
                uplinks.add(port_key(tables.interfaces.get(parent, str(parent))))
        for mac, if_index, _ in tables.fdb:
            if mac in network_macs and mac not in tables.own_macs:
                port = tables.interfaces.get(tables.lag_parents.get(if_index, if_index), str(if_index))
                uplinks.add(port_key(port))
        return uplinks


@dataclass
class Check:
    """What asking one device (one added by hand) found: whether it answers SNMP with one of the communities, or
    only ping, or neither."""
    source: str  # SNMP, NO_SNMP or UNREACHABLE
    error: str = ""
    info: collect.SystemInfo = None
    community: str = ""  # The community string it answered to

    def apply(self, device):
        """Note it on the device, as a crawl would: its sysName (if it hasn't a name), description and kind."""
        device.source, device.error = self.source, self.error
        if self.info is not None:
            device.name = device.name or self.info.name
            device.sys_descr, device.sys_object_id = self.info.descr, self.info.object_id
            if device.kind == UNKNOWN:
                device.kind = collect.classify(self.info.object_id, self.info.descr, platform=device.platform)


def check_device(settings, address, client_factory=SnmpClient, pinger=None):
    """Ask one device for its system details with the communities a crawl would try; ping it if none answers.
    Returns a Check. Reads nothing else (no neighbors or tables): Crawl from Here does that."""
    crawler = Crawler(settings, client_factory=client_factory, pinger=pinger)
    _, info, community = crawler.connect(address)
    if info is not None:
        return Check(SNMP, "", info, community)
    return Check(NO_SNMP, PINGS_NO_SNMP) if crawler.pinger(address) else Check(UNREACHABLE, NO_ANSWER)


def timing_summary(timings):
    """ " in 14.2 s (slowest: MAC tables 9.8 s)" for the crawl log."""
    total = timings.get("total")
    if total is None:
        return ""
    steps = {step: seconds for step, seconds in timings.items() if step != "total"}
    text = f" in {total:.1f} s"
    if steps and total >= 1:
        slowest = max(steps, key=steps.get)
        text += f" (slowest: {slowest} {steps[slowest]:.1f} s)"
    return text


def collect_summary(device, tables):
    """What was read from a device, for the crawl log."""
    parts = [KIND_NAMES.get(device.kind, device.kind).lower(), f"{len(tables.neighbors)} neighbors"]
    if tables.fdb:
        parts.append(f"{len(tables.fdb)} MAC table entries")
    if tables.routes:
        parts.append(f"{len(tables.routes)} routes")
    return ", ".join(parts)


def prefix_length(mask):
    try:
        return ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return 32


def end_device_mac(neighbor):
    """A phone's MAC: LLDP's chassis ID, or from a Cisco phone's CDP name (SEP followed by its MAC)."""
    if neighbor.chassis_mac:
        return neighbor.chassis_mac
    match = re.match(r"SEP([0-9A-Fa-f]{12})", neighbor.name)
    return format_mac(match.group(1)) if match else ""


def _ping(address):
    from ..sweep import ping_once  # Windows ICMP; imported here so the crawler itself runs anywhere
    try:
        return ping_once(address, 1000) is not None
    except OSError:
        return False


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")
