from netmap_fakes import CISCO_ROUTER, Device, build_network, number

from nomad.icmp import IP_DEST_HOST_UNREACHABLE, IP_REQ_TIMED_OUT, IP_SUCCESS, IP_TTL_EXPIRED_TRANSIT, EchoReply
from nomad.netmap import collect, l3
from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.model import NetworkMap
from nomad.snmp import IP_ADDRESS, Value


def crawl(network=None, **options):
    network = network or build_network()
    settings = CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")], **options)
    return Crawler(settings, client_factory=network.client, pinger=network.ping, echo=network.echo).run()


def test_routes_from_cidr_table():
    core = build_network().devices["10.0.0.1"]
    routes = collect.routes(sorted(core.mib.items()))
    assert routes == [
        ("0.0.0.0/0", "10.0.0.5", 51, "static"),
        ("10.0.0.0/24", "", 51, "connected"),
        ("10.10.0.0/24", "", 50, "connected"),
        ("10.50.0.0/16", "10.0.0.254", 51, "static"),
        ("10.60.0.0/16", "10.0.0.253", 51, "ospf"),
    ]  # The null route to 10.66.0.0/16 is left out


def test_routes_from_old_route_table():
    device = Device("old-rtr", "", CISCO_ROUTER)
    for column, value in ((2, number(3)), (7, Value(IP_ADDRESS, "10.1.1.254")), (8, number(4)), (9, number(3)),
                          (11, Value(IP_ADDRESS, "255.255.0.0"))):
        device.set(collect.IP_ROUTE_ENTRY, column, (172, 20, 0, 0), value)
    assert collect.routes([], sorted(device.mib.items())) == [("172.20.0.0/16", "10.1.1.254", 3, "static")]


def test_crawl_keeps_interfaces_and_routes():
    core = crawl().devices["core"]
    assert ["10.0.0.1", 24, "Vlan1"] in core.interfaces_l3
    assert ["10.255.0.1", 32, "Loopback0"] in core.interfaces_l3
    assert ["10.50.0.0/16", "10.0.0.254", "Vlan1", "static"] in core.routes


def test_trace_targets_and_traces():
    network = build_network()
    network_map = crawl(network)
    targets = [address for address, _ in l3.trace_targets(network_map)]
    assert targets == ["10.0.0.254", "10.0.0.253", "10.50.0.1"]
    traces = {item.target: item for item in network_map.traces}
    assert traces["10.0.0.254"].hops == ["10.0.0.1", "10.0.0.254"] and traces["10.0.0.254"].reached
    assert traces["10.50.0.1"].hops == ["10.0.0.1", "10.0.0.254", "", "10.99.0.1"]
    assert not traces["10.50.0.1"].reached
    assert "Static route 10.50.0.0/16" in traces["10.50.0.1"].reason


def test_no_traces_when_turned_off():
    network = build_network()
    assert crawl(network, trace=False).traces == []
    assert network.traced == []


def test_trace_stops_on_silence_and_on_unreachable():
    silent = lambda address, ttl: EchoReply(IP_TTL_EXPIRED_TRANSIT, "10.0.0.1") if ttl == 1 \
        else EchoReply(IP_REQ_TIMED_OUT)
    assert l3.trace("10.9.9.9", silent) == (["10.0.0.1"], False)
    blocked = lambda address, ttl: EchoReply(IP_TTL_EXPIRED_TRANSIT, "10.0.0.1") if ttl == 1 \
        else EchoReply(IP_DEST_HOST_UNREACHABLE, "10.0.0.2")
    assert l3.trace("10.9.9.9", blocked) == (["10.0.0.1", "10.0.0.2"], False)
    reached = lambda address, ttl: EchoReply(IP_SUCCESS, address, 1)
    assert l3.trace("10.9.9.9", reached) == (["10.9.9.9"], True)


def test_graph_joins_routers_through_subnets():
    network_map = crawl()
    nodes, links = l3.l3_graph(network_map)
    subnets = sorted(key for key, node in nodes.items() if node.kind == l3.SUBNET)
    assert subnets == ["net:10.0.0.0/24", "net:10.10.0.0/24", "net:192.0.2.0/30"]  # No /32 loopback
    pairs = {(link.a, link.b) for link in links}
    assert ("core", "net:10.0.0.0/24") in pairs and ("pa-fw1", "net:10.0.0.0/24") in pairs
    assert ("rtr1", "net:10.0.0.0/24") in pairs  # Didn't answer SNMP, placed by its address
    assert ("hop:10.0.0.253", "net:10.0.0.0/24") in pairs  # A next hop that isn't on the map
    traced = {(link.a, link.b) for link in links if link.protocols == ["icmp"]}
    assert (l3.SELF, "core") in traced
    assert ("core", "rtr1") not in traced  # Both on 10.0.0.0/24 already
    assert ("rtr1", "star:10.50.0.1:3") in traced and ("star:10.50.0.1:3", "hop:10.99.0.1") in traced
    labels = {link.a_port for link in links if link.a == "core" and link.b == "net:10.0.0.0/24"}
    assert labels == {"Vl1 10.0.0.1"}


def test_subnet_details_list_devices_and_hosts():
    members, hosts = l3.subnet_details(crawl(), "10.10.0.0/24")
    assert members == [("core.corp.example", "Vl10", "10.10.0.1")]
    assert {host.ip for host in hosts} == {"10.10.0.21", "10.10.0.22", "10.10.0.30"}


def test_old_maps_without_l3_still_load():
    network_map = NetworkMap.from_json('{"devices": [{"key": "a", "name": "a"}], "links": []}')
    assert network_map.traces == [] and network_map.devices["a"].routes == []
    assert l3.l3_graph(network_map) == ({}, [])
