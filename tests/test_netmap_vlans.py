import pytest

from netmap_fakes import CISCO_SWITCH, CISCO_ROUTER, PALO_ALTO, Device, FakeNetwork

from nomad.netmap import collect, vlans
from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.model import Link, NetworkMap
from nomad.netmap.model import Device as MapDevice


def rows(device):
    return sorted(device.mib.items())


def crawl(network, seeds=("10.0.0.1",)):
    return Crawler(CrawlSettings(seeds=list(seeds), trace=False), client_factory=network.client,
                   pinger=network.ping, echo=network.echo).run()


# --------------------------------------------------------------------- Ranges

def test_vlan_text_and_parse_round_trip():
    assert vlans.vlan_text([1, 2, 3, 5, 10, 11, 4094]) == "1-3,5,10-11,4094"
    assert vlans.vlan_text([]) == ""
    assert vlans.parse_vlans("1-3, 5 10-11") == {1, 2, 3, 5, 10, 11}
    for bad in ("0", "5-2", "4095", "ten"):
        with pytest.raises(ValueError):
            vlans.parse_vlans(bad)


def test_svi_and_subinterface_names():
    assert vlans.svi_vlan("Vlan10") == 10 and vlans.svi_vlan("Vl200") == 200 and vlans.svi_vlan("vlan.30") == 30
    assert vlans.svi_vlan("Gi1/0/1") == 0
    assert vlans.bdi_vlan("BDI10") == 10 and vlans.bdi_vlan("BD2") == 2 and vlans.bdi_vlan("Vlan10") == 0
    assert vlans.bdi_vlan("BDX1") == 0
    assert vlans.subinterface_vlan("ethernet1/3.20") == 20
    assert vlans.subinterface_vlan("GigabitEthernet0/0/1.100") == 100
    assert vlans.subinterface_vlan("ae1.5") == 5
    assert vlans.subinterface_vlan("tunnel.1") == 0 and vlans.subinterface_vlan("loopback.2") == 0
    assert vlans.subinterface_vlan("ethernet1/3") == 0


# --------------------------------------------------------------------- Parsing what switches say

def test_vtp_domain_names_and_ports_from_cisco_mibs():
    switch = Device("sw1", "Cisco IOS", CISCO_SWITCH)
    switch.vtp("CORP", 3)
    switch.vlan(1, "default")
    switch.vlan(10, "USERS")
    switch.vlan(1003, "token-ring-default")
    switch.trunk(1, {1, 10, 20, 1500, 4000}, native=99)
    switch.trunk(5, {1, 10}, trunking=False)  # An access port: every switchport has a trunk row
    switch.access(5, 10, voice=20)
    switch.access(6, 10, voice=4096)  # 4096: no voice VLAN
    table = rows(switch)
    assert collect.vtp_domain(table) == ("CORP", "transparent")
    assert collect.vlan_names(table, table) == {1: "default", 10: "USERS"}
    ports = collect.cisco_port_vlans(table, table, table)
    assert ports[1].mode == collect.TRUNK and ports[1].native == 99
    assert ports[1].allowed == {1, 10, 20, 1500, 4000}
    assert (ports[5].mode, ports[5].vlan, ports[5].voice) == (collect.ACCESS, 10, 20)
    assert (ports[6].vlan, ports[6].voice) == (10, 0)
    assert collect.ports_vlans_in_use(ports) == [10, 20, 99]


def test_q_bridge_ports():
    switch = Device("sw2", "other", (1, 3, 6, 1, 4, 1, 11))
    switch.q_vlan(1, "default", ports=[1, 2], untagged=[1, 2])
    switch.q_vlan(10, "users", ports=[1, 3], untagged=[3])
    switch.q_vlan(20, "voice", ports=[1], untagged=[])
    for bridge_port, if_index, pvid in ((1, 101, 1), (2, 102, 1), (3, 103, 10)):
        switch.pvid(bridge_port, if_index, pvid)
    table = rows(switch)
    assert collect.q_vlan_names(table) == {1: "default", 10: "users", 20: "voice"}
    ports = collect.q_port_vlans(table, table, table, table)
    assert ports[101].mode == collect.TRUNK and ports[101].allowed == {1, 10, 20} and ports[101].native == 1
    assert (ports[102].mode, ports[102].vlan) == (collect.ACCESS, 1)
    assert (ports[103].mode, ports[103].vlan) == (collect.ACCESS, 10)


# --------------------------------------------------------------------- Crawling

