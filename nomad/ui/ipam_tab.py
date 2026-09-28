"""IPAM page: each network's subnets and addresses, what's used, reserved and free, with import from the team's
addressing spreadsheets."""
import csv
import ipaddress
import logging
import os

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, \
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTableView, \
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..ipam.spreadsheet import SpreadsheetError, parse_page, read_pages
from ..ipam.store import RESERVED, STATUSES, IpamError, IpamStore
from .common import SortableTableItem, run_in_background, set_hint
from .ipam_dialogs import AddressDialog, ImportDialog, NetworkDialog, SubnetDialog
from .theme import COLORS, accent_button

log = logging.getLogger(__name__)

ADDRESS_COLUMNS = ["Address", "Status", "Name", "MAC Address", "Description", "Last Changed"]
RESULT_COLUMNS = ["Network", "Subnet", "Subnet Name", "Address", "Status", "Name", "Description"]
LIST_EVERY_ADDRESS_UP_TO = 65536  # Larger subnets list only the addresses in use (a /16 is still listed in full)
UNSUBNETTED = "unsubnetted"
FREE = "Free"


def usable_count(network):
    """Addresses that can be handed out: all but the network and broadcast (first and last) addresses."""
    if network.version == 4 and network.num_addresses > 2:
        return network.num_addresses - 2
    return network.num_addresses


