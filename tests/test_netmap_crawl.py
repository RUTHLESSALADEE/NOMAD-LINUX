import re
import threading

from netmap_fakes import (ACC1_MAC, CORE_MAC, FW_MAC, LAB_MACS, PC1_MAC, PHONE_MAC, PRINTER_MAC, FakeAgentClient,
                          build_network)

from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.model import FIREWALL, NO_SNMP, ROUTER, SNMP, SWITCH, UNREACHABLE


def crawl(network, **options):
    settings = CrawlSettings(seeds=options.pop("seeds", ["10.0.0.1"]),
                             overrides=options.pop("overrides", [("10.0.0.12/32", "secret")]), **options)
    return Crawler(settings, client_factory=network.client, pinger=network.ping,
                   echo=network.echo).run()


def test_crawl_finds_every_device_and_link():
    network_map = crawl(build_network())
    devices = network_map.devices
    assert set(devices) == {"core", "acc1", "acc2", "rtr1", "pa-fw1"}
    assert {key: devices[key].source for key in devices} == {
        "core": SNMP, "acc1": SNMP, "acc2": SNMP, "rtr1": NO_SNMP, "pa-fw1": SNMP}
    assert devices["core"].kind == SWITCH
    assert devices["rtr1"].kind == ROUTER
    assert devices["pa-fw1"].kind == FIREWALL
    assert devices["acc2"].hops == 1
    links = {(link.a, link.a_port, link.b, link.b_port, tuple(link.protocols)) for link in network_map.links}
    assert links == {
        ("core", "Te1/0/1", "acc1", "Te1/1/1", ("cdp",)),
        ("core", "Te1/0/2", "acc2", "Eth1/49", ("cdp",)),
        ("core", "Gi1/0/48", "rtr1", "Gi0/0/0", ("cdp",)),
        ("core", "Te1/0/3", "pa-fw1", "Eth1/1", ("lldp",)),
    }
    assert "10.10.0.1" in devices["core"].addresses


def test_hosts_on_edge_ports_only():
    network_map = crawl(build_network())
    hosts = {host.mac: host for host in network_map.hosts}
    assert hosts[PC1_MAC].device == "acc1" and hosts[PC1_MAC].port == "Gi1/0/5"  # Not the core's uplink
    assert hosts[PC1_MAC].ip == "10.10.0.21" and hosts[PC1_MAC].vlan == 10
    assert hosts[PHONE_MAC].name == "SEP00AABBCCDDEE" and hosts[PHONE_MAC].port == "Gi1/0/5"
    assert hosts[PRINTER_MAC].port == "Gi1/0/7"
    assert all(hosts[mac].port == "Eth1/10" for mac in LAB_MACS)
    assert {hosts[mac].vlan for mac in LAB_MACS} == {30, 31}  # From the NX-OS switch's Q-BRIDGE table
    for mac in (CORE_MAC, ACC1_MAC, FW_MAC):  # Network devices' own MACs, and anything on an uplink
        assert mac not in hosts
    assert "SEP00AABBCCDDEE" not in network_map.devices  # Phones are hosts, not map devices


def test_per_vlan_community_used_on_catalyst_only():
    network = build_network()
    crawl(network)
    communities = {community for host, community in network.requests}
    assert {"public@10", "public@1"} <= communities
    acc1 = {community for host, community in network.requests if host == "10.0.0.11"}
    assert not acc1 & {"public@20", "public@30", "public@40"}  # VLANs none of its ports use
    assert not any(community.startswith("secret@") for community in communities)  # acc2 is NX-OS


def test_override_community_tried_first_and_default_for_the_rest():
    network = build_network()
    crawl(network)
    acc2_communities = [community for host, community in network.requests if host == "10.0.0.12"]
    assert acc2_communities[0] == "secret"
    assert "public" not in acc2_communities


def test_scope_and_hops_limit_the_crawl():
    network_map = crawl(build_network(), scope=["10.0.0.0/30"])  # Only the core itself
    assert network_map.devices["core"].source == SNMP
    assert all(device.source != SNMP for key, device in network_map.devices.items() if key != "core")
    assert len(network_map.links) == 4  # Neighbors still shown

    network_map = crawl(build_network(), max_hops=0)
    assert [key for key, device in network_map.devices.items() if device.source == SNMP] == ["core"]


def test_unreachable_seed():
    network = build_network()
    network_map = crawl(network, seeds=["10.9.9.9"])
    assert list(network_map.devices) == ["ip:10.9.9.9"]
    assert network_map.devices["ip:10.9.9.9"].source == UNREACHABLE


def test_same_device_at_two_addresses_is_one_device():
    network = build_network()
    network.devices["10.10.0.1"] = network.devices["10.0.0.1"]
    network_map = crawl(network, seeds=["10.0.0.1", "10.10.0.1"])
    assert sorted(network_map.devices) == ["acc1", "acc2", "pa-fw1", "rtr1", "core"] or \
        set(network_map.devices) == {"core", "acc1", "acc2", "pa-fw1", "rtr1"}
    assert len(network_map.links) == 4


def test_stop_returns_a_partial_map():
    stop = threading.Event()
    network = build_network()
    original = network.client

    def client(host, *args, **kwargs):
        stop.set()  # Stop as soon as the first device is asked
        return original(host, *args, **kwargs)
    network.client = client
    settings = CrawlSettings(seeds=["10.0.0.1"])
    network_map = Crawler(settings, client_factory=network.client, pinger=network.ping,
                          echo=network.echo, should_stop=stop.is_set).run()
    assert network_map.stopped
    assert network_map.finished


