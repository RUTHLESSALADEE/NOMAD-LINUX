import datetime

from netmap_fakes import ACC1_MAC, CISCO_ROUTER, CISCO_SWITCH, Device, build_network

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
        network = renumbered_network()
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

        renumber_acc1(network)  # The service follows acc1 to its new address, and shares it
        now[0] += watch.NEIGHBOR_INTERVAL + 1
        engine.step()
        alice.sync()
        assert alice.load(map_id)[0].devices["acc1"].mgmt_ip == "10.18.0.11"

        assert alice.load(map_id)[0].devices["rtr1"].source != "snmp"
        rtr1_answers(network)  # SNMP turned on on rtr1: the service's re-check finds it and reads it
        now[0] += watch.RECHECK_INTERVAL + 1
        engine.step()
        alice.sync()
        assert alice.load(map_id)[0].devices["rtr1"].source == "snmp"
    finally:
        server.stop()
        thread.join(10)


def test_watch_service_config_round_trip():
    from nomad.ipam.client import TeamKey
    from nomad.netmap import watch_service
    key = TeamKey.from_dict({"format": 1, "server_id": "abc", "hosts": ["srv"], "port": 8443, "fingerprint": "f" * 64,
                             "secret": "s3cret"})
    config = watch_service.make_config(key, [3, "4"], protect=lambda text: text[::-1],
                                       timers={"recheck_interval": 900, "trigger_delay": 10})
    assert config["maps"] == [3, 4] and config["secret"] == "terc3s"
    assert watch_service.config_timers(config) == {"neighbor_interval": 300, "host_interval": 3600,
                                                   "recheck_interval": 900, "trigger_delay": 10}
    older = {"neighbor_interval": 600, "host_interval": 7200}  # A service set up before the other two timers
    assert watch_service.config_timers(older) == {"neighbor_interval": 600, "host_interval": 7200,
                                                  "recheck_interval": 3600, "trigger_delay": 45}
    assert watch_service.key_from_config(config, unprotect=lambda text: text[::-1]).secret == "s3cret"


def renumbered_network():
    """acc1 also has 10.18.0.11; once mapped, its old management address goes away (the network is renumbered)."""
    network = build_network()
    network.devices["10.0.0.11"].address("10.18.0.11", 7)
    return network


def renumber_acc1(network):
    acc1 = network.devices.pop("10.0.0.11")
    network.pingable.discard("10.0.0.11")
    network.add("10.18.0.11", acc1)


def test_a_switch_that_moves_to_another_of_its_addresses_is_followed():
    network = renumbered_network()
    network_map = mapped(network)
    assert network_map.devices["acc1"].mgmt_ip == "10.0.0.11"
    renumber_acc1(network)
    targets = watch.switches(network_map)
    fallbacks = watch.fallback_addresses(network_map, targets, [])
    assert fallbacks["acc1"] == ["10.18.0.11"]
    signatures, moved = watch.read_switches(targets, fallbacks, OPTIONS, network.client)
    assert moved == {"acc1": ("10.0.0.11", "10.18.0.11")} and signatures["acc1"] is not None
    lines = watch.adopt_addresses(network_map, moved)
    assert network_map.devices["acc1"].mgmt_ip == "10.18.0.11"
    assert lines == ["acc1.corp.example doesn't answer at 10.0.0.11 any more but does at 10.18.0.11: that's its "
                     "management address now"]
    refresh(network, network_map, ["acc1"])  # Reading it again (Crawl from Here) works at its new address
    assert network_map.devices["acc1"].source == "snmp" and network_map.devices["acc1"].mgmt_ip == "10.18.0.11"


def test_addresses_outside_the_scope_or_corrected_by_hand_are_not_tried():
    network = renumbered_network()
    network_map = mapped(network)
    renumber_acc1(network)
    targets = watch.switches(network_map)
    assert "acc1" not in watch.fallback_addresses(network_map, targets, ["10.0.0.0/24"])
    network_map.devices["acc1"].correct("mgmt_ip", "10.0.0.111")
    assert "acc1" not in watch.fallback_addresses(network_map, watch.switches(network_map), [])
    assert watch.adopt_addresses(network_map, {"acc1": ("10.0.0.111", "10.18.0.11")}) == []
    assert network_map.devices["acc1"].mgmt_ip == "10.0.0.111"


