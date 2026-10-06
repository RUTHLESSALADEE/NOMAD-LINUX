"""The VLANs page, the Network Map's VLANs tab and highlighting a VLAN on the map."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402
from test_netmap_tab import Window as MapWindow  # noqa: E402
from test_netmap_vlans import crawl, two_switches  # noqa: E402

from nomad.ipam.store import IpamStore  # noqa: E402
from nomad.ipam.vlans import VlanStore  # noqa: E402
from nomad.netmap import store as map_store  # noqa: E402
from nomad.netmap import vlans as map_vlans  # noqa: E402
from nomad.ui import netmap_tab  # noqa: E402
from nomad.ui.ipam_tab import IpamTab  # noqa: E402
from nomad.ui.netmap_view import DeviceItem, FADED, LinkItem  # noqa: E402
from nomad.ui.vlan_dialogs import MapImportDialog, VlanDialog  # noqa: E402
from nomad.ui.vlan_tab import COL_INTERFACES, COL_MAP, VlanTab  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Navigator:
    def __init__(self):
        self.current = None

    def setCurrentWidget(self, widget):
        self.current = widget


class FakeMapPage:
    """The Network Map page, as the VLANs page uses it."""

    def __init__(self, network_map):
        self.network_map = network_map
        self.highlighted = None

    def highlight_vlan(self, vlan, domain=None):
        self.highlighted = (vlan, domain)

    def map_name(self):
        return "Lab map"


class Window(QWidget):
    def __init__(self, network_map=None):
        super().__init__()
        self.navigator = Navigator()
        self.netmap_tab = FakeMapPage(network_map)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def page(app, tmp_path):
    window = Window(crawl(two_switches()))
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    window.ipam_tab = IpamTab(window)
    window.ipam_tab.local_store = store  # So it doesn't open the real database or the tribe's
    tab = VlanTab(window)
    yield tab, store, window
    window.ipam_tab.sync_timer.stop()
    store.close()


def rows(table):
    return [[table.item(row, column).text() for column in range(table.columnCount())]
            for row in range(table.rowCount())]


def test_page_lists_a_domains_vlans_with_ipam_and_map(page):
    tab, store, window = page
    network = store.add_network("Site")
    store.add_subnet(network.id, "10.10.0.0/24", "Users", "10.10.0.1")
    vlans = VlanStore(store)
    domain = vlans.add_domain("CORP switches", network.id, "corp", ranges=[{"first": 10, "last": 99, "name": "Main"}])
    vlans.set_vlan(domain.id, 10, "USERS", subnets=["10.10.0.0/24"])
    vlans.set_vlan(domain.id, 30, "PRINTERS")
    vlans.set_vlan(domain.id, 500, "NOT-THERE")
    tab.fill_domains()
    assert tab.domain_combo.currentText() == "CORP switches  (Local)"
    table = rows(tab.table)
    assert [row[0] for row in table] == ["10", "30", "500"]
    assert table[0][3] == "10-99 Main" and table[0][4] == "10.10.0.0/24" and table[0][5] == "10.10.0.1"
    assert table[0][COL_INTERFACES] == "10.0.0.1 on sw1 Vlan10, 10.10.0.1 on sw1 Vlan10"
    assert table[0][COL_MAP] == "2 switches, 1 access port"
    assert table[1][COL_MAP] == "2 switches, 1 access port (also named PRINT)"  # sw2 calls it PRINT
    assert table[2][COL_MAP] == "not on the switches mapped"
    assert "VTP domain corp" in tab.domain_label.text() and "Site" in tab.domain_label.text()

    tab.table.selectRow(0)
    assert "Users, gateway 10.10.0.1" in tab.details.toHtml() and "sw1" in tab.details.toHtml()
    assert tab.edit_button.isEnabled() and tab.ipam_button.isEnabled() and tab.map_button.isEnabled()
    tab.highlight_on_map()
    assert window.netmap_tab.highlighted == (10, "CORP")  # The map's spelling of the VTP domain

    tab.search_input.setText("print")
    assert [row for row in range(tab.table.rowCount()) if not tab.table.isRowHidden(row)] == [1]


def test_vlan_dialog_links_subnets_and_refuses_a_taken_number(page):
    tab, store, _ = page
    network = store.add_network("Site")
    store.add_subnet(network.id, "10.10.0.0/24", "Users")
    store.add_subnet(network.id, "10.20.0.0/24", "Voice")
    vlans = VlanStore(store)
    domain = vlans.add_domain("Site", network.id)
    vlans.set_vlan(domain.id, 20, "VOICE", subnets=["10.20.0.0/24"])
    tab.fill_domains()
    source = tab.source()
    dialog = VlanDialog(tab, source, domain, number=20)
    assert "recorded already" in dialog.range_label.text()
    dialog.number_input.setValue(10)
    dialog.name_input.setText("Guest Users")
    assert dialog.name_hint.isVisibleTo(dialog)  # Spaces: a warning, not an error
    items = {dialog.subnet_list.item(row).data(0x0100): dialog.subnet_list.item(row)
             for row in range(dialog.subnet_list.count())}
    assert not items["10.20.0.0/24"].flags() & 0x20  # VLAN 20's: can't be ticked here
    dialog.subnet_list.remember_state(items["10.10.0.0/24"])  # A click on the row's text ticks it
    dialog.subnet_list.toggle(items["10.10.0.0/24"])
    assert "1 ticked" in dialog.subnet_hint.text()
    dialog.save()
    assert vlans.vlan(domain.id, 10).subnets == ["10.10.0.0/24"]


def test_vlan_dialog_gives_a_domain_without_a_network_one(page):
    tab, store, _ = page
    network = store.add_network("Site")
    store.add_subnet(network.id, "10.10.0.0/24", "Users")
    vlans = VlanStore(store)
    domain = vlans.add_domain("No network yet")
    tab.fill_domains()
    dialog = VlanDialog(tab, tab.source(), domain, number=10,
                        interfaces_of=lambda number: [("10.10.0.1", 24, "Vlan10", "sw1")] if number == 10 else [])
    assert "Choose the domain's IPAM network" in dialog.subnet_list.item(0).text()
    dialog.network_combo.setCurrentIndex(dialog.network_combo.findData(network.id))
    item = dialog.subnet_list.item(0)
    assert "VLAN interface on the map: 10.0.0.1" not in item.text() and "10.10.0.1/24 on sw1 Vlan10" in item.text()
    item.setCheckState(2)
    dialog.save()
    assert vlans.domain(domain.id).network_id == network.id
    assert vlans.vlan(domain.id, 10).subnets == ["10.10.0.0/24"]


def test_bringing_in_a_maps_vlans_into_a_new_domain(page):
    tab, store, window = page
    store.add_network("Site")
    network_map = window.netmap_tab.network_map
    dialog = MapImportDialog(tab, tab.sources(), network_map, "Lab map")
    assert dialog.domain_combo.currentText() == "New local domain..."
    assert dialog.new_name.text() == "CORP"
    assert [(change.action, change.vlan) for change in dialog.changes] == [
        ("add", 1), ("add", 10), ("add", 20), ("add", 30), ("add", 40)]
    dialog.apply()
    domain = VlanStore(store).domain_named("CORP")
    assert domain.vtp_domain == "CORP"
    assert [(vlan.vlan, vlan.name) for vlan in VlanStore(store).vlans(domain.id)][:2] == [(1, "default"), (10, "USERS")]


# --------------------------------------------------------------------- On the Network Map page

@pytest.fixture
def map_page(app, tmp_path, monkeypatch):
    monkeypatch.setattr(map_store, "maps_dir", lambda: tmp_path)
    tab = netmap_tab.NetworkMapTab(MapWindow())
    tab.resize(1200, 800)
    yield tab
    tab.shutdown()


def test_map_vlans_tab_and_highlight(map_page):
    tab = map_page
    tab.on_crawled(crawl(two_switches()))
    assert tab.vlan_panel.table.rowCount() == 5
    assert tab.vlan_panel.checks.rowCount() == len(map_vlans.check_map(tab.network_map))
    assert "VTP domain CORP" in tab.vlan_panel.summary_label.text()

    tab.highlight_vlan(30, "CORP")
    assert tab.tabs.currentWidget() is tab.view and tab.vlan_bar.isVisibleTo(tab)
    assert "allowed at one end only" in tab.vlan_label.text()
    [link] = [item for item in tab.view.scene().items() if isinstance(item, LinkItem)]
    assert link.vlan_kind == map_vlans.ONE_END
    devices = {item.key: item for item in tab.view.scene().items() if isinstance(item, DeviceItem)}
    assert devices["sw1"].opacity() == 1.0 and devices["sw2"].opacity() == 1.0

    tab.highlight_vlan(40, "CORP")  # Only sw1's access port: sw2 fades, and so does the trunk
    assert devices["sw2"].opacity() == FADED and link.opacity() == FADED

    tab.on_crawled(crawl(two_switches()))  # Mapped again: still highlighted, on the new drawing
    assert tab.vlan_bar.isVisibleTo(tab)
    tab.clear_vlan()
    assert not tab.vlan_bar.isVisibleTo(tab)
    assert all(item.opacity() == 1.0 for item in tab.view.scene().items() if isinstance(item, DeviceItem))

    from nomad.ui.netmap_tab import device_vlans_html, port_html
    assert "VTP domain CORP (server)" in device_vlans_html(tab.network_map.devices["sw1"])
    assert "Access VLAN 10, voice VLAN 20" in port_html(tab.network_map, "sw1", "Gi1/0/2")


def test_map_highlight_fades_switch_without_vlan_even_when_trunk_allows_it(map_page):
    tab = map_page
    network_map = crawl(two_switches())
    switch = network_map.devices["sw2"]
    switch.vlans = [item for item in switch.vlans if item[0] != 30]
    switch.port_vlans["Gi1/0/5"]["vlan"] = 20
    switch.port_vlans["Te1/1/1"]["allowed"] = "1-4094"
    tab.on_crawled(network_map)
    item = next(item for item in tab.vlan_panel.items if item.vlan == 30)
    assert item.switches == ["sw1"]
    tab.highlight_vlan(30, item.domain)
    devices = {item.key: item for item in tab.view.scene().items() if isinstance(item, DeviceItem)}
    assert devices["sw1"].opacity() == 1.0
    assert devices["sw2"].opacity() == FADED
    [link] = [item for item in tab.view.scene().items() if isinstance(item, LinkItem)]
    assert link.vlan_kind == map_vlans.ONE_END


@pytest.mark.parametrize("action_text, signal_name", [
    ("Highlight on Map", "highlight_requested"),
    ("Show on VLANs Page", "vlans_page_requested"),
    ("Add to VLAN Database...", "add_to_database_requested"),
])
def test_map_vlan_context_menu_uses_right_clicked_row(map_page, monkeypatch, action_text, signal_name):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMenu
    tab = map_page
    tab.on_crawled(crawl(two_switches()))
    panel = tab.vlan_panel
    tab.tabs.setCurrentWidget(panel)
    tab.show()
    QApplication.processEvents()
    panel.table.sortItems(1, Qt.DescendingOrder)
    panel.table.selectRow(0)
    cell = panel.table.item(2, 0)
    target = cell.data_object
    received = []
    getattr(panel, signal_name).disconnect()
    getattr(panel, signal_name).connect(lambda *args: received.append(args))

    def choose(menu, position):
        assert panel.selected_item() is target
        return next(action for action in menu.actions() if action.text() == action_text)

    monkeypatch.setattr(QMenu, "exec_", choose)
    assert panel.table.contextMenuPolicy() == Qt.CustomContextMenu
    panel.table.customContextMenuRequested.emit(panel.table.visualItemRect(cell).center())
    assert received == ([()] if signal_name == "add_to_database_requested" else [(target.vlan, target.domain)])


def test_map_checks_context_menu_shows_clicked_finding_and_expands(map_page, monkeypatch):
    from PyQt5.QtWidgets import QMenu
    tab = map_page
    tab.on_crawled(crawl(two_switches()))
    panel = tab.vlan_panel
    tab.tabs.setCurrentWidget(panel)
    tab.show()
    QApplication.processEvents()
    cell = panel.checks.item(0, 0)
    finding = cell.data_object
    received = []
    panel.show_requested.connect(lambda *args: received.append(args))
    chosen_text = "Show on Map"
    monkeypatch.setattr(QMenu, "exec_", lambda menu, position:
                        next(action for action in menu.actions() if action.text() == chosen_text))
    panel.checks.customContextMenuRequested.emit(panel.checks.visualItemRect(cell).center())
    assert received == [(finding.device, finding.port)]
    chosen_text = "Expand Notes and Warnings"
    panel.checks.customContextMenuRequested.emit(panel.checks.visualItemRect(cell).center())
    assert panel.expand_checks_button.isChecked()


def test_stop_highlighting_on_the_menus(map_page):
    from PyQt5.QtWidgets import QMenu
    tab = map_page
    tab.on_crawled(crawl(two_switches()))
    assert tab.add_stop_highlight(QMenu()) is None
    tab.highlight_vlan(10, "CORP")
    menu = QMenu()
    action = tab.add_stop_highlight(menu)
    assert action.text() == "Stop Highlighting VLAN 10 (Show All)"


def test_read_vlans_again_on_a_map_made_without_them(map_page):
    from nomad.netmap.crawl import read_vlans_of
    tab = map_page
    network = two_switches()
    network_map = crawl(network)
    for device in network_map.devices.values():  # As a map made before NOMAD read VLANs
        device.vlans, device.port_vlans, device.vtp_domain = [], {}, ""
    tab.on_crawled(network_map)
    assert tab.vlan_panel.table.rowCount() == 1  # Only VLAN 10, from the SVI's name
    network.devices["10.0.0.2"].vlan(50, "NEW")
    tab.read_vlans = lambda settings, address: read_vlans_of(settings, address, client_factory=network.client)
    tab.read_vlans_again()
    thread = tab.check_threads[-1]
    thread.wait(10000)
    QApplication.processEvents()
    assert tab.vlan_reading is None and tab.vlan_panel.read_button.isEnabled()
    assert tab.vlan_panel.table.rowCount() == 6
    assert "Read the VLANs of 2 devices" in tab.status_label.text()
    assert any("Read sw2's VLANs: 5" in line for line in tab.watcher.lines)


def test_watch_notes_vlan_changes():
    from nomad.netmap.model import Device
    from nomad.netmap.watch import vlan_changes
    device = Device("sw1", "sw1", vlans=[[10, "STAFF"], [30, "NEW"]],
                    port_vlans={"Gi1/0/1": {"mode": "access", "vlan": 30}, "Gi1/0/2": {"mode": "access", "vlan": 10}})
    lines = vlan_changes(device, {10: "USERS", 20: "VOICE"}, {"Gi1/0/1": {"mode": "access", "vlan": 10},
                                                            "Gi1/0/2": {"mode": "access", "vlan": 10}})
    assert lines == ["New VLAN on sw1: 30 NEW", "VLAN gone from sw1: 20 VOICE", "VLAN 10 renamed on sw1: USERS → STAFF",
                     "Ports changed VLAN on sw1: Gi1/0/1 10 → 30"]
    assert vlan_changes(device, {}, {}) == ["Read sw1's VLANs: 2"]


def test_read_routes_again(map_page):
    from nomad.netmap.crawl import read_vlans_of
    tab = map_page
    network = two_switches()
    network_map = crawl(network)
    network_map.devices["sw1"].routes = []
    tab.on_crawled(network_map)
    network.devices["10.0.0.1"].route("10.99.0.0", "255.255.255.0", "10.0.0.2", 1)
    tab.read_vlans = lambda settings, address, **options: read_vlans_of(settings, address,
                                                                         client_factory=network.client, **options)
    shown = []
    tab.map_shown.connect(lambda: shown.append(True))
    assert tab.read_routes_again()
    tab.check_threads[-1].wait(10000)
    QApplication.processEvents()
    assert ["10.99.0.0/24", "10.0.0.2", "TenGigabitEthernet1/0/1", "static"] in [list(route) for route in
                                                                 tab.network_map.devices["sw1"].routes]
    assert shown and "Read the VLANs and routes of 2 devices" in tab.status_label.text()
