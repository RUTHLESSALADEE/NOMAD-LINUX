"""Devices and links added by hand on the Network Map: kept when mapping again, folded into what a crawl finds,
asked over SNMP like the crawl's devices, monitored, and drawn by hand on the map."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from netmap_fakes import build_network  # noqa: E402
from PyQt5.QtCore import QPoint, QSettings, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox, QWidget  # noqa: E402

from nomad.netmap import export, store  # noqa: E402
from nomad.netmap.crawl import CrawlSettings, Crawler, check_device  # noqa: E402
from nomad.netmap.model import NO_SNMP, SNMP, SWITCH, UNCHECKED, UNKNOWN, UNREACHABLE, Device, Host, Link, \
    NetworkMap  # noqa: E402
from nomad.ui import netmap_tab  # noqa: E402
from nomad.ui.netmap_dialogs import DeviceDialog, LinkDialog  # noqa: E402
from nomad.ui.netmap_view import LinkItem  # noqa: E402

SETTINGS = dict(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")])


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


def crawl(**options):
    network = build_network()
    return Crawler(CrawlSettings(**{**SETTINGS, **options}), client_factory=network.client, pinger=network.ping,
                   echo=network.echo).run()


def closet(key="manual:1", **details):
    """An unmanaged switch added by hand, off acc1."""
    return Device(key=key, **{"name": "closet-sw", "kind": SWITCH, "manual": True, "source": UNCHECKED, **details})


def with_closet(network_map, **details):
    device = closet(**details)
    network_map.devices[device.key] = device
    network_map.add_link(Link("acc1", "Gi1/0/24", device.key, "Port 1", manual=True))
    network_map.hosts.append(Host(mac="", device=device.key, port="Port 3", name="label-printer", manual=True))
    network_map.positions[device.key] = (900.0, 900.0)
    return device


@pytest.fixture
def tab(app, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "maps_dir", lambda: tmp_path)
    page = netmap_tab.NetworkMapTab(Window())
    page.resize(1200, 800)
    network = build_network()
    page.check_device = lambda settings, address: check_device(settings, address, client_factory=network.client,
                                                               pinger=network.ping)
    yield page
    page.shutdown()


def finish_checks(tab):
    for thread in list(tab.check_threads):
        thread.wait(5000)
    QApplication.processEvents()  # The answers arrive as queued signals
    QApplication.processEvents()


# ----------------------------------------------------------------- The model

def test_hand_added_devices_links_and_hosts_carry_over():
    older = crawl()
    with_closet(older)
    newer = crawl()
    folded, left_out = newer.carry_manual(older)
    newer.carry_manual_hosts(older)
    assert (folded, left_out) == ([], 0)
    assert newer.devices["manual:1"].manual and newer.devices["manual:1"] is not older.devices["manual:1"]
    link = next(link for link in newer.links if link.manual)
    assert (link.a, link.a_port, link.b, link.b_port) == ("acc1", "Gi1/0/24", "manual:1", "Port 1")
    assert [host.name for host in newer.hosts if host.device == "manual:1"] == ["label-printer"]  # Just once

    again = NetworkMap.from_json(newer.to_json())  # Saved and opened again
    assert again.devices["manual:1"].manual and next(link for link in again.links if link.manual)


def test_links_to_devices_gone_are_left_out():
    older = crawl()
    with_closet(older)
    older.add_link(Link("pa-fw1", "ethernet1/2", "manual:1", "Port 2", manual=True))
    newer = crawl(scope=["10.0.0.0/30"])  # Only the core read; the firewall isn't there
    del newer.devices["pa-fw1"]
    newer.links = [link for link in newer.links if "pa-fw1" not in (link.a, link.b)]
    _, left_out = newer.carry_manual(older)
    assert left_out == 1


def test_a_device_the_crawl_finds_takes_over_from_the_hand_added_one():
    older = crawl()
    # Added by hand with acc2's address before the crawl could read it: links, hosts, place and group move over
    del older.devices["acc2"]
    older.links = [link for link in older.links if "acc2" not in (link.a, link.b)]
    older.hosts = [host for host in older.hosts if host.device != "acc2"]
    hand = with_closet(older, mgmt_ip="10.0.0.12", name="", note="In the closet")
    older.add_link(Link("pa-fw1", "ethernet1/9", hand.key, "Eth1/2", manual=True))
    group = older.new_group("Head Office")
    older.set_group([hand.key], group.key)
    newer = crawl()
    folded, _ = newer.carry_manual(older)
    newer.carry_groups(older, folded)
    assert [(manual.key, found.key) for manual, found in folded] == [("manual:1", "acc2")]
    assert "manual:1" not in newer.devices
    acc2 = newer.devices["acc2"]
    assert (acc2.manual, acc2.note, acc2.kind) == (False, "In the closet", SWITCH)
    assert any(host.name == "label-printer" and host.device == "acc2" for host in newer.hosts)
    pairs = {frozenset((link.a, link.b)): link for link in newer.links}
    assert pairs[frozenset(("acc1", "acc2"))].manual  # No crawled link between them: kept
    assert pairs[frozenset(("pa-fw1", "acc2"))].manual
    assert newer.group_of["acc2"] == group.key


def test_a_link_drawn_by_hand_goes_when_the_crawl_finds_one():
    network_map = crawl()
    with_closet(network_map, name="acc2")  # The same name as a switch the crawl found
    network_map.add_link(Link("core", "Te1/1/9", "manual:1", "", manual=True))
    folded = network_map.fold_manual_devices()
    assert [found.key for _, found in folded] == ["acc2"]
    core_acc2 = [link for link in network_map.links if {link.a, link.b} == {"core", "acc2"}]
    assert len(core_acc2) == 1 and not core_acc2[0].manual  # The crawl's, not the one drawn by hand

    same = Link("core", "Te1/0/1", "acc1", "Te1/1/1", manual=True)  # The very link the crawl found
    assert not network_map.add_link(same).manual


def test_crawl_from_here_reads_a_hand_added_device_that_answers_snmp():
    first = crawl(scope=["10.0.0.0/30"])
    del first.devices["acc2"]
    first.links = [link for link in first.links if "acc2" not in (link.a, link.b)]
    hand = with_closet(first, mgmt_ip="10.0.0.12", name="", source=SNMP)  # Checked: it answers
    network = build_network()
    newer = Crawler(CrawlSettings(seeds=["10.0.0.12"], overrides=[("10.0.0.12/32", "secret")], trace=False),
                    client_factory=network.client, pinger=network.ping, echo=network.echo, known=first).run()
    assert newer.devices["acc2"].source == SNMP  # Read, not taken for the hand-added one already read
    first.merge_crawl(newer)
    folded = first.fold_manual_devices()
    assert [manual.key for manual, _ in folded] == [hand.key] and "manual:1" not in first.devices


# ----------------------------------------------------------------- Asking over SNMP

def test_check_device_says_what_a_crawl_would():
    network = build_network()
    settings = CrawlSettings(**SETTINGS)
    answered = check_device(settings, "10.0.0.12", client_factory=network.client, pinger=network.ping)
    assert answered.source == SNMP and answered.info.name == "acc2"
    no_override = check_device(CrawlSettings(seeds=["10.0.0.12"]), "10.0.0.12", client_factory=network.client,
                               pinger=network.ping)
    assert no_override.source == NO_SNMP and "community string" in no_override.error  # Wrong community
    assert check_device(settings, "10.0.0.254", client_factory=network.client,
                        pinger=network.ping).source == NO_SNMP
    gone = check_device(settings, "10.0.0.99", client_factory=network.client, pinger=network.ping)
    assert gone.source == UNREACHABLE

    device = Device(key="manual:1", mgmt_ip="10.0.0.12", kind=UNKNOWN, manual=True)
    answered.apply(device)
    assert (device.source, device.name, device.kind) == (SNMP, "acc2", SWITCH)
    assert device.found_by == "Added by hand, answers SNMP"
    no_override.apply(device)
    assert device.found_by == "Added by hand, pings, no SNMP" and device.error


# ----------------------------------------------------------------- The dialogs

def test_device_dialog_checks_what_is_entered(app):
    network_map = crawl()
    dialog = DeviceDialog(network_map, linked_to="acc1")
    dialog.name_input.setText("closet-sw")
    dialog.there_port.setEditText("GigabitEthernet1/0/24")
    dialog.here_port.setEditText("Port 1")
    device, link = dialog.values()
    assert (device.key, device.name, device.kind, device.manual, device.source) == ("", "closet-sw", SWITCH, True,
                                                                                    UNCHECKED)
    assert (link.a, link.a_port, link.b, link.b_port, link.manual) == ("acc1", "Gi1/0/24", "", "Port 1", True)
    dialog.ip_input.setText("10.0.0.11")
    with pytest.raises(ValueError, match="acc1"):
        dialog.values()
    dialog.ip_input.setText("10.0.0.300")
    with pytest.raises(ValueError, match="isn't an IP"):
        dialog.values()
    dialog.ip_input.setText("")
    dialog.name_input.setText("ACC2.corp.example")
    with pytest.raises(ValueError, match="already a device called"):
        dialog.values()
    dialog.name_input.setText("")
    with pytest.raises(ValueError, match="name or an IP"):
        dialog.values()

    hand = with_closet(network_map, mgmt_ip="10.0.0.40", source=NO_SNMP, error="Answers ping but not SNMP")
    edit = DeviceDialog(network_map, device=hand)
    assert edit.link_combo is None  # Links are drawn separately once it's on the map
    edit.note_input.setText("Under the desk")
    edited, _ = edit.values()
    assert (edited.key, edited.source, edited.note) == ("manual:1", NO_SNMP, "Under the desk")  # Same address
    edit.ip_input.setText("10.0.0.41")
    assert edit.values()[0].source == UNCHECKED  # A new address: to be asked again


def test_link_dialog_checks_what_is_entered(app):
    network_map = crawl()
    dialog = LinkDialog(network_map, "core", "acc1")
    dialog.a_port.setEditText("Te1/0/1")
    dialog.b_port.setEditText("TenGigabitEthernet1/1/1")
    with pytest.raises(ValueError, match="already on the map"):
        dialog.values()  # The crawl found that one
    dialog.b_port.setEditText("Te1/0/2")
    link = dialog.values()
    assert (link.a, link.b, link.b_port, link.manual) == ("core", "acc1", "Te1/0/2", True)
    dialog.b_combo.setCurrentIndex(dialog.b_combo.findData("core"))
    with pytest.raises(ValueError, match="two different"):
        dialog.values()


# ----------------------------------------------------------------- The page

def accept_with(monkeypatch, dialog_class, fill):
    """Make the page's dialog fill itself in and say OK."""
    class Filled(dialog_class):
        def exec_(self):
            fill(self)
            return QDialog.Accepted
    monkeypatch.setattr(netmap_tab, dialog_class.__name__, Filled)


