"""The Ansible Inventory page: devices from the map and saved sessions, ticking, Set Ansible OS, formats, saving."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QObject, QSettings, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication, QFileDialog, QMainWindow, QStackedWidget

from nomad.netmap.model import FIREWALL, HOST, ROOM, SWITCH, Device, NetworkMap
from nomad.terminal.sessions import Credential, Session, SessionStore
from nomad.ui.inventory_tab import COL_NAME, COL_OS, InventoryTab


class MapPage(QObject):
    map_shown = pyqtSignal()

    def __init__(self, network_map):
        super().__init__()
        self.network_map = network_map

    def map_name(self):
        return "Test map"


class Window(QMainWindow):
    def __init__(self, map_page, store):
        super().__init__()
        self.navigator = QStackedWidget()
        self.setCentralWidget(self.navigator)
        self.netmap_tab, self.session_store = map_page, store
        self.statuses = []

    def show_status(self, message, kind="success", timeout=10000):
        self.statuses.append(message)


def make_map():
    network_map = NetworkMap()
    network_map.devices = {
        "core": Device("core", name="core.corp.example", mgmt_ip="10.0.0.1", kind=SWITCH,
                       sys_object_id="1.3.6.1.4.1.9.1.2494", sys_descr="Cisco IOS Software"),
        "fw": Device("fw", name="edge-fw", mgmt_ip="10.0.0.5", kind=FIREWALL),  # Can't tell its OS
        "pc": Device("pc", name="pc1", mgmt_ip="10.0.0.50", kind=HOST),
    }
    room = network_map.new_group("Room 1", ROOM)
    network_map.set_group(["core"], room.key)
    return network_map


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def setup(app, tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.put(Session("Core", host="10.0.0.1", username="admin", folder="Sites"))
    store.put(Session("jump", host="198.51.100.7", username="ops", folder="Servers"))
    store.credentials.items = [Credential("TACACS", username="netops")]
    map_page = MapPage(make_map())
    window = Window(map_page, store)
    page = InventoryTab(window)
    window.navigator.addWidget(page)
    yield window, page, map_page, store
    page.shutdown()
    window.deleteLater()
    app.processEvents()


def row_of(page, name):
    return next(row for row in range(page.table.rowCount()) if page.table.item(row, COL_NAME).text() == name)


def test_devices_from_the_map_and_sessions(setup):
    window, page, map_page, store = setup
    names = sorted(page.table.item(row, COL_NAME).text() for row in range(page.table.rowCount()))
    assert names == ["core.corp.example", "edge-fw", "jump", "pc1"]
    ticked = sorted(page.table.item(row, COL_NAME).text() for row in range(page.table.rowCount())
                    if page.table.item(row, COL_NAME).checkState() == Qt.Checked)
    assert ticked == ["core.corp.example", "edge-fw", "jump"]  # Not the PC
    assert page.table.item(row_of(page, "core.corp.example"), COL_OS).text() == "Cisco IOS / IOS XE"
    text = page.preview.toPlainText()
    assert 'from the network map "Test map" and saved SSH sessions' in text
    assert "    core:\n      ansible_host: 10.0.0.1\n      ansible_user: admin\n" in text
    assert "room_1:" in text and "sites:" in text and "servers:" in text and "cisco_ios:" in text
    assert "pc1" not in text
    assert "edge-fw: its Ansible OS isn't known" in page.status_label.text()
    assert [page.user_combo.itemText(index) for index in range(page.user_combo.count())] == ["", "netops"]


def test_ticking_set_os_and_options_change_the_inventory(setup):
    window, page, map_page, store = setup
    page.table.item(row_of(page, "jump"), COL_NAME).setCheckState(Qt.Unchecked)
    assert "jump" not in page.preview.toPlainText()
    assert page.choices == {next(entry.key for entry in page.entries if entry.name == "jump"): False}

    page.table.selectRow(row_of(page, "edge-fw"))
    asa = next(action for action in page.os_actions if action.data() == "asa")
    page.on_os_chosen(asa)
    assert page.table.item(row_of(page, "edge-fw"), COL_OS).text() == "Cisco ASA"
    assert "cisco_asa:" in page.preview.toPlainText() and "isn't known" not in page.status_label.text()

    page.user_combo.setEditText("netops")
    page.become_check.setChecked(True)
    page.format_combo.setCurrentIndex(page.format_combo.findData("ini"))
    text = page.preview.toPlainText()
    assert "[all:vars]\nansible_user=netops" in text and "[cisco_asa:vars]" in text
    assert "ansible_become=true" in text

    page.tick_none()
    assert page.preview.toPlainText() == "" and not page.save_button.isEnabled()
    page.tick_network()
    assert "edge-fw" in page.preview.toPlainText() and "jump" not in page.preview.toPlainText()

    page.filter_input.setText("core")
    assert [row for row in range(page.table.rowCount()) if not page.table.isRowHidden(row)] == \
           [row_of(page, "core.corp.example")]


def test_new_sessions_and_maps_are_picked_up(setup):
    window, page, map_page, store = setup
    store.put(Session("dist", host="10.0.0.2"))
    page.refresh()
    assert "dist" in page.preview.toPlainText()
    map_page.network_map = None
    page.sessions_check.setChecked(False)
    assert page.table.rowCount() == 0
    assert "no map is open" in page.source_label.text()


def test_save_and_settings(setup, tmp_path, monkeypatch):
    window, page, map_page, store = setup
    target = tmp_path / "out" / "hosts.yml"
    target.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    page.save_inventory()
    assert target.read_text(encoding="utf-8") == page.preview.toPlainText()
    assert window.statuses[-1] == f"Saved the inventory to {target}."

    page.table.selectRow(row_of(page, "edge-fw"))
    page.on_os_chosen(next(action for action in page.os_actions if action.data() == "panos"))
    page.nomad_vars_check.setChecked(True)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    page.save_settings(settings)
    other = InventoryTab(window)
    other.restore_settings(settings)
    assert other.nomad_vars_check.isChecked() and other.folder == str(target.parent)
    assert other.table.item(row_of(other, "edge-fw"), COL_OS).text() == "Palo Alto PAN-OS"
    assert other.preview.toPlainText().count("nomad_kind") == page.preview.toPlainText().count("nomad_kind")
