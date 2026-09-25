"""Utilities tab: a subnet calculator and Wake-on-LAN."""
import logging

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAbstractItemView, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, \
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget, QVBoxLayout, QWidget

from ..oui import format_mac, vendor
from ..subnet import describe, host_range, parse_subnet, prefix_for_hosts, split
from ..wol import WakeTarget, destinations, send_magic_packet, targets_from_json, targets_to_json, \
    validate_broadcast
from .common import SortableTableItem, set_hint, set_invalid
from .theme import accent_button

log = logging.getLogger(__name__)

SPLIT_COLUMNS = ["Subnet", "First Host", "Last Host", "Broadcast"]
WAKE_COLUMNS = ["Name", "MAC Address", "Vendor", "Subnet Broadcast"]


def read_only_table(columns):
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class UtilitiesTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.network = None
        self.wake_targets = []
        self.init_ui()
        window.adapter_changed.connect(lambda _: self.update_adapter_button())
        window.snapshot_changed.connect(lambda _: self.update_adapter_button())
        self.update_adapter_button()
        self.update_wake_buttons()

    def init_ui(self):
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.build_subnet_group())
        splitter.addWidget(self.build_wake_group())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

    # ----------------------------------------------------------------- Subnet calculator

    def build_subnet_group(self):
        group = QGroupBox("Subnet Calculator")
        layout = QVBoxLayout(group)
        input_row = QHBoxLayout()
        self.subnet_input = QLineEdit()
        self.subnet_input.setPlaceholderText("192.168.1.10/24, 10.0.0.5 255.255.252.0 or 2001:db8::/48")
        self.adapter_button = QPushButton("Adapter's Address")
        input_row.addWidget(self.subnet_input, 1)
        input_row.addWidget(self.adapter_button)
        layout.addLayout(input_row)
        self.subnet_status = QLabel()
        self.subnet_status.setWordWrap(True)
        layout.addWidget(self.subnet_status)

        self.details_table = read_only_table(["", ""])
        self.details_table.horizontalHeader().setVisible(False)
        layout.addWidget(self.details_table, 3)

        split_row = QHBoxLayout()
        split_row.addWidget(QLabel("Split into /"))
        self.split_prefix_input = QSpinBox()
        self.split_prefix_input.setRange(1, 128)
        self.split_prefix_input.setButtonSymbols(QSpinBox.NoButtons)
        split_row.addWidget(self.split_prefix_input)
        split_row.addWidget(QLabel("or subnets of at least"))
        self.hosts_input = QSpinBox()
        self.hosts_input.setRange(1, 2 ** 31 - 1)
        self.hosts_input.setButtonSymbols(QSpinBox.NoButtons)
        self.hosts_input.setToolTip("Works out the prefix for subnets that each hold this many hosts.")
        split_row.addWidget(self.hosts_input)
        split_row.addWidget(QLabel("hosts"))
        self.split_button = QPushButton("Split")
        split_row.addWidget(self.split_button)
        split_row.addStretch()
        layout.addLayout(split_row)
        self.split_status = QLabel()
        self.split_status.setWordWrap(True)
        layout.addWidget(self.split_status)
        self.split_table = read_only_table(SPLIT_COLUMNS)
        layout.addWidget(self.split_table, 2)

        self.subnet_input.textChanged.connect(self.calculate)
        self.adapter_button.clicked.connect(self.use_adapter_address)
        self.hosts_input.editingFinished.connect(self.use_host_count)
        self.split_button.clicked.connect(self.split_subnet)
        self.split_prefix_input.editingFinished.connect(self.split_subnet)
        return group

    def adapter_address(self):
        adapter = self.window.current_adapter()
        return str(adapter.ipv4[0]) if adapter is not None and adapter.ipv4 else None

    def update_adapter_button(self):
        address = self.adapter_address()
        self.adapter_button.setEnabled(address is not None)
        self.adapter_button.setText(f"Adapter's Address ({address})" if address else "Adapter's Address")
        if address and not self.subnet_input.text().strip():
            self.subnet_input.setText(address)

    def use_adapter_address(self):
        address = self.adapter_address()
        if address:
            self.subnet_input.setText(address)

    def calculate(self):
        self.split_table.setRowCount(0)
        self.split_status.clear()
        try:
            address, network = parse_subnet(self.subnet_input.text())
        except ValueError as error:
            self.network = None
            self.details_table.setRowCount(0)
            set_invalid(self.subnet_input, bool(self.subnet_input.text().strip()))
            set_hint(self.subnet_status, str(error) if self.subnet_input.text().strip() else "", "error")
            self.split_button.setEnabled(False)
            return
        set_invalid(self.subnet_input, False)
        self.subnet_status.clear()
        self.network = network
        rows = describe(address, network)
        self.details_table.setRowCount(len(rows))
        for row, (label, value) in enumerate(rows):
            self.details_table.setItem(row, 0, SortableTableItem(label))
            self.details_table.setItem(row, 1, SortableTableItem(value))
        can_split = network.prefixlen < network.max_prefixlen
        self.split_button.setEnabled(can_split)
        self.split_prefix_input.setRange(min(network.prefixlen + 1, network.max_prefixlen), network.max_prefixlen)
        if can_split and self.split_prefix_input.value() <= network.prefixlen:
            self.split_prefix_input.setValue(network.prefixlen + 1)

    def use_host_count(self):
        if self.network is None:
            return
        try:
            prefix = prefix_for_hosts(self.hosts_input.value(), self.network.version)
        except ValueError as error:
            set_hint(self.split_status, str(error), "error")
            return
        if prefix <= self.network.prefixlen:
            set_hint(self.split_status, f"Subnets of {self.hosts_input.value():,} hosts need a /{prefix}, which is "
                                        f"no smaller than {self.network}.", "warning")
            return
        self.split_prefix_input.setValue(prefix)
        self.split_subnet()

    def split_subnet(self):
        if self.network is None:
            return
        try:
            subnets, total = split(self.network, self.split_prefix_input.value())
        except ValueError as error:
            set_hint(self.split_status, str(error), "error")
            return
        self.split_table.setRowCount(len(subnets))
        for row, subnet in enumerate(subnets):
            first, last, _ = host_range(subnet)
            broadcast = str(subnet.broadcast_address) if subnet.version == 4 and subnet.prefixlen <= 30 else ""
            for column, value in enumerate([str(subnet), str(first), str(last), broadcast]):
                self.split_table.setItem(row, column, SortableTableItem(value))
        hosts = host_range(subnets[0])[2] if subnets else 0
        shown = f" (showing the first {len(subnets):,})" if len(subnets) < total else ""
        set_hint(self.split_status, f"{total:,} subnets of /{self.split_prefix_input.value()}, {hosts:,} hosts "
                                    f"each{shown}.", "info")

    # ----------------------------------------------------------------- Wake-on-LAN

    def build_wake_group(self):
        group = QGroupBox("Wake-on-LAN")
        layout = QVBoxLayout(group)
        intro = QLabel("Wake a computer whose network card is set up for Wake-on-LAN. The packet is broadcast on "
                       "every connected subnet; for a computer on another subnet, give that subnet's broadcast "
                       "address (the router must allow directed broadcasts).")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.wake_name_input = QLineEdit()
        self.wake_name_input.setPlaceholderText("Optional, to save it")
        self.wake_mac_input = QLineEdit()
        self.wake_mac_input.setPlaceholderText("00-11-22-33-44-55")
        self.wake_broadcast_input = QLineEdit()
        self.wake_broadcast_input.setPlaceholderText("Optional, such as 192.168.20.255")
        form = QFormLayout()
        form.addRow("Name:", self.wake_name_input)
        form.addRow("MAC address:", self.wake_mac_input)
        form.addRow("Subnet broadcast:", self.wake_broadcast_input)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.wake_button = accent_button("Wake")
        self.save_wake_button = QPushButton("Save")
        self.delete_wake_button = QPushButton("Delete")
        for button in (self.wake_button, self.save_wake_button, self.delete_wake_button):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.wake_status = QLabel()
        self.wake_status.setWordWrap(True)
        layout.addWidget(self.wake_status)
        self.wake_table = read_only_table(WAKE_COLUMNS)
        layout.addWidget(self.wake_table, 1)

        self.wake_button.clicked.connect(self.wake)
        self.wake_mac_input.returnPressed.connect(self.wake)
        self.wake_mac_input.textChanged.connect(lambda: set_invalid(self.wake_mac_input, False))
        self.wake_broadcast_input.textChanged.connect(lambda: set_invalid(self.wake_broadcast_input, False))
        self.save_wake_button.clicked.connect(self.save_target)
        self.delete_wake_button.clicked.connect(self.delete_target)
        self.wake_table.itemSelectionChanged.connect(self.on_target_selected)
        self.wake_table.doubleClicked.connect(lambda _: self.wake())
        self.wake_name_input.textChanged.connect(self.update_wake_buttons)
        return group

    def read_wake_form(self):
        """Returns a WakeTarget from the form, or None after showing what's wrong."""
        mac = format_mac(self.wake_mac_input.text())
        if not mac:
            set_invalid(self.wake_mac_input, True)
            set_hint(self.wake_status, "Enter the computer's MAC address, such as 00-11-22-33-44-55.", "error")
            return None
        try:
            broadcast = validate_broadcast(self.wake_broadcast_input.text())
        except ValueError as error:
            set_invalid(self.wake_broadcast_input, True)
            set_hint(self.wake_status, str(error), "error")
            return None
        return WakeTarget(self.wake_name_input.text().strip(), mac, broadcast)

    def wake(self):
        target = self.read_wake_form()
        if target is None:
            return
        addresses = [address for adapter in self.window.snapshot.real_adapters() if adapter.status == "Up"
                     for address in adapter.ipv4 if not address.ip.is_link_local]
        try:
            sent = send_magic_packet(target.mac, destinations(addresses, target.broadcast))
        except (OSError, ValueError) as error:
            set_hint(self.wake_status, f"Couldn't send the wake-up packet: {error}", "error")
            return
        who = target.name or target.mac
        set_hint(self.wake_status, f"Sent the wake-up packet for {who} ({sent} broadcast{'' if sent == 1 else 's'}). "
                                   "A computer usually takes 10-60 seconds to start; try pinging it.", "success")
        log.info("Sent Wake-on-LAN for %s", target.mac)

    def wake_device(self, mac, name=""):
        """Fill in a device from another tab (Sweep, ARP)."""
        self.wake_mac_input.setText(mac)
        self.wake_name_input.setText(name)
        self.wake_broadcast_input.clear()
        set_hint(self.wake_status, "Press Wake to send the packet, or Save to keep this device.", "info")

    def save_target(self):
        target = self.read_wake_form()
        if target is None:
            return
        if not target.name:
            set_hint(self.wake_status, "Give the device a name to save it.", "error")
            self.wake_name_input.setFocus()
            return
        self.wake_targets = [saved for saved in self.wake_targets if saved.name != target.name] + [target]
        self.wake_targets.sort(key=lambda saved: saved.name.lower())
        self.fill_wake_table(select=target.name)
        set_hint(self.wake_status, f"Saved {target.name}.", "success")

    def delete_target(self):
        target = self.selected_target()
        if target is None:
            return
        reply = QMessageBox.question(self, "Delete Device", f"Delete the saved device {target.name}?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.wake_targets.remove(target)
            self.fill_wake_table()

    def fill_wake_table(self, select=None):
        self.wake_table.setRowCount(len(self.wake_targets))
        for row, target in enumerate(self.wake_targets):
            for column, value in enumerate([target.name, target.mac, vendor(target.mac), target.broadcast]):
                self.wake_table.setItem(row, column, SortableTableItem(value, data=target))
            if target.name == select:
                self.wake_table.selectRow(row)
        self.update_wake_buttons()

    def selected_target(self):
        rows = self.wake_table.selectionModel().selectedRows()
        return self.wake_table.item(rows[0].row(), 0).data_object if rows else None

    def on_target_selected(self):
        target = self.selected_target()
        if target is not None:
            self.wake_name_input.setText(target.name)
            self.wake_mac_input.setText(target.mac)
            self.wake_broadcast_input.setText(target.broadcast)
        self.update_wake_buttons()

    def update_wake_buttons(self):
        self.delete_wake_button.setEnabled(self.selected_target() is not None)

    # ----------------------------------------------------------------- Tab interface

    def save_settings(self, settings):
        settings.setValue("utilities/subnet", self.subnet_input.text())
        settings.setValue("utilities/wake_targets", targets_to_json(self.wake_targets))

    def restore_settings(self, settings):
        self.subnet_input.setText(settings.value("utilities/subnet", "", str))
        self.wake_targets = targets_from_json(settings.value("utilities/wake_targets", "[]", str))
        self.fill_wake_table()
        self.update_adapter_button()

    def shutdown(self):
        pass
