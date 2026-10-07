from netmap_fakes import CISCO_SWITCH, PALO_ALTO, Device, number, string

from nomad.netmap import collect
from nomad.netmap.model import AP, FIREWALL, HOST, PHONE, ROUTER, SWITCH, UNKNOWN, normalize_name, port_key, short_port
from nomad.snmp import oid_text


def rows(device):
    return sorted(device.mib.items())


def test_system_info():
    device = Device("sw1.corp.example", "Cisco IOS Software", CISCO_SWITCH)
    info = collect.system_info(rows(device))
    assert (info.name, info.descr, info.object_id) == ("sw1.corp.example", "Cisco IOS Software",
                                                       oid_text(CISCO_SWITCH))


def test_cdp_neighbors_with_address_and_capabilities():
    device = Device("sw1", "", CISCO_SWITCH)
    device.interface(3, "GigabitEthernet1/0/3")
    device.cdp(3, 7, "sw2.corp(FOC1)", "GigabitEthernet1/0/48", "10.1.2.3", "cisco WS-C2960X-48", 0x29)
    device.cdp(3, 8, "SEP001122334455", "Port 1", "", "Cisco IP Phone 7841", 0x90)
    interfaces = collect.interface_names(rows(device), rows(device))
    found = collect.cdp_neighbors(rows(device), interfaces)
    assert [(item.local_port, item.name, item.port, item.address) for item in found] == [
        ("GigabitEthernet1/0/3", "sw2.corp(FOC1)", "GigabitEthernet1/0/48", "10.1.2.3"),
        ("GigabitEthernet1/0/3", "SEP001122334455", "Port 1", "")]
    assert found[0].capabilities == {"switch", "router"}
    assert found[1].capabilities == {"host", "phone"}


def test_lldp_neighbors_with_management_address_in_index():
    device = Device("core", "", CISCO_SWITCH)
    device.lldp(3, "Te1/0/3", 1, "pa-fw1", "ethernet1/1", "10.0.0.5", "00-1B-17-00-00-05", 0x08, "Palo Alto PA-3220")
    table = rows(device)
    found = collect.lldp_neighbors(table, collect.lldp_local_ports(table), {},
                                   collect.lldp_management_addresses(table))
    assert len(found) == 1
    neighbor = found[0]
    assert (neighbor.local_port, neighbor.name, neighbor.port, neighbor.address, neighbor.chassis_mac) == \
        ("Te1/0/3", "pa-fw1", "ethernet1/1", "10.0.0.5", "00-1B-17-00-00-05")
    assert neighbor.capabilities == {"router"}
    assert collect.classify(capabilities=neighbor.capabilities, platform=neighbor.platform) == FIREWALL


def test_lldp_neighbor_without_system_name_uses_chassis_mac():
    device = Device("core", "", CISCO_SWITCH)
    device.lldp(4, "Gi1/0/4", 2, "", "eth0", chassis_mac="52-54-00-12-34-56", capabilities=0x01)
    table = rows(device)
    neighbor = collect.lldp_neighbors(table, collect.lldp_local_ports(table), {}, {})[0]
    assert neighbor.name == "52-54-00-12-34-56"
    assert "station" in neighbor.capabilities


def test_lldp_computer_without_capabilities_is_a_host():
    """Windows' LLDP agent announces no capabilities, and its NIC's MAC as the port ID."""
    device = Device("core", "", CISCO_SWITCH)
    device.lldp(15, "Gi1/0/15", 1, "WAAAAANB3704Q2", "", capabilities=0, port_mac="64-4E-D7-1E-E9-8A")
    table = rows(device)
    neighbor = collect.lldp_neighbors(table, collect.lldp_local_ports(table), {}, {})[0]
    assert neighbor.port_mac == "64-4E-D7-1E-E9-8A" and neighbor.port == "64-4E-D7-1E-E9-8A"
    assert not neighbor.capabilities
    assert collect.neighbor_kind(neighbor) == HOST


def test_lldp_neighbor_without_capabilities_named_by_its_os_is_a_host():
    device = Device("core", "", CISCO_SWITCH)
    device.lldp(15, "Gi1/0/15", 1, "build-01", "eth0", capabilities=0, descr="Ubuntu 24.04 LTS Linux 6.8.0")
    table = rows(device)
    neighbor = collect.lldp_neighbors(table, collect.lldp_local_ports(table), {}, {})[0]
    assert collect.neighbor_kind(neighbor) == HOST


