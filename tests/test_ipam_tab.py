"""The IP Addresses page: selecting each kind of subnet shows it without errors, and SSH/SCP to an address."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu, QTreeWidgetItemIterator, QWidget  # noqa: E402

from nomad.ipam.store import USED, IpamStore  # noqa: E402
from nomad.terminal.sessions import Session  # noqa: E402
from nomad.ui.ipam_tab import LOCAL, IpamTab  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Window(QWidget):
    """Stands in for the main window: the page only calls it for other pages and the busy indicator."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_selecting_each_subnet(app, tmp_path):
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    network = store.add_network("Lab")
    store.add_subnet(network.id, "10.0.0.0/24", "LAN", gateway="10.0.0.1")
    store.add_subnet(network.id, "10.0.1.0/29", "68900 MAIN TCN Loopback", loopbacks=True)
    store.add_subnet(network.id, "fd00::/64", "IPv6")
    store.set_address(network.id, "10.0.1.0", USED, "rtr1")
    tab = IpamTab(Window())
    tab.local_store, tab.source, tab.network_id = store, LOCAL, network.id
    tab.fill_tree()
    shown = {}
    items = QTreeWidgetItemIterator(tab.tree)
    while items.value():
        tab.tree.setCurrentItem(items.value())
        tab.show_subnet()
        shown[items.value().text(0)] = tab.subnet_label.text()
        items += 1
    assert "loopbacks, each /32" in shown["10.0.1.0/29"] and "1 of 8 recorded" in shown["10.0.1.0/29"]
    assert "netmask 255.255.255.0" in shown["10.0.0.0/24"]
    assert "fd00::/64" in shown
    store.close()


def test_search_by_network_and_kind(app, tmp_path):
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    first, second = store.add_network("First"), store.add_network("Second")
    for network in (first, second):
        store.add_subnet(network.id, "10.0.0.0/24", "68890 LAN")
        store.set_address(network.id, "10.0.0.7", USED, "68890-sw1")
    tab = IpamTab(Window())
    tab.local_store = store
    tab.fill_networks()
    assert [tab.search_network_combo.itemText(index) for index in range(tab.search_network_combo.count())] ==         ["All networks", "First", "Second"]

    def results():
        return [tuple(tab.results_table.item(row, column).text() for column in (0, 1, 3))
                for row in range(tab.results_table.rowCount())]

    tab.search_input.setText("68890")
    tab.search()
    assert len(results()) == 4 and "in any network" in tab.results_label.text()
    tab.search_network_combo.setCurrentIndex(tab.search_network_combo.findText("Second"))  # Searches again
    assert sorted(results()) == [("Second", "10.0.0.0/24", ""), ("Second", "10.0.0.0/24", "10.0.0.7")]
    tab.search_kind_combo.setCurrentIndex(tab.search_kind_combo.findText("Addresses"))
    assert results() == [("Second", "10.0.0.0/24", "10.0.0.7")]
    assert tab.results_label.text() == "1 result for '68890' (addresses) in Second"

    # Matching one detail only, which shows in the results
    store.add_subnet(first.id, "10.0.1.0/24", "Voice", fields={"Telephony Rng": "68890"})
    tab.fill_networks()
    assert tab.search_match_combo.findText("Telephony Rng") >= 0
    tab.search_kind_combo.setCurrentIndex(0)
    tab.search_network_combo.setCurrentIndex(0)
    tab.search_match_combo.setCurrentIndex(tab.search_match_combo.findText("Telephony Rng"))
    assert results() == [("First", "10.0.1.0/24", "")]
    assert tab.results_table.item(0, 6).text() == "Telephony Rng: 68890"
    assert tab.results_label.text() == "1 result for Telephony Rng '68890' in any network"
    tab.search_network_combo.setCurrentIndex(tab.search_network_combo.findText("Second"))

    # The chosen network stays chosen when the list of networks is refreshed, and falls back to All if it's gone
    tab.fill_networks()
    assert tab.search_network_combo.currentText() == "Second"
    store.delete_network(second.id)
    tab.fill_networks()
    assert tab.search_network_combo.currentText() == "All networks"
    store.close()


class SessionPage:
    def __init__(self, matches):
        self.matches, self.opened = matches, []

    def saved_matches(self, host, aliases=(), protocol="SSH"):
        self.asked = (host, list(aliases))
        return self.matches

    def open_address(self, host, protocol="SSH", aliases=(), name="", folder="", use_saved=True):
        self.opened.append((host, list(aliases), name, folder, use_saved))


def test_ssh_and_scp_use_the_saved_session(app, tmp_path):
    store = IpamStore(str(tmp_path / "ipam.db"), user="tester")
    network = store.add_network("Lab")
    store.add_subnet(network.id, "10.0.0.0/24", "Core / Mgmt")
    store.set_address(network.id, "10.0.0.5", USED, "core-sw1")
    window = Window()
    window.terminal_tab = SessionPage([Session("Core-SW1", host="core-sw1", username="admin")])
    window.scp_tab = SessionPage([])
    tab = IpamTab(window)
    tab.local_store, tab.source, tab.network_id = store, LOCAL, network.id
    tab.fill_tree()
    tab.tree.setCurrentItem(tab.tree.topLevelItem(0))
    tab.show_subnet()
    menu = QMenu()
    tab.add_session_actions(menu, "10.0.0.5")
    actions = {action.text(): action for action in menu.actions()}
    assert list(actions) == ["SSH (Core-SW1)", "SSH as a New Session", "SCP"]
    assert window.terminal_tab.asked == ("10.0.0.5", ["core-sw1"])
    actions["SSH (Core-SW1)"].trigger()
    actions["SCP"].trigger()
    assert window.terminal_tab.opened == [("10.0.0.5", ["core-sw1"], "core-sw1", "Lab/Core - Mgmt", True)]
    assert window.scp_tab.opened == [("10.0.0.5", ["core-sw1"], "core-sw1", "Lab/Core - Mgmt", True)]
    store.close()