def test_adding_a_device_checks_it_monitors_it_and_keeps_it(tab, monkeypatch):
    tab.on_crawled(crawl())

    def fill(dialog):
        dialog.name_input.setText("closet-sw")
        dialog.ip_input.setText("10.0.0.253")  # Pings, no SNMP
        dialog.link_combo.setCurrentIndex(dialog.link_combo.findData("acc1"))
        dialog.there_port.setEditText("Gi1/0/24")
    accept_with(monkeypatch, DeviceDialog, fill)
    tab.add_device(linked_to="acc1")
    device = tab.network_map.devices["manual:1"]
    assert "manual:1" in tab.view.items_by_key and tab.view.items_by_key["manual:1"].isSelected()
    assert any(link.manual and {link.a, link.b} == {"acc1", "manual:1"} for link in tab.network_map.links)
    assert "manual:1" in tab.monitor.targets  # Pinged while monitoring, like the rest
    assert "Checking whether it answers SNMP" in tab.status_label.text()

    finish_checks(tab)
    assert device.source == NO_SNMP and "community string" in device.error
    assert "community string" in tab.status_label.text()
    row = next(row for row in export.device_rows(tab.network_map) if row[0] == "closet-sw")
    assert row[export.DEVICE_COLUMNS.index("Found by")] == "Added by hand, pings, no SNMP"
    assert "Added by hand, pings, no SNMP" in netmap_tab.device_html(tab.network_map, "manual:1")
    saved = store.load(tab.map_path)
    assert saved.devices["manual:1"].manual and saved.devices["manual:1"].source == NO_SNMP

    tab.on_crawled(crawl())  # Mapping again: still there, linked, and asked again
    assert tab.network_map.devices["manual:1"].manual
    assert any(link.manual for link in tab.network_map.links)
    assert "manual:1" in tab.checking or tab.check_threads
    finish_checks(tab)
    assert tab.network_map.devices["manual:1"].source == NO_SNMP


