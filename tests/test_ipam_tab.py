"""The IP Addresses page: selecting each kind of subnet shows it without errors."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QTreeWidgetItemIterator, QWidget  # noqa: E402

from nomad.ipam.store import USED, IpamStore  # noqa: E402
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
