"""ARP tab: the ARP / IPv6 neighbor table, with vendors, and warnings about duplicate IPs and ARP spoofing."""
import logging
import time

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, \
    QLineEdit, QMenu, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget

from ..neighbors import PERMANENT, UNRESOLVED_STATES, MacHistory, clear_neighbor_cache, delete_neighbor, \
    load_neighbors, shared_macs
from .common import SortableTableItem, run_in_background, set_hint
from .theme import COLORS

log = logging.getLogger(__name__)

COLUMNS = ["IP Address", "MAC Address", "Vendor", "State", "Interface", "Warning"]
COL_ADDRESS, COL_MAC, COL_VENDOR, COL_STATE, COL_INTERFACE, COL_WARNING = range(len(COLUMNS))
AUTO_REFRESH_AFTER_SECONDS = 3
WARNING_BACKGROUND = COLORS["warning_background"]


class NeighborsTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.table_data = None
        self.history = MacHistory()
        self.loading = False
        self.last_load = 0.0
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.family_combo = QComboBox()
        self.family_combo.addItem("IPv4 (ARP)", 4)
        self.family_combo.addItem("IPv6 (neighbors)", 6)
        toolbar.addWidget(self.family_combo)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by address, MAC, vendor, interface...")
        self.filter_input.setClearButtonEnabled(True)
        toolbar.addWidget(self.filter_input, 1)
        self.adapter_only_check = QCheckBox("Selected adapter only")
        self.hide_multicast_check = QCheckBox("Hide multicast and broadcast")
        self.hide_multicast_check.setChecked(True)
        self.hide_unresolved_check = QCheckBox("Hide unreachable")
        self.hide_unresolved_check.setToolTip("Hide addresses that didn't answer ARP, so have no MAC address.")
        for check in (self.adapter_only_check, self.hide_multicast_check, self.hide_unresolved_check):
            toolbar.addWidget(check)
        layout.addLayout(toolbar)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(False)
        self.warning_label.setStyleSheet(f"background: {WARNING_BACKGROUND}; border: 1px solid "
                                         f"{COLORS['warning']}; padding: 6px;")
        layout.addWidget(self.warning_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_ADDRESS, Qt.AscendingOrder)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel()
        bottom.addWidget(self.status_label, 1)
        self.refresh_button = QPushButton("Refresh")
        self.delete_button = QPushButton("Delete Entry")
        self.delete_button.setToolTip("Remove the selected entry, so Windows asks for its MAC address again.")
        self.clear_button = QPushButton("Clear ARP Cache")
        self.clear_button.setToolTip("Empty the ARP and IPv6 neighbor caches (like arp -d *). Useful after "
                                     "replacing a device that kept its IP address.")
        for button in (self.refresh_button, self.delete_button, self.clear_button):
            bottom.addWidget(button)
        layout.addLayout(bottom)

        self.family_combo.currentIndexChanged.connect(self.fill_table)
        self.filter_input.textChanged.connect(self.fill_table)
        for check in (self.adapter_only_check, self.hide_multicast_check, self.hide_unresolved_check):
            check.toggled.connect(self.fill_table)
        self.window.adapter_changed.connect(lambda _: self.fill_table() if self.adapter_only_check.isChecked()
                                            else None)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.refresh_button.clicked.connect(self.refresh)
        self.delete_button.clicked.connect(lambda: self.delete_entry(self.selected_neighbor()))
        self.clear_button.clicked.connect(self.clear_cache)
        self.update_buttons()

    # ----------------------------------------------------------------- Tab interface

    def save_settings(self, settings):
        settings.setValue("arp/family", self.family_combo.currentData())
        settings.setValue("arp/adapter_only", self.adapter_only_check.isChecked())
        settings.setValue("arp/hide_multicast", self.hide_multicast_check.isChecked())
        settings.setValue("arp/hide_unresolved", self.hide_unresolved_check.isChecked())

    def restore_settings(self, settings):
        self.family_combo.setCurrentIndex(max(0, self.family_combo.findData(settings.value("arp/family", 4, int))))
        self.adapter_only_check.setChecked(settings.value("arp/adapter_only", False, bool))
        self.hide_multicast_check.setChecked(settings.value("arp/hide_multicast", True, bool))
        self.hide_unresolved_check.setChecked(settings.value("arp/hide_unresolved", False, bool))

    def shutdown(self):
        pass

    def refresh_if_stale(self):
        if time.monotonic() - self.last_load > AUTO_REFRESH_AFTER_SECONDS:
            self.refresh()

    # ----------------------------------------------------------------- Loading

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        self.refresh_button.setEnabled(False)
        self.window.set_busy("arp", "Reading the ARP table")
        run_in_background(load_neighbors, self.on_loaded, self.on_load_failed)

    def on_loaded(self, table_data):
        self.loading = False
        self.last_load = time.monotonic()
        self.window.clear_busy("arp")
        self.refresh_button.setEnabled(True)
        for neighbor, previous in self.history.update(table_data.neighbors):
            log.warning("%s on %s changed MAC address from %s to %s", neighbor.address, neighbor.interface_name,
                        previous, neighbor.mac)
        self.table_data = table_data
        self.fill_table()

    def on_load_failed(self, error):
        self.loading = False
        self.window.clear_busy("arp")
        self.refresh_button.setEnabled(True)
        set_hint(self.status_label, f"Couldn't read the ARP table: {error}", "error")

    # ----------------------------------------------------------------- Table

    def gateways(self):
        return {gateway for adapter in self.window.snapshot.adapters.values()
                for gateway in adapter.gateways4 + adapter.gateways6}

    def visible_neighbors(self):
        family = self.family_combo.currentData()
        adapter = self.window.current_adapter()
        words = self.filter_input.text().lower().split()
        for neighbor in self.table_data.neighbors:
            if neighbor.family != family:
                continue
            if self.adapter_only_check.isChecked() and (adapter is None or neighbor.interface != adapter.index):
                continue
            if self.hide_multicast_check.isChecked() and neighbor.is_multicast:
                continue
            if self.hide_unresolved_check.isChecked() and (neighbor.state in UNRESOLVED_STATES or not neighbor.mac):
                continue
            text = " ".join((neighbor.address, neighbor.mac, neighbor.vendor, neighbor.state,
                             neighbor.interface_name)).lower()
            if all(word in text for word in words):
                yield neighbor

    def warnings(self):
        """({(interface, address): warning} for the table, [banner lines]) about possible conflicts or spoofing."""
        by_row, banner = {}, []
        gateways = self.gateways()
        networks = [address.network for adapter in self.window.snapshot.adapters.values()
                    for address in adapter.ipv4]
        for (interface, mac), addresses in shared_macs(self.table_data.neighbors, networks).items():
            gateway = next((address for address in addresses if address in gateways), None)
            others = [address for address in addresses if address != gateway]
            if gateway:
                note = f"Same MAC as the gateway {gateway}"
                banner.append(f"The gateway {gateway}'s MAC address {mac} also answers for {', '.join(others)}. "
                              "That's normal if the gateway owns those addresses (or does proxy ARP), but it can "
                              "also mean another device is impersonating the gateway (ARP spoofing).")
            else:
                note = f"MAC shared with {len(addresses) - 1} other address{'' if len(addresses) == 2 else 'es'}"
            for address in (others if gateway else addresses):
                by_row[(interface, address)] = note
        for neighbor in self.table_data.neighbors:
            earlier = self.history.earlier_macs(neighbor)
            if earlier:
                by_row[(neighbor.interface, neighbor.address)] = f"MAC changed (was {', '.join(earlier)})"
                banner.append(f"{neighbor.address} has answered from more than one MAC address "
                              f"({', '.join(earlier + [neighbor.mac])}). Two devices may be using the same IP "
                              "address.")
        for interface_name, address in self.table_data.duplicate_addresses:
            banner.append(f"Windows found another device already using this computer's address {address} on "
                          f"{interface_name}. Change one of their IP addresses.")
        return by_row, banner

    def fill_table(self):
        if self.table_data is None:
            return
        selected = self.selected_neighbor()
        selected_key = (selected.interface, selected.address) if selected else None
        row_warnings, banner = self.warnings()
        gateways = self.gateways()
        neighbors = list(self.visible_neighbors())

        table = self.table
        table.setSortingEnabled(False)
        table.setRowCount(len(neighbors))
        for row, neighbor in enumerate(neighbors):
            warning = row_warnings.get((neighbor.interface, neighbor.address), "")
            address = neighbor.address + (" (gateway)" if neighbor.address in gateways else "")
            cells = [(address, neighbor.sort_key), (neighbor.mac, neighbor.mac), (neighbor.vendor, neighbor.vendor),
                     (neighbor.state, neighbor.state), (neighbor.interface_name, neighbor.interface_name.lower()),
                     (warning, warning)]
            for column, (text, sort_key) in enumerate(cells):
                item = SortableTableItem(text, sort_key, neighbor)
                if warning:
                    item.setBackground(QColor(WARNING_BACKGROUND))
                elif neighbor.state in UNRESOLVED_STATES:
                    item.setForeground(QColor(COLORS["disabled"]))
                if neighbor.state == PERMANENT and column == COL_STATE:
                    item.setToolTip("Static entry: added by hand or by Windows, and never expires.")
                table.setItem(row, column, item)
        table.setSortingEnabled(True)

        self.warning_label.setText("\n\n".join(banner))
        self.warning_label.setVisible(bool(banner))
        for row in range(table.rowCount()):
            neighbor = table.item(row, 0).data_object
            if (neighbor.interface, neighbor.address) == selected_key:
                table.selectRow(row)
                break
        shown = len(neighbors)
        total = sum(1 for neighbor in self.table_data.neighbors if neighbor.family == self.family_combo.currentData())
        set_hint(self.status_label, f"Showing {shown} of {total} entries.", "info")
        self.update_buttons()

    def selected_neighbor(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data_object if rows else None

    def update_buttons(self):
        self.delete_button.setEnabled(self.selected_neighbor() is not None)

    def show_context_menu(self, position):
        item = self.table.itemAt(position)
        if item is None:
            return
        self.table.selectRow(item.row())
        neighbor = item.data_object
        address = neighbor.address.partition("%")[0]
        menu = QMenu(self)
        actions = {
            menu.addAction("Ping"): lambda: self.window.sweep_tab.ping(address),
            menu.addAction("Scan Ports"): lambda: self.window.sweep_tab.scan_ports(address),
        }
        menu.addSeparator()
        actions[menu.addAction("Copy Address")] = lambda: QApplication.clipboard().setText(address)
        if neighbor.mac:
            actions[menu.addAction("Copy MAC Address")] = lambda: QApplication.clipboard().setText(neighbor.mac)
            actions[menu.addAction("Wake-on-LAN...")] = lambda: self.window.wake_device(neighbor.mac)
        menu.addSeparator()
        actions[menu.addAction("Delete Entry")] = lambda: self.delete_entry(neighbor)
        chosen = menu.exec_(self.table.viewport().mapToGlobal(position))
        if chosen in actions:
            actions[chosen]()

    # ----------------------------------------------------------------- Changes

    def delete_entry(self, neighbor):
        if neighbor is None:
            return
        self.window.run_change(f"Deleting the ARP entry for {neighbor.address}", lambda: delete_neighbor(neighbor),
                               on_success=lambda _: self.after_change(f"Deleted the entry for {neighbor.address}."))

    def clear_cache(self):
        reply = QMessageBox.question(self, "Clear ARP Cache",
                                     "Empty the ARP and IPv6 neighbor caches on every adapter?\n\nWindows will "
                                     "ask for each MAC address again the next time it's needed.",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.Yes:
            self.window.run_change("Clearing the ARP cache", clear_neighbor_cache,
                                   on_success=lambda _: self.after_change("ARP cache cleared."))

    def after_change(self, message):
        self.window.show_status(message)
        self.history = MacHistory()  # Entries relearned after a change aren't conflicts
        self.refresh()
