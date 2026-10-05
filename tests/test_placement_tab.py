"""The Subnet Placement page: its rows and findings, how to treat a subnet, and moving one."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402
from test_placement import lab  # noqa: E402

from nomad.ipam.placement import DONE, IN_PROGRESS, LOCAL, PlacementStore  # noqa: E402
from nomad.ipam.store import IpamStore  # noqa: E402
from nomad.ipam.vlans import VlanStore  # noqa: E402
from nomad.ui.ipam_tab import IpamTab  # noqa: E402
from nomad.ui.placement_dialogs import MoveDialog, ScopeDialog  # noqa: E402
from nomad.ui.placement_tab import COLUMNS, PlacementTab  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeMapPage(QObject):
    map_shown = pyqtSignal()
    routes_read = pyqtSignal(int, int)

    def __init__(self, network_map):
        super().__init__()
        self.network_map = network_map
        self.read = 0
        self.can_read = True
        self.reading = False

    def map_name(self):
        return "Lab"

    def reading_routes(self):
        return self.reading

    def read_routes_again(self):
        if not self.can_read:
            return False
        self.read += 1
        self.reading = True
        return True

    def finish_reading(self, read=4, failed=0):
        self.reading = False
        self.routes_read.emit(read, failed)


class Window(QWidget):
    def __init__(self, network_map):
        super().__init__()
        self.netmap_tab = FakeMapPage(network_map)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def page(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    network_map = lab()
    network_map.devices["r2"].interfaces_l3.append(["10.30.0.9", 24, "Gi0/7"])  # 10.30.0.0/24 in two places
    window = Window(network_map)
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    window.ipam_tab = IpamTab(window)
    window.ipam_tab.local_store = store
    tab = PlacementTab(window)
    network = store.add_network("Lab")
    for cidr in ("10.30.0.0/24", "10.50.0.0/24", "192.168.1.0/24"):
        store.add_subnet(network.id, cidr, f"net {cidr}")
    vlans = VlanStore(store)
    domain = vlans.add_domain("Site", network.id)
    vlans.set_vlan(domain.id, 50, "USERS", subnets=["10.50.0.0/24"])
    tab.fill_networks()
    yield tab, store, network, domain, window
    window.ipam_tab.sync_timer.stop()
    store.close()


def cell(tab, cidr, column):
    for number in range(tab.table.rowCount()):
        if tab.table.item(number, 0).text() == cidr:
            return tab.table.item(number, COLUMNS.index(column)).text()
    return None


def select(tab, cidr):
    for number in range(tab.table.rowCount()):
        if tab.table.item(number, 0).text() == cidr:
            tab.table.selectRow(number)
            return tab.selected_row()


def test_rows_and_findings(page):
    tab, _, _, _, _ = page
    assert cell(tab, "10.30.0.0/24", "Scope") == "Advertised"
    assert cell(tab, "10.30.0.0/24", "Status") == "Problem"
    assert cell(tab, "10.50.0.0/24", "Planned (VLANs Page)") == "VLAN 50 USERS (Site)"
    assert cell(tab, "10.50.0.0/24", "Status") == "OK"
    assert cell(tab, "192.168.1.0/24", "Scope") == "Local"
    assert cell(tab, "10.10.0.0/24", "VRF") == "GUEST"
    assert "1 with problems" in tab.summary_label.text()
    select(tab, "10.30.0.0/24")
    assert "in 2 places" in tab.details.toHtml() and "Routes to it" in tab.details.toHtml()
    tab.show_combo.setCurrentIndex(tab.show_combo.findData("issues"))
    assert [tab.table.item(number, 0).text() for number in range(tab.table.rowCount())] == ["10.30.0.0/24"]
    tab.read_routes_again()
    assert tab.window.netmap_tab.read == 1
    tab.window.netmap_tab.finish_reading(4, 1)
    assert "Read the routes of 4 devices (1 device didn't answer SNMP)" in tab.status_label.text()


def test_treating_a_subnet_as_one_segment(page):
    tab, store, network, _, _ = page
    row = select(tab, "10.30.0.0/24")
    dialog = ScopeDialog(tab, PlacementStore(store), network.id, row)
    dialog.scope_combo.setCurrentIndex(dialog.scope_combo.findData(LOCAL))
    dialog.note_input.setText("reused on purpose")
    dialog.save()
    tab.changed()
    assert cell(tab, "10.30.0.0/24", "Scope") == "Local (set)"
    assert cell(tab, "10.30.0.0/24", "Status") == "Warning"  # Others have routes to it: leaking


def test_moving_a_subnet(page):
    tab, store, network, domain, window = page
    row = select(tab, "10.50.0.0/24")
    dialog = MoveDialog(tab, PlacementStore(store), VlanStore(store), network.id, row, window.netmap_tab.network_map)
    assert dialog.from_combo.currentData() == (domain.id, 50)
    dialog.to_vlan.setValue(60)
    assert "new: added to the domain" in dialog.to_vlan_label.text()
    dialog.save()
    tab.changed()
    assert cell(tab, "10.50.0.0/24", "Move") == "Planned: to VLAN 60"
    select(tab, "10.50.0.0/24")
    assert tab.start_button.isEnabled() and not tab.move_button.isEnabled()
    tab.start_move()
    select(tab, "10.50.0.0/24")
    assert PlacementStore(store).open_move(network.id, "10.50.0.0/24").status == IN_PROGRESS
    tab.check_move()  # Reads the routes again first, then checks
    assert window.netmap_tab.read == 1 and "Reading the routes" in tab.status_label.text()
    tab.check_move()  # Already reading: waits for that read
    assert window.netmap_tab.read == 1
    window.netmap_tab.finish_reading()
    assert "10.50.0.0/24: not done yet" in tab.status_label.text()
    window.netmap_tab.can_read = False  # Mapping, say: checked on the map as it was last read
    select(tab, "10.50.0.0/24")
    tab.check_move()
    assert "not done yet" in tab.status_label.text() and "couldn't be read again" in tab.status_label.text()
    tab.complete_move()  # Confirmed although the map doesn't show it done
    assert PlacementStore(store).moves(network.id, "10.50.0.0/24")[0].status == DONE
    assert VlanStore(store).vlan(domain.id, 60).subnets == ["10.50.0.0/24"]
    assert VlanStore(store).vlan(domain.id, 50).subnets == []
    assert cell(tab, "10.50.0.0/24", "Planned (VLANs Page)") == "VLAN 60 (Site)"
    select(tab, "10.50.0.0/24")
    assert "Earlier moves" in tab.details.toHtml()
