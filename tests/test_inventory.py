"""Ansible inventories from the map and saved sessions: platform detection, entries, groups and both formats.

The YAML and INI written here were checked against ansible-core 2.21's own inventory parser (same groups, hosts and
variables from both formats)."""
from netmap_fakes import build_network

from nomad import inventory
from nomad.inventory import INI, NONE, YAML, Entry, Options, build, collect_entries, detect_platform, render
from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.model import AP, BUILDING, FIREWALL, HOST, ROOM, ROUTER, SWITCH, UNKNOWN, Device, NetworkMap
from nomad.terminal.sessions import Session, TELNET


def device(key, **values):
    return Device(key=key, name=values.pop("name", key), **values)


def test_platforms_detected_from_what_the_map_read():
    cases = [
        (dict(sys_descr="Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE)",
              sys_object_id="1.3.6.1.4.1.9.1.2494", kind=SWITCH), "ios"),
        (dict(sys_descr="Cisco NX-OS(tm) n9000, Software (n9000-dk9)", sys_object_id="1.3.6.1.4.1.9.12.3.1.3.1812",
              kind=SWITCH), "nxos"),
        (dict(platform="N9K-C93180YC-EX", kind=SWITCH), "nxos"),  # Only seen over CDP
        (dict(platform="cisco WS-C2960X-48TS-L", kind=SWITCH), "ios"),
        (dict(platform="cisco ISR4331/K9", kind=ROUTER), "ios"),
        (dict(sys_descr="Cisco IOS XR Software (ASR9K)", sys_object_id="1.3.6.1.4.1.9.1.1017", kind=ROUTER), "iosxr"),
        (dict(sys_descr="Cisco Adaptive Security Appliance Version 9.16(4)", sys_object_id="1.3.6.1.4.1.9.1.2313",
              kind=FIREWALL), "asa"),
        (dict(sys_descr="Cisco Firepower Threat Defense, Version 7.2", sys_object_id="1.3.6.1.4.1.9.1.2663",
              kind=FIREWALL), NONE),
        (dict(sys_object_id="1.3.6.1.4.1.25461.2.3.38", kind=FIREWALL), "panos"),
        (dict(platform="Palo Alto Networks PA-3220", kind=FIREWALL), "panos"),
        (dict(sys_descr="Juniper Networks, Inc. ex4300-48p Ethernet Switch, kernel JUNOS 21.4R3",
              sys_object_id="1.3.6.1.4.1.2636.1.1.1.2.132", kind=SWITCH), "junos"),
        (dict(sys_descr="Arista Networks EOS version 4.30.1F", sys_object_id="1.3.6.1.4.1.30065.1.3011", kind=SWITCH),
         "eos"),
        (dict(sys_object_id="1.3.6.1.4.1.12356.101.1.60", kind=FIREWALL), "fortios"),
        (dict(platform="cisco AIR-AP2802I-B-K9", kind=AP), NONE),  # An access point isn't managed on its own
        (dict(platform="Windows 11", kind=HOST), NONE),
        (dict(kind=UNKNOWN), NONE),
    ]
    for values, expected in cases:
        assert detect_platform(device("d", **values)) == expected, values


def test_entries_join_sessions_to_their_devices():
    network_map = NetworkMap()
    network_map.devices = {
        "core": device("core", name="core.corp.example", mgmt_ip="10.0.0.1", kind=SWITCH,
                       sys_object_id="1.3.6.1.4.1.9.1.1"),
        "edge": device("edge", name="edge-rtr", mgmt_ip="", addresses=["192.0.2.1"], kind=ROUTER),
        "pc": device("pc", name="pc1", kind=HOST),
    }
    site = network_map.new_group("HQ")
    building = network_map.new_group("Bldg A", BUILDING, site.key)
    room = network_map.new_group("101", ROOM, building.key)
    network_map.set_group(["core"], room.key)
    sessions = [
        Session("Core switch", host="10.0.0.1", username="admin", port=2222, folder="Sites/HQ"),  # By address
        Session("edge-rtr", host="edge-rtr.corp.example", folder="WAN"),  # By name
        Session("Core again", host="10.0.0.1", username="other"),  # The device's first session wins
        Session("lab", host="198.51.100.7"),  # Not on the map
        Session("old", protocol=TELNET, host="10.9.9.9"),  # Ansible's network_cli is SSH only
        Session("blank", host=" "),
    ]
    entries = {entry.key: entry for entry in collect_entries(network_map, sessions)}
    assert sorted(entries) == ["device:core", "device:edge", "device:pc", f"session:{sessions[3].id}"]
    core = entries["device:core"]
    assert (core.name, core.address, core.port, core.username, core.folder) == \
           ("core.corp.example", "10.0.0.1", 2222, "admin", "Sites/HQ")
    assert core.location == ["HQ", "Bldg A", "101"] and core.source_text == "Map + Session"
    assert core.detected == "ios" and core.default_choice()
    edge = entries["device:edge"]
    assert edge.address == "192.0.2.1" and edge.folder == "WAN"  # No management address: its first one
    assert not entries["device:pc"].default_choice()  # A host, with no address either
    lab = entries[f"session:{sessions[3].id}"]
    assert (lab.name, lab.address, lab.source_text, lab.kind) == ("lab", "198.51.100.7", "Session", "")
    assert lab.default_choice()
    assert [entry.name for entry in collect_entries(None, sessions)] == ["Core again", "Core switch", "edge-rtr",
                                                                          "lab"]


