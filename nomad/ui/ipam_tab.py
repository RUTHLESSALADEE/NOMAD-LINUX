"""IPAM page: each network's subnets and addresses, what's used, reserved and free, with import from the tribe's
addressing spreadsheets.

Networks come from two places: this computer's own database (Local), and the tribe's, shared by the NOMAD IPAM server
(Tribe). A laptop connects to the server with the tribe key file; it keeps a copy of the tribe's data, synced at start,
every few minutes and on Sync Now, so it can look everything up offline, and sends its changes straight to the
server while it can reach it. On the server itself, NOMAD running as administrator manages the server's data
directly (Server admin): only there can spreadsheets be imported and tribe networks added or deleted.
"""
import csv
import ipaddress
import logging
import os
import threading
import time

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, \
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTableView, \
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..ipam.client import OldServerError, ServerUnreachable, TeamKeyError, TeamStore, admin_key, forget_key, \
    load_saved_key, read_key_file, save_key
from ..ipam.server import ConflictError, server_dir
from ..ipam.spreadsheet import SpreadsheetError, parse_page, read_pages
from ..ipam.store import RESERVED, STATUSES, IpamError, IpamStore
from ..system import log_dir
from .common import SortableTableItem, run_in_background, set_hint
from .ipam_dialogs import AddressDialog, ImportDialog, NetworkDialog, SubnetDialog
from .theme import COLORS, accent_button

log = logging.getLogger(__name__)

ADDRESS_COLUMNS = ["Address", "Status", "Name", "MAC Address", "Description", "Last Changed"]
RESULT_COLUMNS = ["Network", "Subnet", "Subnet Name", "Address", "Status", "Name", "Description"]
LIST_EVERY_ADDRESS_UP_TO = 65536  # Larger subnets list only the addresses in use (a /16 is still listed in full)
UNSUBNETTED = "unsubnetted"
FREE = "Free"
LOCAL, TEAM = "local", "team"
SYNC_EVERY_MS = 5 * 60 * 1000  # A fallback: the watcher normally syncs the moment anything changes
WAIT_SECONDS = 25  # How long each wait at the server lasts before it's renewed
RETRY_SECONDS = 15  # After losing the server, how soon to try again
POLL_SECONDS = 30  # How often to check an older server (without instant sync) for changes
STATUS_REFRESH_MS = 30 * 1000
ADMIN_COPY_FILE = "ipam-server-admin.db"
OFFLINE_NOTE = "The IPAM server can't be reached, so tribe networks are read-only for now (changing them offline " \
               "comes in a later version)."


class ServerWatcher(QObject):
    """Keeps a request waiting at the IPAM server on a background thread, so this laptop hears about every change
    the moment it's made (anyone's), and notices quickly when the server goes away or comes back."""
    changed = pyqtSignal(int)  # The server's new revision
    reachability = pyqtSignal(bool, str)  # (reachable, why not)
    rejected = pyqtSignal(str)  # The server refused the tribe key
    outdated = pyqtSignal()  # The server is an older NOMAD without instant sync: it's checked every 30 s instead

    def __init__(self, client, revision, parent=None):
        super().__init__(parent)
        self.client, self.revision = client, revision
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="IPAM server watcher", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()  # The thread ends when its current wait returns (it's a daemon, so it never delays exit)

    def run(self):
        polling = False
        confirmed = False  # Whether the server has answered since starting (or since it was last unreachable)
        while not self.stopping.is_set():
            try:
                if not confirmed:
                    # A quick check first, so being back online shows at once rather than after a whole wait
                    revision = self.client.status()["revision"]
                    confirmed = True
                elif polling:
                    self.stopping.wait(POLL_SECONDS)
                    revision = self.client.status()["revision"]
                else:
                    revision = self.client.wait(self.revision, WAIT_SECONDS)
            except OldServerError:
                polling = True
                self.outdated.emit()
                continue
            except TeamKeyError as error:
                if not self.stopping.is_set():
                    self.rejected.emit(str(error))
                self.stopping.wait(60)
                continue
            except ServerUnreachable as error:  # Only this means the server can't be reached
                confirmed = False
                self.emit_reachability(False, str(error))
                self.stopping.wait(RETRY_SECONDS)
                continue
            except IpamError as error:  # The server answered, but with a problem: it's still there
                log.info("IPAM server watcher: %s", error)
                self.stopping.wait(RETRY_SECONDS)
                continue
            self.emit_reachability(True, "")
            if revision > self.revision and not self.stopping.is_set():
                self.revision = revision
                self.changed.emit(revision)

    def emit_reachability(self, reachable, reason):
        if not self.stopping.is_set():
            self.reachability.emit(reachable, reason)


