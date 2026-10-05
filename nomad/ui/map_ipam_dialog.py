"""Recording what a network map found in IPAM: its devices' and hosts' addresses that IPAM doesn't have (or has with
another MAC address), reviewed first. Nothing is written until Record Ticked is pressed, and only what's ticked."""
import logging

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, \
    QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..ipam.map_compare import HOST
from ..ipam.reconcile import MAC_DIFFERS, NOT_RECORDED
from ..ipam.store import USED, IpamError
from .common import set_hint
from .theme import COLORS

log = logging.getLogger(__name__)

COLUMNS = ["", "Address", "What", "Where", "Name in IPAM", "MAC Address", "IPAM Now"]
COL_TICK, COL_NAME = 0, 4
SHOW = [("Not in IPAM, or with another MAC", "changes"), ("Not in IPAM", NOT_RECORDED),
        ("With another MAC in IPAM", MAC_DIFFERS), ("Everything on the map in this network", "all")]


class RecordDialog(QDialog):
    """addresses: [map_compare.MapAddress] judged against the network (store, network_id)."""

    def __init__(self, parent, store, network_id, network_name, addresses, title="Record in IPAM", offline=False):
        super().__init__(parent)
        self.store, self.network_id = store, network_id
        self.addresses = [item for item in addresses if item.finding is not None]
        self.recorded = self.updated = 0
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.resize(1000, 560)
        layout = QVBoxLayout(self)
        outside = len(addresses) - len(self.addresses)
        intro = QLabel(f"Addresses the map found in <b>{network_name}</b>'s subnets. Tick the ones to record (a "
                       "name can be changed first: double-click it), then Record Ticked. Nothing is written to IPAM "
                       "until then." + (f" {outside} address{'es' if outside != 1 else ''} outside the network's "
                                        "subnets aren't listed." if outside else "")
                       + (" The IPAM server can't be reached: they're sent when it's back." if offline else ""))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        self.show_combo = QComboBox()
        for label, key in SHOW:
            self.show_combo.addItem(label, key)
        row.addWidget(self.show_combo)
        row.addStretch()
        layout.addLayout(row)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.record_button = buttons.addButton("Record Ticked", QDialogButtonBox.AcceptRole)
        self.record_button.setProperty("accent", True)
        buttons.accepted.connect(self.record)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.ticks = {}  # Address -> ticked, kept while the list is filtered
        self.names = {}  # Address -> name typed
        self.show_combo.currentIndexChanged.connect(self.fill)
        self.table.itemChanged.connect(self.on_item_changed)
        self.fill()

    def shown(self):
        mode = self.show_combo.currentData()
        if mode == "all":
            return self.addresses
        states = (NOT_RECORDED, MAC_DIFFERS) if mode == "changes" else (mode,)
        return [item for item in self.addresses if item.finding.state in states]

    def fill(self):
        self.table.blockSignals(True)
        items = self.shown()
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            state = item.finding.state
            changeable = state in (NOT_RECORDED, MAC_DIFFERS)
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if changeable else Qt.NoItemFlags)
            tick.setCheckState(Qt.Checked if self.ticks.get(item.ip, state == NOT_RECORDED) and changeable
                               else Qt.Unchecked)
            tick.setData(Qt.UserRole, item.ip)
            self.table.setItem(row, COL_TICK, tick)
            record = item.finding.record
            name = self.names.get(item.ip, item.name if record is None else record.name or item.name)
            values = [item.ip, "Host" if item.kind == HOST else "Device", item.where, name, item.mac,
                      item.finding.text]
            for column, text in enumerate(values, start=1):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                editable = column == COL_NAME and state == NOT_RECORDED
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                cell.setFlags(flags | Qt.ItemIsEditable if editable else flags)
                if column == len(values) and state in (NOT_RECORDED, MAC_DIFFERS):
                    cell.setForeground(QColor(COLORS["warning"]))
                self.table.setItem(row, column, cell)
        self.table.resizeColumnsToContents()
        self.table.blockSignals(False)
        self.show_count()

    def on_item_changed(self, cell):
        ip = self.table.item(cell.row(), COL_TICK).data(Qt.UserRole)
        if cell.column() == COL_TICK:
            self.ticks[ip] = cell.checkState() == Qt.Checked
        elif cell.column() == COL_NAME:
            self.names[ip] = cell.text().strip()
        self.show_count()

    def chosen(self):
        """The MapAddresses ticked (whether shown now or not)."""
        return [item for item in self.addresses if item.finding.state in (NOT_RECORDED, MAC_DIFFERS)
                and self.ticks.get(item.ip, item.finding.state == NOT_RECORDED)]

    def show_count(self):
        chosen = self.chosen()
        new = sum(1 for item in chosen if item.finding.state == NOT_RECORDED)
        self.record_button.setEnabled(bool(chosen))
        if not self.addresses:
            set_hint(self.status_label, "None of the map's addresses are in this network's subnets.", "info")
        else:
            set_hint(self.status_label, f"{new} to record, {len(chosen) - new} MAC address"
                     f"{'es' if len(chosen) - new != 1 else ''} to update.", "info")

    def record(self):
        chosen = self.chosen()
        try:
            for item in chosen:
                record = item.finding.record
                if item.finding.state == NOT_RECORDED:
                    self.store.set_address(self.network_id, item.ip, USED, self.names.get(item.ip, item.name),
                                           item.mac)
                    self.recorded += 1
                else:
                    self.store.set_address(self.network_id, item.ip, record.status, record.name, item.mac,
                                           record.description, record.fields)
                    self.updated += 1
        except IpamError as error:
            QMessageBox.warning(self, "Not All Recorded", f"Recorded {self.recorded} and updated {self.updated} of "
                                f"{len(chosen)}. {error}")
            self.reject()
            return
        self.accept()