def entry(name, address="", **values):
    return Entry(key=f"test:{name}", name=name, address=address, **values)


def test_groups_nest_by_location_and_folder_and_hold_platform_variables():
    entries = [
        entry("core-sw1.corp.example", "10.0.0.1", kind=SWITCH, detected="ios", location=["HQ", "Bldg A", "101"],
              folder="Sites/HQ", username="admin", from_map=True, from_session=True),
        entry("acc-sw2", "10.0.0.2", kind=SWITCH, detected="nxos", location=["HQ"], from_map=True),
        entry("pa-fw1", "10.0.0.5", kind=FIREWALL, detected="panos", from_map=True),
        entry("lab box", "198.51.100.7", port=2222, folder="Lab", from_session=True),
    ]
    built = build(entries, Options(username="netops"))
    assert list(built.hosts) == ["core-sw1", "acc-sw2", "pa-fw1", "lab-box"]
    assert built.hosts["core-sw1"] == {"ansible_host": "10.0.0.1", "ansible_user": "admin"}
    assert built.hosts["lab-box"] == {"ansible_host": "198.51.100.7", "ansible_port": 2222}
    assert built.all_vars == {"ansible_user": "netops"}
    assert built.top == ["hq", "lab", "sites", "switches", "firewalls", "cisco_ios", "cisco_nxos", "panos"]
    assert built.groups["hq"].children == ["hq_bldg_a"] and built.groups["hq"].hosts == ["acc-sw2"]
    assert built.groups["hq_bldg_a"].children == ["hq_bldg_a_101"]
    assert built.groups["hq_bldg_a_101"].hosts == ["core-sw1"]
    assert built.groups["sites"].children == ["sites_hq"] and built.groups["sites_hq"].hosts == ["core-sw1"]
    assert built.groups["switches"].hosts == ["core-sw1", "acc-sw2"]
    assert built.groups["cisco_ios"].vars == {"ansible_network_os": "cisco.ios.ios",
                                              "ansible_connection": "ansible.netcommon.network_cli"}
    assert built.groups["panos"].vars == {"ansible_network_os": "paloaltonetworks.panos.panos",
                                          "ansible_connection": "ansible.netcommon.httpapi",
                                          "ansible_httpapi_use_ssl": True, "ansible_httpapi_validate_certs": False}
    assert built.collections == ["ansible.netcommon", "cisco.ios", "cisco.nxos", "paloaltonetworks.panos"]
    assert built.problems == []

    flat = build(entries, Options(by_location=False, by_kind=False, by_platform=False, by_folder=False,
                                  short_names=False, session_users=False, become=True, nomad_vars=True))
    assert flat.top == [] and flat.groups == {}
    assert flat.hosts["core-sw1.corp.example"] == {
        "ansible_host": "10.0.0.1", "ansible_network_os": "cisco.ios.ios",
        "ansible_connection": "ansible.netcommon.network_cli", "ansible_become": True,
        "ansible_become_method": "enable", "nomad_kind": "Switch", "nomad_location": "HQ / Bldg A / 101",
        "nomad_folder": "Sites/HQ"}
    assert "ansible_become" not in flat.hosts["acc-sw2"]  # NX-OS has no enable mode
    assert "ansible_become" not in flat.hosts["pa-fw1"]


def test_platforms_chosen_by_hand_win_and_unknown_ones_are_reported():
    entries = [entry("fw", "10.0.0.9", kind=FIREWALL), entry("sw", "10.0.0.8", kind=SWITCH, detected="ios"),
               entry("nameless", kind=ROUTER, detected="ios")]
    built = build(entries, Options(), {"test:sw": NONE})
    assert "cisco_ios" in built.groups and built.groups["cisco_ios"].hosts == ["nameless"]
    assert built.problems == ["fw: its Ansible OS isn't known; choose it with Set Ansible OS.",
                              "sw: its Ansible OS isn't known; choose it with Set Ansible OS.",
                              "nameless: no address, so Ansible will look its name up in DNS."]
    built = build(entries[:1], Options(), {"test:fw": "asa"})
    assert built.groups["cisco_asa"].hosts == ["fw"] and built.problems == []


