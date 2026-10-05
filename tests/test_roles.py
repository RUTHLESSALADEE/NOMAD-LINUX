"""Subnet roles: what each subnet is for (a VLAN's, a point-to-point link, loopbacks, a tunnel, a routed port's, a
container, or other), worked out from the map and IPAM or set by hand, and what the Subnet Placement and VLANs pages
expect of each."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402
from test_ipam_server import offline_store, plan, server, team_store  # noqa: E402,F401 (fixture)
from test_placement_tab import cell, page, select  # noqa: E402,F401 (fixture)

from nomad.ipam.client import OldServerError, ServerUnreachable  # noqa: E402
from nomad.ipam.placement import PROBLEM, WARNING, PlacementStore, evaluate  # noqa: E402
from nomad.ipam.placement_team import TeamPlacementStore  # noqa: E402
from nomad.ipam.roles import AUTO, CONTAINER, LOOPBACK, OTHER, POINT_TO_POINT, ROUTED, TUNNEL, VLAN, detect, \
    port_role, subnet_roles  # noqa: E402
from nomad.ipam.server import ADMIN  # noqa: E402
from nomad.ipam.store import IpamError, IpamStore  # noqa: E402
from nomad.ipam.vlans import VlanStore, suggested_links  # noqa: E402
from nomad.netmap.model import Device, Link, NetworkMap  # noqa: E402
from nomad.netmap.placement import places  # noqa: E402
from nomad.ui.placement_dialogs import ScopeDialog  # noqa: E402
from nomad.ui.vlan_dialogs import LinkSuggestionsDialog, Source, VlanDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def local(tmp_path):
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    yield store
    store.close()


def wan():
    """A DMVPN hub and two spokes (Tunnel0, no physical link between them), two routers on a carrier circuit (a /30
    the map has no link for), loopbacks (two routers given the same one by mistake), a router port plugged into a
    switch's access VLAN, and a firewall's routed port. core has routes to all of them, so they're advertised."""
    network_map = NetworkMap()

    def device(key, interfaces, **extra):
        network_map.devices[key] = Device(key, key, source="snmp", interfaces_l3=[list(item) for item in interfaces],
                                          **extra)

    device("hub", [("172.16.5.1", 24, "Tunnel0"), ("10.255.0.1", 32, "Loopback0")])
    device("spoke1", [("172.16.5.2", 24, "Tunnel0"), ("10.9.9.1", 30, "Gi0/1"), ("10.255.0.2", 32, "Loopback0")])
    device("spoke2", [("172.16.5.3", 24, "Tunnel0"), ("10.9.9.2", 30, "Gi0/1"), ("10.255.0.2", 32, "Loopback0"),
                      ("10.40.0.1", 24, "Gi0/2")])
    device("sw", [], vlans=[[40, "USERS"]], port_vlans={"Gi1/0/5": {"mode": "access", "vlan": 40}})
    device("fw", [("10.60.0.1", 24, "ethernet1/3"), ("10.61.0.1", 24, "tunnel.5"), ("10.62.0.1", 24, "BD62")])
    network_map.links.append(Link("spoke2", "Gi0/2", "sw", "Gi1/0/5"))
    routes = [[cidr, "10.0.0.2", "Gi0/0", "ospf"] for cidr in
              ("172.16.5.0/24", "10.9.9.0/30", "10.255.0.1/32", "10.255.0.2/32", "10.40.0.0/24", "10.60.0.0/24")]
    device("core", [("10.0.0.1", 30, "Gi0/0")], routes=routes)
    return network_map


def rows_for(local, network_map, network):
    vlans, placements = VlanStore(local), PlacementStore(local)
    return {row.cidr: row for row in evaluate(network_map, local, vlans, placements, network.id)}


def texts(row):
    return [(finding.severity, finding.text) for finding in row.findings]


# --------------------------------------------------------------------- Working roles out

def test_tunnel_and_loopback_interfaces():
    for port in ("Tunnel0", "Tu10", "tunnel.5", "tunnel"):
        assert port_role(port) == TUNNEL
    for port in ("Loopback0", "Lo0", "loopback.1", "lo0.0"):
        assert port_role(port) == LOOPBACK
    for port in ("Gi0/1", "Vlan10", "ethernet1/3", "Tu-something", "LoadBalancer1"):
        assert port_role(port) == ""