def test_adding_a_device_with_no_map_open_starts_one(tab, monkeypatch):
    accept_with(monkeypatch, DeviceDialog, lambda dialog: dialog.name_input.setText("lonely-sw"))
    tab.add_device(place=(10.0, 20.0))
    assert list(tab.network_map.devices) == ["manual:1"]
    assert tab.view.positions()["manual:1"] == (10.0, 20.0)
    assert tab.map_path is not None and store.load(tab.map_path).devices["manual:1"].name == "lonely-sw"
    assert "IP address" in tab.status_label.text()


def test_editing_and_deleting_a_device_added_by_hand(tab, monkeypatch):
    network_map = crawl()
    del network_map.devices["acc2"]  # Not read yet, so its address is free to give the one added by hand
    network_map.links = [link for link in network_map.links if "acc2" not in (link.a, link.b)]
    network_map.hosts = [host for host in network_map.hosts if host.device != "acc2"]
    with_closet(network_map)
    tab.on_crawled(network_map)
    hand = tab.network_map.devices["manual:1"]

    accept_with(monkeypatch, DeviceDialog, lambda dialog: dialog.ip_input.setText("10.0.0.12"))
    tab.edit_device("manual:1")
    finish_checks(tab)
    assert tab.network_map.devices["manual:1"].source == NO_SNMP  # Its community isn't one the page has
    tab.overrides = [("10.0.0.12/32", "secret")]
    tab.check_devices(["manual:1"], announce=True)
    finish_checks(tab)
    assert tab.network_map.devices["manual:1"].source == SNMP  # The page's community override worked
    assert "Crawl from Here" in tab.status_label.text()

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    tab.delete_devices(["manual:1"])
    assert "manual:1" not in tab.network_map.devices and "manual:1" not in tab.view.items_by_key
    assert not any(link.manual for link in tab.network_map.links)
    assert not any(host.name == "label-printer" for host in tab.network_map.hosts)
    assert hand.key not in store.load(tab.map_path).devices


