"""Moving a subnet from one IPAM network to another: its addresses (and, if asked, the subnets inside it) go with it,
its role and placement too, its VLAN links are dropped (with a VLAN of the new network offered), and anything in the
way refuses it. Within this computer's networks, within the tribe's (on the server), and from this computer's into
the tribe's."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from test_integration import app  # noqa: E402,F401 (fixture)
from test_ipam_server import key_for, plan as import_plan, server, team_store  # noqa: F401 (fixture)

from nomad.ipam.client import OldServerError
from nomad.ipam.network_move import move, plan, to_tribe
from nomad.ipam.placement import IN_PROGRESS, LOCAL, PlacementStore
from nomad.ipam.server import ADMIN
from nomad.ipam.store import RESERVED, IpamError, IpamStore
from nomad.ipam.vlans import VlanStore
from nomad.ipam.workbook import export_workbook


@pytest.fixture
def local(tmp_path):
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    yield store
    store.close()


def two_networks(store):
    sipr = store.add_network("11AB SIPR")
    nipr = store.add_network("11AB NIPR")
    store.add_subnet(sipr.id, "10.20.0.0/22", "Block", "10.20.0.1")
    store.add_subnet(sipr.id, "10.20.1.0/24", "Inside", "10.20.1.1")
    store.add_subnet(sipr.id, "10.30.0.0/24", "Stays")
    for ip, name in (("10.20.0.5", "in-block"), ("10.20.1.9", "inside"), ("10.30.0.4", "stays")):
        store.set_address(sipr.id, ip, name=name, mac="aa-bb-cc-dd-ee-ff")
    store.set_address(sipr.id, "10.20.0.6", RESERVED, "reserved-one")
    store.add_subnet(nipr.id, "10.99.0.0/24", "NIPR's")
    return sipr, nipr


def cidrs(store, network):
    return [subnet.cidr for subnet in store.subnets(network.id)]


def ips(store, network):
    return [address.ip for address in store.addresses(network.id)]


def test_plan_says_what_goes(local):
    sipr, _ = two_networks(local)
    whole = plan(local, sipr.id, "10.20.0.0/22")
    assert whole.cidrs == ["10.20.0.0/22", "10.20.1.0/24"] and not whole.left
    assert [address.ip for address in whole.addresses] == ["10.20.0.5", "10.20.0.6", "10.20.1.9"]
    alone = plan(local, sipr.id, "10.20.0.0/22", take_nested=False)
    assert alone.cidrs == ["10.20.0.0/22"] and [item.cidr for item in alone.left] == ["10.20.1.0/24"]
    assert [address.ip for address in alone.addresses] == ["10.20.0.5", "10.20.0.6"]  # Not the /24's
    with pytest.raises(IpamError, match="isn't a subnet"):
        plan(local, sipr.id, "10.77.0.0/24")


def test_move_with_what_belongs_to_it(local):
    sipr, nipr = two_networks(local)
    vlans, placements = VlanStore(local), PlacementStore(local)
    old_domain = vlans.add_domain("SIPR switches", sipr.id)
    vlans.set_vlan(old_domain.id, 20, "BLOCK", subnets=["10.20.0.0/22", "10.30.0.0/24"])
    new_domain = vlans.add_domain("NIPR switches", nipr.id)
    vlans.set_vlan(new_domain.id, 20, "NEW-BLOCK")
    placements.set_role(sipr.id, "10.20.1.0/24", "routed")
    placements.set_placement(sipr.id, "10.20.0.0/22", LOCAL, note="reused")

    done = move(local, sipr.id, "10.20.0.0/22", nipr.id, take_nested=True, link=(new_domain.id, 20))
    assert [item[1].vlan for item in done.links] == [20]
    assert cidrs(local, sipr) == ["10.30.0.0/24"] and ips(local, sipr) == ["10.30.0.4"]
    assert cidrs(local, nipr) == ["10.20.0.0/22", "10.20.1.0/24", "10.99.0.0/24"]
    moved = {address.ip: address for address in local.addresses(nipr.id)}
    assert set(moved) == {"10.20.0.5", "10.20.0.6", "10.20.1.9"}
    assert moved["10.20.0.6"].status == RESERVED and moved["10.20.0.5"].mac == "aa-bb-cc-dd-ee-ff"
    block = next(subnet for subnet in local.subnets(nipr.id) if subnet.cidr == "10.20.0.0/22")
    assert (block.name, block.gateway) == ("Block", "10.20.0.1")
    assert vlans.vlan(old_domain.id, 20).subnets == ["10.30.0.0/24"]  # The old link dropped, the other kept
    assert vlans.vlan(new_domain.id, 20).subnets == ["10.20.0.0/22"]  # Linked in the new network's domain
    assert placements.role(nipr.id, "10.20.1.0/24").role == "routed" and placements.roles(sipr.id) == {}
    assert placements.placement(nipr.id, "10.20.0.0/22").note == "reused" and placements.placements(sipr.id) == {}

    move(local, nipr.id, "10.20.0.0/22", sipr.id, take_nested=False, link=(old_domain.id, 21))  # Back, alone
    assert cidrs(local, sipr) == ["10.20.0.0/22", "10.30.0.0/24"] and "10.20.1.0/24" in cidrs(local, nipr)
    assert vlans.vlan(old_domain.id, 21).subnets == ["10.20.0.0/22"]  # A new VLAN, made for it
    assert ips(local, nipr) == ["10.20.1.9"]


def test_refused_when_something_is_in_the_way(local):
    sipr, nipr = two_networks(local)
    local.add_subnet(nipr.id, "10.20.2.0/24", "NIPR has part of it")
    with pytest.raises(IpamError, match="10.20.2.0/24 \\(NIPR has part of it\\) is in the way"):
        move(local, sipr.id, "10.20.0.0/22", nipr.id)
    local.delete_subnet(next(subnet.id for subnet in local.subnets(nipr.id) if subnet.cidr == "10.20.2.0/24"))
    local.set_address(nipr.id, "10.20.3.3", name="stray")  # An address there, in no subnet
    with pytest.raises(IpamError, match="recorded there already: 10.20.3.3"):
        move(local, sipr.id, "10.20.0.0/22", nipr.id)
    assert cidrs(local, sipr)[0] == "10.20.0.0/22"  # Nothing happened
    local.free_address(nipr.id, "10.20.3.3")
    domain = VlanStore(local).add_domain("SIPR switches", sipr.id)
    placements = PlacementStore(local)
    planned = placements.plan_move(sipr.id, "10.20.1.0/24", to_domain_id=domain.id, to_vlan=30)
    placements.update_move(planned.id, status=IN_PROGRESS)
    with pytest.raises(IpamError, match="being moved on the Subnet Placement page"):
        move(local, sipr.id, "10.20.0.0/22", nipr.id)
    with pytest.raises(IpamError, match="already"):
        move(local, sipr.id, "10.30.0.0/24", sipr.id)


def test_both_networks_still_export(local, tmp_path):
    sipr, nipr = two_networks(local)
    move(local, sipr.id, "10.20.0.0/22", nipr.id)
    export_workbook(str(tmp_path / "after.xlsx"), [(local, local.network(sipr.id)), (local, local.network(nipr.id))])


def test_moves_through_the_tribe(server, tmp_path):
    admin = team_store(server, tmp_path, "admin", ADMIN)
    first, second = admin.import_networks([import_plan("11AB SIPR"), import_plan("11AB NIPR")])
    admin.delete_subnet(admin.subnets(second.id)[0].id, with_addresses=True)  # NIPR empty, so SIPR's can go there
    alice = team_store(server, tmp_path, "alice")
    vlans = VlanStore(alice.copy)
    alice.move_subnet(first.id, "10.0.0.0/24", second.id)
    assert cidrs(alice, first) == [] and cidrs(alice, second) == ["10.0.0.0/24"]
    assert ips(alice, second) == ["10.0.0.5"]
    bob = team_store(server, tmp_path, "bob")
    assert cidrs(bob, second) == ["10.0.0.0/24"]  # On the server, for everyone
    assert vlans.domains() == []

    mine = IpamStore(str(tmp_path / "mine.db"), user="alice")  # Planned on this computer, then shared
    own = mine.add_network("Planning")
    mine.add_subnet(own.id, "10.5.0.0/24", "New site", "10.5.0.1")
    mine.set_address(own.id, "10.5.0.10", name="printer")
    PlacementStore(mine).set_role(own.id, "10.5.0.0/24", "vlan")
    to_tribe(mine, alice, own.id, "10.5.0.0/24", first.id)
    assert cidrs(mine, own) == [] and ips(mine, own) == []
    alice.sync()
    assert cidrs(alice, first) == ["10.5.0.0/24"] and alice.address(first.id, "10.5.0.10").name == "printer"
    assert PlacementStore(alice.copy).role(first.id, "10.5.0.0/24").role == "vlan"
    with pytest.raises(IpamError, match="in the way"):  # The server refuses what's in the way: nothing leaves here
        mine.add_subnet(own.id, "10.5.0.0/25", "Again")
        to_tribe(mine, alice, own.id, "10.5.0.0/25", first.id)
    assert cidrs(mine, own) == ["10.5.0.0/25"]

    alice.copy.set_meta("server_api", 9)
    with pytest.raises(OldServerError, match="needs updating"):
        alice.move_subnet(second.id, "10.0.0.0/24", first.id)
    mine.close()
    alice.close()


# --------------------------------------------------------------------- On the IP Addresses page

@pytest.fixture
def page(app, tmp_path):
    from test_integration import Window
    from nomad.ui.integration import Integration
    from nomad.ui.ipam_tab import IpamTab
    from nomad.ui.placement_tab import PlacementTab
    from nomad.ui.vlan_tab import VlanTab
    from nomad.ui import netmap_tab
    window = Window()
    window.integration = Integration(window)
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    window.ipam_tab = IpamTab(window)
    window.ipam_tab.local_store = store
    window.netmap_tab = netmap_tab.NetworkMapTab(window)
    window.vlan_tab = VlanTab(window)
    window.placement_tab = PlacementTab(window)
    window.integration.connect_pages()
    yield window, store
    window.netmap_tab.shutdown()
    window.ipam_tab.sync_timer.stop()
    store.close()


def select_subnet(ipam, store, network, cidr):
    ipam.fill_networks(f"local:{network.id}")
    ipam.fill_tree(select=next(subnet for subnet in store.subnets(network.id) if subnet.cidr == cidr))


def test_move_dialog(page):
    from nomad.ui.network_move_dialog import MoveToNetworkDialog
    window, store = page
    sipr, nipr = two_networks(store)
    vlans = VlanStore(store)
    vlans.set_vlan(vlans.add_domain("SIPR switches", sipr.id).id, 20, subnets=["10.20.0.0/22"])
    nipr_domain = vlans.add_domain("NIPR switches", nipr.id)
    vlans.set_vlan(nipr_domain.id, 20, "TWENTY")
    ipam = window.ipam_tab
    select_subnet(ipam, store, sipr, "10.20.0.0/22")
    dialog = MoveToNetworkDialog(ipam, ipam, "local", sipr.id, ipam.selected_subnet())
    assert dialog.target_combo.currentData() == f"local:{nipr.id}"
    assert dialog.nested_check.isVisibleTo(dialog) and dialog.nested_check.isChecked()
    assert dialog.vlan_combo.currentData() == (nipr_domain.id, 20)  # The same number there
    text = dialog.summary.text()
    assert "<b>2</b> subnets and <b>3</b> recorded addresses" in text and "VLAN 20 (SIPR switches)" in text
    dialog.nested_check.setChecked(False)
    assert "<b>1</b> subnet and <b>2</b> recorded addresses" in dialog.summary.text()
    assert "1 subnet inside it stays" in dialog.summary.text()
    store.add_subnet(nipr.id, "10.20.3.0/24", "In the way")
    dialog.refresh()
    assert not dialog.move_button.isEnabled() and "is in the way" in dialog.problem_label.text()

    store.delete_subnet(next(subnet.id for subnet in store.subnets(nipr.id) if subnet.cidr == "10.20.3.0/24"))
    import nomad.ui.network_move_dialog as dialogs
    original = dialogs.MoveToNetworkDialog.exec_
    dialogs.MoveToNetworkDialog.exec_ = lambda self: (self.nested_check.setChecked(True), self.do_move(),
                                                      self.result())[-1]
    try:
        ipam.move_to_network()
    finally:
        dialogs.MoveToNetworkDialog.exec_ = original
    assert ipam.network_id == nipr.id and ipam.current.cidr == "10.20.0.0/22"  # Shown where it is now
    assert "Moved 10.20.0.0/22 and the subnet inside it to 11AB NIPR" in ipam.status_label.text()
    assert window.integration.network == f"local:{nipr.id}"  # The other pages follow


def test_add_subnet_with_role_and_vlan(page, monkeypatch):
    from nomad.ui import ipam_dialogs
    window, store = page
    sipr, _ = two_networks(store)
    vlans = VlanStore(store)
    domain = vlans.add_domain("SIPR switches", sipr.id)
    vlans.set_vlan(domain.id, 1, "DEFAULT")
    ipam = window.ipam_tab
    select_subnet(ipam, store, sipr, "10.30.0.0/24")

    def fill(dialog):
        dialog.cidr_input.setText("10.40.0.0/24")
        dialog.name_input.setText("New users")
        dialog.role_combo.setCurrentIndex(dialog.role_combo.findData("vlan"))
        assert dialog.vlan_combo.itemText(1) == "New VLAN 2 in SIPR switches (the next free)"
        dialog.vlan_combo.setCurrentIndex(1)
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(ipam_dialogs.SubnetDialog, "exec_", fill)
    ipam.add_subnet()
    assert vlans.vlan(domain.id, 2).subnets == ["10.40.0.0/24"]
    assert PlacementStore(store).role(sipr.id, "10.40.0.0/24").role == "vlan"


def test_deleting_a_subnet_takes_its_links_and_settings(page, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    window, store = page
    sipr, _ = two_networks(store)
    vlans, placements = VlanStore(store), PlacementStore(store)
    domain = vlans.add_domain("SIPR switches", sipr.id)
    vlans.set_vlan(domain.id, 30, "STAYS", subnets=["10.30.0.0/24"])
    placements.set_role(sipr.id, "10.30.0.0/24", "routed")
    ipam = window.ipam_tab
    select_subnet(ipam, store, sipr, "10.30.0.0/24")
    said = []

    def answer(box):
        said.append(box.informativeText())
        box.setProperty("chosen", True)
        delete = next(button for button in box.buttons() if box.buttonRole(button) == QMessageBox.AcceptRole)
        delete.click()
    monkeypatch.setattr(QMessageBox, "exec_", answer)
    ipam.delete_subnet()
    assert "VLAN 30 (SIPR switches) is removed too" in said[0] and "role and placement settings" in said[0]
    assert "10.30.0.0/24" not in cidrs(store, sipr)
    assert vlans.vlan(domain.id, 30).subnets == [] and placements.roles(sipr.id) == {}


def test_links_left_over_after_a_subnet_goes(local):
    from nomad.ipam.placement import WARNING, evaluate
    sipr, _ = two_networks(local)
    vlans = VlanStore(local)
    domain = vlans.add_domain("SIPR switches", sipr.id)
    vlans.set_vlan(domain.id, 30, subnets=["10.30.0.0/24"])
    local.delete_subnet(next(subnet.id for subnet in local.subnets(sipr.id) if subnet.cidr == "10.30.0.0/24"))
    rows = {row.cidr: row for row in evaluate(None, local, vlans, PlacementStore(local), sipr.id)}
    assert any(finding.severity == WARNING and "Not a subnet in IPAM now" in finding.text and
               "linked to VLAN 30 (SIPR switches)" in finding.text for finding in rows["10.30.0.0/24"].findings)


def test_move_button_under_the_subnet_list(page):
    import nomad.ui.network_move_dialog as dialogs
    window, store = page
    sipr, _ = two_networks(store)
    ipam = window.ipam_tab
    select_subnet(ipam, store, sipr, "10.30.0.0/24")
    assert ipam.move_subnet_button.isEnabled()
    opened = []
    original = dialogs.MoveToNetworkDialog.exec_
    dialogs.MoveToNetworkDialog.exec_ = lambda self: opened.append(self.subnet.cidr) or 0
    try:
        ipam.move_subnet_button.click()  # Clicked passes False: still the subnet selected
    finally:
        dialogs.MoveToNetworkDialog.exec_ = original
    assert opened == ["10.30.0.0/24"]