def test_roles_from_the_map():
    network_map = wan()
    found = {}
    for (_, cidr), place_list in places(network_map).items():
        found[cidr] = detect(cidr, places=place_list, network_map=network_map)
    assert found["172.16.5.0/24"][0] == TUNNEL and "hub Tunnel0" in found["172.16.5.0/24"][1]
    assert found["10.61.0.0/24"][0] == TUNNEL  # A Palo Alto tunnel interface
    assert found["10.255.0.2/32"][0] == LOOPBACK
    assert found["10.9.9.0/30"][0] == POINT_TO_POINT
    assert found["10.40.0.0/24"] == (VLAN, "spoke2 Gi0/2 is plugged into VLAN 40 on sw Gi1/0/5")
    assert found["10.60.0.0/24"][0] == ROUTED
    assert found["10.62.0.0/24"] == (VLAN, "in VLAN 62 on the map: fw BD62")  # A bridge-domain interface: an SVI


def test_roles_from_ipam_and_the_vlans_page(local):
    network = local.add_network("Lab")
    subnets = {cidr: local.add_subnet(network.id, cidr, name, loopbacks=loopbacks) for cidr, name, loopbacks in (
        ("10.255.0.0/24", "Router loopbacks", True), ("10.0.0.0/16", "Site block", False),
        ("10.0.1.0/24", "Vlan 6", False), ("10.0.2.0/30", "", False), ("10.0.3.0/24", "Printers", False),
        ("10.0.4.7/32", "RTR1 Lo0", False), ("10.0.5.0/24", "Not sure", False))}
    assert detect("10.255.0.0/24", subnets["10.255.0.0/24"])[0] == LOOPBACK
    assert detect("10.0.1.0/24", subnets["10.0.1.0/24"]) == (VLAN, "its name is Vlan 6")
    assert detect("10.0.2.0/30", subnets["10.0.2.0/30"])[0] == POINT_TO_POINT
    assert detect("10.0.3.0/24", subnets["10.0.3.0/24"], linked=[30]) == (VLAN, "linked to VLAN 30 on the VLANs page")
    assert detect("10.0.4.7/32", subnets["10.0.4.7/32"])[0] == LOOPBACK
    assert detect("10.0.5.0/24", subnets["10.0.5.0/24"])[0] == OTHER  # Nothing says: other
    roles = subnet_roles(local, PlacementStore(local), VlanStore(local), network.id)
    assert roles["10.0.0.0/16"].role == CONTAINER  # Other subnets are inside it
    assert roles["10.0.5.0/24"].text == "Other"


def test_setting_a_role(local):
    network = local.add_network("Lab")
    local.add_subnet(network.id, "10.0.5.0/24", "Not sure")
    placements = PlacementStore(local)
    with pytest.raises(IpamError, match="Unknown role"):
        placements.set_role(network.id, "10.0.5.0/24", "wormhole")
    placements.set_role(network.id, "10.0.5.0/24", TUNNEL)
    assert placements.set_role(network.id, "10.0.5.0/24", ROUTED).version == 2
    info = subnet_roles(local, placements, VlanStore(local), network.id)["10.0.5.0/24"]
    assert (info.role, info.text, info.detected) == (ROUTED, "Routed port (set)", OTHER)
    placements.set_role(network.id, "10.0.5.0/24", AUTO)  # Back to automatic: forgotten
    assert placements.roles(network.id) == {}


# --------------------------------------------------------------------- What Subnet Placement expects of each

