import datetime

from netmap_fakes import ACC1_MAC, CISCO_SWITCH, Device, build_network

from nomad.netmap import watch
from nomad.netmap.crawl import CrawlSettings, Crawler

NEW_PC = "3C-52-82-00-00-77"
NOW = datetime.datetime(2026, 10, 2, 9, 30)
OPTIONS = watch.WatchOptions(overrides=[("10.0.0.12/32", "secret")])


def mapped(network):
    settings = CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")], trace=False)
    network_map = Crawler(settings, client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    watch.mark_hosts_seen(network_map, NOW - datetime.timedelta(days=1))
    return network_map


def poll(network, network_map, baseline):
    signatures = watch.read_signatures(watch.switches(network_map), OPTIONS, network.client)
    return baseline.changed(network_map, signatures)


def refresh(network, network_map, keys):
    seeds = [network_map.devices[key].mgmt_ip for key in keys]
    crawled = watch.crawl_from(network_map, OPTIONS, seeds, network.client, network.ping, network.echo)
    return watch.apply_refresh(network_map, crawled, NOW, by="tester")


def plug_in_switch(network):
    """A new access switch on acc1 Gi1/0/7, with a PC on it."""
    acc1 = network.devices["10.0.0.11"]
    acc1.cdp(7, 3, "acc3.corp.example(FOC333)", "GigabitEthernet1/0/1", "10.0.0.13", "cisco C9200-24P", 0x29)
    acc3 = network.add("10.0.0.13", Device("acc3.corp.example", "Cisco IOS Software, Catalyst", CISCO_SWITCH))
    acc3.interface(1, "GigabitEthernet1/0/1", "00-1A-2B-00-00-13")
    acc3.interface(3, "GigabitEthernet1/0/3", "00-1A-2B-00-00-13")
    acc3.address("10.0.0.13", 1)
    acc3.cdp(1, 1, "acc1.corp.example", "GigabitEthernet1/0/7", "10.0.0.11", "cisco C9300-48P", 0x29)
    acc3.vlan(10)
    acc3.learned(ACC1_MAC, 1, 1, vlan=10)
    acc3.learned(NEW_PC, 3, 3, vlan=10)
    return acc3


def test_first_poll_changes_nothing_on_a_fresh_map():
    network = build_network()
    network_map = mapped(network)
    baseline = watch.NeighborBaseline()
    assert poll(network, network_map, baseline) == []
    assert poll(network, network_map, baseline) == []


def test_new_switch_found_crawled_and_noted():
    network = build_network()
    network_map = mapped(network)
    baseline = watch.NeighborBaseline()
    poll(network, network_map, baseline)
    plug_in_switch(network)
    assert poll(network, network_map, baseline) == ["acc1"]
    result = refresh(network, network_map, ["acc1"])
    assert result.devices == ["acc3"]
    assert network_map.news["device:acc3"]["where"] == "acc1.corp.example Gi1/0/7"
    assert network_map.news["device:acc3"]["by"] == "tester"
    assert NEW_PC in result.hosts and f"host:{NEW_PC}" in network_map.news
    assert any(line.startswith("New device: acc3") for line in result.lines)
    assert poll(network, network_map, baseline) == []  # Seen now


def test_deleted_device_stays_off():
    network = build_network()
    network_map = mapped(network)
    network_map.remove_devices(["acc2"], remember=True)
    baseline = watch.NeighborBaseline()
    assert "core" not in poll(network, network_map, baseline)  # acc2 is deleted, not new
    result = refresh(network, network_map, ["core"])
    assert "acc2" not in network_map.devices and not result.devices


def test_host_back_within_days_is_not_news():
    network = build_network()
    network_map = mapped(network)
    network_map.host_seen[NEW_PC] = (NOW - datetime.timedelta(days=3)).date().isoformat()
    plug_in_switch(network)
    result = refresh(network, network_map, ["acc1"])
    assert NEW_PC not in result.hosts
    assert network_map.host_seen[NEW_PC] == NOW.date().isoformat()


def test_acknowledge_and_prune():
    network = build_network()
    network_map = mapped(network)
    plug_in_switch(network)
    refresh(network, network_map, ["acc1"])
    assert watch.acknowledge(network_map, [f"host:{NEW_PC}"]) == [f"host:{NEW_PC}"]
    assert set(network_map.news) == {"device:acc3"}
    network_map.remove_devices(["acc3"])
    assert network_map.news == {}


def test_news_saved_with_the_map():
    from nomad.netmap.model import NetworkMap
    network = build_network()
    network_map = mapped(network)
    plug_in_switch(network)
    refresh(network, network_map, ["acc1"])
    loaded = NetworkMap.from_json(network_map.to_json())
    assert loaded.news == network_map.news and loaded.host_seen == network_map.host_seen


def test_watch_service_engine_adds_to_the_tribe_map(tmp_path):
    import threading
    from test_ipam_server import key_for
    from nomad.ipam.client import TeamClient
    from nomad.ipam.server import IpamServer
    from nomad.netmap import watch_service
    from nomad.netmap.tribe import TribeMaps

    server = IpamServer(tmp_path / "server", host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    try:
        key = key_for(server)
        alice = TribeMaps(key.server_id, TeamClient(key, user="alice"), tmp_path / "alice.db")
        network = build_network()
        map_id = alice.create("HQ", mapped(network), {"scope": []},
                              {"communities": ["public"], "overrides": [["10.0.0.12/32", "secret"]]})
        assert alice.lease(map_id, "alice-gui")["yours"]  # NOMAD open on Alice's laptop is watching

        now = [1000.0]
        service_maps = TribeMaps(key.server_id, TeamClient(key, user="svc", computer="WS9"), tmp_path / "svc.db")
        engine = watch_service.WatchEngine(
            service_maps, [map_id], "ws9-service", client_factory=network.client, clock=lambda: now[0],
            listen=False, crawl=lambda network_map, options, seeds, should_stop: watch.crawl_from(
                network_map, options, seeds, network.client, network.ping, network.echo, should_stop))
        engine.step()  # Syncs, takes over the watching from NOMAD, asks the switches for their neighbors
        assert engine.watched[map_id].active
        assert not alice.lease(map_id, "alice-gui")["yours"]
        plug_in_switch(network)
        now[0] += watch.NEIGHBOR_INTERVAL + 1
        engine.step()
        alice.sync()
        found, _ = alice.load(map_id)
        assert "acc3" in found.devices and "device:acc3" in found.news
        assert found.news["device:acc3"]["by"].endswith("(NOMAD Map Watcher)")

        engine.queue.delay = 0
        engine.on_trap(type("Trap", (), {"trap_oid": (1, 3, 6, 1, 6, 3, 1, 1, 5, 4), "agent": "10.0.0.13"})(),
                       "10.0.0.13")
        engine.step()  # Reads acc3 again, nothing new
        alice.sync()
        assert alice.load(map_id)[0].news == found.news
    finally:
        server.stop()
        thread.join(10)


def test_watch_service_config_round_trip():
    from nomad.ipam.client import TeamKey
    from nomad.netmap import watch_service
    key = TeamKey.from_dict({"format": 1, "server_id": "abc", "hosts": ["srv"], "port": 8443, "fingerprint": "f" * 64,
                             "secret": "s3cret"})
    config = watch_service.make_config(key, [3, "4"], protect=lambda text: text[::-1])
    assert config["maps"] == [3, 4] and config["secret"] == "terc3s"
    assert watch_service.key_from_config(config, unprotect=lambda text: text[::-1]).secret == "s3cret"