def test_drawing_a_link_on_the_map(tab, monkeypatch):
    tab.on_crawled(crawl())
    tab.tabs.setCurrentWidget(tab.view)
    tab.show()
    QApplication.processEvents()
    ports = {}

    def fill(dialog):
        ports["ends"] = (dialog.a_combo.currentData(), dialog.b_combo.currentData())
        dialog.a_port.setEditText("Gi1/0/48")
    accept_with(monkeypatch, LinkDialog, fill)
    tab.draw_link_from("acc1")
    assert tab.view.drawing is not None
    QTest.mouseClick(tab.view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     tab.view.mapFromScene(tab.view.items_by_key["acc2"].pos()))
    assert tab.view.drawing is None and ports["ends"] == ("acc1", "acc2")
    drawn = next(link for link in tab.network_map.links if link.manual)
    assert (drawn.a, drawn.a_port, drawn.b) == ("acc1", "Gi1/0/48", "acc2")
    line = next(item for item in tab.view.link_items if drawn in item.links)
    assert isinstance(line, LinkItem) and line.manual
    row = next(row for row in range(tab.links_table.rowCount())
               if tab.links_table.item(row, 0).data_object.link is drawn)
    assert tab.links_table.item(row, export.LINK_COLUMNS.index("Seen by")).text() == "Drawn by hand"

    tab.draw_link_from("core")  # A click on the background stops
    QTest.mouseClick(tab.view.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(2, 2))
    assert tab.view.drawing is None and sum(link.manual for link in tab.network_map.links) == 1

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    tab.delete_links([drawn])
    assert drawn not in tab.network_map.links
    tab.hide()
