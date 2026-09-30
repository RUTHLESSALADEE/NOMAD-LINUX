"""Crawling the network: ask each device for its CDP/LLDP neighbors over SNMP, then ask those neighbors, and so on.

Starts from one or more seed addresses and stays within the scope (subnets, hops and a device limit). Devices that
don't answer SNMP are pinged, so the map can tell "wrong community or ACL" from "can't be reached". Switches' MAC
tables (per VLAN on Cisco IOS, where each VLAN has its own table) and routers' ARP tables put hosts on the edge
ports they're plugged into.
"""
import datetime
import ipaddress
import logging
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from ..oui import format_mac, vendor
from ..snmp import V2C, SnmpClient, SnmpError, parse_oid
from . import collect
from .model import AP, HOST, NETWORK_KINDS, NO_SNMP, PHONE, SNMP, UNKNOWN, UNREACHABLE, Device, Host, \
    Link, NetworkMap, display_name, normalize_name, port_key, short_port

log = logging.getLogger(__name__)

WORKERS = 8
STOPPED = "Stopped"
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
    workers: int = WORKERS


def parse_networks(lines):
    """Subnets from text, one per line (or separated by commas or spaces). Raises ValueError naming a bad one."""
    networks = []
    for item in " ".join(lines).replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"'{item}' isn't a subnet or address.") from None
    return networks