def two_switches():
    """sw1 (10.0.0.1, VTP server) Te1/0/1 -- Te1/1/1 sw2 (10.0.0.2, VTP client), with a router-on-a-stick on sw1."""
    network = FakeNetwork()
    sw1 = network.add("10.0.0.1", Device("sw1", "Cisco IOS Software, Catalyst", CISCO_SWITCH))
    sw1.interface(1, "TenGigabitEthernet1/0/1")
    sw1.interface(2, "GigabitEthernet1/0/2")
    sw1.interface(3, "GigabitEthernet1/0/3")
    sw1.interface(50, "Vlan10")
    sw1.address("10.0.0.1", 50)
    sw1.address("10.10.0.1", 50)
    sw1.vtp("CORP", 2)
    for vlan, name in ((1, "default"), (10, "USERS"), (20, "VOICE"), (30, "PRINTERS")):
        sw1.vlan(vlan, name)
    sw1.trunk(1, {1, 10, 20, 30}, native=1)
    sw1.access(2, 10, voice=20)
    sw1.access(3, 40)  # Not a VLAN sw1 has
    sw1.cdp(1, 1, "sw2", "TenGigabitEthernet1/1/1", "10.0.0.2", "cisco C9300", 0x28)
    sw2 = network.add("10.0.0.2", Device("sw2", "Cisco IOS Software, Catalyst", CISCO_SWITCH))
    sw2.interface(1, "TenGigabitEthernet1/1/1")
    sw2.interface(5, "GigabitEthernet1/0/5")
    sw2.address("10.0.0.2", 1)
    sw2.vtp("CORP", 1)
    for vlan, name in ((1, "default"), (10, "USERS"), (20, "VOICE"), (30, "PRINT")):
        sw2.vlan(vlan, name)
    sw2.trunk(1, {1, 10, 20}, native=99)
    sw2.access(5, 30)
    sw2.cdp(1, 1, "sw1", "TenGigabitEthernet1/0/1", "10.0.0.1", "cisco C9500", 0x28)
    return network


def test_crawl_keeps_vlans_on_devices():
    network_map = crawl(two_switches())
    sw1, sw2 = network_map.devices["sw1"], network_map.devices["sw2"]
    assert (sw1.vtp_domain, sw1.vtp_mode, sw2.vtp_mode) == ("CORP", "server", "client")
    assert sw1.vlans == [[1, "default"], [10, "USERS"], [20, "VOICE"], [30, "PRINTERS"]]
    assert sw1.port_vlans["Te1/0/1"] == {"mode": "trunk", "native": 1, "allowed": "1,10,20,30"}
    assert sw1.port_vlans["Gi1/0/2"] == {"mode": "access", "vlan": 10, "voice": 20}
    assert sw2.port_vlans["Te1/1/1"]["native"] == 99
    restored = NetworkMap.from_json(network_map.to_json())  # Saved with the map
    assert restored.devices["sw1"].port_vlans == sw1.port_vlans and restored.devices["sw1"].vlans == sw1.vlans


def test_map_vlans_focus_and_checks():
    network_map = crawl(two_switches())
    found = {item.vlan: item for item in vlans.map_vlans(network_map)}
    assert set(found) == {1, 10, 20, 30, 40}
    assert found[10].name == "USERS" and sorted(found[10].switches) == ["sw1", "sw2"]
    assert found[10].access_ports == [("sw1", "Gi1/0/2")] and found[20].voice_ports == [("sw1", "Gi1/0/2")]
    assert [gateway.subnet for gateway in found[10].gateways] == ["10.0.0.0/24", "10.10.0.0/24"]
    assert set(found[30].names) == {"PRINTERS", "PRINT"}

    focus = vlans.focus(network_map, 30, "CORP")
    assert list(focus.links.values()) == [vlans.ONE_END]  # Allowed on sw1's end only
    focus = vlans.focus(network_map, 10, "CORP")
    assert list(focus.links.values()) == [vlans.TAGGED]
    assert focus.devices["sw1"].startswith("has it, gateway, 1 access port")

    texts = [finding.text for finding in vlans.check_map(network_map)]
    assert any("native VLAN 1 on one end, 99 on the other" in text for text in texts)
    assert any("VLAN 30 allowed only on sw1 Te1/0/1" in text for text in texts)
    assert any("Gi1/0/3 is in VLAN 40, which the switch doesn't have" in text for text in texts)
    assert any("VLAN 30 in VTP domain CORP has different names" in text for text in texts)
    assert vlans.check_map(network_map)[0].severity == vlans.ERROR


def test_link_known_at_one_end_only_counts_that_end():
    network_map = crawl(two_switches())
    network_map.devices["sw2"].port_vlans = {}  # As if sw2 couldn't be read
    assert list(vlans.focus(network_map, 30, "CORP").links.values()) == [vlans.TAGGED]
    sw1 = network_map.devices["sw1"]
    assert vlans.carries(vlans.port_info(sw1, "TenGigabitEthernet1/0/1"), 30) == vlans.TAGGED
    assert vlans.carries(vlans.port_info(sw1, "Te1/0/1"), 40) == ""


def test_router_subinterfaces_count_in_linked_switches_domain():
    network_map = NetworkMap()
    network_map.devices["sw"] = MapDevice("sw", "sw", vtp_domain="CORP", vlans=[[100, "GUESTS"]])
    network_map.devices["rtr"] = MapDevice("rtr", "rtr", interfaces_l3=[["192.168.100.1", 24, "Gi0/0/1.100"]])
    network_map.links.append(Link("sw", "Gi1/0/1", "rtr", "Gi0/0/1"))
    found = {(item.domain, item.vlan): item for item in vlans.map_vlans(network_map)}
    gateway = found[("CORP", 100)].gateways[0]
    assert gateway.device == "rtr" and gateway.guessed and gateway.subnet == "192.168.100.0/24"


def test_firewall_and_router_answer_without_vlans():
    network = FakeNetwork()
    network.add("10.0.0.5", Device("fw", "Palo Alto Networks PA-3220", PALO_ALTO))
    network.add("10.0.0.6", Device("rtr", "Cisco IOS Software, ISR", CISCO_ROUTER))
    network_map = crawl(network, seeds=("10.0.0.5", "10.0.0.6"))
    assert all(not device.vlans and not device.port_vlans for device in network_map.devices.values())