def test_crawl_over_real_udp():
    """One device served over UDP by the SNMP tests' agent, to check the crawler with the real client."""
    agent = FakeAgentClient(build_network().devices["10.0.0.1"])
    try:
        network_map = Crawler(CrawlSettings(seeds=["127.0.0.1"], scope=["127.0.0.1/32"], timeout=500, retries=0,
                                            trace=False),
                              client_factory=agent.factory, pinger=lambda address: False).run()
    finally:
        agent.close()
    core = network_map.devices["core"]
    assert core.source == SNMP and core.kind == SWITCH
    assert len(network_map.links) == 4


def crawl_with_events(**options):
    network = build_network()
    events = []
    settings = CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")], **options)
    network_map = Crawler(settings, client_factory=network.client, pinger=network.ping, echo=network.echo,
                          events=lambda kind, *details: events.append((kind, *details))).run()
    return network_map, events


def test_progress_events():
    network_map, events = crawl_with_events()
    started = [event[1] for event in events if event[0] == "started"]
    finished = {}
    for event in events:
        if event[0] == "finished":
            finished.setdefault(event[1], event[2])  # rtr1 finishes again after its traceroute
    assert set(started) == set(finished)  # Everything started also finished
    assert finished["10.0.0.1"] == SNMP and finished["10.0.0.254"] == NO_SNMP
    assert finished["10.50.0.1"] == "traced"
    assert ("finished", "10.0.0.254", "traced") in events
    counts = [event[1] for event in events if event[0] == "counts"][-1]
    assert counts["read"] == 4 and counts["no_snmp"] == 1 and counts["reading"] == 0 and counts["queued"] == 0
    steps = {event[2] for event in events if event[0] == "step" and event[1] == "10.0.0.11"}
    assert {"Trying community 1 of 1", "CDP", "MAC tables: 2 of 2 VLANs read"} <= steps
    log_lines = [event[1] for event in events if event[0] == "log"]
    assert any(line.startswith("Found acc1.corp.example (10.0.0.11) through CDP on core.corp.example Te1/0/1")
               for line in log_lines)
    assert any(re.match(r"Read core\.corp\.example \(10\.0\.0\.1\) in \d+\.\d s.*: switch, 4 neighbors", line)
               for line in log_lines)
    assert "acc1.corp.example: MAC tables for the 2 of 5 VLANs its ports use (1, 10)" in log_lines
    assert any("no answer with community 1 of 1" in line for line in log_lines)  # rtr1
    assert not any("secret" in line or "public" in line for line in log_lines)  # Communities stay out of the log
    snapshots = [event[1] for event in events if event[0] == "map"]
    assert snapshots and set(snapshots[-1].devices) == set(network_map.devices)
    assert snapshots[-1].devices["core"] is not network_map.devices["core"]  # A copy, safe to draw meanwhile


def test_log_says_why_devices_are_not_asked():
    _, events = crawl_with_events(scope=["10.0.0.0/30"])
    log_lines = [event[1] for event in events if event[0] == "log"]
    assert "Not asking acc2 (10.0.0.12): it's outside the scope" in log_lines
    _, events = crawl_with_events(max_devices=2)
    assert any(event[1].startswith("Reached the limit of 2 devices") for event in events if event[0] == "log")


def test_crawl_from_here_adds_to_a_map():
    from nomad.netmap.model import Host
    first = crawl(build_network(), scope=["10.0.0.0/30"])  # Only the core read; the rest seen as neighbors
    assert first.devices["acc1"].source != SNMP
    first.hosts.append(Host(mac="", device="acc1", port="Gi1/0/20", name="old-printer", manual=True))
    core_hosts = [host for host in first.hosts if host.device == "core"]

    network = build_network()
    settings = CrawlSettings(seeds=["10.0.0.11"], overrides=[("10.0.0.12/32", "secret")], trace=False)
    newer = Crawler(settings, client_factory=network.client, pinger=network.ping, echo=network.echo,
                    known=first).run()
    assert not any(host == "10.0.0.1" for host, _ in network.requests)  # The core was read already
    assert newer.devices["acc1"].source == SNMP and "core" in newer.devices  # Same key, not a second core

    links_before = len(first.links)
    added, read = first.merge_crawl(newer)
    assert read == {"acc1"} and added == []  # acc1 was already on the map, as a neighbor
    assert first.devices["acc1"].source == SNMP and first.devices["core"].source == SNMP
    assert len(first.links) == links_before  # The core-acc1 link, seen from both ends, is one link
    acc1_hosts = {host.port for host in first.hosts if host.device == "acc1"}
    assert {"Gi1/0/5", "Gi1/0/7", "Gi1/0/20"} <= acc1_hosts  # Found ones, and the one added by hand
    assert [host for host in first.hosts if host.device == "core"] == core_hosts


def test_preview_leaves_the_map_alone():
    first = crawl(build_network(), scope=["10.0.0.0/30"])
    network = build_network()
    newer = Crawler(CrawlSettings(seeds=["10.0.0.11"], trace=False), client_factory=network.client,
                    pinger=network.ping, echo=network.echo, known=first).run()
    sources = {key: device.source for key, device in first.devices.items()}
    preview = first.preview_with(newer)
    assert preview.devices["acc1"].source == SNMP
    assert {key: device.source for key, device in first.devices.items()} == sources
