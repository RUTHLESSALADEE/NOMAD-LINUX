"""MAC Finder: reading what's typed, searching the map, asking the switches now, and the history of where MACs were."""
import pytest

from netmap_fakes import ACC1_MAC, CORE_MAC, LAB_MACS, PC1_MAC, PHONE_MAC, PRINTER_MAC, build_network

from nomad.netmap import macfind
from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.macfind import IP, LIVE, MAC_FULL, MAC_PART, NAME, Location, Locator, parse_list, parse_queries, \
    parse_query, search_map
from nomad.netmap.sightings import SightingLog

PC1_DIGITS = PC1_MAC.replace("-", "")


def settings():
    return CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")])


@pytest.fixture
def crawled():
    """(fake network, the map a crawl of it makes)."""
    network = build_network()
    network_map = Crawler(settings(), client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    network.requests.clear()
    return network, macfind.snapshot_map(network_map)


def locator(network, network_map, **options):
    return Locator(network_map, settings(), client_factory=network.client, **options)


def locate(network, network_map, text, hinted=True):
    query = parse_query(text)
    hints = {0: search_map(network_map, query)} if hinted else {}
    found, problem = locator(network, network_map).run([query], hints)[0]
    return found, problem


# --------------------------------------------------------------------- What's typed

@pytest.mark.parametrize("text", [
    "3c:52:82:00:00:01", "3C-52-82-00-00-01", "3c52.8200.0001", "3c5282000001", "3c 52 82 00 00 01",
    "3c5282-000001", "3c:52:82:0:0:1", "  3C52.8200.0001  ", "'3c-52-82-00-00-01'",
])
def test_a_whole_mac_in_any_format(text):
    query = parse_query(text)
    assert query.kind == MAC_FULL
    assert query.digits == PC1_DIGITS
    assert query.mac == PC1_MAC


@pytest.mark.parametrize("text,digits", [
    ("3c52", "3C52"), ("3c:52:82", "3C5282"), ("0001", "0001"), ("52:82:0", "52820"), ("2.8200", "28200"),
    ("acc1", "ACC1"),
])
def test_part_of_a_mac(text, digits):
    query = parse_query(text)
    assert query.kind == MAC_PART
    assert query.digits == digits


def test_part_of_a_mac_matches_wherever_it_is():
    assert parse_query("52:82:0").matches(PC1_MAC)  # The first and last groups may be parts of groups
    assert parse_query("c5282").matches(PC1_MAC)
    assert not parse_query("c5283").matches(PC1_MAC)
    assert parse_query("acc1").maybe_name and not parse_query("3c:52").maybe_name


def test_ip_addresses_and_names():
    assert parse_query("10.10.0.21").kind == IP and parse_query("10.10.0.21").address == "10.10.0.21"
    assert parse_query("fe80::1%12").kind == IP
    assert parse_query("pc-0142.corp.example").kind == NAME
    assert parse_query("sw-core1").kind == NAME


@pytest.mark.parametrize("text,message", [
    ("", "Enter a MAC"), ("3c:52:82:00:00:01:02", "more hex digits"), ("a:b", "too short"),
    ("not a mac!", "isn't a MAC"),
])
def test_what_isnt_a_search(text, message):
    with pytest.raises(ValueError, match=message):
        parse_query(text)


def test_several_searches_in_the_box_and_duplicates_dropped():
    queries, problems = parse_queries("3c:52:82:00:00:01, 3c5282000001; 10.10.0.30, !!")
    assert [query.kind for query in queries] == [MAC_FULL, IP]
    assert len(problems) == 1


def test_a_list_takes_each_lines_first_mac():
    text = "\n".join(["Desk,Owner,MAC", "D-101,Pat,3c:52:82:00:00:01", "printer\t00-00-48-00-00-09",
                      "10.10.0.22", "", "aa bb cc dd ee ff", "D-102,Sam,nothing here!"])
    queries, problems = parse_list(text)
    assert [query.text for query in queries] == ["3c:52:82:00:00:01", "00-00-48-00-00-09", "10.10.0.22",
                                                 "aa bb cc dd ee ff"]
    assert queries[-1].mac == "AA-BB-CC-DD-EE-FF"
    assert problems == ["Line 1: no MAC address or IP address in 'Desk,Owner,MAC'.",
                        "Line 7: no MAC address or IP address in 'D-102,Sam,nothing here!'."]


# --------------------------------------------------------------------- On the map

def test_map_search_by_mac_part_ip_and_name(crawled):
    _, network_map = crawled
    for text in ("3c:52:82:00:00:01", "3c5282", "10.10.0.21"):
        found = search_map(network_map, parse_query(text))
        assert [(location.switch, location.port, location.vlan, location.ip) for location in found] == [
            ("acc1.corp.example", "Gi1/0/5", 10, "10.10.0.21")]
    phone = search_map(network_map, parse_query("SEP00AABBCCDDEE"))
    assert [location.mac for location in phone] == [PHONE_MAC]
    assert search_map(network_map, parse_query("10.99.99.99")) == []


def test_map_results_say_the_way_there_and_whats_on_the_port(crawled):
    _, network_map = crawled
    pc = search_map(network_map, parse_query(PC1_MAC))[0]
    assert pc.path == [["core.corp.example", "Te1/0/1"], ["acc1.corp.example", "Gi1/0/5"]]
    assert "phone SEP00AABBCCDDEE" in pc.note  # A PC plugged into the phone
    assert pc.vendor and pc.source == macfind.MAP and pc.when == network_map.finished
    lab = search_map(network_map, parse_query(LAB_MACS[3]))[0]
    assert lab.port_macs == 10 and "unmanaged switch" in lab.note
    assert len(search_map(network_map, parse_query("52:54:00:00:00"))) == 10


# --------------------------------------------------------------------- On the network now

def test_locate_asks_the_switch_the_map_had_it_on_first(crawled):
    network, network_map = crawled
    found, problem = locate(network, network_map, PC1_MAC)
    assert problem == ""
    [location] = found
    assert (location.switch, location.port, location.vlan, location.source) == ("acc1.corp.example", "Gi1/0/5", 10,
                                                                                  LIVE)
    assert {host for host, _ in network.requests} == {"10.0.0.11"}  # Only acc1, and only VLAN 10's table
    assert {community for _, community in network.requests} <= {"public", "public@10"}


def test_locate_without_the_map_asks_every_switch_and_picks_the_edge_port(crawled):
    network, network_map = crawled
    [location], _ = locate(network, network_map, PC1_MAC, hinted=False)
    assert (location.switch, location.port) == ("acc1.corp.example", "Gi1/0/5")
    assert location.seen_on == [["core.corp.example", "Te1/0/1"]]  # The core's uplink to acc1 has it too


def test_locate_finds_a_mac_that_moved(crawled):
    network, network_map = crawled
    network.devices["10.0.0.11"].learned(PC1_MAC, 7, 7, vlan=10)  # Its entry now says Gi1/0/7, with the printer
    [location], _ = locate(network, network_map, PC1_MAC)
    assert location.port == "Gi1/0/7"


def test_locate_follows_an_uplink_to_the_next_switch(crawled):
    network, network_map = crawled
    hint = Location(query="", mac=PC1_MAC, device="core", vlan=10)  # Where an old map had it: the core
    found = locator(network, network_map).locate_mac("x", PC1_DIGITS, hint)
    assert [(location.switch, location.port) for location in found] == [("acc1.corp.example", "Gi1/0/5")]
    assert found[0].seen_on == [["core.corp.example", "Te1/0/1"]]
    assert [host for host, _ in network.requests if host not in ("10.0.0.1", "10.0.0.11")] == []


def test_a_mac_beyond_a_device_snmp_cant_read(crawled):
    network, network_map = crawled
    hidden = "00-50-56-00-00-77"
    network.devices["10.0.0.1"].learned(hidden, 4, 4, vlan=10)  # Gi1/0/48: the link to rtr1 (pings, no SNMP)
    [location], _ = locate(network, network_map, hidden)
    assert (location.switch, location.port) == ("core.corp.example", "Gi1/0/48")
    assert "rtr1" in location.note and "can't read" in location.note


def test_a_switchs_own_mac(crawled):
    network, network_map = crawled
    [location], _ = locate(network, network_map, ACC1_MAC)
    assert location.switch == "acc1.corp.example" and location.port == ""
    assert "own MAC" in location.note


def test_a_mac_no_switch_has(crawled):
    network, network_map = crawled
    assert locate(network, network_map, "02-00-00-00-00-99") == ([], "")


def test_nx_os_mac_in_a_vlan_the_map_does_not_list_is_found_in_the_whole_table(crawled):
    network, network_map = crawled
    [location], _ = locate(network, network_map, LAB_MACS[3])
    assert (location.switch, location.port, location.vlan) == ("acc2", "Eth1/10", 30)


def test_locate_by_ip_asks_the_routers_arp_table(crawled):
    network, network_map = crawled
    found, problem = locate(network, network_map, "10.10.0.30", hinted=False)
    assert problem == ""
    assert [(location.mac, location.switch, location.port, location.ip) for location in found] == [
        (PRINTER_MAC, "acc1.corp.example", "Gi1/0/7", "10.10.0.30")]


def test_locate_by_ip_uses_this_computers_arp_first(crawled):
    network, network_map = crawled
    asked = []
    query = parse_query("10.10.0.21")
    finder = locator(network, network_map, arp_lookup=lambda address: asked.append(address) or PC1_MAC)
    [location], _ = finder.run([query])[0]
    assert asked == ["10.10.0.21"] and location.port == "Gi1/0/5"


def test_locate_by_name_looks_it_up_in_dns(crawled):
    network, network_map = crawled
    finder = locator(network, network_map, resolve=lambda name: "10.10.0.30" if name == "printer1" else "")
    [location], _ = finder.run([parse_query("printer1")])[0]
    assert location.mac == PRINTER_MAC and location.ip == "10.10.0.30"
    found, problem = finder.run([parse_query("nobody-here")])[0]
    assert found == [] and "DNS" in problem


def test_part_of_a_mac_reads_every_switchs_table(crawled):
    network, network_map = crawled
    found, _ = locate(network, network_map, "52:54:00")
    assert sorted(location.mac for location in found) == sorted(LAB_MACS)
    assert all(location.port == "Eth1/10" and location.port_macs == 10 for location in found)
    assert "unmanaged switch" in found[0].note
    for mac in (CORE_MAC, ACC1_MAC):  # Network devices' MACs aren't hosts
        assert not any(location.mac == mac and location.port for location in found)


def test_a_long_list_reads_the_tables_once(crawled):
    network, network_map = crawled
    macs = LAB_MACS[:macfind.TABLE_READ_AT] + [PC1_MAC]
    finder = locator(network, network_map)
    results = finder.run([parse_query(mac) for mac in macs])
    assert finder.tables is not None
    assert [results[index][0][0].port for index in range(len(macs))] == ["Eth1/10"] * macfind.TABLE_READ_AT + [
        "Gi1/0/5"]
    assert results[len(macs) - 1][0][0].ip == "10.10.0.21"  # From the core's ARP table


def test_results_are_said_as_they_are_found_and_stop_stops(crawled):
    network, network_map = crawled
    said = []
    finder = locator(network, network_map, events=lambda kind, *details: said.append(kind))
    finder.run([parse_query(PC1_MAC), parse_query(PRINTER_MAC)])
    assert said.count("result") == 2 and "step" in said
    stopped = locator(network, network_map, should_stop=lambda: True)
    assert stopped.run([parse_query(PC1_MAC)]) == {}


def test_an_unanswering_switch_is_skipped(crawled):
    network, network_map = crawled
    network.devices["10.0.0.1"].communities = set()  # The core no longer answers
    finder = locator(network, network_map)
    [location], _ = finder.run([parse_query(PC1_MAC)], {})[0]
    assert location.port == "Gi1/0/5"
    assert finder.clients["core"] is None


# --------------------------------------------------------------------- Where it's been

def place(mac, switch, port, when, source=LIVE, ip=""):
    return Location(query="", mac=mac, switch=switch, port=port, when=when, source=source, ip=ip, vlan=10)


def test_history_keeps_one_row_per_place(tmp_path):
    log = SightingLog(tmp_path / "history.db")
    assert log.history(PC1_MAC) == [] and log.count() == (0, 0)
    log.record([place(PC1_MAC, "acc1", "Gi1/0/5", "2026-10-01T09:00:00")])
    log.record([place(PC1_MAC, "acc1", "GigabitEthernet1/0/5", "2026-10-02T09:00:00", ip="10.10.0.21")])
    assert log.record([place(PC1_MAC, "acc2", "Eth1/3", "2026-10-03T09:00:00")]) == 1  # Moved
    log.record([place(PC1_MAC, "acc2", "Eth1/3", "2026-10-04T09:00:00")])
    spans = log.history(PC1_MAC)
    assert [(span.switch, span.port, span.first_seen[:10], span.last_seen[:10]) for span in spans] == [
        ("acc2", "Eth1/3", "2026-10-03", "2026-10-04"), ("acc1", "Gi1/0/5", "2026-10-01", "2026-10-02")]
    assert spans[1].ip == "10.10.0.21" and spans[0].mac == PC1_MAC
    log.record([place(PC1_MAC, "acc1", "Gi1/0/5", "2026-09-30T09:00:00", source="map")], "Old map")
    assert len(log.history(PC1_MAC)) == 2  # An older map: the first place started earlier
    assert log.history(PC1_MAC)[1].first_seen.startswith("2026-09-30")
    assert log.count() == (1, 2)


def test_history_search_and_clear(tmp_path):
    log = SightingLog(tmp_path / "history.db")
    log.record([place(PC1_MAC, "acc1", "Gi1/0/5", "2026-10-01T09:00:00", ip="10.10.0.21"),
                place(PRINTER_MAC, "acc1", "Gi1/0/7", "2026-10-01T09:00:00"),
                place(PC1_MAC, "acc2", "Eth1/3", "2026-10-02T09:00:00")])
    latest = log.latest(parse_query("3c5282"))
    assert [(sighting.mac, sighting.switch) for sighting in latest] == [(PC1_MAC, "acc2")]
    assert [sighting.mac for sighting in log.latest(parse_query(PRINTER_MAC))] == [PRINTER_MAC]
    assert [sighting.port for sighting in log.latest(parse_query("10.10.0.21"))] == ["Gi1/0/5"]
    log.clear(PC1_MAC)
    assert log.history(PC1_MAC) == [] and log.count() == (1, 1)
    log.clear()
    assert log.count() == (0, 0)


def test_history_ignores_what_has_no_place(tmp_path):
    log = SightingLog(tmp_path / "history.db")
    assert log.record([place(PC1_MAC, "", "", "2026-10-01T09:00:00"), place("", "acc1", "Gi1/0/5", "x")]) == 0
    assert not (tmp_path / "history.db").exists()