def test_tunnel_and_point_to_point_ends_are_one_place(local):
    """Advertised from three routers' tunnels, or both ends of a circuit the map doesn't show: not in two places."""
    network = local.add_network("WAN")
    for cidr in ("172.16.5.0/24", "10.9.9.0/30"):
        local.add_subnet(network.id, cidr)
    rows = rows_for(local, wan(), network)
    for cidr in ("172.16.5.0/24", "10.9.9.0/30"):
        assert rows[cidr].scope == "advertised"
        assert not any(severity == PROBLEM for severity, _ in texts(rows[cidr])), texts(rows[cidr])
    PlacementStore(local).set_role(network.id, "172.16.5.0/24", OTHER)  # Not a tunnel after all: separate places
    rows = rows_for(local, wan(), network)
    assert any(severity == PROBLEM and "in 3 places" in text for severity, text in texts(rows["172.16.5.0/24"]))


def test_loopbacks(local):
    """Router loopbacks inside IPAM's loopback subnet belong to it; one on two routers is a problem."""
    network = local.add_network("WAN")
    local.add_subnet(network.id, "10.255.0.0/24", "Router loopbacks", loopbacks=True)
    rows = rows_for(local, wan(), network)
    alone, twice = rows["10.255.0.1/32"], rows["10.255.0.2/32"]
    assert alone.pool.cidr == "10.255.0.0/24" and alone.role.role == LOOPBACK and texts(alone) == []
    assert texts(twice)[0][0] == PROBLEM and "same loopback address on 2 devices" in texts(twice)[0][1]
    assert not any("in 2 places" in text for _, text in texts(twice))  # Said once


def test_point_to_point_with_three_devices_and_roles_that_dont_fit(local):
    network = local.add_network("WAN")
    for cidr in ("10.60.0.0/24", "172.16.5.0/24", "10.40.0.0/24"):
        local.add_subnet(network.id, cidr)
    placements, vlans = PlacementStore(local), VlanStore(local)
    placements.set_role(network.id, "172.16.5.0/24", POINT_TO_POINT)  # A tunnel on the map
    placements.set_role(network.id, "10.60.0.0/24", VLAN)  # Routed: fits (both are LANs to the map)
    domain = vlans.add_domain("Site", network.id)
    vlans.set_vlan(domain.id, 99, "WRONG", subnets=["172.16.5.0/24"])
    rows = rows_for(local, wan(), network)
    tunnel = texts(rows["172.16.5.0/24"])
    assert (WARNING, "Set as point-to-point, but it's on tunnel interfaces on the map: hub Tunnel0, spoke1 "
                     "Tunnel0, spoke2 Tunnel0.") in tunnel
    assert any(severity == WARNING and "3 devices have addresses in it" in text for severity, text in tunnel)
    assert all(severity != WARNING for severity, _ in texts(rows["10.60.0.0/24"]))
    placements.set_role(network.id, "172.16.5.0/24", AUTO)
    rows = rows_for(local, wan(), network)
    assert any("Linked to VLAN 99 (Site) on the VLANs page, but it's a tunnel" in text
               for _, text in texts(rows["172.16.5.0/24"]))
    # In VLAN 40 on the map (through the switch port) but not linked: noted, as for an SVI
    assert any("plugged into VLAN 40 on sw Gi1/0/5), but not linked" in text
               for _, text in texts(rows["10.40.0.0/24"]))


def test_vlan_named_subnet_not_linked_is_noted(local):
    network = local.add_network("Site")
    local.add_subnet(network.id, "10.6.0.0/24", "Vlan 6")
    local.add_subnet(network.id, "10.7.0.0/30", "Transit")
    vlans = VlanStore(local)
    rows = rows_for(local, None, network)
    assert texts(rows["10.6.0.0/24"]) == []  # No VLAN domains for the network yet: nothing to link to
    vlans.add_domain("Site", network.id)
    rows = rows_for(local, None, network)
    assert any("A VLAN's subnet (its name is Vlan 6), but not linked" in text for _, text in texts(rows["10.6.0.0/24"]))
    assert texts(rows["10.7.0.0/30"]) == []  # Point-to-point: not expected in a VLAN


# --------------------------------------------------------------------- Shared through the tribe

