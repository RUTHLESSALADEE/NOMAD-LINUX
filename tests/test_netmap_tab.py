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