def ago(seconds):
    """"just now", "4 min ago", "3 h ago", "2 days ago"."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


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
        self.local_store = None
        self.team = None  # TeamStore, when connected to the IPAM server
        self.source = LOCAL  # Where the selected network comes from
        self.subnets = []
        self.network_id = None
        self.current = None  # The selected Subnet, UNSUBNETTED, or None
        self.syncing = False
        self.sync_again = False  # A change arrived during a sync: sync once more when it ends
        self.watcher = None
        self.server_outdated = False  # The server lacks instant sync (an older NOMAD)
        self.init_ui()
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_now)
        self.sync_timer.start(SYNC_EVERY_MS)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.show_team_status)
        self.status_timer.start(STATUS_REFRESH_MS)
        QTimer.singleShot(1500, self.open_store)  # Sync at start, even before the page is opened

    @property
    def store(self):
        """The store the selected network is in."""
        return self.team if self.source == TEAM and self.team is not None else self.local_store

    def can_edit(self):
        """Whether the selected network can be changed now (tribe networks need the server)."""
        return self.source == LOCAL or (self.team is not None and self.team.online)

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
        self.team_button = QPushButton("Tribe")
        team_menu = QMenu(self.team_button)
        self.connect_action = team_menu.addAction("Connect with Tribe Key File...", self.connect_with_key_file)
        self.sync_action = team_menu.addAction("Sync Now", lambda: self.sync_now(announce=True))
        self.disconnect_action = team_menu.addAction("Disconnect from the Tribe...", self.disconnect_team)
        self.team_button.setMenu(team_menu)
        top.addWidget(self.team_button)
        self.server_label = QLabel()
        self.server_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.server_label)
        top.addStretch()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Find an address, name or subnet in every network")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(240)
        top.addWidget(self.search_input)
        layout.addLayout(top)
        self.team_label = QLabel()
        self.team_label.setWordWrap(True)
        layout.addWidget(self.team_label)
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
        self.next_free_button.setProperty("normal_tip", "Record the lowest free address in this subnet (not the "
                                                        "network, broadcast or gateway address).")
        self.next_free_button.setToolTip(self.next_free_button.property("normal_tip"))
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
        """Open the databases the first time they're needed, so starting NOMAD doesn't wait on them."""
        if self.local_store is None:
            try:
                self.local_store = IpamStore()
            except Exception as error:  # A damaged or locked database file
                log.exception("Couldn't open the IPAM database")
                set_hint(self.status_label, f"Couldn't open the IPAM database: {error}", "error")
                return False
            self.connect_team()
            self.fill_networks()
        return True

    def showEvent(self, event):
        super().showEvent(event)
        self.open_store()

    def save_settings(self, settings):
        if self.network_id:
            settings.setValue("ipam/network", f"{self.source}:{self.network_id}")
        settings.setValue("ipam/hide_free", self.hide_free_check.isChecked())

    def restore_settings(self, settings):
        self.saved_network_id = settings.value("ipam/network", "", str)
        self.hide_free_check.setChecked(settings.value("ipam/hide_free", False, bool))

    def shutdown(self):
        self.sync_timer.stop()
        self.stop_watching()
        for store in (self.local_store, self.team):
            if store is not None:
                store.close()
        self.local_store = self.team = None

    # ----------------------------------------------------------------- The tribe's server

    def connect_team(self):
        """Use the server's data: as its admin on the server itself (NOMAD running as administrator), or through
        the saved tribe key on a laptop."""
        self.stop_watching()
        if self.team is not None:
            self.team.close()
            self.team = None
        key, path = None, None
        if getattr(self.window, "admin", False):
            key = admin_key()
            path = log_dir() / ADMIN_COPY_FILE
        if key is None:
            key = load_saved_key()
            path = None
        if key is not None:
            try:
                self.team = TeamStore(key, path)
            except Exception as error:  # A damaged copy: say so rather than failing the page
                log.exception("Couldn't open the copy of the tribe's IPAM data")
                set_hint(self.status_label, f"Couldn't open the copy of the tribe's data: {error}", "error")
        self.show_team_status()
        self.sync_now()
        self.server_outdated = False
        if self.team is not None:
            self.watcher = ServerWatcher(self.team.client, self.team.revision, self)
            self.watcher.changed.connect(self.on_server_changed)
            self.watcher.reachability.connect(self.on_reachability)
            self.watcher.outdated.connect(self.on_server_outdated)
            self.watcher.rejected.connect(self.on_key_rejected)
            self.watcher.start()

    def stop_watching(self):
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher.changed.disconnect()
            self.watcher.reachability.disconnect()
            self.watcher.outdated.disconnect()
            self.watcher.rejected.disconnect()
            self.watcher = None

    def on_key_rejected(self, reason):
        if self.team is not None:
            self.team.online, self.team.last_error, self.team.key_rejected = False, reason, True
            self.show_team_status()
            self.update_permissions()

    def on_server_outdated(self):
        log.info("The IPAM server is an older version without instant sync; checking it every %d s", POLL_SECONDS)
        self.server_outdated = True
        self.show_team_status()

    def on_server_changed(self, _revision):
        """Someone changed the tribe's data: sync now (or right after the sync that's running)."""
        if self.syncing:
            self.sync_again = True
        else:
            self.sync_now()

    def on_reachability(self, reachable, reason):
        if self.team is None:
            return
        if reachable:
            if not self.team.online:
                self.sync_now()  # Back online: catch up (the sync marks it online)
            return
        if self.team.online or self.team.last_error != reason:
            self.team.online, self.team.last_error = False, reason
            self.show_team_status()
            self.update_permissions()

    def connect_with_key_file(self):
        if not self.open_store():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Connect to the Tribe's IPAM Server", "",
                                              "NOMAD tribe key (*.nomadkey);;All files (*)")
        if not path:
            return
        try:
            key = read_key_file(path)
            save_key(key)
        except (IpamError, OSError) as error:
            set_hint(self.status_label, str(error), "error")
            return
        self.connect_team()
        set_hint(self.status_label, "Tribe key saved (encrypted for your Windows account). You can delete the key "
                                    "file now, or keep it somewhere safe: anyone with it can change the tribe's "
                                    "IPAM.", "success")

    def disconnect_team(self):
        if QMessageBox.question(self, "Disconnect from the Tribe",
                                "Stop using the tribe's IPAM server on this computer? The saved tribe key and the "
                                "copy of the tribe's networks are removed; your local networks are kept.") != \
                QMessageBox.Yes:
            return
        forget_key()
        if self.team is not None:
            self.team.reset_copy()
            self.team.close()
            self.team = None
        self.connect_team()
        self.fill_networks()

    def sync_now(self, announce=False):
        """Fetch the server's changes on a worker thread, then apply them here."""
        if self.team is None or self.syncing:
            return
        self.syncing = True
        client, revision = self.team.client, self.team.revision
        if announce:
            set_hint(self.team_label, "Tribe: syncing...", "info")

        def fetch():
            try:
                return client.fetch_all_changes(revision)
            except IpamError as error:  # Offline or refused: expected, so not logged as a failure
                return error

        run_in_background(fetch, lambda result: self.sync_done(result, announce), self.sync_failed)

    def sync_done(self, result, announce):
        self.syncing = False
        if self.team is None:
            return
        if self.sync_again:
            self.sync_again = False
            QTimer.singleShot(0, self.sync_now)
        if isinstance(result, Exception):
            self.sync_failed(result)
            if announce:
                set_hint(self.status_label, f"Couldn't sync: {result}", "warning")
            return
        status, items, revision = result
        self.team.apply_sync(items, revision, status)
        self.update_buttons()  # Back online: tribe networks can be changed again
        if items:
            log.info("Synced %d changes from the IPAM server", len(items))
            self.refresh_everything()
        self.show_team_status()
        if announce:
            set_hint(self.status_label, f"Synced: {len(items)} change{'' if len(items) == 1 else 's'} from the "
                                        "server.", "success")

    def sync_failed(self, error):
        self.syncing = False
        self.sync_again = False
        if self.team is None:
            return
        self.team.online, self.team.last_error = False, str(error)
        self.team.key_rejected = isinstance(error, TeamKeyError)
        log.info("IPAM sync failed: %s", error)
        self.show_team_status()
        self.update_permissions()

    def refresh_everything(self):
        """Show new data from a sync, keeping the selected network, subnet and address where they still exist."""
        selected = self.selected_addresses()
        scroll = self.table.verticalScrollBar().value()
        self.fill_networks()
        self.table.verticalScrollBar().setValue(scroll)
        if len(selected) == 1:
            self.select_address(selected[0])

    def show_team_status(self):
        team = self.team
        self.connect_action.setEnabled(team is None or not team.admin)
        self.sync_action.setEnabled(team is not None)
        self.disconnect_action.setEnabled(team is not None and not team.admin)
        admin = team is not None and team.admin
        self.import_button.setText("Import to Tribe Server..." if admin else "Import Locally (Not Shared)...")
        if team is None:
            hint = ""
            if server_dir().exists():  # Its contents are hidden from NOMAD without administrator rights
                hint = " This computer is the IPAM server: restart NOMAD as administrator (File menu) to manage it."
            set_hint(self.team_label, "Tribe: not connected. To share networks with the tribe, use Tribe > Connect "
                                      "with Tribe Key File." + hint, "info")
            set_hint(self.server_label, "No IPAM server", "info")
            self.server_label.setToolTip("Not connected to the tribe's IPAM server.")
            return
        synced = f"synced {ago(time.time() - team.last_sync)}" if team.last_sync else "not synced yet"
        who = "Server admin: this is the server's IPAM; imports and new networks go to it" if admin else "Tribe"
        server = "This computer (IPAM server)" if admin else team.server_name
        if team.key_rejected:
            set_hint(self.team_label, f"{who}: {team.last_error}", "error")
            set_hint(self.server_label, f"● {server} · tribe key not accepted", "error")
        elif team.online and self.server_outdated:
            update = "use Tools > IPAM Server > Update Service here" if admin else "ask for it to be updated"
            set_hint(self.team_label, f"{who} · {synced}. The IPAM server is running an older version of NOMAD, so "
                                      f"changes arrive within {POLL_SECONDS} seconds instead of at once ({update}).",
                     "warning")
            set_hint(self.server_label, f"● {server} · connected (needs updating)", "warning")
        elif team.online:
            set_hint(self.team_label, f"{who} · {synced}; changes arrive as they're made", "success")
            set_hint(self.server_label, f"● {server} · connected", "success")
        elif team.last_error:
            set_hint(self.team_label, f"{who} · offline, {synced}. {OFFLINE_NOTE}", "warning")
            set_hint(self.server_label, f"● {server} · offline", "error")
        else:
            set_hint(self.team_label, f"{who} · connecting... ({synced})", "info")
            set_hint(self.server_label, f"● {server} · connecting...", "warning")
        details = [f"IPAM server: {team.server_name}, last reached at {team.server_address}",
                   f"Addresses tried: {', '.join(team.key.hosts)} (port {team.key.port})",
                   f"Last synced: {time.strftime('%Y-%m-%d %H:%M', time.localtime(team.last_sync))}"
                   if team.last_sync else "Not synced yet"]
        if team.last_error:
            details.append(f"Last problem: {team.last_error}")
        self.server_label.setToolTip("\n".join(details))

    def report(self, error, action="That change"):
        """Explain a change the server refused, and sync so the page shows the latest."""
        if isinstance(error, ServerUnreachable):
            self.team.online = False
            self.show_team_status()
            self.update_permissions()
        QMessageBox.warning(self, "Not Changed", f"{action} wasn't made. {error}")
        if isinstance(error, (ConflictError, TeamKeyError)):
            self.sync_now()

    def after_dialog(self):
        """A dialog may have hit a conflict or lost the server: bring the tribe's copy and status up to date."""
        if self.source == TEAM and self.team is not None:
            self.show_team_status()
            self.update_permissions()
            self.sync_now()

    # ----------------------------------------------------------------- Networks

    def fill_networks(self, select_id=None):
        """Every network: the tribe's first (marked Tribe), then this computer's (marked Local)."""
        if self.local_store is None:
            return
        select_id = select_id or (f"{self.source}:{self.network_id}" if self.network_id else "") or \
            getattr(self, "saved_network_id", "")
        self.network_combo.blockSignals(True)
        self.network_combo.clear()
        sources = [(TEAM, self.team), (LOCAL, self.local_store)] if self.team is not None else \
            [(LOCAL, self.local_store)]
        for source, store in sources:
            for network in store.networks():
                label = f"{network.name}   ({'Tribe' if source == TEAM else 'Local'})" if self.team else network.name
                self.network_combo.addItem(label, f"{source}:{network.id}")
        index = self.network_combo.findData(select_id)
        self.network_combo.setCurrentIndex(index if index >= 0 else 0)
        self.network_combo.blockSignals(False)
        self.on_network_chosen()

    def on_network_chosen(self):
        data = self.network_combo.currentData()
        if data:
            self.source, self.network_id = data.split(":", 1)
        else:
            self.source, self.network_id = LOCAL, None
        has_network = self.network_id is not None
        for widget in (self.tree, self.subnet_filter):
            widget.setEnabled(has_network)
        self.export_action.setEnabled(has_network)
        if not has_network:
            self.details_label.setText("No networks yet. Import your addressing spreadsheet (Import Spreadsheet...), "
                                       "add one with Network > New Network, or connect to the tribe's IPAM server "
                                       "with Tribe > Connect with Tribe Key File.")
        self.fill_tree()

    def update_permissions(self):
        """Enable what can be changed: tribe networks need the server, and only its admin adds or deletes them."""
        has_network = self.network_id is not None
        editable = has_network and self.can_edit()
        admin = self.team is not None and self.team.admin
        self.add_subnet_button.setEnabled(editable)
        self.edit_network_action.setEnabled(editable)
        self.delete_network_action.setEnabled(editable and (self.source == LOCAL or admin))
        self.import_button.setToolTip("Import networks from an addressing spreadsheet into the server's IPAM." if admin
                                      else "Import networks from an addressing spreadsheet (.xlsx, or .csv for one "
                                           "page) into this computer's IPAM (Local). The tribe's networks are "
                                           "imported on the server.")
        tip = "" if editable or not has_network else OFFLINE_NOTE
        for widget in (self.add_subnet_button, self.edit_subnet_button, self.delete_subnet_button,
                       self.next_free_button, self.edit_address_button, self.free_button):
            widget.setToolTip(tip or widget.property("normal_tip") or "")

    def network(self):
        return self.store.network(self.network_id) if self.network_id else None

    def new_network(self):
        if not self.open_store():
            return
        admin = self.team is not None and self.team.admin
        target = self.team if admin else self.local_store
        dialog = NetworkDialog(self, target)
        accepted = dialog.exec_()
        self.after_dialog()
        if accepted:
            self.fill_networks(f"{TEAM if admin else LOCAL}:{dialog.result_item.id}")

    def edit_network(self):
        dialog = NetworkDialog(self, self.store, self.network())
        accepted = dialog.exec_()
        self.after_dialog()
        if accepted:
            self.fill_networks()

    def delete_network(self):
        network = self.network()
        count = len(self.store.addresses(network.id))
        where = " from the server, for the whole tribe" if self.source == TEAM else ""
        if QMessageBox.question(self, "Delete Network",
                                f"Delete {network.name}{where}, with its {len(self.subnets)} subnets and {count} "
                                "recorded addresses?") != QMessageBox.Yes:
            return
        try:
            self.store.delete_network(network.id)
        except IpamError as error:
            self.report(error, "Deleting the network")
            return
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
        if not self.can_edit():
            return
        dialog = SubnetDialog(self, self.store, self.network_id, cidr=cidr)
        accepted = dialog.exec_()
        self.after_dialog()
        if accepted:
            self.fill_tree(select=dialog.result_item)

    def edit_subnet(self):
        subnet = self.selected_subnet()
        if subnet is None or not self.can_edit():
            return
        dialog = SubnetDialog(self, self.store, self.network_id, self.store.subnet(subnet.id))
        accepted = dialog.exec_()
        self.after_dialog()
        if accepted:
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
        try:
            self.store.delete_subnet(subnet.id, with_addresses=box.clickedButton() is with_addresses)
        except IpamError as error:
            self.report(error, "Deleting the subnet")
            return
        self.current = None
        self.fill_tree()

    def subnet_menu(self, position):
        if self.network_id is None:
            return
        menu = QMenu(self)
        subnet = self.selected_subnet()
        editable = self.can_edit()
        menu.addAction("Add Subnet...", self.add_subnet).setEnabled(editable)
        if subnet is not None:
            menu.addAction("Edit...", self.edit_subnet).setEnabled(editable)
            menu.addAction("Delete...", self.delete_subnet).setEnabled(editable)
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
        editable = self.can_edit()
        has_subnet = self.selected_subnet() is not None
        self.edit_subnet_button.setEnabled(has_subnet and editable)
        self.delete_subnet_button.setEnabled(has_subnet and editable)
        self.next_free_button.setEnabled(has_subnet and editable)
        selected = self.selected_addresses()
        self.edit_address_button.setEnabled(len(selected) == 1 and editable)
        self.free_button.setEnabled(editable and any(address in self.model.recorded for address in selected))
        self.update_permissions()

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
        if not self.can_edit():
            return
        record = self.store.address(self.network_id, address)
        dialog = AddressDialog(self, self.store, self.network_id, str(address), record, note)
        accepted = dialog.exec_()
        self.after_dialog()
        if accepted:
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
        accepted = dialog.exec_()
        self.after_dialog()
        if accepted:
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
        try:
            with self.store.transaction():
                for address in recorded:
                    self.store.free_address(self.network_id, address)
        except IpamError as error:
            self.report(error, "Marking them free")
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
            menu.addAction("Edit...", self.edit_address).setEnabled(self.can_edit())
        if any(address in self.model.recorded for address in selected):
            menu.addAction("Mark Free", self.free_addresses).setEnabled(self.can_edit())
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
        results = []
        for source, store in ((TEAM, self.team), (LOCAL, self.local_store)):
            if store is not None:
                results += [(source, network, subnet, address) for network, subnet, address in store.search(text)]
        self.results_table.setSortingEnabled(False)
        self.results_table.setRowCount(len(results))
        for row, (source, network, subnet, address) in enumerate(results):
            if self.team is not None:
                network = type(network)(**{**network.__dict__, "name": f"{network.name} "
                                                                        f"({'Tribe' if source == TEAM else 'Local'})"})
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
                item = SortableTableItem(value, sort_key, (source, network, subnet, address))
                self.results_table.setItem(row, column, item)
        self.results_table.setSortingEnabled(True)
        self.results_table.resizeColumnsToContents()
        count = len(results)
        self.results_label.setText(f"{count} result{'' if count == 1 else 's'} for {text!r}" if count else
                                   f"Nothing matches {text!r} in any network.")
        self.right_stack.setCurrentIndex(1)

    def go_to_result(self, row, _column):
        source, network, subnet, address = self.results_table.item(row, 0).data_object
        index = self.network_combo.findData(f"{source}:{network.id}")
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
        admin = self.team is not None and self.team.admin
        target = self.team if admin else self.local_store
        dialog = ImportDialog(self, target, os.path.basename(path), sheets, skipped, to_server=admin,
                              team_connected=self.team is not None)
        if dialog.exec_() and dialog.imported:
            names = ", ".join(network.name for network in dialog.imported)
            self.network_id = None
            self.fill_networks(f"{TEAM if admin else LOCAL}:{dialog.imported[0].id}")
            if admin:
                set_hint(self.status_label, f"Imported {names} to the IPAM server: every connected laptop has them "
                                            "now.", "success")
            else:
                set_hint(self.status_label, f"Imported {names} to this computer only. They are NOT shared with the "
                                            "tribe (only the IPAM server can import tribe networks).", "warning")

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