class Crawler:
    def __init__(self, settings, client_factory=SnmpClient, pinger=None, should_stop=lambda: False,
                 progress=lambda message: None):
        self.settings = settings
        self.client_factory = client_factory
        self.pinger = pinger or _ping
        self.should_stop = should_stop
        self.progress = progress
        self.scope = parse_networks(settings.scope)
        self.overrides = sorted(((ipaddress.ip_network(subnet, strict=False), community)
                                 for subnet, community in settings.overrides),
                                key=lambda item: item[0].prefixlen, reverse=True)
        self.map = NetworkMap(seeds=list(settings.seeds))
        self.aliases = {}  # Address or normalized name -> device key
        self.tables = {}  # Device key -> DeviceTables, for placing hosts at the end
        self.asked = set()  # Addresses already asked (or queued)
        self.capabilities = {}  # Device key -> what its neighbors' CDP/LLDP say it is (router, switch...)

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
        ip = ipaddress.ip_address(address)
        chosen = [community for network, community in self.overrides if ip.version == network.version
                  and ip in network]
        return list(dict.fromkeys(chosen + list(self.settings.communities)))

    # ----------------------------------------------------------------- Crawl

    def run(self):
        self.map.started = _now()
        queue = []
        for seed in self.settings.seeds:
            if seed not in self.asked:
                self.asked.add(seed)
                queue.append((seed, 0, None))
        done = 0
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
                    self.progress(f"Read {done} of {done + len(queue) + len(running)} devices "
                                  f"({len(self.map.devices)} found so far): {address}")
            self.map.stopped = self.should_stop()
            if self.map.stopped:
                for future in running:
                    future.cancel()
        self.place_hosts()
        self.map.finished = _now()
        return self.map

    def visit(self, address):
        """Read one device. Returns (DeviceTables or None, error). Runs on a worker thread."""
        client = info = None
        for community in self.communities_for(address):
            if self.should_stop():
                return None, STOPPED
            try:
                client = self.client_factory(address, community, self.settings.version,
                                             timeout=self.settings.timeout, retries=self.settings.retries)
                info = collect.system_info(client.get([parse_oid(collect.SYS_DESCR), parse_oid(collect.SYS_OBJECT_ID),
                                                       parse_oid(collect.SYS_NAME)]))
                break
            except (SnmpError, OSError) as problem:
                log.debug("SNMP to %s: %s", address, problem)
                client = None
        if client is None:
            if self.pinger(address):
                return None, "Answers ping but not SNMP: check the community string and the device's SNMP ACL."
            return None, "No answer to SNMP or ping."
        tables = collect.DeviceTables(info=info)
        self.read_tables(client, tables, community)
        return tables, ""

    def walk(self, client, root, tables, what):
        try:
            return list(client.walk(parse_oid(root), should_stop=self.should_stop))
        except (SnmpError, OSError) as problem:
            tables.warnings.append(f"{what}: {problem}")
            return []

    def read_tables(self, client, tables, community):
        tables.interfaces = collect.interface_names(self.walk(client, collect.IF_NAME, tables, "Interface names"),
                                                    self.walk(client, collect.IF_DESCR, tables, "Interfaces"))
        tables.addresses = collect.ip_addresses(self.walk(client, collect.IP_ADDR_ENTRY, tables, "IP addresses"))
        tables.neighbors = collect.cdp_neighbors(self.walk(client, collect.CDP_CACHE_ENTRY, tables, "CDP"),
                                                 tables.interfaces)
        lldp_rows = self.walk(client, collect.LLDP_REM_ENTRY, tables, "LLDP")
        if lldp_rows:
            local_ports = collect.lldp_local_ports(self.walk(client, collect.LLDP_LOC_PORT_ENTRY, tables, "LLDP"))
            addresses = collect.lldp_management_addresses(
                self.walk(client, collect.LLDP_REM_MAN_ADDR_IF_SUBTYPE, tables, "LLDP"))
            tables.neighbors += collect.lldp_neighbors(lldp_rows, local_ports, tables.interfaces, addresses)
        tables.arp = collect.arp(self.walk(client, collect.ARP_PHYS_ADDRESS, tables, "ARP"))
        if not self.settings.collect_hosts:
            return
        tables.own_macs = collect.own_macs(self.walk(client, collect.IF_PHYS_ADDRESS, tables, "Interfaces"))
        tables.lag_parents = collect.lag_parents(self.walk(client, collect.IF_STACK_STATUS, tables, "Port-channels"),
                                                 self.walk(client, collect.LAG_ATTACHED, tables, "Port-channels"))
        info = tables.info
        vlan_list = collect.vlans(self.walk(client, collect.VTP_VLAN_STATE, tables, "VLANs")) \
            if info.object_id.startswith(collect.CISCO + ".") else []
        if vlan_list and "nx-os" not in info.descr.lower():
            # Catalyst IOS keeps a MAC table per VLAN, read with community@vlan
            for vlan in vlan_list:
                if self.should_stop():
                    return
                try:
                    vlan_client = self.client_factory(client.host, f"{community}@{vlan}", self.settings.version,
                                                      timeout=self.settings.timeout, retries=self.settings.retries)
                    entries = list(vlan_client.walk(parse_oid(collect.FDB_ENTRY), should_stop=self.should_stop))
                    ports = list(vlan_client.walk(parse_oid(collect.BASE_PORT_IFINDEX), should_stop=self.should_stop))
                except (SnmpError, OSError):
                    continue  # A VLAN with no ports here doesn't answer on some models
                tables.fdb += collect.fdb(entries, ports, vlan)
        else:
            tables.fdb = collect.fdb(self.walk(client, collect.FDB_ENTRY, tables, "MAC table"),
                                     self.walk(client, collect.BASE_PORT_IFINDEX, tables, "MAC table"))

    # ----------------------------------------------------------------- Putting results on the map

    def find(self, address="", name=""):
        return self.aliases.get(address) or (self.aliases.get(normalize_name(name)) if name else None)

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
            device = self.add_device(key, mgmt_ip=address)
            if device.source != SNMP and error != STOPPED:
                device.source = NO_SNMP if error.startswith("Answers ping") else UNREACHABLE
                device.error = error
                device.hops = hops
            return []

        info = tables.info
        existing = self.find(address, info.name)
        if existing and self.map.devices[existing].source == SNMP:
            self.aliases[address] = existing
            return []  # Reached the same device at a second address
        if key is None:
            key = existing or normalize_name(info.name) or f"ip:{address}"
        elif existing and existing != key:
            self.merge(key, existing)  # Found under two names (a neighbor's view, and its own sysName)
            key = existing
        device = self.add_device(key, mgmt_ip=address)
        device.name = info.name or device.name  # Its own sysName over how a neighbor wrote it
        device.source, device.error, device.hops = SNMP, "", hops
        device.sys_descr, device.sys_object_id = info.descr, info.object_id
        device.addresses = [ip for ip, _, _ in tables.addresses] or [address]
        for ip in device.addresses:
            self.aliases.setdefault(ip, key)
        device.kind = collect.classify(info.object_id, info.descr, frozenset(self.capabilities.get(key, ())),
                                       device.platform)
        if tables.warnings:
            log.info("%s: %s", device.label, "; ".join(tables.warnings))
        self.tables[key] = tables

        next_visits = []
        for neighbor in tables.neighbors:
            kind = collect.classify(capabilities=neighbor.capabilities, platform=neighbor.platform)
            if kind in END_DEVICE_KINDS:
                continue  # Placed on its port as a host at the end
            other = self.find(neighbor.address, neighbor.name) or normalize_name(neighbor.name)
            if other == key:
                continue
            known = other in self.map.devices
            other_device = self.add_device(other, name=display_name(neighbor.name), mgmt_ip=neighbor.address,
                                           platform=neighbor.platform)
            if not known:
                other_device.kind, other_device.hops = kind, hops + 1
            self.capabilities.setdefault(other, set()).update(neighbor.capabilities)
            self.map.add_link(Link(key, short_port(neighbor.local_port), other, short_port(neighbor.port),
                                   [neighbor.protocol]))
            address_ok = neighbor.address and neighbor.address not in self.asked and self.in_scope(neighbor.address)
            if other_device.source == SNMP or not address_ok or hops + 1 > self.settings.max_hops:
                continue
            if kind not in NETWORK_KINDS | {UNKNOWN}:
                continue  # Access points and the like: shown, not asked
            if len(self.asked) >= self.settings.max_devices:
                continue
            self.asked.add(neighbor.address)
            next_visits.append((neighbor.address, hops + 1, other))
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
            if other is not None and other.kind not in (PHONE, HOST, AP):
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
