"""Ansible Inventory page: an Ansible inventory (YAML or INI) of the Network Map's devices and the saved SSH sessions,
grouped by site, building and room, kind, Ansible OS and session folder.

Tick the devices to put in it; the Ansible OS each needs is worked out from what the map read of it, or chosen with
Set Ansible OS. The inventory is shown as it's made, to copy or save. Passwords are never put in it.
"""
import datetime
import ipaddress
import json
import logging
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QAbstractItemView, QAction, QApplication, QCheckBox, QComboBox, QFileDialog, \
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QVBoxLayout, QWidget

from .. import __version__
from .. import inventory
from ..inventory import EXTENSIONS, FORMAT_NAMES, INI, NONE, PLATFORMS, YAML
from ..netmap.model import NETWORK_KINDS
from .common import ColumnFitter, SortableTableItem, set_hint
from .theme import accent_button, monospace_font

log = logging.getLogger(__name__)

COLUMNS = ["Name", "Address", "Kind", "Model", "Ansible OS", "Location", "Folder", "From"]
COL_NAME, COL_ADDRESS, COL_KIND, COL_MODEL, COL_OS, COL_LOCATION, COL_FOLDER, COL_FROM = range(len(COLUMNS))
KEY_ROLE = Qt.UserRole + 1
DETECTED = "detected"  # Set Ansible OS: back to what the map says
PROBLEMS_SHOWN = 4
CHECKS = ("by_location", "by_kind", "by_platform", "by_folder", "short_names", "session_users", "become",
          "nomad_vars")


def address_key(text):
    try:
        address = ipaddress.ip_address(text)
        return (0, address.version, int(address))
    except ValueError:
        return (1, 0, text.lower())


class InventoryTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.entries = []
        self.platforms = {}  # Entry key -> platform key chosen by hand (NONE: not one Ansible manages)
        self.choices = {}  # Entry key -> ticked or not, where the user changed it
        self.built = None  # The last inventory.Inventory made
        self.filling = False
        self.folder = ""  # Where the last inventory was saved
        self.init_ui()
        page = self.map_page()
        if page is not None and hasattr(page, "map_shown"):
            page.map_shown.connect(self.on_map_shown)
        self.refresh()

    def init_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel("Make an Ansible inventory from the Network Map's devices and your saved SSH sessions. Tick "
                       "the devices to put in it; each gets the Ansible OS its make and model call for (choose it "
                       "with Set Ansible OS where the map can't tell). Passwords are never written to it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        sources = QHBoxLayout()
        sources.addWidget(QLabel("Devices from:"))
        self.map_check = QCheckBox("Network Map")
        self.map_check.setChecked(True)
        self.map_check.setToolTip("The devices on the map open on the Network Map page, in their sites, buildings "
                                  "and rooms")
        self.map_check.toggled.connect(self.refresh)
        sources.addWidget(self.map_check)
        self.sessions_check = QCheckBox("Saved SSH sessions")
        self.sessions_check.setChecked(True)
        self.sessions_check.setToolTip("Terminal's saved SSH sessions, in their folders. A session of a device on "
                                       "the map joins it, giving its user name and port.")
        self.sessions_check.toggled.connect(self.refresh)
        sources.addWidget(self.sessions_check)
        sources.addStretch()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Read the map and the saved sessions again")
        self.refresh_button.clicked.connect(self.refresh)
        sources.addWidget(self.refresh_button)
        layout.addLayout(sources)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        groups = QHBoxLayout()
        groups.addWidget(QLabel("Group by:"))
        self.by_location_check = self.option_check(groups, "Site / building / room", True,
                                                   "The map's sites, buildings and rooms, each in the one it's in")
        self.by_kind_check = self.option_check(groups, "Kind", True, "switches, routers, firewalls...")
        self.by_platform_check = self.option_check(groups, "Ansible OS", True,
                                                   "cisco_ios, cisco_nxos, panos...: each group holds its "
                                                   "ansible_network_os and connection. Off: they're set on each "
                                                   "device.")
        self.by_folder_check = self.option_check(groups, "Session folders", True,
                                                 "The saved sessions' folders, each in the one it's in")
        groups.addStretch()
        layout.addLayout(groups)

        options = QHBoxLayout()
        options.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        for fmt in (YAML, INI):
            self.format_combo.addItem(FORMAT_NAMES[fmt], fmt)
        self.format_combo.currentIndexChanged.connect(self.update_preview)
        options.addWidget(self.format_combo)
        options.addSpacing(12)
        options.addWidget(QLabel("ansible_user:"))
        self.user_combo = QComboBox()
        self.user_combo.setEditable(True)
        self.user_combo.setMinimumWidth(140)
        self.user_combo.lineEdit().setPlaceholderText("Not set")
        self.user_combo.setToolTip("The user name Ansible logs in with, for every device (the saved credentials' "
                                   "user names are listed). Leave it empty to give it on the command line (-u).")
        self.user_combo.currentTextChanged.connect(self.update_preview)
        options.addWidget(self.user_combo)
        self.session_users_check = self.option_check(options, "Sessions' own user names", True,
                                                     "A saved session's user name for its device, where it isn't "
                                                     "the one above")
        self.short_names_check = self.option_check(options, "Short names", True,
                                                   "core-sw1 rather than core-sw1.corp.example")
        self.become_check = self.option_check(options, "Enable mode", False,
                                              "ansible_become with the enable method, for Cisco IOS, ASA and Arista "
                                              "EOS logins that don't land in privileged mode")
        self.nomad_vars_check = self.option_check(options, "NOMAD details", False,
                                                  "nomad_kind, nomad_model, nomad_location and nomad_folder on each "
                                                  "device, for playbooks to use")
        options.addStretch()
        layout.addLayout(options)

        tools = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter devices: name, address, kind, model, OS, location, folder "
                                             "(Ctrl+F)")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self.apply_filter)
        tools.addWidget(self.filter_input, 1)
        tools.addWidget(QLabel("Tick:"))
        self.network_button = QPushButton("Network Devices")
        self.network_button.setToolTip("Tick the switches, routers, firewalls and devices with an Ansible OS shown, "
                                       "untick the rest shown")
        self.network_button.clicked.connect(self.tick_network)
        tools.addWidget(self.network_button)
        self.all_button = QPushButton("All Shown")
        self.all_button.clicked.connect(self.tick_all)
        tools.addWidget(self.all_button)
        self.none_button = QPushButton("None Shown")
        self.none_button.clicked.connect(self.tick_none)
        tools.addWidget(self.none_button)
        self.os_menu = QMenu(self)
        self.os_actions = []  # Made in Python and kept, so no wrapper outlives what Qt made
        for key, text in [(DETECTED, "As Detected from the Map")] + \
                         [(platform.key, platform.name) for platform in PLATFORMS.values()] + \
                         [(NONE, "None (Not Managed by Ansible)")]:
            action = QAction(text, self.os_menu)
            action.setData(key)
            self.os_menu.addAction(action)
            self.os_actions.append(action)
            if key == DETECTED:
                self.os_menu.addSeparator()
        self.os_menu.triggered.connect(self.on_os_chosen)
        self.os_button = QPushButton("Set Ansible OS")
        self.os_button.setToolTip("The Ansible OS of the selected devices, where the map got it wrong or couldn't "
                                  "tell")
        self.os_button.setMenu(self.os_menu)
        tools.addWidget(self.os_button)
        layout.addLayout(tools)

        self.splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        ColumnFitter(self.table)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_NAME, Qt.AscendingOrder)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_table_menu)
        self.splitter.addWidget(self.table)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.preview.setFont(monospace_font())
        self.preview.setPlaceholderText("Tick devices to see their inventory here.")
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        layout.addWidget(self.splitter, 1)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy the inventory to the clipboard")
        self.copy_button.clicked.connect(self.copy_inventory)
        buttons.addWidget(self.copy_button)
        self.save_button = accent_button("Save As...")
        self.save_button.clicked.connect(self.save_inventory)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def option_check(self, layout, text, checked, tooltip):
        check = QCheckBox(text)
        check.setChecked(checked)
        check.setToolTip(tooltip)
        check.toggled.connect(self.update_preview)
        layout.addWidget(check)
        return check

    # ----------------------------------------------------------------- Tab interface

    def focus_find(self):
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def save_settings(self, settings):
        settings.setValue("inventory/options", json.dumps({
            "map": self.map_check.isChecked(), "sessions": self.sessions_check.isChecked(),
            "format": self.format_combo.currentData(), "user": self.user_combo.currentText(),
            **{name: getattr(self, f"{name}_check").isChecked() for name in CHECKS}}))
        settings.setValue("inventory/platforms", json.dumps(self.platforms))
        settings.setValue("inventory/choices", json.dumps(self.choices))
        settings.setValue("inventory/folder", self.folder)
        settings.setValue("inventory/splitter", self.splitter.saveState())

    def restore_settings(self, settings):
        try:
            options = json.loads(settings.value("inventory/options", "", str) or "{}")
            self.platforms = {str(key): str(value) for key, value in
                              json.loads(settings.value("inventory/platforms", "", str) or "{}").items()
                              if value in PLATFORMS or value == NONE}
            self.choices = {str(key): bool(value) for key, value in
                            json.loads(settings.value("inventory/choices", "", str) or "{}").items()}
        except (ValueError, AttributeError) as error:
            log.warning("Couldn't read the Ansible Inventory page's settings: %s", error)
            options = {}
        self.filling = True  # Each change would make the inventory again
        try:
            self.map_check.setChecked(bool(options.get("map", True)))
            self.sessions_check.setChecked(bool(options.get("sessions", True)))
            index = self.format_combo.findData(options.get("format", YAML))
            self.format_combo.setCurrentIndex(max(index, 0))
            self.user_combo.setEditText(str(options.get("user", "")))
            for name in CHECKS:
                if name in options:
                    getattr(self, f"{name}_check").setChecked(bool(options[name]))
        finally:
            self.filling = False
        self.folder = settings.value("inventory/folder", "", str)
        state = settings.value("inventory/splitter")
        if state is not None:
            self.splitter.restoreState(state)
        self.refresh()

    def shutdown(self):
        pass

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()  # Sessions may have been saved, or another map opened, since

    # ----------------------------------------------------------------- Where the devices come from

    def map_page(self):
        return getattr(self.window, "netmap_tab", None)

    def open_map(self):
        page = self.map_page()
        return getattr(page, "network_map", None) if page is not None else None

    def map_name(self):
        page = self.map_page()
        try:
            return page.map_name() if page is not None else ""
        except AttributeError:
            return ""

    def session_store(self):
        return getattr(self.window, "session_store", None)

    def on_map_shown(self):
        if self.isVisible():
            self.refresh()

    def refresh(self):
        if self.filling:
            return
        network_map = self.open_map() if self.map_check.isChecked() else None
        store = self.session_store()
        sessions = store.source_sessions() if store is not None and self.sessions_check.isChecked() else []
        self.entries = inventory.collect_entries(network_map, sessions)
        self.fill_users(store)
        self.update_source_label(network_map, sessions)
        self.fill_table()
        self.update_preview()

    def fill_users(self, store):
        """The saved credentials' user names, to choose from (anything can be typed)."""
        text = self.user_combo.currentText()
        names = []
        if store is not None and getattr(store, "credentials", None) is not None:
            for credential in store.credentials.sorted():
                if credential.username and credential.username not in names:
                    names.append(credential.username)
        if names == [self.user_combo.itemText(index) for index in range(1, self.user_combo.count())]:
            return
        self.user_combo.blockSignals(True)
        self.user_combo.clear()
        self.user_combo.addItems([""] + names)
        self.user_combo.setEditText(text)
        self.user_combo.blockSignals(False)

    def update_source_label(self, network_map, sessions):
        parts = []
        if self.map_check.isChecked():
            if network_map is None:
                parts.append("no map is open on the Network Map page")
            else:
                name = self.map_name()
                parts.append(f"{len(network_map.devices)} devices on the map" + (f' "{name}"' if name else ""))
        if self.sessions_check.isChecked():
            count = sum(1 for session in sessions if session.protocol == "SSH")
            parts.append(f"{count} saved SSH session{'s' if count != 1 else ''}")
        if not parts:
            set_hint(self.source_label, "Choose where the devices come from.", "warning")
            return
        set_hint(self.source_label, "From " + " and ".join(parts) + ".",
                 "warning" if self.map_check.isChecked() and network_map is None else "info")

    def sources_text(self):
        parts = []
        if self.map_check.isChecked() and self.open_map() is not None:
            parts.append(f'the network map "{self.map_name()}"' if self.map_name() else "the network map")
        if self.sessions_check.isChecked() and any(entry.from_session for entry in self.entries):
            parts.append("saved SSH sessions")
        return " and ".join(parts)

    # ----------------------------------------------------------------- The devices

    def entry_platform(self, entry):
        return self.platforms.get(entry.key, entry.detected)

    def ticked(self, entry):
        return self.choices.get(entry.key, entry.default_choice())

    def fill_table(self):
        self.filling = True
        self.table.setSortingEnabled(False)
        try:
            self.table.setRowCount(len(self.entries))
            for row, entry in enumerate(self.entries):
                name = SortableTableItem(entry.name, entry.name.lower())
                name.setFlags(name.flags() | Qt.ItemIsUserCheckable)
                name.setCheckState(Qt.Checked if self.ticked(entry) else Qt.Unchecked)
                name.setData(KEY_ROLE, entry.key)
                self.table.setItem(row, COL_NAME, name)
                self.table.setItem(row, COL_ADDRESS, SortableTableItem(entry.address, address_key(entry.address)))
                self.table.setItem(row, COL_KIND, SortableTableItem(entry.kind_text))
                self.table.setItem(row, COL_MODEL, SortableTableItem(entry.model))
                self.table.setItem(row, COL_OS, self.os_item(entry))
                self.table.setItem(row, COL_LOCATION, SortableTableItem(entry.location_text))
                self.table.setItem(row, COL_FOLDER, SortableTableItem(entry.folder))
                self.table.setItem(row, COL_FROM, SortableTableItem(entry.source_text))
        finally:
            self.table.setSortingEnabled(True)
            self.filling = False
        self.apply_filter()

    def os_item(self, entry):
        platform = PLATFORMS.get(self.entry_platform(entry))
        item = SortableTableItem(platform.name if platform is not None else "")
        if entry.key in self.platforms:
            font = QFont(item.font())
            font.setItalic(True)
            item.setFont(font)
            detected = PLATFORMS.get(entry.detected)
            item.setToolTip("Chosen by hand. From the map: " + (detected.name if detected else "not known"))
        elif platform is None and entry.kind in NETWORK_KINDS:
            item.setToolTip("The map can't tell: choose it with Set Ansible OS")
        return item

    def entry_at(self, row):
        item = self.table.item(row, COL_NAME)
        key = item.data(KEY_ROLE) if item is not None else None
        return next((entry for entry in self.entries if entry.key == key), None)

    def apply_filter(self):
        words = self.filter_input.text().strip().lower().split()
        for row in range(self.table.rowCount()):
            text = " ".join(self.table.item(row, column).text().lower() for column in range(len(COLUMNS))
                            if self.table.item(row, column) is not None)
            self.table.setRowHidden(row, not all(word in text for word in words))

    def visible_rows(self):
        return [row for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]

    def on_item_changed(self, item):
        if self.filling or item.column() != COL_NAME:
            return
        key = item.data(KEY_ROLE)
        entry = next((entry for entry in self.entries if entry.key == key), None)
        if entry is None:
            return
        checked = item.checkState() == Qt.Checked
        if checked == entry.default_choice():
            self.choices.pop(key, None)
        else:
            self.choices[key] = checked
        self.update_preview()

    def set_ticks(self, wanted):
        """Tick or untick the rows shown: wanted(entry) says which."""
        self.filling = True
        try:
            for row in self.visible_rows():
                entry = self.entry_at(row)
                if entry is None:
                    continue
                checked = wanted(entry)
                self.table.item(row, COL_NAME).setCheckState(Qt.Checked if checked else Qt.Unchecked)
                if checked == entry.default_choice():
                    self.choices.pop(entry.key, None)
                else:
                    self.choices[entry.key] = checked
        finally:
            self.filling = False
        self.update_preview()

    def tick_network(self):
        self.set_ticks(self.is_network_device)

    def is_network_device(self, entry):
        return entry.kind in NETWORK_KINDS or self.entry_platform(entry) != NONE

    def tick_all(self):
        self.set_ticks(self.always)

    def tick_none(self):
        self.set_ticks(self.never)

    @staticmethod
    def always(entry):
        return True

    @staticmethod
    def never(entry):
        return False

    def selected_entries(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [entry for entry in (self.entry_at(row) for row in rows) if entry is not None]

    def show_table_menu(self, position):
        if self.selected_entries():
            self.os_menu.exec_(self.table.viewport().mapToGlobal(position))

    def on_os_chosen(self, action):
        entries = self.selected_entries()
        if not entries:
            self.show_status("Select the devices first (Ctrl or Shift-click for several).", "info")
            return
        key = action.data()
        for entry in entries:
            if key == DETECTED or key == entry.detected:
                self.platforms.pop(entry.key, None)
            else:
                self.platforms[entry.key] = key
        self.filling = True
        self.table.setSortingEnabled(False)
        try:
            for row in range(self.table.rowCount()):
                entry = self.entry_at(row)
                if entry in entries:
                    self.table.setItem(row, COL_OS, self.os_item(entry))
        finally:
            self.table.setSortingEnabled(True)
            self.filling = False
        self.update_preview()

    # ----------------------------------------------------------------- The inventory

    def options(self):
        return inventory.Options(format=self.format_combo.currentData() or YAML,
                                 username=self.user_combo.currentText().strip(),
                                 **{name: getattr(self, f"{name}_check").isChecked() for name in CHECKS})

    def update_preview(self, *_):
        if self.filling:
            return
        options = self.options()
        chosen = [entry for entry in self.entries if self.ticked(entry)]
        self.built = inventory.build(chosen, options, self.platforms)
        made_by = f"NOMAD {__version__} on {datetime.datetime.now():%Y-%m-%d %H:%M}"
        comments = inventory.header(self.built, made_by, self.sources_text())
        self.preview.setPlainText(inventory.render(self.built, options.format, comments) if chosen else "")
        self.update_status()

    def update_status(self):
        built = self.built
        hosts = len(built.hosts) if built is not None else 0
        self.copy_button.setEnabled(hosts > 0)
        self.save_button.setEnabled(hosts > 0)
        if not self.entries:
            set_hint(self.status_label, "No devices: open a map on the Network Map page or save SSH sessions in "
                                        "Terminal.", "info")
            return
        if not hosts:
            set_hint(self.status_label, f"None of the {len(self.entries)} devices are ticked.", "info")
            return
        text = f"{hosts} device{'s' if hosts != 1 else ''} in {len(built.groups)} " \
               f"group{'s' if len(built.groups) != 1 else ''}."
        if built.problems:
            shown = built.problems[:PROBLEMS_SHOWN]
            more = len(built.problems) - len(shown)
            text += " " + " ".join(shown) + (f" And {more} more." if more else "")
        set_hint(self.status_label, text, "warning" if built.problems else "success")

    def copy_inventory(self):
        QApplication.clipboard().setText(self.preview.toPlainText())
        self.show_status("Copied the inventory to the clipboard.", "info")

    def save_inventory(self):
        fmt = self.format_combo.currentData() or YAML
        filters = "YAML inventory (*.yml *.yaml);;All files (*)" if fmt == YAML else \
            "INI inventory (*.ini);;All files (*)"
        name = f"inventory{EXTENSIONS[fmt]}"
        start = str(Path(self.folder) / name) if self.folder else name
        path, _ = QFileDialog.getSaveFileName(self, "Save Ansible Inventory", start, filters)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as file:
                file.write(self.preview.toPlainText())
        except OSError as error:
            self.show_status(f"Couldn't save the inventory: {error}", "error")
            return
        self.folder = str(Path(path).parent)
        self.show_status(f"Saved the inventory to {path}.")

    def show_status(self, message, kind="success"):
        show = getattr(self.window, "show_status", None)
        if show is not None:
            show(message, kind)
