"""The Network Map page: showing a crawled map, finding things on it, the tables, exports and settings."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from netmap_fakes import LAB_MACS, PC1_MAC, build_network  # noqa: E402
from PyQt5.QtCore import QSettings, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from nomad.netmap import store  # noqa: E402
from nomad.netmap.crawl import CrawlSettings, Crawler  # noqa: E402
from nomad.ui import netmap_tab  # noqa: E402
from nomad.ui.netmap_view import DeviceItem, HostPortItem, LinkItem  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Window(QWidget):
    """Stands in for the main window."""
    adapter_changed = pyqtSignal(object)
    snapshot_changed = pyqtSignal(object)

    def current_adapter(self):
        return None

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def crawled():
    network = build_network()
    return Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                   client_factory=network.client, pinger=network.ping,
                   echo=network.echo).run()


@pytest.fixture
def tab(app, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "maps_dir", lambda: tmp_path)
    page = netmap_tab.NetworkMapTab(Window())
    page.resize(1200, 800)
    yield page
    page.shutdown()


def test_showing_a_map(tab, crawled, tmp_path):
    tab.on_crawled(crawled)
    items = tab.view.scene().items()
    assert sum(isinstance(item, DeviceItem) for item in items) == 5
    assert sum(isinstance(item, LinkItem) for item in items) == 4
    assert tab.devices_table.rowCount() == 5
    assert tab.links_table.rowCount() == 4
    assert tab.hosts_table.rowCount() == len(crawled.hosts)
    assert tab.map_path is not None and tab.map_path.parent == tmp_path  # Saved automatically
    assert "Done: 5 devices" in tab.status_label.text()
    device_items = {item.device.key: item for item in items if isinstance(item, DeviceItem)}
    for key in device_items:  # No two devices on top of each other
        for other in device_items:
            if key < other:
                a, b = device_items[key].pos(), device_items[other].pos()
                assert (a - b).manhattanLength() > 50


def test_finding_a_host_opens_its_switch(tab, crawled):
    tab.on_crawled(crawled)
    assert tab.view.find(PC1_MAC.replace("-", ":").lower())
    selected = tab.view.scene().selectedItems()
    assert len(selected) == 1 and isinstance(selected[0], HostPortItem) and selected[0].port == "Gi1/0/5"
    assert "Gi1/0/5" in tab.details.toPlainText()
    assert tab.view.find("pa-fw")
    assert "Firewall" in tab.details.toPlainText()
    assert not tab.view.find("no-such-thing")


def test_expanding_hosts_and_shared_ports(tab, crawled):
    tab.on_crawled(crawled)
    acc2 = tab.view.items_by_key["acc2"]
    assert acc2.host_count == len(LAB_MACS)
    tab.view.toggle_hosts(acc2)
    assert [item.port for item in acc2.port_items] == ["Eth1/10"]
    tab.view.toggle_hosts(acc2)
    assert acc2.port_items == []


def test_dragged_positions_are_saved_and_kept_after_recrawl(tab, crawled):
    tab.on_crawled(crawled)
    tab.view.items_by_key["core"].setPos(5000, 5000)
    tab.save_positions()
    reloaded = store.load(tab.map_path)
    assert reloaded.positions["core"] == (5000, 5000)
    network = build_network()
    again = Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                    client_factory=network.client, pinger=network.ping,
                   echo=network.echo).run()
    tab.on_crawled(again)
    assert tab.view.items_by_key["core"].pos().x() == 5000


def test_exports(tab, crawled, tmp_path):
    tab.on_crawled(crawled)
    image = tab.view.render_image(scale=1)
    assert image.width() > 300 and image.height() > 100
    svg = tmp_path / "map.svg"
    tab.view.render_svg(svg)
    assert "<svg" in svg.read_text(encoding="utf-8")


def test_settings_round_trip(tab, tmp_path, app):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    tab.seeds_input.setText("10.0.0.1")
    tab.communities, tab.overrides = ["public", "backup"], [("10.20.0.0/16", "secret")]
    tab.scope, tab.max_hops = ["10.0.0.0/8"], 3
    tab.save_settings(settings)
    settings.sync()
    assert "secret" not in (tmp_path / "settings.ini").read_text(encoding="utf-8", errors="replace")
    other = netmap_tab.NetworkMapTab(Window())
    other.restore_settings(settings)
    assert other.seeds_input.text() == "10.0.0.1"
    assert other.communities == ["public", "backup"] and other.overrides == [("10.20.0.0/16", "secret")]
    assert other.scope == ["10.0.0.0/8"] and other.max_hops == 3


def test_device_details_list_links_and_hosts(crawled):
    text = netmap_tab.device_html(crawled, "acc1")
    assert "Te1/1/1" in text and "core.corp.example" in text
    assert "SEP00AABBCCDDEE" in text


def test_logical_view(tab, crawled):
    tab.on_crawled(crawled)
    keys = set(tab.l3_view.items_by_key)
    assert {"core", "pa-fw1", "rtr1", "net:10.0.0.0/24", "net:10.10.0.0/24", "hop:10.0.0.253", "self"} <= keys
    tab.tabs.setCurrentWidget(tab.l3_view)
    tab.find_input.setText("10.10.0.0")
    tab.find()
    text = tab.details.toPlainText()
    assert "Hosts on the map (3)" in text and "core.corp.example" in text
    assert tab.l3_view.find("10.99.0.1")
    assert "10.50.0.1" in tab.details.toPlainText()  # The trace that found it
    assert "Routes" in netmap_tab.device_html(crawled, "core")


def test_logical_drawio_export(tab, crawled, tmp_path, monkeypatch):
    tab.on_crawled(crawled)
    tab.tabs.setCurrentWidget(tab.l3_view)
    target = tmp_path / "logical.drawio"
    monkeypatch.setattr(tab, "export_path", lambda *args: target)
    tab.export_drawio()
    text = target.read_text(encoding="utf-8")
    assert "10.10.0.0/24" in text and "dashed=1" in text


def test_compare_with_an_older_map(tab, crawled, tmp_path):
    import copy
    older = copy.deepcopy(crawled)
    del older.devices["acc2"]
    older.links = [link for link in older.links if "acc2" not in (link.a, link.b)]
    older.hosts = [host for host in older.hosts if host.device != "acc2"]
    older_path = store.save(older, tmp_path / "older.nomadmap")
    tab.on_crawled(crawled)
    tab.compare_with(older_path)
    dialog = tab.compare_dialog
    assert dialog is not None and dialog.table.rowCount() >= 2  # The device and its link (hosts hidden)
    assert tab.view.items_by_key["acc2"].highlight is not None
    assert tab.view.items_by_key["core"].highlight is None
    dialog.churn_check.setChecked(True)
    assert dialog.table.rowCount() == 2 + len(LAB_MACS)
    dialog.close()
    assert tab.view.items_by_key["acc2"].highlight is None


def test_scope_dialog_values(app):
    from nomad.ui.netmap_dialogs import ScopeDialog
    dialog = ScopeDialog(["10.0.0.0/8"], 4, 100, True, False)
    assert dialog.values() == (["10.0.0.0/8"], 4, 100, True, False)


def test_host_dialog_checks_what_is_entered(tab, crawled):
    from nomad.ui.netmap_dialogs import HostDialog
    dialog = HostDialog(crawled, device="acc1", port="GigabitEthernet1/0/20")
    dialog.name_input.setText("label-printer")
    dialog.mac_input.setText("00:11:22:33:44:55")
    dialog.vlan_input.setValue(10)
    host = dialog.values()
    assert (host.device, host.port, host.mac, host.vlan, host.manual) == \
        ("acc1", "Gi1/0/20", "00-11-22-33-44-55", 10, True)
    dialog.mac_input.setText("not a mac")
    with pytest.raises(ValueError, match="isn't a MAC"):
        dialog.values()
    dialog.mac_input.setText(PC1_MAC)
    with pytest.raises(ValueError, match="already on the map"):
        dialog.values()
    dialog.mac_input.setText("")
    dialog.ip_input.setText("10.10.0.300")
    with pytest.raises(ValueError, match="isn't an IP"):
        dialog.values()
    dialog.ip_input.setText("")
    dialog.name_input.setText("")
    with pytest.raises(ValueError, match="at least"):
        dialog.values()
    assert "Gi1/0/5" in [dialog.port_combo.itemText(index) for index in range(dialog.port_combo.count())]


def test_adding_and_deleting_hosts(tab, crawled, monkeypatch):
    from nomad.netmap.model import Host
    from PyQt5.QtWidgets import QMessageBox
    tab.on_crawled(crawled)
    before = len(crawled.hosts)
    printer = Host(mac="", device="acc1", port="Gi1/0/20", ip="10.10.0.50", name="old-printer", manual=True)
    crawled.hosts.append(printer)
    tab.hosts_changed(printer)
    assert tab.hosts_table.rowCount() == before + 1
    assert "Gi1/0/20" in [item.port for item in tab.view.items_by_key["acc1"].port_items]  # Opened to show it
    assert any(host.name == "old-printer" and host.manual for host in store.load(tab.map_path).hosts)

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    lab = [host for host in crawled.hosts if host.mac in LAB_MACS]
    tab.delete_hosts(lab)
    assert tab.hosts_table.rowCount() == before + 1 - len(LAB_MACS)
    assert tab.view.items_by_key["acc2"].host_count == 0
    assert not any(host.mac in LAB_MACS for host in store.load(tab.map_path).hosts)


def test_hand_added_hosts_survive_mapping_again(tab, crawled):
    from nomad.netmap.model import Host
    tab.on_crawled(crawled)
    crawled.hosts.append(Host(mac="", device="acc1", port="Gi1/0/20", ip="10.10.0.50", name="old-printer",
                              manual=True))
    crawled.hosts.append(Host(mac=PC1_MAC, device="acc1", port="Gi1/0/9", name="reception-pc", manual=True,
                              note="Moved?"))
    crawled.hosts.append(Host(mac="", device="gone-switch", port="Gi0/1", name="orphan", manual=True))
    network = build_network()
    again = Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                    client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    tab.on_crawled(again)
    hosts = tab.network_map.hosts
    assert [host.name for host in hosts if host.manual] == ["old-printer"]
    found = next(host for host in hosts if host.mac == PC1_MAC)
    assert (found.port, found.name, found.note, found.manual) == ("Gi1/0/5", "reception-pc", "Moved?", False)
    assert "1 host added by hand wasn't kept" in tab.status_label.text()


def test_live_map_and_progress_while_crawling(tab):
    network = build_network()
    events = []
    final = Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                    client_factory=network.client, pinger=network.ping, echo=network.echo,
                    events=lambda kind, *details: events.append((kind, details))).run()
    tab.crawl_progress.start()
    tab.worker = object()  # As if crawling
    started = next(index for index, (kind, _) in enumerate(events) if kind == "started")
    tab.on_crawl_event(*events[started])
    tab.crawl_progress.refresh()
    assert tab.crawl_progress.reading_table.rowCount() == 1
    assert tab.crawl_progress.reading_table.item(0, 0).text() == "10.0.0.1"
    first_map = next(index for index, (kind, _) in enumerate(events) if kind == "map")
    for kind, details in events[started + 1:first_map + 1]:
        tab.on_crawl_event(kind, details)
    assert "core" in tab.view.items_by_key  # Drawn before the crawl is done
    assert tab.network_map is None and tab.displayed_map() is tab.live_map
    tab.view.items_by_key["core"].setPos(4000, 4000)  # Dragged while it crawls
    for kind, details in events[first_map + 1:]:
        tab.on_crawl_event(kind, details)
    tab.crawl_progress.refresh()
    assert tab.crawl_progress.reading_table.rowCount() == 0
    assert tab.crawl_progress.counts_label.text().startswith("Traceroute to 3 addresses")  # The last phase
    tab.on_crawled(final)
    tab.worker = None
    tab.on_thread_finished()
    assert tab.network_map is final and tab.view.items_by_key["core"].pos().x() == 4000
    log_text = "\n".join(tab.crawl_progress.lines)
    assert "Found acc1.corp.example (10.0.0.11) through CDP" in log_text and "Finished in" in log_text
    assert not tab.crawl_progress.row.isVisible()
    assert all(item.highlight is None for item in tab.view.items_by_key.values())


def test_progress_text():
    from nomad.ui.netmap_progress import counts_text, duration, estimate
    counts = {"read": 10, "reading": 8, "queued": 22, "found": 60, "no_snmp": 2, "unreachable": 0}
    assert estimate(counts, 60) == "about 3 min left so far"  # 30 left at 12 a minute
    assert estimate(dict(counts, read=2), 60) == ""  # Too soon to tell
    text = counts_text(counts, 75)
    assert text.startswith("Read 10  ·  reading 8  ·  queued 22  ·  60 found  ·  2 ping but no SNMP  ·  1:15")
    assert duration(3725) == "1:02:05"


def test_hosts_hidden_until_asked_for_with_vlans(tab, crawled):
    from nomad.ui.netmap_view import host_line
    tab.on_crawled(crawled)
    assert not tab.hosts_check.isChecked()
    assert all(not item.port_items for item in tab.view.items_by_key.values())  # Hidden by default
    tab.hosts_check.setChecked(True)
    shown = {key for key, item in tab.view.items_by_key.items() if item.port_items}
    assert shown == {"acc1", "acc2"}
    gi5 = next(item for item in tab.view.items_by_key["acc1"].port_items if item.port == "Gi1/0/5")
    assert sorted(host_line(host) for host in gi5.hosts) == [("10.10.0.21", "VLAN 10"),
                                                             ("SEP00AABBCCDDEE  10.10.0.22", "VLAN 10")]
    tab.hosts_check.setChecked(False)
    assert all(not item.port_items for item in tab.view.items_by_key.values())


def test_selecting_several_devices(tab, crawled, app):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    tab.on_crawled(crawled)
    QTest.keyClick(tab.view, Qt.Key_A, Qt.ControlModifier)
    assert len(tab.view.scene().selectedItems()) == 5
    assert "5 selected" in tab.details.toPlainText()
    # Moving one of a selection moves them all (Qt's own behavior for selected movable items)
    assert all(item.flags() & item.ItemIsMovable for item in tab.view.items_by_key.values())


def show_in_labels(tab, key):
    from PyQt5.QtWidgets import QMenu
    menu, actions = QMenu(), {}
    tab.add_show_in(menu, actions, key)
    return {action.text(): run for action, run in actions.items()}


def test_show_in_other_tabs(tab, crawled):
    tab.on_crawled(crawled)
    tab.tabs.setCurrentWidget(tab.view)
    choices = show_in_labels(tab, "core")
    assert list(choices) == ["Show in Logical (L3)", "Show in Devices", "Show in Links"]  # Not the tab showing

    choices["Show in Links"]()
    assert tab.tabs.currentWidget() is tab.links_table
    rows = sorted({index.row() for index in tab.links_table.selectionModel().selectedRows()})
    assert len(rows) == 4  # Every link of the core
    assert "Show in Links" not in show_in_labels(tab, "core")

    show_in_labels(tab, "acc2")["Show in Devices"]()
    assert tab.tabs.currentWidget() is tab.devices_table
    selected = tab.devices_table.selectionModel().selectedRows()
    assert len(selected) == 1 and tab.devices_table.item(selected[0].row(), 0).text() == "acc2"

    show_in_labels(tab, "core")["Show in Logical (L3)"]()
    assert tab.tabs.currentWidget() is tab.l3_view
    assert [item.key for item in tab.l3_view.scene().selectedItems()] == ["core"]
    assert "Show in Logical (L3)" not in show_in_labels(tab, "acc2")  # No IP interfaces: not on the L3 view

    show_in_labels(tab, "rtr1")["Show in Physical (L2)"]()
    assert tab.tabs.currentWidget() is tab.view
    assert [item.key for item in tab.view.scene().selectedItems()] == ["rtr1"]


def test_links_table_shows_both_ends(tab, crawled):
    tab.on_crawled(crawled)
    row = next(row for row in range(tab.links_table.rowCount()) if tab.links_table.item(row, 2).text() == "acc2")
    assert set(tab.links_table.item(row, 0).data_object) == {"core", "acc2"}
    tab.show_in(tab.view, list(tab.links_table.item(row, 0).data_object))
    assert {item.key for item in tab.view.scene().selectedItems()} == {"core", "acc2"}


def test_filtering_the_hosts_table(tab, crawled, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    tab.on_crawled(crawled)
    vlan_column = 6
    hosts_filter = tab.table_filters[tab.hosts_table]
    hosts_filter.set_filter(vlan_column, {"30"})
    visible = [row for row in range(tab.hosts_table.rowCount()) if not tab.hosts_table.isRowHidden(row)]
    assert len(visible) == 5
    assert tab.tabs.tabText(tab.tabs.indexOf(tab.hosts_table)) == f"Hosts (5 of {len(crawled.hosts)})"

    tab.hosts_table.selectAll()  # Ctrl+A also selects rows a filter hides: they mustn't be deleted
    assert len(tab.selected_table_hosts()) == 5
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    tab.delete_hosts(tab.selected_table_hosts())
    assert len(tab.network_map.hosts) == len(crawled.hosts) and len(crawled.hosts) > 0
    assert not any(host.vlan == 30 for host in tab.network_map.hosts)
    assert tab.tabs.tabText(tab.tabs.indexOf(tab.hosts_table)).startswith("Hosts (0 of")  # Kept after refilling

    hosts_filter.clear()
    assert tab.tabs.tabText(tab.tabs.indexOf(tab.hosts_table)) == "Hosts"


def test_show_in_clears_a_filter_hiding_the_row(tab, crawled):
    tab.on_crawled(crawled)
    tab.table_filters[tab.devices_table].set_filter(2, {"Firewall"})  # Kind
    tab.show_in(tab.devices_table, ["core"])
    assert not tab.table_filters[tab.devices_table].active
    selected = tab.devices_table.selectionModel().selectedRows()
    assert tab.devices_table.item(selected[0].row(), 0).text() == "core.corp.example"


def test_tables_start_a_to_z(tab, crawled):
    tab.on_crawled(crawled)
    names = [tab.devices_table.item(row, 0).text() for row in range(tab.devices_table.rowCount())]
    assert names == sorted(names, key=str.lower)
