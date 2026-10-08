"""Carry VLAN on the Network Map page: opening it from the menus, Plan (reading the switches again first), sending
each switch's step, Verify, and the route set by hand (Pick on Map, Fill In Between, Edit This Route)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from netmap_fakes import CISCO_SWITCH, Device, FakeNetwork, number  # noqa: E402
from PyQt5.QtCore import Qt, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu, QMessageBox, QWidget  # noqa: E402

from nomad.netmap import collect, store  # noqa: E402
from nomad.netmap.crawl import CrawlSettings, Crawler, read_vlans_of  # noqa: E402
from nomad.ui import netmap_tab  # noqa: E402
from nomad.ui.terminal_view import CONNECTED  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Window(QWidget):
    adapter_changed = pyqtSignal(object)
    snapshot_changed = pyqtSignal(object)

    def current_adapter(self):
        return None

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Model:
    def cursor_position(self):
        return type("Position", (), {"line": 0})()

    def line_text(self, line):
        return "sw2#"


class View:
    title, state, model = "sw2", CONNECTED, Model()

    def __init__(self):
        self.sent = []

    def send_block(self, text, final_enter=True, min_delay=0):
        self.sent.append(text)
        return True


def switch(name, address):
    device = Device(name, "Cisco IOS Software, Catalyst L3 Switch Software", CISCO_SWITCH)
    device.interface(51, "Vlan1")
    device.address(address, 51)
    device.set(collect.STP_TYPE, 0, number(5))  # Rapid-PVST+
    device.vlan(1, "default")
    return device


def chain():
    """core (VLAN 20 and its gateway, Vlan20) - sw1 (has 20; its link to sw2 allows only 1) - sw2 (no VLAN 20)."""
    network = FakeNetwork()
    core = network.add("10.0.0.1", switch("core", "10.0.0.1"))
    core.interface(1, "GigabitEthernet0/1")
    core.interface(50, "Vlan20")
    core.address("10.1.20.1", 50)
    core.vlan(20, "USERS")
    core.trunk(1, [1, 20])
    core.cdp(1, 1, "sw1", "GigabitEthernet0/1", "10.0.0.2", "cisco WS-C3850", 0x28)
    sw1 = network.add("10.0.0.2", switch("sw1", "10.0.0.2"))
    sw1.interface(1, "GigabitEthernet0/1")
    sw1.interface(2, "GigabitEthernet0/2")
    sw1.vlan(20, "USERS")
    sw1.trunk(1, [1, 20])
    sw1.trunk(2, [1])
    sw1.cdp(1, 1, "core", "GigabitEthernet0/1", "10.0.0.1", "cisco WS-C3850", 0x28)
    sw1.cdp(2, 2, "sw2", "GigabitEthernet0/1", "10.0.0.3", "cisco WS-C2960", 0x28)
    sw2 = network.add("10.0.0.3", switch("sw2", "10.0.0.3"))
    sw2.interface(1, "GigabitEthernet0/1")
    sw2.interface(5, "GigabitEthernet0/5")
    sw2.vlan(10, "STAFF")
    sw2.trunk(1, [1])
    sw2.access(5, 10)
    sw2.cdp(1, 1, "sw1", "GigabitEthernet0/2", "10.0.0.2", "cisco WS-C3850", 0x28)
    return network


@pytest.fixture
def setup(app, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "maps_dir", lambda: tmp_path)
    network = chain()
    page = netmap_tab.NetworkMapTab(Window())
    page.resize(1200, 800)
    page.read_vlans = lambda settings, address, **options: read_vlans_of(settings, address,
                                                                          client_factory=network.client, **options)
    network_map = Crawler(CrawlSettings(seeds=["10.0.0.1"], trace=False, collect_hosts=False),
                          client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    page.on_crawled(network_map)
    keys = {device.label: key for key, device in page.network_map.devices.items()}
    yield page, network, keys
    if page.carry_dialog is not None:
        page.carry_dialog.close()
    page.shutdown()


def wait_for(dialog):
    while dialog.thread is not None:
        dialog.thread.wait(10000)
        QApplication.processEvents()
    QApplication.processEvents()


def row_texts(table, column):
    return [table.item(row, column).text() for row in range(table.rowCount())]


def test_carry_a_vlan_plan_send_and_verify(setup, monkeypatch):
    page, network, keys = setup
    page.carry_vlan(b=keys["sw2"], port="Gi0/5")
    dialog = page.carry_dialog
    assert dialog.isVisible() and dialog.b_combo.currentData() == keys["sw2"]
    assert [dialog.edge_list.item(row).checkState() == Qt.Checked for row in range(dialog.edge_list.count())][0]
    dialog.set_vlan(20)
    assert dialog.name_input.text() == "USERS"  # The name the map already has for it
    dialog.plan_button.click()
    wait_for(dialog)
    plan = dialog.plan
    assert plan.ok and plan.route == [keys["sw1"], keys["sw2"]]
    assert {keys["sw1"], keys["sw2"]} <= dialog.read_keys
    assert dialog.stp[keys["sw1"]].mode == "rapid-pvst"
    assert row_texts(dialog.steps_table, 0) == ["1. sw1", "2. sw2"]
    assert "allow it on Gi0/2" in dialog.steps_table.item(0, 1).text()
    assert "create VLAN 20" in dialog.steps_table.item(1, 1).text()
    assert "Gi0/5" in dialog.steps_table.item(1, 1).text()
    assert "reachable" in dialog.gateway_label.text()
    assert page.view.items_by_key[keys["sw2"]].highlight is not None
    dialog.steps_table.selectRow(1)
    assert dialog.preview.toPlainText().startswith("configure terminal\nvlan 20\n name USERS")
    dialog.preview_combo.setCurrentIndex(1)
    assert "no vlan 20" in dialog.preview.toPlainText()

    view = View()
    monkeypatch.setattr(QMessageBox, "exec_", lambda self: QMessageBox.Yes)
    assert dialog.session_sender.send_to(view, dialog.step_text(1, "config"), tag=(1, "config"))
    assert view.sent and "switchport trunk allowed vlan add 20" in view.sent[0]
    assert dialog.steps_table.item(1, 2).text() == "Sent to sw2"

    # The switches as they'd be once both steps were typed in
    network.devices["10.0.0.2"].trunk(2, [1, 20])
    network.devices["10.0.0.3"].vlan(20, "USERS")
    network.devices["10.0.0.3"].trunk(1, [1, 20])
    network.devices["10.0.0.3"].access(5, 20)
    dialog.verify_button.click()
    wait_for(dialog)
    assert row_texts(dialog.steps_table, 2) == ["Done", "Done"]
    assert "Verified" in dialog.status_label.text()
    dialog.close()
    assert page.view.items_by_key[keys["sw2"]].highlight is None


def test_verify_says_what_isnt_done(setup):
    page, network, keys = setup
    page.carry_vlan(vlan=20, b=keys["sw2"])
    dialog = page.carry_dialog
    dialog.plan_button.click()
    wait_for(dialog)
    network.devices["10.0.0.2"].trunk(2, [1, 20])  # Only sw1 done
    dialog.verify_button.click()
    wait_for(dialog)
    statuses = row_texts(dialog.steps_table, 2)
    assert statuses[0] == "Done" and statuses[1].startswith("Not done") and "VLAN 20 isn't on sw2" in statuses[1]


def test_route_set_by_hand_picked_on_the_map(setup):
    page, network, keys = setup
    page.carry_vlan(vlan=20)
    dialog = page.carry_dialog
    dialog.hand_radio.setChecked(True)
    dialog.toggle_picking()
    assert page.view.picking and dialog.pick_button.text() == "Stop Picking"
    page.view.device_picked.emit(keys["core"])
    page.view.device_picked.emit(keys["core"])  # The same one twice in a row: once
    page.view.device_picked.emit(keys["sw2"])
    page.on_escape()
    assert not page.view.picking and dialog.pick_button.text() == "Pick on Map"
    assert dialog.route == [keys["core"], keys["sw2"]]
    assert "No link between core and sw2" in dialog.route_table.item(0, 2).text()
    assert "Fix the route first" in dialog.check_ready()
    dialog.fill_in_between()
    assert dialog.route == [keys["core"], keys["sw1"], keys["sw2"]]
    assert dialog.check_ready() == ""
    dialog.plan_button.click()
    wait_for(dialog)
    plan = dialog.plan
    assert plan.by_hand and plan.route == [keys["core"], keys["sw1"], keys["sw2"]]
    assert not dialog.edit_route_button.isEnabled()  # Already set by hand


def test_edit_this_route_copies_the_way_nomad_chose(setup):
    page, network, keys = setup
    page.carry_vlan(vlan=20, b=keys["sw2"])
    dialog = page.carry_dialog
    dialog.plan_button.click()
    wait_for(dialog)
    dialog.edit_route()
    assert dialog.hand_radio.isChecked() and dialog.route == [keys["sw1"], keys["sw2"]]
    assert dialog.plan is None  # Changed: plan again


def test_changing_anything_needs_a_new_plan(setup):
    page, network, keys = setup
    page.carry_vlan(vlan=20, b=keys["sw2"])
    dialog = page.carry_dialog
    dialog.plan_button.click()
    wait_for(dialog)
    assert dialog.plan is not None and dialog.verify_button.isEnabled()
    dialog.set_vlan(30)
    assert dialog.plan is None and not dialog.verify_button.isEnabled() and dialog.steps_table.rowCount() == 0


def test_menus_open_it(setup):
    page, network, keys = setup
    menu, actions = QMenu(), {}
    page.carry_vlan_menu(menu, actions, keys["sw2"], [keys["sw2"], keys["core"]])
    labels = [action.text() for action in actions]
    assert labels == ["Carry a VLAN Here...", "Carry a VLAN Between These..."]
    next(handler for action, handler in actions.items() if action.text().startswith("Carry a VLAN Between"))()
    dialog = page.carry_dialog
    assert dialog.a_combo.currentData() == keys["core"] and dialog.b_combo.currentData() == keys["sw2"]
    page.vlan_panel.carry_requested.emit(20, "")
    assert dialog.vlan() == 20 and dialog.isVisible()


def test_ticking_a_redundant_link_plans_again_without_reading(setup):
    page, network, keys = setup
    # A second way to sw2, straight from core, allowing only VLAN 1: a triangle once 20 reaches sw2
    core, sw2 = network.devices["10.0.0.1"], network.devices["10.0.0.3"]
    core.interface(2, "GigabitEthernet0/2")
    core.trunk(2, [1])
    core.cdp(2, 2, "sw2", "GigabitEthernet0/2", "10.0.0.3", "cisco WS-C2960", 0x28)
    sw2.interface(2, "GigabitEthernet0/2")
    sw2.trunk(2, [1])
    sw2.cdp(2, 2, "core", "GigabitEthernet0/2", "10.0.0.1", "cisco WS-C3850", 0x28)
    page.on_crawled(Crawler(CrawlSettings(seeds=["10.0.0.1"], trace=False, collect_hosts=False),
                            client_factory=network.client, pinger=network.ping, echo=network.echo).run())
    keys = {device.label: key for key, device in page.network_map.devices.items()}
    page.carry_vlan(vlan=20, b=keys["sw2"])
    dialog = page.carry_dialog
    dialog.plan_button.click()
    wait_for(dialog)
    assert dialog.redundant_list.count() == 1 and "Rapid-PVST+" in dialog.redundant_list.item(0).text()
    steps = dialog.steps_table.rowCount()
    asked = len(network.requests)
    dialog.redundant_list.item(0).setCheckState(Qt.Checked)
    assert dialog.thread is None and len(network.requests) == asked  # Planned again from what was read
    assert dialog.steps_table.rowCount() > steps
    assert dialog.steps_table.item(dialog.steps_table.rowCount() - 1, 1).text().endswith("(redundant links)")


def test_a_switch_that_doesnt_answer_is_planned_from_the_map(setup):
    page, network, keys = setup
    page.read_vlans = lambda settings, address, **options: (None, None)
    page.carry_vlan(vlan=20, b=keys["sw2"])
    dialog = page.carry_dialog
    dialog.plan_button.click()
    wait_for(dialog)
    assert dialog.plan.ok and dialog.plan.route == [keys["sw1"], keys["sw2"]]
    assert any("Didn't answer SNMP" in dialog.findings_list.item(row).text()
               for row in range(dialog.findings_list.count()))


def test_open_session_to_a_switch_uses_its_saved_telnet_session_without_ssh(setup):
    from nomad.terminal.sessions import SSH, TELNET
    page, network, keys = setup
    opened = []

    class Terminal:
        def saved_matches(self, host, aliases=(), protocol=SSH):
            return ["r2s3 telnet"] if protocol == TELNET else []

        def open_address(self, address, protocol, **options):
            opened.append((address, protocol))
            return None

    page.carry_vlan(vlan=20, b=keys["sw2"])
    dialog = page.carry_dialog
    dialog.session_sender.terminal = lambda: Terminal()
    dialog.session_sender.open_device({"address": "10.0.0.3", "aliases": ["sw2"], "name": "sw2"}, lambda: "")
    assert opened == [("10.0.0.3", TELNET)]
