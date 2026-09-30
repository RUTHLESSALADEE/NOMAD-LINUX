"""IPAM tools beyond editing one thing at a time: checking a network's data, comparing it with the workbook, finding
free space for new subnets, changing many addresses or subnets at once, and picking a free address for an adapter."""
import ipaddress
import logging
import socket

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, \
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..ipam.checks import ERROR, INFO, SEVERITY_LABELS, WARNING, check_network
from ..ipam.compare import ACTION_LABELS, ADDRESS, NETWORK, SUBNET, apply_changes
from ..ipam.store import RESERVED, STATUSES, USED, IpamError
from .common import set_hint
from .theme import COLORS

log = logging.getLogger(__name__)

SEVERITY_COLORS = {ERROR: "error", WARNING: "warning", INFO: "muted"}


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.horizontalHeader().setStretchLastSection(True)
    table.setWordWrap(True)
    return table


def _count(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _item(text, color=None, data=None):
    item = QTableWidgetItem(text)
    item.setToolTip(text)
    if color:
        item.setForeground(QColor(COLORS[color]))
    if data is not None:
        item.setData(Qt.UserRole, data)
    return item


# --------------------------------------------------------------------- Check Data

CHECK_COLUMNS = ["", "Problem", "Where", "Details"]


class CheckDataDialog(QDialog):
    """Likely mistakes in a network's data; double-click one to go to it (the dialog stays open)."""

    def __init__(self, parent, store, network, go_to):
        super().__init__(parent)
        self.store, self.network, self.go_to = store, network, go_to
        self.setWindowTitle(f"Check Data: {network.name}")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.resize(1100, 560)
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.summary_label = QLabel()
        filters.addWidget(self.summary_label, 1)
        self.show_notes = QCheckBox("Show notes")
        self.show_notes.setChecked(True)
        self.show_notes.setToolTip("Notes are things worth a look that are often fine.")
        filters.addWidget(self.show_notes)
        layout.addLayout(filters)
        self.table = _table(CHECK_COLUMNS)
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel("Double-click a finding to go to it. Check Again after fixing things."))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        again = buttons.addButton("Check Again", QDialogButtonBox.ActionRole)
        again.clicked.connect(self.run)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.show_notes.toggled.connect(self.fill)
        self.table.cellDoubleClicked.connect(self.open_finding)
        self.run()

    def run(self):
        self.findings = check_network(self.store, self.network.id)
        self.fill()

    def fill(self):
        shown = [finding for finding in self.findings if self.show_notes.isChecked() or finding.severity != INFO]
        counts = {severity: sum(1 for finding in self.findings if finding.severity == severity)
                  for severity in (ERROR, WARNING, INFO)}
        if not self.findings:
            set_hint(self.summary_label, "Nothing found: no likely mistakes in this network.", "success")
        else:
            set_hint(self.summary_label, f"{_count(counts[ERROR], 'error')}, {_count(counts[WARNING], 'warning')} "
                                         f"and {_count(counts[INFO], 'note')}.",
                     "error" if counts[ERROR] else "warning" if counts[WARNING] else "info")
        self.table.setRowCount(len(shown))
        for row, finding in enumerate(shown):
            color = SEVERITY_COLORS[finding.severity]
            values = [SEVERITY_LABELS[finding.severity], finding.check, finding.where, finding.message]
            for column, value in enumerate(values):
                self.table.setItem(row, column, _item(value, color if column == 0 else None, finding))
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(1, min(self.table.columnWidth(1), 300))
        self.table.setColumnWidth(2, min(self.table.columnWidth(2), 320))
        self.table.resizeRowsToContents()

    def showEvent(self, event):
        super().showEvent(event)
        self.table.resizeRowsToContents()  # Now the details column has its real width to wrap in

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.table.resizeRowsToContents()

    def open_finding(self, row, _column):
        finding = self.table.item(row, 0).data(Qt.UserRole)
        self.go_to(finding.subnet, finding.ip)


# --------------------------------------------------------------------- Compare with Workbook

COMPARE_COLUMNS = ["Make", "What", "Change", "Details", "Note"]
KIND_LABELS = {NETWORK: "Network detail", SUBNET: "Subnet", ADDRESS: "Address"}