class AddressModel(QAbstractTableModel):
    """The addresses of one subnet: every address (free ones too) or just the ones in use, without building a row
    object per address, so even a /16 lists instantly."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.network = None
        self.recorded = {}  # {address: Address}
        self.special = {}  # {address: "Network" / "Broadcast" / "Gateway"}
        self.nested = []  # [(Block, name)] subnets inside this one
        self.rows = None  # [address] when not listing every address
        self.first = 0

    def load(self, network, recorded, special, nested, every_address):
        self.beginResetModel()
        self.network, self.recorded, self.special, self.nested = network, recorded, special, nested
        self.first = int(network.network_address) if network is not None else 0
        if network is not None and every_address:
            self.rows = None
        else:
            self.rows = sorted(set(recorded) | set(special))
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() or (self.network is None and self.rows is None):
            return 0
        return len(self.rows) if self.rows is not None else self.network.num_addresses

    def columnCount(self, parent=QModelIndex()):
        return len(ADDRESS_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return ADDRESS_COLUMNS[section]
        return None

    def address_at(self, row):
        if self.rows is not None:
            return self.rows[row]
        return ipaddress.ip_address(self.first + row) if self.network.version == 4 else \
            ipaddress.IPv6Address(self.first + row)

    def nested_name(self, address):
        for network, name in self.nested:
            if address in network:
                return network, name
        return None

    def status_text(self, address):
        record = self.recorded.get(address)
        special = self.special.get(address)
        if record is not None:
            status = STATUSES.get(record.status, record.status)
            return f"{special} · {status}" if special else status
        if special:
            return special
        nested = self.nested_name(address)
        if nested:
            return f"In {nested[0]}"
        return FREE

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        address = self.address_at(index.row())
        record = self.recorded.get(address)
        column = index.column()
        if role == Qt.DisplayRole:
            if column == 0:
                return str(address)
            if column == 1:
                return self.status_text(address)
            if record is None:
                if column == 2 and address not in self.special:
                    nested = self.nested_name(address)
                    return nested[1] if nested else ""
                return ""
            if column == 5:
                return f"{record.modified[:10]} {record.modified_by}" if record.modified else ""
            return (record.name, record.mac, record.description)[column - 2]
        if role == Qt.ForegroundRole:
            if record is None:
                return QColor(COLORS["muted"])
            if record.status == RESERVED and column == 1:
                return QColor(COLORS["warning"])
        if role == Qt.FontRole and record is None and address in self.special:
            font = QFont()
            font.setItalic(True)
            return font
        if role == Qt.UserRole:
            return address
        return None


class IpamTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = None
        self.subnets = []
        self.network_id = None
        self.current = None  # The selected Subnet, UNSUBNETTED, or None
        self.init_ui()

    # ----------------------------------------------------------------- Layout

    def init_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Network:"))
        self.network_combo = QComboBox()
        self.network_combo.setMinimumWidth(220)
        self.network_combo.setToolTip("Each network is separate (such as an air-gapped network), so the same "
                                      "addresses can be used in more than one.")
        top.addWidget(self.network_combo)
        self.network_button = QPushButton("Network")
        network_menu = QMenu(self.network_button)
        network_menu.addAction("New Network...", self.new_network)
        self.edit_network_action = network_menu.addAction("Edit Network...", self.edit_network)
        self.delete_network_action = network_menu.addAction("Delete Network...", self.delete_network)
        network_menu.addSeparator()
        self.export_action = network_menu.addAction("Export to CSV...", self.export_csv)
        self.network_button.setMenu(network_menu)
        top.addWidget(self.network_button)
        self.import_button = QPushButton("Import Spreadsheet...")
        self.import_button.setToolTip("Import networks from an addressing spreadsheet (.xlsx, or .csv for one page).")
        top.addWidget(self.import_button)
        top.addStretch()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find an address, name or subnet in every network")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(300)
        top.addWidget(self.search_input)
        layout.addLayout(top)
        self.details_label = QLabel()
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details_label)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.subnet_filter = QLineEdit()
        self.subnet_filter.setPlaceholderText("Filter subnets")
        self.subnet_filter.setClearButtonEnabled(True)
        left_layout.addWidget(self.subnet_filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Subnet", "Name", "Used"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        left_layout.addWidget(self.tree, 1)
        subnet_buttons = QHBoxLayout()
        self.add_subnet_button = QPushButton("Add Subnet...")
        self.edit_subnet_button = QPushButton("Edit...")
        self.delete_subnet_button = QPushButton("Delete")
        for button in (self.add_subnet_button, self.edit_subnet_button, self.delete_subnet_button):
            subnet_buttons.addWidget(button)
        subnet_buttons.addStretch()
        left_layout.addLayout(subnet_buttons)
        splitter.addWidget(left)

        self.right_stack = QStackedWidget()
        addresses_page = QWidget()
        right_layout = QVBoxLayout(addresses_page)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.subnet_label = QLabel()
        self.subnet_label.setWordWrap(True)
        self.subnet_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right_layout.addWidget(self.subnet_label)
        address_buttons = QHBoxLayout()
        self.next_free_button = accent_button("Use Next Free...")
        self.next_free_button.setToolTip("Record the lowest free address in this subnet (not the network, broadcast "
                                         "or gateway address).")
        self.edit_address_button = QPushButton("Edit...")
        self.free_button = QPushButton("Mark Free")
        self.hide_free_check = QCheckBox("Hide free addresses")
        for widget in (self.next_free_button, self.edit_address_button, self.free_button):
            address_buttons.addWidget(widget)
        address_buttons.addStretch()
        address_buttons.addWidget(self.hide_free_check)
        right_layout.addLayout(address_buttons)
        self.model = AddressModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 8)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        right_layout.addWidget(self.table, 1)
        self.right_stack.addWidget(addresses_page)

        results_page = QWidget()
        results_layout = QVBoxLayout(results_page)
        results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_label = QLabel()
        results_layout.addWidget(self.results_label)
        self.results_table = QTableWidget(0, len(RESULT_COLUMNS))
        self.results_table.setHorizontalHeaderLabels(RESULT_COLUMNS)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setSortingEnabled(True)
        results_layout.addWidget(self.results_table, 1)
        results_layout.addWidget(QLabel("Double-click a result to go to it. Clear the search to go back."))
        self.right_stack.addWidget(results_page)
        splitter.addWidget(self.right_stack)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 640])
        layout.addWidget(splitter, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.network_combo.currentIndexChanged.connect(self.on_network_chosen)
        self.import_button.clicked.connect(self.import_spreadsheet)
        self.search_input.returnPressed.connect(self.search)
        self.search_input.textChanged.connect(lambda text: None if text else self.right_stack.setCurrentIndex(0))
        self.subnet_filter.textChanged.connect(self.filter_tree)
        self.tree.currentItemChanged.connect(lambda *_: self.show_subnet())
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_subnet())
        self.tree.customContextMenuRequested.connect(self.subnet_menu)
        self.add_subnet_button.clicked.connect(lambda: self.add_subnet())
        self.edit_subnet_button.clicked.connect(self.edit_subnet)
        self.delete_subnet_button.clicked.connect(self.delete_subnet)
        self.next_free_button.clicked.connect(self.use_next_free)
        self.edit_address_button.clicked.connect(self.edit_address)
        self.free_button.clicked.connect(self.free_addresses)
        self.hide_free_check.toggled.connect(lambda: self.show_subnet())
        self.table.doubleClicked.connect(lambda _: self.edit_address())
        self.table.customContextMenuRequested.connect(self.address_menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.update_buttons())
        self.results_table.cellDoubleClicked.connect(self.go_to_result)

    # ----------------------------------------------------------------- Page interface

    def open_store(self):
        """Open the database the first time the page is used, so starting NOMAD doesn't wait on it."""
        if self.store is None:
            try:
                self.store = IpamStore()
            except Exception as error:  # A damaged or locked database file
                log.exception("Couldn't open the IPAM database")
                set_hint(self.status_label, f"Couldn't open the IPAM database: {error}", "error")
                return False
            self.fill_networks()
        return True

    def showEvent(self, event):
        super().showEvent(event)
        self.open_store()

    def save_settings(self, settings):
        if self.network_id:
            settings.setValue("ipam/network", self.network_id)
        settings.setValue("ipam/hide_free", self.hide_free_check.isChecked())

    def restore_settings(self, settings):
        self.saved_network_id = settings.value("ipam/network", "", str)
        self.hide_free_check.setChecked(settings.value("ipam/hide_free", False, bool))

    def shutdown(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    # ----------------------------------------------------------------- Networks

    def fill_networks(self, select_id=None):
        select_id = select_id or self.network_id or getattr(self, "saved_network_id", "")
        self.network_combo.blockSignals(True)
        self.network_combo.clear()
        for network in self.store.networks():
            self.network_combo.addItem(network.name, network.id)
        index = self.network_combo.findData(select_id)
        self.network_combo.setCurrentIndex(index if index >= 0 else 0)
        self.network_combo.blockSignals(False)
        self.on_network_chosen()

    def on_network_chosen(self):
        self.network_id = self.network_combo.currentData()
        has_network = self.network_id is not None
        for widget in (self.add_subnet_button, self.tree, self.subnet_filter):
            widget.setEnabled(has_network)
        self.edit_network_action.setEnabled(has_network)
        self.delete_network_action.setEnabled(has_network)
        self.export_action.setEnabled(has_network)
        if not has_network:
            self.details_label.setText("No networks yet. Import your addressing spreadsheet (Import Spreadsheet...) "
                                       "or add one with Network > New Network.")
        self.fill_tree()

    def network(self):
        return self.store.network(self.network_id) if self.network_id else None

    def new_network(self):
        if not self.open_store():
            return
        dialog = NetworkDialog(self, self.store)
        if dialog.exec_():
            self.fill_networks(dialog.result_item.id)

    def edit_network(self):
        dialog = NetworkDialog(self, self.store, self.network())
        if dialog.exec_():
            self.fill_networks(self.network_id)

    def delete_network(self):
        network = self.network()
        count = len(self.store.addresses(network.id))
        if QMessageBox.question(self, "Delete Network",
                                f"Delete {network.name} with its {len(self.subnets)} subnets and {count} recorded "
                                "addresses?") != QMessageBox.Yes:
            return
        self.store.delete_network(network.id)
        self.network_id = None
        self.fill_networks()

    def show_network_details(self):
        network = self.network()
        if network is None:
            return
        parts = [network.description] if network.description else []
        parts += [f"{name}: {value}" for name, value in network.fields.items()]
        self.details_label.setText("   ·   ".join(parts))

    # ----------------------------------------------------------------- Subnet tree

    def fill_tree(self, select=None):
        """Subnets nested under the blocks that hold them, with how much of each is used."""
        previous = select if select is not None else self.current
        self.tree.clear()
        self.subnets = self.store.subnets(self.network_id) if self.network_id else []
        if self.network_id is None:
            self.show_subnet()
            return
        self.show_network_details()
        stack = []  # [(network, item)] of the blocks holding the current subnet
        select_item = None
        for subnet in self.subnets:
            network = subnet.network
            while stack and not (stack[-1][0].version == network.version and network.subnet_of(stack[-1][0])):
                stack.pop()
            used = self.store.count_addresses(self.network_id, network)
            item = QTreeWidgetItem([subnet.cidr, subnet.name, f"{used} / {usable_count(network):,}"])
            item.setData(0, Qt.UserRole, subnet)
            item.setToolTip(1, subnet.name)
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            stack.append((network, item))
            if previous is not None and previous != UNSUBNETTED and getattr(previous, "id", None) == subnet.id:
                select_item = item
        outside = self.unsubnetted_addresses()
        if outside:
            item = QTreeWidgetItem(["(Not in any subnet)", "", str(len(outside))])
            item.setData(0, Qt.UserRole, UNSUBNETTED)
            item.setForeground(0, QColor(COLORS["warning"]))
            self.tree.addTopLevelItem(item)
            if previous == UNSUBNETTED:
                select_item = item
        self.tree.expandAll()
        for column in range(3):
            self.tree.resizeColumnToContents(column)
        self.tree.setColumnWidth(1, min(self.tree.columnWidth(1), 260))
        self.filter_tree()
        if select_item is None and self.tree.topLevelItemCount():
            select_item = self.tree.topLevelItem(0)
        self.tree.setCurrentItem(select_item)
        self.show_subnet()

    def unsubnetted_addresses(self):
        networks = [subnet.network for subnet in self.subnets]
        return [address for address in self.store.addresses(self.network_id)
                if not any(address.address.version == network.version and address.address in network
                           for network in networks)]

    def filter_tree(self):
        text = self.subnet_filter.text().strip().lower()

        def visit(item):
            children = [visit(item.child(index)) for index in range(item.childCount())]
            matches = not text or any(text in item.text(column).lower() for column in range(2)) or any(children)
            item.setHidden(not matches)
            return matches

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    def selected_subnet(self):
        item = self.tree.currentItem()
        value = item.data(0, Qt.UserRole) if item is not None else None
        return value if value != UNSUBNETTED else None

    def add_subnet(self, cidr=""):
        dialog = SubnetDialog(self, self.store, self.network_id, cidr=cidr)
        if dialog.exec_():
            self.fill_tree(select=dialog.result_item)

    def edit_subnet(self):
        subnet = self.selected_subnet()
        if subnet is None:
            return
        dialog = SubnetDialog(self, self.store, self.network_id, self.store.subnet(subnet.id))
        if dialog.exec_():
            self.fill_tree(select=dialog.result_item)

    def delete_subnet(self):
        subnet = self.selected_subnet()
        if subnet is None:
            return
        count = self.store.count_addresses(self.network_id, subnet.network)
        box = QMessageBox(QMessageBox.Question, "Delete Subnet", f"Delete {subnet.cidr} ({subnet.name or 'no name'})?",
                          parent=self)
        if count:
            box.setInformativeText(f"It has {count} recorded address{'' if count == 1 else 'es'}.")
            with_addresses = box.addButton("Delete Subnet and Addresses", QMessageBox.DestructiveRole)
            box.addButton("Delete Just the Subnet", QMessageBox.AcceptRole)
        else:
            with_addresses = None
            box.addButton("Delete", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec_()
        if box.buttonRole(box.clickedButton()) == QMessageBox.RejectRole:
            return
        self.store.delete_subnet(subnet.id, with_addresses=box.clickedButton() is with_addresses)
        self.current = None
        self.fill_tree()

    def subnet_menu(self, position):
        if self.network_id is None:
            return
        menu = QMenu(self)
        subnet = self.selected_subnet()
        menu.addAction("Add Subnet...", self.add_subnet)
        if subnet is not None:
            menu.addAction("Edit...", self.edit_subnet)
            menu.addAction("Delete...", self.delete_subnet)
            menu.addSeparator()
            menu.addAction("Copy Subnet", lambda: QApplication.clipboard().setText(subnet.cidr))
            menu.addAction("Open in Subnet Calculator", lambda: self.open_calculator(subnet.cidr))
        menu.exec_(self.tree.viewport().mapToGlobal(position))

    def open_calculator(self, cidr):
        self.window.navigator.setCurrentWidget(self.window.utilities_tab)
        self.window.utilities_tab.calculate_subnet(cidr)

    # ----------------------------------------------------------------- Addresses

    def show_subnet(self):
        item = self.tree.currentItem()
        self.current = item.data(0, Qt.UserRole) if item is not None else None
        if self.current is None:
            self.model.load(None, {}, {}, [], False)
            self.subnet_label.setText("")
        elif self.current == UNSUBNETTED:
            recorded = {address.address: address for address in self.unsubnetted_addresses()}
            self.model.load(None, recorded, {}, [], False)
            self.subnet_label.setText("Recorded addresses outside every subnet in this network. Add a subnet for "
                                      "them, or mark them free.")
        else:
            subnet = self.current
            network = subnet.network
            recorded = {address.address: address for address in self.store.addresses(self.network_id, network)}
            nested = [(other.network, other.name) for other in self.subnets if other.id != subnet.id and
                      other.network.version == network.version and other.network.subnet_of(network)]
            every = not self.hide_free_check.isChecked() and network.num_addresses <= LIST_EVERY_ADDRESS_UP_TO
            self.model.load(network, recorded, subnet.special_addresses(), nested, every)
            parts = [f"<b>{subnet.cidr}</b>"]
            if subnet.name:
                parts.append(subnet.name)
            if subnet.gateway:
                parts.append(f"gateway {subnet.gateway}")
            parts.append(f"netmask {network.netmask}" if network.version == 4 else f"{network.num_addresses:,} "
                                                                                      "addresses")
            parts.append(f"{len(recorded)} of {usable_count(network):,} recorded")
            parts += [f"{name}: {value}" for name, value in subnet.fields.items()]
            if subnet.description:
                parts.append(subnet.description)
            if network.num_addresses > LIST_EVERY_ADDRESS_UP_TO and not self.hide_free_check.isChecked():
                parts.append("<i>(too large to list every address, so only those recorded are shown)</i>")
            self.subnet_label.setText("   ·   ".join(parts))
        self.table.resizeColumnToContents(0)
        metrics = self.table.fontMetrics()
        for column, sample in ((1, "Gateway · Reserved"), (2, "M" * 16), (3, "00-00-00-00-00-00")):
            self.table.setColumnWidth(column, metrics.horizontalAdvance(sample) + 24)
        self.update_buttons()

    def selected_addresses(self):
        return [self.model.address_at(index.row()) for index in self.table.selectionModel().selectedRows()]

    def update_buttons(self):
        has_subnet = self.selected_subnet() is not None
        self.edit_subnet_button.setEnabled(has_subnet)
        self.delete_subnet_button.setEnabled(has_subnet)
        self.next_free_button.setEnabled(has_subnet)
        selected = self.selected_addresses()
        self.edit_address_button.setEnabled(len(selected) == 1)
        self.free_button.setEnabled(any(address in self.model.recorded for address in selected))

    def edit_address(self, address=None):
        if address is None:
            selected = self.selected_addresses()
            if len(selected) != 1:
                return
            address = selected[0]
        note = ""
        special = self.model.special.get(address)
        if special and address not in self.model.recorded:
            note = f"This is the subnet's {special.lower()} address."
        record = self.store.address(self.network_id, address)
        dialog = AddressDialog(self, self.store, self.network_id, str(address), record, note)
        if dialog.exec_():
            self.refresh_current(address)

    def use_next_free(self):
        subnet = self.selected_subnet()
        if subnet is None:
            return
        address = self.store.next_free(subnet)
        if address is None:
            set_hint(self.status_label, f"{subnet.cidr} has no free addresses left.", "warning")
            return
        dialog = AddressDialog(self, self.store, self.network_id, str(address))
        if dialog.exec_():
            self.refresh_current(address)
            set_hint(self.status_label, f"Recorded {address} in {subnet.cidr}.", "success")

    def free_addresses(self):
        recorded = [address for address in self.selected_addresses() if address in self.model.recorded]
        if not recorded:
            return
        names = ", ".join(str(address) for address in recorded[:5]) + (" ..." if len(recorded) > 5 else "")
        if QMessageBox.question(self, "Mark Free", f"Mark {names} free, forgetting what's recorded?") != \
                QMessageBox.Yes:
            return
        with self.store.transaction():
            for address in recorded:
                self.store.free_address(self.network_id, address)
        self.refresh_current()

    def refresh_current(self, address=None):
        """Show changes to the current subnet's addresses, keeping the scroll position and selection."""
        scroll = self.table.verticalScrollBar().value()
        self.fill_tree()
        self.table.verticalScrollBar().setValue(scroll)
        if address is not None:
            self.select_address(address)

    def select_address(self, address):
        if self.model.rows is not None:
            row = self.model.rows.index(address) if address in self.model.rows else -1
        elif address.version == self.model.network.version:
            row = int(address) - self.model.first  # Every address is listed, so the row is its offset
        else:
            row = -1
        if 0 <= row < self.model.rowCount():
            self.table.selectRow(row)
            self.table.scrollTo(self.model.index(row, 0), QAbstractItemView.PositionAtCenter)

    def address_menu(self, position):
        selected = self.selected_addresses()
        if not selected:
            return
        menu = QMenu(self)
        host = str(selected[0])
        if len(selected) == 1:
            menu.addAction("Edit...", self.edit_address)
        if any(address in self.model.recorded for address in selected):
            menu.addAction("Mark Free", self.free_addresses)
        menu.addAction("Copy", lambda: QApplication.clipboard().setText(
            "\n".join(self.model.data(self.model.index(index.row(), 0)) + "\t" +
                      self.model.data(self.model.index(index.row(), 2)) for index in
                      self.table.selectionModel().selectedRows())))
        if len(selected) == 1:
            menu.addSeparator()
            menu.addAction("Ping", lambda: self.go_to(self.window.ping_tab, "ping_host", host))
            menu.addAction("Traceroute", lambda: self.go_to(self.window.traceroute_tab, "trace_host", host))
            menu.addAction("Scan Ports", lambda: self.go_to(self.window.ports_tab, "scan_host", host))
            menu.addAction("SSH", lambda: self.window.terminal_tab.open_address(host))
        menu.exec_(self.table.viewport().mapToGlobal(position))

    def go_to(self, page, method, host):
        self.window.navigator.setCurrentWidget(page)
        getattr(page, method)(host)

    # ----------------------------------------------------------------- Finding

    def search(self):
        if not self.open_store():
            return
        text = self.search_input.text().strip()
        if not text:
            self.right_stack.setCurrentIndex(0)
            return
        results = self.store.search(text)
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(len(results))
        for row, (network, subnet, address) in enumerate(results):
            status = STATUSES.get(address.status, "Free") if address is not None else ""
            values = [network.name, subnet.cidr if subnet else "", subnet.name if subnet else "",
                      address.ip if address else "", status, address.name if address else "",
                      (address.description if address else subnet.description if subnet else "")]
            for column, value in enumerate(values):
                sort_key = None
                if column == 3 and value:
                    sort_key = (address.address.version, int(address.address))
                elif column == 1 and value:
                    sort_key = (subnet.network.version, int(subnet.network.network_address))
                item = SortableTableItem(value, sort_key, (network, subnet, address))
                self.results_table.setItem(row, column, item)
        self.results_table.setSortingEnabled(True)
        self.results_table.resizeColumnsToContents()
        count = len(results)
        self.results_label.setText(f"{count} result{'' if count == 1 else 's'} for {text!r}" if count else
                                   f"Nothing matches {text!r} in any network.")
        self.right_stack.setCurrentIndex(1)

    def go_to_result(self, row, _column):
        network, subnet, address = self.results_table.item(row, 0).data_object
        index = self.network_combo.findData(network.id)
        self.network_combo.setCurrentIndex(index)
        self.subnet_filter.clear()
        self.fill_tree(select=subnet if subnet is not None else UNSUBNETTED)
        self.search_input.clear()
        self.right_stack.setCurrentIndex(0)
        if address is not None:
            self.select_address(address.address)

    # ----------------------------------------------------------------- Import and export

    def import_spreadsheet(self):
        if not self.open_store():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import Addressing Spreadsheet", "",
                                              "Spreadsheets (*.xlsx *.xlsm *.csv);;All files (*)")
        if not path:
            return
        set_hint(self.status_label, f"Reading {os.path.basename(path)}...", "info")
        self.import_button.setEnabled(False)

        def read():
            sheets, skipped = [], []
            for title, rows in read_pages(path):
                try:
                    sheets.append(parse_page(title, rows))
                except SpreadsheetError as error:
                    log.info("Import: skipping page: %s", error)
                    skipped.append(title)
            return sheets, skipped

        run_in_background(read, lambda result: self.review_import(path, *result), self.import_failed)

    def import_failed(self, error):
        self.import_button.setEnabled(True)
        log.warning("Import failed: %s", error)
        set_hint(self.status_label, f"Couldn't import: {error}", "error")

    def review_import(self, path, sheets, skipped):
        self.import_button.setEnabled(True)
        if not sheets:
            message = (f"No addressing pages in {os.path.basename(path)}: no page has a header row with Subnet and "
                       f"Mask columns (pages: {', '.join(skipped)}).")
            log.warning("Import: %s", message)
            set_hint(self.status_label, message, "error")
            return
        set_hint(self.status_label, "", "info")
        dialog = ImportDialog(self, self.store, os.path.basename(path), sheets, skipped)
        if dialog.exec_() and dialog.imported:
            names = ", ".join(network.name for network in dialog.imported)
            self.network_id = None
            self.fill_networks(dialog.imported[0].id)
            set_hint(self.status_label, f"Imported {names}.", "success")

    def export_csv(self):
        network = self.network()
        if network is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Network", f"{network.name}.csv", "CSV files (*.csv)")
        if not path:
            return
        subnets = self.subnets
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["Subnet", "Subnet Name", "Gateway", "Address", "Status", "Name", "MAC Address",
                                 "Description", "Last Changed", "Changed By"])
                for subnet in subnets:
                    writer.writerow([subnet.cidr, subnet.name, subnet.gateway, "", "", "", "", subnet.description,
                                     subnet.modified, subnet.modified_by])
                for address in self.store.addresses(network.id):
                    holding = [subnet for subnet in subnets if address.address in subnet.network]
                    subnet = max(holding, key=lambda subnet: (subnet.network.prefixlen, subnet.network.first),
                                 default=None)
                    writer.writerow([subnet.cidr if subnet else "", subnet.name if subnet else "", "", address.ip,
                                     STATUSES.get(address.status, address.status), address.name, address.mac,
                                     address.description, address.modified, address.modified_by])
        except OSError as error:
            set_hint(self.status_label, f"Couldn't save {path}: {error.strerror or error}", "error")
            return
        set_hint(self.status_label, f"Exported {network.name} to {path}.", "success")