def test_a_switch_that_is_simply_down_stays_where_it_was():
    network = renumbered_network()
    network_map = mapped(network)
    network.devices.pop("10.0.0.11")  # Off: no address answers
    targets = watch.switches(network_map)
    signatures, moved = watch.read_switches(targets, watch.fallback_addresses(network_map, targets, []), OPTIONS,
                                            network.client)
    assert signatures["acc1"] is None and moved == {}


def rtr1_answers(network, communities=("public",), v3_users=()):
    """rtr1 (which only pinged) has SNMP turned on."""
    return network.add("10.0.0.254", Device("rtr1.corp.example", "Cisco IOS Software, ISR", CISCO_ROUTER,
                                            communities=communities, v3_users=v3_users))


def test_timers_are_kept_within_limits():
    assert watch.timer_values() == {"neighbor_interval": 300, "host_interval": 3600, "recheck_interval": 3600,
                                    "trigger_delay": 45}
    assert watch.timer_values({"neighbor_interval": 5, "host_interval": "junk", "recheck_interval": 10 ** 9,
                               "trigger_delay": 0}) == {"neighbor_interval": 60, "host_interval": 3600,
                                                        "recheck_interval": 86400, "trigger_delay": 0}


def test_devices_that_dont_answer_snmp_are_asked_again():
    network = build_network()
    network_map = mapped(network)
    assert watch.unread_devices(network_map, []) == {"rtr1": "10.0.0.254"}
    assert watch.unread_devices(network_map, ["10.0.0.0/30"]) == {}  # Outside the scope
    assert watch.recheck(watch.unread_devices(network_map, []), OPTIONS, network.client) == {}
    rtr1_answers(network)
    found = watch.recheck(watch.unread_devices(network_map, []), OPTIONS, network.client)
    assert list(found) == ["rtr1"] and found["rtr1"][0] == "10.0.0.254" and found["rtr1"][1].community == "public"
    refresh(network, network_map, ["rtr1"])  # Read, as the watcher does next
    assert network_map.devices["rtr1"].source == "snmp" and watch.unread_devices(network_map, []) == {}


def test_a_device_set_up_with_an_snmpv3_user_answers_once_the_map_has_the_user():
    from dataclasses import replace
    from nomad.snmpv3 import V3User
    user = V3User("nomad", "sha", "authpass1", "aes128", "privpass1")
    network = build_network()
    network_map = mapped(network)
    rtr1_answers(network, communities=(), v3_users=(user,))
    targets = watch.unread_devices(network_map, [])
    assert watch.recheck(targets, OPTIONS, network.client) == {}  # The map only has community strings
    found = watch.recheck(targets, replace(OPTIONS, communities=[user, "public"]), network.client)
    assert found["rtr1"][1].community == user
    assert watch.credential_text(user) == "v3 user nomad (SHA-1, AES-128)"
    assert watch.credential_text("s3cret") == "a community string"  # Never the string itself


def test_a_seed_that_did_not_answer_is_folded_into_the_device_once_it_does():
    """As R3 in the lab: a seed that only pinged (keyed by its address) is the router a neighbor's CDP named."""
    network = build_network()
    network.pingable.add("10.0.0.250")
    settings = CrawlSettings(seeds=["10.0.0.1", "10.0.0.250"], overrides=[("10.0.0.12/32", "secret")], trace=False)
    network_map = Crawler(settings, client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    assert {"ip:10.0.0.250", "rtr1"} <= set(network_map.devices)  # Nothing yet says they're one device
    network_map.positions["ip:10.0.0.250"] = (500.0, 600.0)
    rtr1 = rtr1_answers(network)
    rtr1.address("10.0.0.254", 1)
    rtr1.address("10.0.0.250", 2)
    network.devices["10.0.0.250"] = rtr1
    found = watch.recheck(watch.unread_devices(network_map, []), OPTIONS, network.client)
    assert set(found) == {"ip:10.0.0.250", "rtr1"}
    result = refresh(network, network_map, list(found))
    assert "ip:10.0.0.250" not in network_map.devices and network_map.devices["rtr1"].source == "snmp"
    assert "rtr1" not in result.devices  # Not news: it was on the map already