def test_lldp_neighbor_without_capabilities_otherwise_stays_unknown():
    device = Device("core", "", CISCO_SWITCH)
    device.lldp(15, "Gi1/0/15", 1, "mystery", "port 1", capabilities=0)
    table = rows(device)
    neighbor = collect.lldp_neighbors(table, collect.lldp_local_ports(table), {}, {})[0]
    assert collect.neighbor_kind(neighbor) == UNKNOWN


def test_lldp_keeps_every_management_address():
    device = Device("core", "", CISCO_SWITCH)
    device.lldp(3, "Te1/0/3", 1, "fw", "eth1", "10.0.0.5")
    device.lldp(3, "Te1/0/3", 1, "fw", "eth1", "10.0.0.6")
    table = rows(device)
    found = collect.lldp_neighbors(table, collect.lldp_local_ports(table), {},
                                   collect.lldp_management_addresses(table))
    assert [neighbor.address for neighbor in found] == ["10.0.0.5", "10.0.0.6"]


def test_vlans_skip_reserved_and_inactive():
    device = Device("sw", "", CISCO_SWITCH)
    for vlan in (1, 10, 20, 1002, 1005):
        device.vlan(vlan)
    device.set(collect.VTP_VLAN_STATE, 1, 30, number(2))  # Suspended
    assert collect.vlans(rows(device)) == [1, 10, 20]


def test_fdb_keeps_learned_entries_mapped_to_interfaces():
    device = Device("sw", "", CISCO_SWITCH)
    device.learned("3C-52-82-00-00-01", 5, 10105)
    device.learned("00-1A-2B-00-00-11", 1, 10101, status=4)  # Self
    device.learned("3C-52-82-00-00-02", 9, 10109)
    device.set(collect.FDB_ENTRY, 2, (1, 2, 3, 4, 5, 6), number(77))  # A bridge port with no ifIndex
    entries = collect.fdb(rows(device), rows(device), vlan=10)
    assert sorted(entries) == [("3C-52-82-00-00-01", 10105, 10), ("3C-52-82-00-00-02", 10109, 10)]


def test_arp_and_ip_addresses():
    device = Device("rtr", "", CISCO_SWITCH)
    device.arp(5, "10.1.1.20", "3C-52-82-00-00-01")
    device.arp(6, "10.2.1.20", "3C-52-82-00-00-01")
    device.address("10.1.1.1", 5)
    assert collect.arp(rows(device)) == {"3C-52-82-00-00-01": ["10.1.1.20", "10.2.1.20"]}
    assert collect.ip_addresses(rows(device)) == [("10.1.1.1", 5, "255.255.255.0")]


def test_lag_parents_from_if_stack():
    device = Device("sw", "", CISCO_SWITCH)
    device.lag(11, 100)
    device.lag(12, 100)
    device.set(collect.IF_STACK_STATUS, 0, 11, number(1))  # "Nothing above": ignored
    assert collect.lag_parents(rows(device), []) == {11: 100, 12: 100}


def test_classify():
    assert collect.classify(oid_text(PALO_ALTO), "PA-3220") == FIREWALL
    assert collect.classify("", "", frozenset({"router", "switch"}), "cisco WS-C3850-48P") == SWITCH
    assert collect.classify("", "", frozenset({"router", "switch"}), "cisco ISR4331/K9") == ROUTER
    assert collect.classify("", "", frozenset({"host", "phone"}), "Cisco IP Phone 8845") == PHONE
    assert collect.classify("", "", frozenset({"bridge"}), "cisco C9120AXI-B") == AP
    assert collect.classify(oid_text(CISCO_SWITCH), "Cisco NX-OS(tm) n9000") == SWITCH
    assert collect.classify("1.3.6.1.4.1.9999.1", "Something") == UNKNOWN


def test_names_and_ports():
    assert normalize_name("core-sw1.corp.example(FOC1234X0YZ)") == "core-sw1"
    assert normalize_name("10.0.0.1") == "10.0.0.1"
    assert short_port("TenGigabitEthernet1/0/1") == "Te1/0/1"
    assert short_port("GigabitEthernet0/0/0") == "Gi0/0/0"
    assert short_port("Ethernet1/49") == "Eth1/49"
    assert short_port("Port 1") == "Port 1"
    assert port_key("GigabitEthernet1/0/5") == port_key("Gi1/0/5") == "gi1/0/5"


def test_text_ignores_trailing_nulls():
    assert collect.text(string(b"sw1\0\0")) == "sw1"