def test_names_are_made_safe_and_unique():
    entries = [entry("Core Switch #1", "10.0.0.1"), entry("core-switch-1", "10.0.0.2"),
               entry("sw.a.example", "1.1.1.1"), entry("sw.b.example", "1.1.1.2"), entry("10.0.0.3", "10.0.0.3"),
               entry("", "10.0.0.4"), entry("", "")]
    built = build(entries, Options())
    assert list(built.hosts) == ["Core-Switch-1", "core-switch-1-2", "sw", "sw-2", "10.0.0.3", "10.0.0.4"]
    assert built.hosts["10.0.0.3"] == {}  # Its name is its address
    assert built.problems == ["test:: left out, with no name or address."]
    assert built.names["test:sw.b.example"] == "sw-2"


def test_group_names_ansible_rejects_are_renamed():
    assert inventory.group_name("Site A") == "site_a"
    assert inventory.group_name("HQ", "Bldg #2", "Room 1.01") == "hq_bldg_2_room_1_01"
    assert inventory.group_name("2nd floor") == "g_2nd_floor"
    assert inventory.group_name("***") == "group"
    entries = [entry("switches", "10.0.0.1", kind=SWITCH, detected="ios", folder="all")]
    built = build(entries, Options())
    assert built.top == ["all_group", "switches_group", "cisco_ios"]
    assert built.problems == ["Group all is called all_group: Ansible keeps that name for itself.",
                              "Group switches is called switches_group: a device has that name."]


def test_yaml_quotes_what_would_not_read_back_as_text():
    plain = ["core-sw1", "10.0.0.1", "cisco.ios.ios", "ansible.netcommon.network_cli", "Sites/HQ", "N9K-C93180YC-EX"]
    for text in plain:
        assert inventory.yaml_scalar(text) == text
    quoted = ["yes", "No", "on", "null", "~", "", "1234", "1.5", "0x1F", "1e3", "2026-10-07", "12:30", "HQ / Bldg A",
              "fe80::1", "#tag", "a: b", "-dash", 'say "hi"', "back\\slash"]
    for text in quoted:
        assert inventory.yaml_scalar(text).startswith('"'), text
    assert inventory.yaml_scalar('say "hi"') == r'"say \"hi\""'
    assert inventory.yaml_scalar(True) == "true" and inventory.yaml_scalar(2222) == "2222"


def test_yaml_and_ini_text():
    entries = [entry("core", "10.0.0.1", kind=SWITCH, detected="ios", location=["HQ", "Bldg A"],
                     username='we"ird #user'),
               entry("lab", "10.0.0.7", folder="Lab")]
    built = build(entries, Options(username="net ops", by_kind=False))
    comments = inventory.header(built, "NOMAD 9.9", 'the network map "Test"')
    assert render(built, YAML, comments) == '''\
# Ansible inventory made by NOMAD 9.9 from the network map "Test".
# Collections it needs: ansible-galaxy collection install ansible.netcommon cisco.ios
# Passwords aren't kept here: run with --ask-pass (-k), or keep ansible_password in an ansible-vault file.

all:
  vars:
    ansible_user: "net ops"
  hosts:
    core:
      ansible_host: 10.0.0.1
      ansible_user: "we\\"ird #user"
    lab:
      ansible_host: 10.0.0.7
  children:
    hq:
      children:
        hq_bldg_a:
          hosts:
            core:
    lab_group:
      hosts:
        lab:
    cisco_ios:
      vars:
        ansible_network_os: cisco.ios.ios
        ansible_connection: ansible.netcommon.network_cli
      hosts:
        core:
'''
    assert render(built, INI) == '''\
core ansible_host=10.0.0.1 ansible_user="we\\"ird #user"
lab ansible_host=10.0.0.7

[all:vars]
ansible_user="net ops"

[hq:children]
hq_bldg_a

[hq_bldg_a]
core

[lab_group]
lab

[cisco_ios]
core

[cisco_ios:vars]
ansible_network_os=cisco.ios.ios
ansible_connection=ansible.netcommon.network_cli
'''


def test_a_crawled_map_end_to_end():
    network = build_network()
    network_map = Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                          client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    entries = collect_entries(network_map)
    detected = {entry.name: entry.detected for entry in entries if entry.default_choice()}
    assert detected == {"acc1.corp.example": "ios", "acc2": "nxos", "core.corp.example": "ios", "pa-fw1": "panos",
                        "rtr1.corp.example": "ios"}
    built = build([entry for entry in entries if entry.default_choice()], Options())
    assert sorted(built.hosts) == ["acc1", "acc2", "core", "pa-fw1", "rtr1"]
    assert sorted(built.groups["cisco_ios"].hosts) == ["acc1", "core", "rtr1"]
    assert built.groups["routers"].hosts == ["rtr1"] and built.groups["firewalls"].hosts == ["pa-fw1"]