class CompareDialog(QDialog):
    """What differs between a network and its workbook page: tick what to bring in, then Apply."""

    def __init__(self, parent, store, network, page, changes):
        super().__init__(parent)
        self.store, self.network, self.changes = store, network, changes
        self.made = 0
        self.setWindowTitle(f"Compare {network.name} with {page}")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.resize(1200, 640)
        layout = QVBoxLayout(self)
        intro = QLabel(f"How <b>{network.name}</b> differs from the workbook page <b>{page}</b>. Ticked changes "
                       "make IPAM match the workbook. Unticked by default: anything only IPAM has (it may have been "
                       "recorded in NOMAD), and anything changed or deleted in NOMAD since it was imported (NOMAD "
                       "probably has the newer answer).")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        bulk = QHBoxLayout()
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("Everything", None)
        for kind, label in KIND_LABELS.items():
            self.filter_combo.addItem(f"{label}s", kind)
        bulk.addWidget(QLabel("Show:"))
        bulk.addWidget(self.filter_combo)
        bulk.addStretch()
        for label, state in (("Tick All Shown", True), ("Untick All Shown", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, state=state: self.tick_shown(state))
            bulk.addWidget(button)
        layout.addLayout(bulk)
        self.table = _table(COMPARE_COLUMNS)
        layout.addWidget(self.table, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.apply_button = buttons.addButton("Apply Ticked Changes", QDialogButtonBox.AcceptRole)
        self.apply_button.setProperty("accent", True)
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.filter_combo.currentIndexChanged.connect(self.fill)
        self.table.itemChanged.connect(self.on_ticked)
        self.fill()

    def shown(self):
        kind = self.filter_combo.currentData()
        return [change for change in self.changes if kind is None or change.kind == kind]

    def fill(self):
        self.table.blockSignals(True)
        shown = self.shown()
        self.table.setRowCount(len(shown))
        for row, change in enumerate(shown):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            tick.setCheckState(Qt.Checked if change.chosen else Qt.Unchecked)
            tick.setData(Qt.UserRole, change)
            self.table.setItem(row, 0, tick)
            color = {"add": "success", "update": "warning", "remove": "error"}[change.action]
            self.table.setItem(row, 1, _item(f"{KIND_LABELS[change.kind]} {change.key}"))
            self.table.setItem(row, 2, _item(ACTION_LABELS[change.action], color))
            self.table.setItem(row, 3, _item(change.describe()))
            self.table.setItem(row, 4, _item(change.note, "muted"))
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(3, min(self.table.columnWidth(3), 520))
        self.update_state()

    def on_ticked(self, item):
        if item.column() == 0:
            item.data(Qt.UserRole).chosen = item.checkState() == Qt.Checked
            self.update_state()

    def tick_shown(self, state):
        for change in self.shown():
            change.chosen = state
        self.fill()

    def update_state(self):
        ticked = sum(change.chosen for change in self.changes)
        self.apply_button.setEnabled(bool(ticked))
        if not self.changes:
            set_hint(self.status_label, "No differences: IPAM matches the workbook page.", "success")
        else:
            set_hint(self.status_label, f"{len(self.changes)} differences; {ticked} ticked to make.", "info")

    def apply(self):
        made, failed = apply_changes(self.store, self.network.id, self.changes)
        self.made = made
        log.info("Compare with workbook: made %d changes to %s, %d couldn't be made", made, self.network.name,
                 len(failed))
        if failed:
            listing = "; ".join(f"{change.key}: {error}" for change, error in failed[:5])
            set_hint(self.status_label, f"Made {made} changes; {len(failed)} couldn't be made ({listing}"
                                        f"{' ...' if len(failed) > 5 else ''}).", "error")
            for change, _ in failed:
                change.note = "Couldn't be made (see below)."
            done = {id(change) for change in self.changes if change.chosen} - {id(change) for change, _ in failed}
            self.changes = [change for change in self.changes if id(change) not in done]
            self.fill()
            return
        self.accept()


# --------------------------------------------------------------------- Find Free Blocks

class FreeBlocksDialog(QDialog):
    """Free space in a subnet (no other subnet, recorded address or gateway in it), to add new subnets in."""

    def __init__(self, parent, store, subnet, add_subnet):
        super().__init__(parent)
        self.store, self.subnet, self.add_subnet = store, subnet, add_subnet
        block = subnet.network
        self.setWindowTitle(f"Free Blocks in {subnet.cidr}")
        self.resize(560, 520)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItem("Any (largest first)", None)
        for prefix in range(block.prefixlen + 1, min(block.first.max_prefixlen, block.prefixlen + 17) + 1):
            count = 1 << (block.first.max_prefixlen - prefix)
            self.size_combo.addItem(f"/{prefix}  ({count:,} addresses)", prefix)
        row.addWidget(self.size_combo, 1)
        layout.addLayout(row)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.table = _table(["Free block", "Addresses", "Range"])
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.add_button = buttons.addButton("Add as Subnet...", QDialogButtonBox.ActionRole)
        self.add_button.setProperty("accent", True)
        self.add_button.clicked.connect(self.add)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.size_combo.currentIndexChanged.connect(self.fill)
        self.table.itemSelectionChanged.connect(lambda: self.add_button.setEnabled(bool(self.selected())))
        self.table.cellDoubleClicked.connect(lambda *_: self.add())
        self.fill()

    def fill(self):
        prefix = self.size_combo.currentData()
        blocks, free = self.store.free_blocks(self.subnet, prefix)
        total = self.subnet.network.num_addresses
        what = f"/{prefix} blocks" if prefix else "blocks"
        self.summary_label.setText(f"{free:,} of {total:,} addresses are free. "
                                   f"{len(blocks):,}{'+' if len(blocks) >= 500 else ''} free {what}.")
        self.table.setRowCount(len(blocks))
        for row, block in enumerate(blocks):
            self.table.setItem(row, 0, _item(str(block), data=str(block)))
            self.table.setItem(row, 1, _item(f"{block.num_addresses:,}"))
            self.table.setItem(row, 2, _item(f"{block.first} - {block.last}"))
        self.table.resizeColumnsToContents()
        self.add_button.setEnabled(False)

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def add(self):
        cidr = self.selected()
        if cidr and self.add_subnet(cidr):
            self.fill()


# --------------------------------------------------------------------- Changing many at once

LEAVE = "leave"


class BulkAddressDialog(QDialog):
    """Set the status, description or a detail of several addresses at once (free ones are recorded)."""

    def __init__(self, parent, count):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {count} Addresses")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        note = QLabel(f"Change the {count} selected addresses. Only what you change here is changed; names and MAC "
                      "addresses stay as they are. Free addresses among them are recorded.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.status_combo = QComboBox()
        self.status_combo.addItem("Leave as it is", LEAVE)
        for status, label in STATUSES.items():
            self.status_combo.addItem(label, status)
        form.addRow("Status:", self.status_combo)
        self.description_check = QCheckBox("Set to:")
        self.description_input = QLineEdit()
        self.description_input.setEnabled(False)
        self.description_check.toggled.connect(self.description_input.setEnabled)
        description_row = QHBoxLayout()
        description_row.addWidget(self.description_check)
        description_row.addWidget(self.description_input, 1)
        form.addRow("Description:", description_row)
        self.detail_name = QLineEdit()
        self.detail_name.setPlaceholderText("Detail, such as Rack")
        self.detail_value = QLineEdit()
        self.detail_value.setPlaceholderText("Value (empty removes the detail)")
        detail_row = QHBoxLayout()
        detail_row.addWidget(self.detail_name)
        detail_row.addWidget(self.detail_value)
        form.addRow("Detail:", detail_row)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        """{"status": ..., "description": ..., "detail": (name, value)}: only what's to change."""
        values = {}
        if self.status_combo.currentData() != LEAVE:
            values["status"] = self.status_combo.currentData()
        if self.description_check.isChecked():
            values["description"] = self.description_input.text().strip()
        if self.detail_name.text().strip():
            values["detail"] = (self.detail_name.text().strip(), self.detail_value.text().strip())
        return values


def apply_to_addresses(store, network_id, addresses, values):
    """Apply BulkAddressDialog's values to [address]; free ones are recorded (as used, unless a status is given).
    Returns (changed, [(address, error)])."""
    changed, failed = 0, []
    for address in addresses:
        record = store.address(network_id, str(address))
        status = values.get("status") or (record.status if record else USED)
        fields = dict(record.fields) if record else {}
        if "detail" in values:
            name, value = values["detail"]
            if value:
                fields[name] = value
            else:
                fields.pop(name, None)
        description = values.get("description", record.description if record else "")
        try:
            store.set_address(network_id, str(address), status, record.name if record else "",
                              record.mac if record else "", description, fields)
            changed += 1
        except IpamError as error:
            failed.append((address, str(error)))
    return changed, failed


class BulkSubnetDialog(QDialog):
    """Set a detail (such as Telephony Rng), the description or Loopbacks on several subnets at once."""

    def __init__(self, parent, count, detail_names=()):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {count} Subnets")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        note = QLabel(f"Change the {count} selected subnets. Only what you change here is changed.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.detail_name = QComboBox()
        self.detail_name.setEditable(True)
        self.detail_name.addItem("")
        for name in detail_names:
            self.detail_name.addItem(name)
        self.detail_name.lineEdit().setPlaceholderText("Detail, such as Telephony Rng")
        self.detail_value = QLineEdit()
        self.detail_value.setPlaceholderText("Value (empty removes the detail)")
        detail_row = QHBoxLayout()
        detail_row.addWidget(self.detail_name, 1)
        detail_row.addWidget(self.detail_value, 1)
        form.addRow("Detail:", detail_row)
        self.description_check = QCheckBox("Set to:")
        self.description_input = QLineEdit()
        self.description_input.setEnabled(False)
        self.description_check.toggled.connect(self.description_input.setEnabled)
        description_row = QHBoxLayout()
        description_row.addWidget(self.description_check)
        description_row.addWidget(self.description_input, 1)
        form.addRow("Description:", description_row)
        self.loopbacks_combo = QComboBox()
        self.loopbacks_combo.addItem("Leave as it is", LEAVE)
        self.loopbacks_combo.addItem("Loopbacks (every address a /32; removes the gateway)", True)
        self.loopbacks_combo.addItem("Not loopbacks", False)
        form.addRow("Loopbacks:", self.loopbacks_combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        values = {}
        name = self.detail_name.currentText().strip()
        if name:
            values["detail"] = (name, self.detail_value.text().strip())
        if self.description_check.isChecked():
            values["description"] = self.description_input.text().strip()
        if self.loopbacks_combo.currentData() != LEAVE:
            values["loopbacks"] = self.loopbacks_combo.currentData()
        return values


def apply_to_subnets(store, subnets, values):
    """Apply BulkSubnetDialog's values. Returns (changed, [(subnet, error)])."""
    changed, failed = 0, []
    for subnet in subnets:
        changes = {}
        if "detail" in values:
            name, value = values["detail"]
            fields = dict(subnet.fields)
            if value:
                fields[name] = value
            else:
                fields.pop(name, None)
            changes["fields"] = fields
        if "description" in values:
            changes["description"] = values["description"]
        if "loopbacks" in values:
            changes["loopbacks"] = values["loopbacks"]
        try:
            store.update_subnet(subnet.id, **changes)
            changed += 1
        except IpamError as error:
            failed.append((subnet, str(error)))
    return changed, failed


# --------------------------------------------------------------------- A free address for an adapter

class AddressPickerDialog(QDialog):
    """Pick a free address from IPAM for an adapter: a network and subnet (starting with the one the adapter is in),
    and the address (the next free one, or another typed in). Records it in IPAM once the new settings are kept."""

    def __init__(self, parent, stores, adapter=None):
        super().__init__(parent)
        self.stores = stores  # [(source label, store)]
        self.setWindowTitle("Free Address from IPAM")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.network_combo = QComboBox()
        self.subnet_combo = QComboBox()
        self.subnet_filter = QLineEdit()
        self.subnet_filter.setPlaceholderText("Filter subnets by address or name")
        self.subnet_filter.setClearButtonEnabled(True)
        self.address_input = QLineEdit()
        self.gateway_label = QLabel()
        self.name_input = QLineEdit(socket.gethostname())
        self.record_check = QCheckBox("Record it in IPAM as used once the new settings are kept")
        self.record_check.setChecked(True)
        form.addRow("Network:", self.network_combo)
        form.addRow("", self.subnet_filter)
        form.addRow("Subnet:", self.subnet_combo)
        form.addRow("Address:", self.address_input)
        form.addRow("Gateway:", self.gateway_label)
        form.addRow("Record as:", self.name_input)
        form.addRow("", self.record_check)
        layout.addLayout(form)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.use_button = buttons.addButton("Fill In the Form", QDialogButtonBox.AcceptRole)
        self.use_button.setProperty("accent", True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for label, store in stores:
            for network in store.networks():
                self.network_combo.addItem(f"{network.name}   ({label})" if len(stores) > 1 else network.name,
                                           (store, network))
        self.network_combo.currentIndexChanged.connect(self.fill_subnets)
        self.subnet_filter.textChanged.connect(self.fill_subnets)
        self.subnet_combo.currentIndexChanged.connect(self.show_subnet)
        self.address_input.textChanged.connect(self.check_address)
        self.record_check.toggled.connect(self.name_input.setEnabled)
        self.choose_for(adapter)
        self.fill_subnets()

    def choose_for(self, adapter):
        """Start with the network and subnet holding the adapter's address or gateway, if IPAM has one."""
        wanted = []
        if adapter is not None:
            wanted = [interface.ip for interface in adapter.ipv4] + \
                [ipaddress.ip_address(gateway) for gateway in adapter.gateways4]
        self.preferred_subnet = None
        for index in range(self.network_combo.count()):
            store, network = self.network_combo.itemData(index)
            for address in wanted:
                subnet = store.subnet_for(network.id, address)
                if subnet is not None:
                    self.network_combo.setCurrentIndex(index)
                    self.preferred_subnet = subnet.id
                    return

    def fill_subnets(self):
        data = self.network_combo.currentData()
        text = self.subnet_filter.text().strip().casefold()
        previous = self.subnet_combo.currentData()
        self.subnet_combo.blockSignals(True)
        self.subnet_combo.clear()
        if data is not None:
            store, network = data
            for subnet in store.subnets(network.id):
                if subnet.network.version != 4 or subnet.loopbacks:
                    continue  # An adapter's address: IPv4, and not a loopback
                label = f"{subnet.cidr}   {subnet.name}".strip()
                if text and text not in label.casefold():
                    continue
                self.subnet_combo.addItem(label, subnet)
        wanted = previous.id if previous is not None else self.preferred_subnet
        for index in range(self.subnet_combo.count()):
            if self.subnet_combo.itemData(index).id == wanted:
                self.subnet_combo.setCurrentIndex(index)
        self.subnet_combo.blockSignals(False)
        self.show_subnet()

    def show_subnet(self):
        subnet = self.subnet_combo.currentData()
        data = self.network_combo.currentData()
        if subnet is None or data is None:
            self.address_input.clear()
            self.gateway_label.setText("—")
            self.check_address()
            return
        free = data[0].next_free(subnet)
        self.address_input.setText(str(free) if free else "")
        self.gateway_label.setText(subnet.gateway or "(none recorded)")
        self.check_address()

    def choice(self):
        """(store, network, subnet, address) or None."""
        data, subnet = self.network_combo.currentData(), self.subnet_combo.currentData()
        try:
            address = ipaddress.ip_address(self.address_input.text().strip())
        except ValueError:
            return None
        if data is None or subnet is None or address not in subnet.network:
            return None
        return data[0], data[1], subnet, address

    def check_address(self):
        subnet = self.subnet_combo.currentData()
        choice = self.choice()
        if subnet is None:
            set_hint(self.status_label, "Pick a network and subnet (IPv4, not loopbacks).", "info")
        elif not self.address_input.text().strip():
            set_hint(self.status_label, f"{subnet.cidr} has no free addresses left.", "error")
        elif choice is None:
            set_hint(self.status_label, f"That isn't an address in {subnet.cidr}.", "error")
        else:
            store, network, _, address = choice
            record = store.address(network.id, str(address))
            special = subnet.special_addresses().get(address)
            if special:
                set_hint(self.status_label, f"{address} is the subnet's {special.lower()} address.", "error")
                choice = None
            elif record is not None:
                set_hint(self.status_label, f"{address} is recorded as {STATUSES.get(record.status, '').lower()}"
                                            f"{' for ' + record.name if record.name else ''}. Pick a free one.",
                         "error" if record.status != RESERVED else "warning")
                choice = None
            else:
                set_hint(self.status_label, f"{address}/{subnet.network.prefixlen} is free. The form gets the "
                                            "address, mask and gateway; nothing changes until you apply it.",
                         "success")
        self.use_button.setEnabled(choice is not None)