def test_roles_through_the_tribe(server, tmp_path):
    admin = team_store(server, tmp_path, "admin", ADMIN)
    [network] = admin.import_networks([plan()])
    alice = team_store(server, tmp_path, "alice")
    placements = TeamPlacementStore(alice)
    assert placements.can_change_roles
    placements.set_role(network.id, "10.0.0.0/24", TUNNEL)
    bob = TeamPlacementStore(team_store(server, tmp_path, "bob"))
    assert bob.role(network.id, "10.0.0.0/24").role == TUNNEL
    bob.set_role(network.id, "10.0.0.0/24", OTHER)
    with pytest.raises(IpamError, match="changed by bob"):
        placements.set_role(network.id, "10.0.0.0/24", VLAN)
    assert [subnet.version for subnet in alice.subnets(network.id)] == [1]  # The subnet itself is untouched
    alice.copy.set_meta("server_api", 8)  # A server from before roles: refused here rather than lost there
    with pytest.raises(OldServerError, match="needs updating"):
        placements.set_role(network.id, "10.0.0.0/24", VLAN)
    assert not placements.can_change_roles
    alice.close()
    offline = TeamPlacementStore(offline_store(server, tmp_path, "bob2"))
    with pytest.raises(ServerUnreachable):
        offline.set_role(network.id, "10.0.0.0/24", VLAN)


# --------------------------------------------------------------------- On the pages

def test_role_column_filter_and_dialog(page):
    tab, store, network, _, _ = page
    assert cell(tab, "10.0.12.0/30", "Role") == "Point-to-point"
    assert cell(tab, "10.50.0.0/24", "Role") == "VLAN"
    tab.role_combo.setCurrentIndex(tab.role_combo.findData(POINT_TO_POINT))
    assert [tab.table.item(number, 0).text() for number in range(tab.table.rowCount())] == ["10.0.12.0/30"]
    tab.role_combo.setCurrentIndex(0)
    row = select(tab, "192.168.1.0/24")
    assert "routed ports on the map" in tab.details.toHtml()
    dialog = ScopeDialog(tab, PlacementStore(store), network.id, row)
    assert dialog.role_combo.currentData() == AUTO and "routed port" in dialog.role_combo.currentText()
    dialog.role_combo.setCurrentIndex(dialog.role_combo.findData(OTHER))
    dialog.save()
    tab.changed()
    assert cell(tab, "192.168.1.0/24", "Role") == "Other (set)"
    assert PlacementStore(store).placement(network.id, "192.168.1.0/24") is None  # Only the role changed


def test_vlan_window_leaves_out_subnets_that_arent_vlans(app, local):
    network = local.add_network("Site")
    for cidr, name in (("10.10.0.0/24", "Users"), ("10.0.12.0/30", "Transit"), ("10.255.0.0/24", "Loopbacks"),
                       ("10.20.0.0/24", "Vlan 20"), ("10.21.0.0/30", "Vlan 21 transit")):
        local.add_subnet(network.id, cidr, name, loopbacks=cidr == "10.255.0.0/24")
    vlans, placements = VlanStore(local), PlacementStore(local)
    domain = vlans.add_domain("Site", network.id)
    source = Source("local", "Local", local, vlans)

    def roles_of(network_id):
        return subnet_roles(local, placements, vlans, network_id)

    dialog = VlanDialog(None, source, domain, number=10, roles_of=roles_of)
    listed = [dialog.subnet_list.item(row).data(0x0100) for row in range(dialog.subnet_list.count())]
    assert listed == ["10.10.0.0/24", "10.20.0.0/24", "10.21.0.0/30"]  # A /30 named for a VLAN is a VLAN's
    assert dialog.others_check.isVisibleTo(dialog) and "the 2 subnets" in dialog.others_check.text()
    dialog.others_check.setChecked(True)
    labels = [dialog.subnet_list.item(row).text() for row in range(dialog.subnet_list.count())]
    assert "10.0.12.0/30  Transit  [Point-to-point]" in labels and len(labels) == 5

    placements.set_role(network.id, "10.20.0.0/24", ROUTED)  # Named for a VLAN, but set otherwise
    assert [number for number, _, _ in suggested_links(local, vlans, domain)] == [20, 21]
    suggestions = LinkSuggestionsDialog(None, source, domain, roles_of(network.id))
    assert [cidr for _, cidr, _ in suggestions.suggestions] == ["10.21.0.0/30"]
