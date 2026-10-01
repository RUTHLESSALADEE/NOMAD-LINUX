"""Network Map page: crawl switches, routers and firewalls over SNMP from a starting device, and draw what's
connected to what (CDP/LLDP), with the hosts on each switch port (MAC and ARP tables)."""
import datetime
import html
import ipaddress
import json
import logging
import socket
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, \
    QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QShortcut, QSplitter, QTabWidget, QTextBrowser, \
    QToolButton, QVBoxLayout, QWidget

from ..netmap import diff, export, l3, monitor, store
from ..netmap.crawl import CrawlSettings, Crawler
from ..netmap.layout import merge_positions
from ..netmap.model import FIREWALL, KIND_NAMES, NO_SNMP, ROUTER, SNMP, SOURCE_NAMES, SWITCH, UNREACHABLE, \
    port_key, short_port
from ..snmp import V2C
from ..terminal.credentials import CredentialError, protect, unprotect
from .common import SortableTableItem, StoppableThread, read_only_table, set_hint
from .host_menu import HostActions
from .netmap_monitor import NetworkMonitor
from .netmap_progress import CrawlProgress
from .netmap_dialogs import CommunitiesDialog, CompareDialog, HostDialog, ScopeDialog
from .netmap_view import MapView
from .table_filter import TableFilter
from .theme import COLORS, accent_button

log = logging.getLogger(__name__)

MAP_FILTER = f"Network maps (*{store.EXTENSION})"
KIND_WEIGHTS = {FIREWALL: 3, ROUTER: 2, SWITCH: 1}  # Breaks ties when choosing the top device
SAVE_DELAY_MS = 1000
ROUTES_SHOWN = 50
DEFAULTS = {"max_hops": 6, "max_devices": 500, "timeout": 2000}


def ip_sort_key(text):
    try:
        return (0, int(ipaddress.ip_address(text.split(",")[0].strip())))
    except ValueError:
        return (1, text.lower())


def parse_seeds(text):
    return [item for item in text.replace(",", " ").split() if item]


class CrawlThread(StoppableThread):
    progress = pyqtSignal(str)
    event = pyqtSignal(str, object)  # A Crawler event: kind, details
    crawled = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

    def run(self):
        try:
            seeds = []
            for seed in self.settings.seeds:
                try:
                    seeds.append(str(ipaddress.ip_address(seed)))
                except ValueError:
                    try:
                        seeds.append(socket.gethostbyname(seed))
                    except OSError:
                        self.failed.emit(f"Couldn't find '{seed}'. Enter an IP address or a name DNS knows.")
                        return
            self.settings.seeds = seeds
            network_map = Crawler(self.settings, should_stop=lambda: self.stopping, progress=self.progress.emit,
                                  events=lambda kind, *details: self.event.emit(kind, details)).run()
        except Exception as error:  # Shown to the user rather than lost
            log.exception("Network map crawl failed")
            self.failed.emit(f"The crawl failed: {error}")
            return
        self.crawled.emit(network_map)


class NetworkMapTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.worker = None
        self.network_map = None
        self.map_path = None
        self.communities, self.overrides = ["public"], []
        self.version, self.timeout = V2C, DEFAULTS["timeout"]
        self.scope, self.max_hops, self.max_devices = [], DEFAULTS["max_hops"], DEFAULTS["max_devices"]
        self.collect_hosts = True
        self.trace = True
        self.l3_nodes = {}
        self.compare_dialog = None
        self.live_map = None  # The map so far, drawn while a crawl runs
        self.live_positions = {}
        self.crawled_map = False
        self.crawl_progress = CrawlProgress(self)
        self.monitor = NetworkMonitor(self)
        self.history_map = None  # The map whose monitoring history the Monitor log is showing
        self.host_actions = HostActions(window, self)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(SAVE_DELAY_MS)
        self.save_timer.timeout.connect(self.save_positions)
        self.init_ui()
        window.adapter_changed.connect(lambda _: self.update_gateway_button())
        window.snapshot_changed.connect(lambda _: self.update_gateway_button())
        self.update_gateway_button()
        self.update_buttons()

    def init_ui(self):
        layout = QVBoxLayout(self)
        crawl_row = QHBoxLayout()
        self.seeds_input = QLineEdit()
        self.seeds_input.setPlaceholderText("Core switch or gateway to start from (IP addresses, separated by commas)")
        self.gateway_button = QPushButton("Adapter's Gateway")
        self.communities_button = QPushButton("Communities...")
        self.communities_button.setToolTip("The SNMP community strings to try, including ones for particular subnets.")
        self.scope_button = QPushButton("Scope...")
        self.scope_button.setToolTip("Which subnets the crawl may go into, how many hops, and whether to read "
                                     "MAC tables for hosts and traceroute for the logical view.")
        self.start_button = accent_button("Start")
        self.start_button.setToolTip("Read each device's CDP/LLDP neighbors over SNMP, then theirs, and so on.")
        self.stop_button = QPushButton("Stop")
        crawl_row.addWidget(QLabel("Start from:"))
        crawl_row.addWidget(self.seeds_input, 1)
        for widget in (self.gateway_button, self.communities_button, self.scope_button, self.start_button,
                       self.stop_button):
            crawl_row.addWidget(widget)
        layout.addLayout(crawl_row)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addWidget(self.crawl_progress.row)

        tools = QHBoxLayout()
        self.open_button = QPushButton("Open...")
        self.recent_button = QToolButton()
        self.recent_button.setText("Recent")
        self.recent_button.setPopupMode(QToolButton.InstantPopup)
        self.recent_menu = QMenu(self.recent_button)
        self.recent_menu.aboutToShow.connect(self.fill_recent_menu)
        self.recent_button.setMenu(self.recent_menu)
        self.save_button = QPushButton("Save As...")
        self.export_button = QToolButton()
        self.export_button.setText("Export")
        self.export_button.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_button)
        export_menu.addAction("Picture (PNG)...", self.export_png)
        export_menu.addAction("Drawing (SVG)...", self.export_svg)
        export_menu.addAction("draw.io / Visio (.drawio)...", self.export_drawio)
        export_menu.addSeparator()
        export_menu.addAction("Devices (CSV)...", lambda: self.export_csv("devices"))
        export_menu.addAction("Links (CSV)...", lambda: self.export_csv("links"))
        export_menu.addAction("Hosts (CSV)...", lambda: self.export_csv("hosts"))
        self.export_button.setMenu(export_menu)
        self.compare_button = QToolButton()
        self.compare_button.setText("Compare")
        self.compare_button.setToolTip("Compare this map with an earlier one: devices and links that appeared or "
                                       "went away, and hosts that moved port.")
        self.compare_button.setPopupMode(QToolButton.InstantPopup)
        self.compare_menu = QMenu(self.compare_button)
        self.compare_menu.aboutToShow.connect(self.fill_compare_menu)
        self.compare_button.setMenu(self.compare_menu)
        self.find_input = QLineEdit()
        self.find_input.setClearButtonEnabled(True)
        self.fit_button = QPushButton("Fit")
        self.fit_button.setToolTip("Zoom to show the whole map. Scroll to zoom, and drag the background to move "
                                   "around.")
        self.arrange_button = QPushButton("Re-arrange")
        self.arrange_button.setToolTip("Lay the map out again, forgetting where devices were dragged to.")
        for widget in (self.open_button, self.recent_button, self.save_button, self.export_button,
                       self.compare_button):
            tools.addWidget(widget)
        tools.addSpacing(16)
        self.monitor_check = QCheckBox("Monitor")
        self.monitor_check.setToolTip("Ping the devices on the map every so often, show which are up or down, and "
                                      "log when one goes down or comes back (on the Monitor tab). Keeps going on "
                                      "other pages while NOMAD is open.")
        self.interval_combo = QComboBox()
        for seconds in monitor.INTERVALS:
            self.interval_combo.addItem(f"every {monitor.duration_text(seconds)}", seconds)
        self.interval_combo.setCurrentIndex(monitor.INTERVALS.index(monitor.DEFAULT_INTERVAL))
        self.interval_combo.setToolTip("How often to ping each device.")
        self.monitor_label = QLabel()
        tools.addWidget(self.monitor_check)
        tools.addWidget(self.interval_combo)
        tools.addWidget(self.monitor_label)
        tools.addSpacing(16)
        tools.addWidget(self.find_input, 1)
        self.hosts_check = QCheckBox("Show Hosts")
        self.hosts_check.setToolTip("Show every switch's hosts, a box per port with each host's VLAN. Or double-click "
                                    "one switch to show just its hosts.")
        tools.addWidget(self.hosts_check)
        tools.addWidget(self.fit_button)
        tools.addWidget(self.arrange_button)
        layout.addLayout(tools)

        self.tabs = QTabWidget()
        self.view = MapView()
        self.l3_view = MapView()
        self.devices_table = read_only_table(export.DEVICE_COLUMNS)
        self.links_table = read_only_table(export.LINK_COLUMNS)
        self.hosts_table = read_only_table(export.HOST_COLUMNS)
        self.tabs.addTab(self.view, "Physical (L2)")
        self.tabs.addTab(self.l3_view, "Logical (L3)")
        self.tabs.addTab(self.devices_table, "Devices")
        self.tabs.addTab(self.links_table, "Links")
        self.tabs.addTab(self.hosts_table, "Hosts")
        self.tabs.addTab(self.crawl_progress.tab, "Crawl")
        self.tabs.addTab(self.monitor.tab, "Monitor")
        self.table_names = {self.devices_table: "Devices", self.links_table: "Links", self.hosts_table: "Hosts"}
        self.table_filters = {table: TableFilter(table, lambda shown, total, table=table:
                                                 self.show_filtered_count(table, shown, total))
                              for table in self.table_names}
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([900, 260])
        layout.addWidget(self.splitter, 1)
        self.show_details(None)

        self.gateway_button.clicked.connect(self.use_gateway)
        self.communities_button.clicked.connect(self.edit_communities)
        self.scope_button.clicked.connect(self.edit_scope)
        self.start_button.clicked.connect(self.start)
        self.seeds_input.returnPressed.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.open_button.clicked.connect(self.open_map)
        self.save_button.clicked.connect(self.save_map_as)
        self.find_input.returnPressed.connect(self.find)
        self.find_input.textChanged.connect(self.on_find_text)
        self.find_texts = {}  # Sub-tab -> what was in the find box there: each tab has its own
        self.find_owner = self.tabs.currentWidget()
        self.tabs.currentChanged.connect(self.on_subtab_changed)
        self.set_find_placeholder()
        self.fit_button.clicked.connect(lambda: self.current_view().fit())
        self.arrange_button.clicked.connect(self.rearrange)
        self.hosts_check.toggled.connect(self.view.set_all_hosts_shown)
        self.monitor_check.toggled.connect(self.on_monitor_toggled)
        self.interval_combo.currentIndexChanged.connect(
            lambda _: self.monitor.set_interval(self.interval_combo.currentData()))
        self.monitor.statuses_changed.connect(self.show_statuses)
        self.monitor.history_added.connect(self.keep_history)
        for view in (self.view, self.l3_view):
            view.selection_changed.connect(self.show_details)
            view.positions_changed.connect(self.save_timer.start)
            view.context_requested.connect(self.show_device_menu)
        self.devices_table.itemDoubleClicked.connect(lambda item: self.show_on_map("device", item.row()))
        self.links_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for table, handler in ((self.devices_table, self.show_devices_table_menu),
                               (self.links_table, self.show_links_table_menu)):
            table.setContextMenuPolicy(Qt.CustomContextMenu)
            table.customContextMenuRequested.connect(handler)
        self.hosts_table.itemDoubleClicked.connect(lambda item: self.show_on_map("host", item.row()))
        self.hosts_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.hosts_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.hosts_table.customContextMenuRequested.connect(self.show_hosts_table_menu)
        delete = QShortcut(QKeySequence.Delete, self.hosts_table, context=Qt.WidgetShortcut)
        delete.activated.connect(lambda: self.delete_hosts(self.selected_table_hosts()))
        self.view.port_context_requested.connect(self.show_port_menu)

    # ----------------------------------------------------------------- Page interface

    def save_settings(self, settings):
        settings.setValue("netmap/seeds", self.seeds_input.text())
        try:
            settings.setValue("netmap/communities", protect(json.dumps({"communities": self.communities,
                                                                        "overrides": self.overrides})))
        except CredentialError as error:
            log.warning("Couldn't save the map's community strings: %s", error)
        settings.setValue("netmap/version", self.version)
        settings.setValue("netmap/timeout", self.timeout)
        settings.setValue("netmap/scope", "\n".join(self.scope))
        settings.setValue("netmap/max_hops", self.max_hops)
        settings.setValue("netmap/max_devices", self.max_devices)
        settings.setValue("netmap/collect_hosts", self.collect_hosts)
        settings.setValue("netmap/trace", self.trace)
        settings.setValue("netmap/monitor", self.monitor_check.isChecked())
        settings.setValue("netmap/monitor_interval", self.interval_combo.currentData())
        settings.setValue("netmap/last_map", str(self.map_path) if self.map_path else "")
        settings.setValue("netmap/splitter", self.splitter.saveState())

    def restore_settings(self, settings):
        self.seeds_input.setText(settings.value("netmap/seeds", "", str))
        self.communities = [settings.value("snmp/community", "public", str) or "public"]
        stored = settings.value("netmap/communities", "", str)
        if stored:
            try:
                saved = json.loads(unprotect(stored))
                self.communities = saved.get("communities") or self.communities
                self.overrides = [tuple(item) for item in saved.get("overrides", [])]
            except (CredentialError, ValueError, TypeError) as error:
                log.warning("Couldn't read the map's saved community strings: %s", error)
        self.version = settings.value("netmap/version", V2C, int)
        self.timeout = settings.value("netmap/timeout", DEFAULTS["timeout"], int)
        self.scope = [line for line in settings.value("netmap/scope", "", str).splitlines() if line.strip()]
        self.max_hops = settings.value("netmap/max_hops", DEFAULTS["max_hops"], int)
        self.max_devices = settings.value("netmap/max_devices", DEFAULTS["max_devices"], int)
        self.collect_hosts = settings.value("netmap/collect_hosts", True, bool)
        self.trace = settings.value("netmap/trace", True, bool)
        splitter = settings.value("netmap/splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)
        last = settings.value("netmap/last_map", "", str)
        if last and Path(last).is_file():
            try:
                self.show_map(store.load(last), Path(last), fit=True)
            except (OSError, ValueError) as error:
                log.warning("Couldn't reopen the last network map %s: %s", last, error)
        interval = settings.value("netmap/monitor_interval", monitor.DEFAULT_INTERVAL, int)
        if interval in monitor.INTERVALS:
            self.interval_combo.setCurrentIndex(monitor.INTERVALS.index(interval))
        if settings.value("netmap/monitor", False, bool) and self.network_map is not None:
            self.monitor_check.setChecked(True)  # Carry on watching from where it was left

    def shutdown(self):
        self.monitor.shutdown()
        if self.save_timer.isActive():
            self.save_timer.stop()
            self.save_positions()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(self.timeout * 2 + 3000)

    # ----------------------------------------------------------------- Inputs

    def gateway(self):
        adapter = self.window.current_adapter()
        return adapter.gateways4[0] if adapter is not None and adapter.gateways4 else None

    def update_gateway_button(self):
        gateway = self.gateway()
        self.gateway_button.setEnabled(gateway is not None)
        self.gateway_button.setText(f"Adapter's Gateway ({gateway})" if gateway else "Adapter's Gateway")

    def use_gateway(self):
        if self.gateway():
            self.seeds_input.setText(self.gateway())

    def edit_communities(self):
        dialog = CommunitiesDialog(self.communities, self.overrides, self.version, self.timeout, self)
        if dialog.exec_() == QDialog.Accepted:
            self.communities, self.overrides, self.version, self.timeout = dialog.values()

    def edit_scope(self):
        dialog = ScopeDialog(self.scope, self.max_hops, self.max_devices, self.collect_hosts, self.trace, self)
        if dialog.exec_() == QDialog.Accepted:
            self.scope, self.max_hops, self.max_devices, self.collect_hosts, self.trace = dialog.values()

    # ----------------------------------------------------------------- Crawling

    def crawl_from(self, address):
        """Start a crawl from one device (from the map's right-click menu)."""
        self.seeds_input.setText(address)
        self.start()

    def start(self):
        if self.worker is not None:
            return
        seeds = parse_seeds(self.seeds_input.text())
        if not seeds:
            if self.gateway():
                self.use_gateway()
                seeds = [self.gateway()]
            else:
                set_hint(self.status_label, "Enter the address of a switch or router to start from.", "error")
                return
        settings = CrawlSettings(seeds=seeds, communities=list(self.communities), overrides=list(self.overrides),
                                 scope=list(self.scope), max_hops=self.max_hops, max_devices=self.max_devices,
                                 version=self.version, timeout=self.timeout, collect_hosts=self.collect_hosts,
                                 trace=self.trace)
        self.worker = CrawlThread(settings, self)
        self.worker.event.connect(self.on_crawl_event)
        self.worker.crawled.connect(self.on_crawled)
        self.worker.failed.connect(lambda message: set_hint(self.status_label, message, "error"))
        self.worker.finished.connect(self.on_thread_finished)
        set_hint(self.status_label, f"Mapping from {', '.join(seeds)}. The map fills in as devices are read; the "
                                    "Crawl tab shows what each one is doing and a log of what was found.", "info")
        self.live_map = None
        self.live_positions = dict(self.network_map.positions) if self.network_map else {}
        self.crawled_map = False
        if self.tabs.currentWidget() not in (self.view, self.crawl_progress.tab):
            self.tabs.setCurrentWidget(self.view)
        self.crawl_progress.start()
        self.window.set_busy("netmap", "Mapping the network")
        self.worker.start()
        self.update_buttons()

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            set_hint(self.status_label, "Stopping: finishing the devices being read...", "warning")

    def on_thread_finished(self):
        self.worker = None
        self.crawl_progress.finish()
        live, self.live_map = self.live_map, None
        self.view.set_highlights({})
        if live is not None and not self.crawled_map:  # Failed part way: back to the map there was
            if self.network_map is not None:
                self.show_map(self.network_map, self.map_path)
            else:
                self.view.scene().clear()
                self.view.items_by_key = {}
        self.window.clear_busy("netmap")
        self.update_buttons()

    def on_crawl_event(self, kind, details):
        self.crawl_progress.handle(kind, details)
        if kind == "map":
            self.show_live(details[0])
        elif kind in ("started", "finished") and self.live_map is not None:
            self.ring_devices_being_read()

    def show_live(self, snapshot):
        """Draw the map found so far. Devices already drawn stay where they are (or where they were dragged)."""
        if QApplication.mouseButtons() != Qt.NoButton:
            return  # Mid-drag or mid-pan: the next snapshot is only a moment away
        first = self.live_map is None
        if not first:
            self.live_positions.update(self.view.positions())
        nodes = list(snapshot.devices)
        edges = [(link.a, link.b) for link in snapshot.links]
        root = self.network_map.root if self.network_map and self.network_map.root in snapshot.devices else None
        self.live_positions = merge_positions(nodes, edges, self.live_positions, root=root,
                                              weight=lambda key: KIND_WEIGHTS.get(snapshot.devices[key].kind, 0))
        self.live_map = snapshot
        self.view.set_map(snapshot, self.live_positions)
        self.view.set_statuses(self.monitor.status)
        self.ring_devices_being_read()
        if first:
            self.view.request_fit()
        elif self.view.auto_fit:
            self.view.fit()

    def ring_devices_being_read(self):
        if self.live_map is None:
            return
        by_address = {device.mgmt_ip: key for key, device in self.live_map.devices.items() if device.mgmt_ip}
        self.view.set_highlights({by_address[address]: COLORS["accent"]
                                  for address in self.crawl_progress.reading if address in by_address})

    def displayed_map(self):
        """The map being drawn: the one so far while crawling, otherwise the one open."""
        return self.live_map or self.network_map

    def on_crawled(self, network_map):
        self.crawled_map = True
        dropped = []
        if self.live_positions:  # Where devices were drawn (and dragged) while it crawled
            network_map.positions = {key: position for key, position in self.live_positions.items()
                                     if key in network_map.devices}
            if self.live_map is not None:
                network_map.positions.update({key: position for key, position in self.view.positions().items()
                                              if key in network_map.devices})
        if self.network_map is not None:  # Keep where the user put devices that are still there
            if not self.live_positions:
                network_map.positions = {key: position for key, position in self.network_map.positions.items()
                                         if key in network_map.devices}
            network_map.root = self.network_map.root if self.network_map.root in network_map.devices else ""
            dropped = network_map.carry_manual_hosts(self.network_map)
            network_map.status_log = self.network_map.status_log  # The same network's monitoring history
            if self.history_map is self.network_map:
                self.history_map = network_map  # So the Monitor log isn't reloaded
        self.live_map = None
        path = None
        try:
            path = store.save(network_map)
        except OSError as error:
            log.warning("Couldn't save the network map: %s", error)
        self.show_map(network_map, path, fit=not self.live_positions or self.view.auto_fit)
        snmp_count = sum(1 for device in network_map.devices.values() if device.source == SNMP)
        problems = sum(1 for device in network_map.devices.values() if device.source in (NO_SNMP, UNREACHABLE))
        message = (f"{'Stopped' if network_map.stopped else 'Done'}: {len(network_map.devices)} devices "
                   f"({snmp_count} read over SNMP), {len(network_map.links)} links, {len(network_map.hosts)} hosts"
                   + (f", {len(network_map.traces)} traceroutes" if network_map.traces else "") + ".")
        if problems:
            message += f" {problems} didn't answer SNMP (dashed or red; select one to see why)."
        if dropped:
            message += (" 1 host added by hand wasn't kept: its switch isn't on this map." if len(dropped) == 1
                        else f" {len(dropped)} hosts added by hand weren't kept: their switch isn't on this map.")
        if path:
            message += f" Saved as {path.name}."
        set_hint(self.status_label, message, "warning" if network_map.stopped or problems or dropped
                 else "success")

    # ----------------------------------------------------------------- Showing a map

    def show_map(self, network_map, path=None, fit=False):
        self.network_map, self.map_path = network_map, path
        nodes = list(network_map.devices)
        edges = [(link.a, link.b) for link in network_map.links]
        positions = merge_positions(nodes, edges, network_map.positions, root=network_map.root or None,
                                    weight=lambda key: KIND_WEIGHTS.get(network_map.devices[key].kind, 0))
        network_map.positions = positions
        self.view.set_map(network_map, positions)
        self.l3_nodes, l3_links = l3.l3_graph(network_map)
        l3_positions = merge_positions(list(self.l3_nodes), [(link.a, link.b) for link in l3_links],
                                       network_map.l3_positions,
                                       weight=lambda key: 1 if self.l3_nodes[key].kind == l3.DEVICE else 0)
        network_map.l3_positions = l3_positions
        self.l3_view.set_graph(self.l3_nodes, l3_links, l3_positions)
        self.monitor.set_map(network_map)
        if network_map is not self.history_map:
            self.history_map = network_map
            self.monitor.load_history(network_map.status_log)
        self.show_statuses()
        self.fill_tables()
        self.show_details(None)
        if self.hosts_check.isChecked():
            self.view.set_all_hosts_shown(True)
        if fit:
            self.view.request_fit()
            self.l3_view.request_fit()
        self.update_buttons()

    def current_view(self):
        """The drawing showing (or the physical one while a table is)."""
        return self.l3_view if self.tabs.currentWidget() is self.l3_view else self.view

    def rearrange(self):
        """Lay out the view showing again, forgetting where things were dragged to."""
        if self.network_map is None:
            return
        if self.current_view() is self.l3_view:
            self.network_map.l3_positions = {}
        else:
            self.network_map.positions = {}
        self.show_map(self.network_map, self.map_path, fit=True)
        self.save_positions()

    def save_positions(self):
        if self.network_map is None or self.map_path is None:
            return
        self.network_map.positions = self.view.positions()
        self.network_map.l3_positions = self.l3_view.positions()
        try:
            store.save(self.network_map, self.map_path)
        except OSError as error:
            log.warning("Couldn't save the network map's layout: %s", error)

    def fill_tables(self):
        network_map = self.network_map
        self.device_keys = [device.key for device in sorted(network_map.devices.values(),
                                                            key=lambda device: device.label.lower())]
        fill_table(self.devices_table, export.device_rows(network_map, self.monitor.status_text), ip_columns={2},
                   keys=self.device_keys)
        fill_table(self.links_table, export.link_rows(network_map),
                   keys=[(link.a, link.b) for link in export.sorted_links(network_map)])
        fill_table(self.hosts_table, export.host_rows(network_map), ip_columns={1},
                   keys=list(range(len(network_map.hosts))))
        for table_filter in self.table_filters.values():
            table_filter.apply()  # Filters carry over to the new rows

    def show_filtered_count(self, table, shown, total):
        """ "Hosts (12 of 340)" on the tab while its table is filtered."""
        name = self.table_names[table]
        filtered = self.table_filters[table].active if table in getattr(self, "table_filters", {}) else False
        self.tabs.setTabText(self.tabs.indexOf(table), f"{name} ({shown} of {total})" if filtered else name)

    def show_on_map(self, kind, row):
        item = (self.devices_table if kind == "device" else self.hosts_table).item(row, 0)
        if item is None or self.network_map is None:
            return
        self.tabs.setCurrentWidget(self.view)
        if kind == "device":
            self.view.show_device(item.data_object)
        else:
            self.view.show_host(self.network_map.hosts[item.data_object])

    # ----------------------------------------------------------------- Find (Ctrl+F), local to each sub-tab

    def focus_find(self):
        """Ctrl+F: the find box, for whichever sub-tab is showing."""
        self.find_input.setFocus()
        self.find_input.selectAll()

    def on_subtab_changed(self, _index):
        self.find_texts[self.find_owner] = self.find_input.text()
        self.find_owner = self.tabs.currentWidget()
        self.find_input.blockSignals(True)  # The table it now belongs to is already filtered by its own text
        self.find_input.setText(self.find_texts.get(self.find_owner, ""))
        self.find_input.blockSignals(False)
        self.set_find_placeholder()
        self.update_buttons()

    def set_find_placeholder(self):
        here = self.tabs.currentWidget()
        if here in self.table_names:
            text = f"Filter the {self.table_names[here].lower()}: words in any column (Ctrl+F)"
        elif here is self.crawl_progress.tab:
            text = "Find in the crawl log (Enter for the next) (Ctrl+F)"
        elif here is self.l3_view:
            text = "Find a router, subnet or hop: name or address (Ctrl+F)"
        else:
            text = "Find a device or host: name, IP, MAC or vendor (Ctrl+F)"
        self.find_input.setPlaceholderText(text)

    def on_find_text(self, text):
        here = self.tabs.currentWidget()
        if here in self.table_filters:
            self.table_filters[here].set_text(text)  # Tables filter as you type

    def find(self):
        text = self.find_input.text().strip()
        here = self.tabs.currentWidget()
        if not text or here in self.table_filters:
            return
        if here is self.crawl_progress.tab:
            if not self.crawl_progress.find(text):
                set_hint(self.status_label, f"'{text}' isn't in the crawl log.", "warning")
            return
        view = self.current_view()
        if not view.find(text):
            set_hint(self.status_label, f"Nothing on the map matches '{text}'.", "warning")

    def show_details(self, selection):
        network_map = self.displayed_map()
        if network_map is None or selection is None:
            if network_map is None:
                text = ("<p>Start from a core switch or your gateway. Each device's CDP and LLDP neighbors are read "
                        "over SNMP, then theirs, until the whole network (within the scope) is mapped.</p>"
                        "<p>Double-click a switch to show its hosts by port, with each one's VLAN (or tick Show "
                        "Hosts for every switch). Right-click a device for SSH, ping, SNMP and more.</p>"
                        "<p>Drag the background to move around. Hold Shift and drag to draw a box round several "
                        "devices, then drag any of them to move them together.</p>")
            else:
                text = "<p>Select a device to see its details.</p>"
            self.details.setHtml(text)
            return
        if selection[0] == "many":
            self.details.setHtml(f"<p>{selection[1]} selected. Drag any of them to move them all.</p>"
                                 "<p>Shift and drag the background to select a group, Ctrl+click to add or remove "
                                 "one, and Ctrl+A to select everything.</p>")
        elif selection[0] == "device":
            self.details.setHtml(device_html(network_map, selection[1], self.monitor.status(selection[1])))
        elif selection[0] == "node":
            node = self.l3_nodes.get(selection[1])
            if node is not None:
                self.details.setHtml(subnet_html(network_map, node.label) if node.kind == l3.SUBNET
                                     else node_html(network_map, node))
        else:
            self.details.setHtml(port_html(network_map, selection[1], selection[2]))

    def show_device_menu(self, key, position):
        shown = self.displayed_map()
        device = shown.devices.get(key) if shown else None
        if device is None:
            self.show_node_menu(key, position)
            return
        menu = QMenu(self)
        actions = self.host_actions.add_to(menu, device.mgmt_ip) if device.mgmt_ip else {}
        menu.addSeparator()
        self.add_show_in(menu, actions, key)
        menu.addSeparator()
        if device.mgmt_ip:
            actions[menu.addAction("Crawl from Here")] = lambda: self.crawl_from(device.mgmt_ip)
        if self.worker is None:
            actions[menu.addAction("Put at the Top")] = lambda: self.put_at_top(key)
        if self.current_view() is self.view and self.worker is None:
            actions[menu.addAction("Add Host...")] = lambda: self.add_host(key)
        item = self.view.items_by_key.get(key)
        if item is not None and item.host_count and self.tabs.currentWidget() is self.view:
            label = "Hide Hosts" if item.expanded else "Show Hosts"
            actions[menu.addAction(label)] = lambda: self.view.toggle_hosts(item)
        menu.addSeparator()
        actions[menu.addAction("Copy Name")] = lambda: QApplication.clipboard().setText(device.label)
        if device.mgmt_ip:
            actions[menu.addAction("Copy Address")] = lambda: QApplication.clipboard().setText(device.mgmt_ip)
        chosen = menu.exec_(position)
        if chosen in actions:
            actions[chosen]()

    def add_show_in(self, menu, actions, key):
        """Show in Physical (L2) / Logical (L3) / Devices / Links, leaving out the one showing and any the device
        isn't in."""
        here = self.tabs.currentWidget()
        crawling = self.worker is not None  # The tables and logical view are the map from before, until it's done
        choices = [(self.view, "Show in Physical (L2)", key in self.view.items_by_key),
                   (self.l3_view, "Show in Logical (L3)", not crawling and key in self.l3_view.items_by_key),
                   (self.devices_table, "Show in Devices", not crawling and bool(self.table_rows(self.devices_table,
                                                                                               key))),
                   (self.links_table, "Show in Links", not crawling and bool(self.table_rows(self.links_table, key)))]
        for widget, label, possible in choices:
            if widget is not here and possible:
                actions[menu.addAction(label)] = lambda widget=widget: self.show_in(widget, [key])

    def table_rows(self, table, key):
        """Rows of the Devices or Links table for a device (a link's row names both its ends)."""
        rows = []
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            data = item.data_object if item is not None else None
            if data == key or (isinstance(data, tuple) and key in data):
                rows.append(row)
        return rows

    def show_in(self, widget, keys):
        """Go to the tab and select these devices there."""
        self.tabs.setCurrentWidget(widget)
        if widget in (self.view, self.l3_view):
            widget.show_devices(keys)
            return
        rows = sorted({row for key in keys for row in self.table_rows(widget, key)})
        widget.clearSelection()
        if not rows:
            return
        if any(widget.isRowHidden(row) for row in rows):
            self.table_filters[widget].clear()  # A filter was hiding it
        if any(widget.isRowHidden(row) for row in rows):
            self.find_input.setText("")  # Its find box's words, then (the box is this table's now)
        mode = widget.selectionMode()
        widget.setSelectionMode(QAbstractItemView.MultiSelection)  # So selectRow adds rather than replaces
        for row in rows:
            widget.selectRow(row)
        widget.setSelectionMode(mode)
        widget.scrollToItem(widget.item(rows[0], 0))

    def show_devices_table_menu(self, position):
        item = self.devices_table.itemAt(position)
        if item is None:
            return
        self.devices_table.selectRow(item.row())
        key = self.devices_table.item(item.row(), 0).data_object
        self.show_device_menu(key, self.devices_table.viewport().mapToGlobal(position))

    def show_links_table_menu(self, position):
        item = self.links_table.itemAt(position)
        if item is None or self.network_map is None:
            return
        if not self.links_table.item(item.row(), 0).isSelected():
            self.links_table.clearSelection()
            self.links_table.selectRow(item.row())
        rows = sorted({index.row() for index in self.links_table.selectionModel().selectedRows()
                       if not self.links_table.isRowHidden(index.row())})
        keys = list(dict.fromkeys(key for row in rows for key in self.links_table.item(row, 0).data_object))
        menu = QMenu(self)
        actions = {}
        ends = "Both Ends" if len(rows) == 1 else "Their Ends"
        actions[menu.addAction(f"Show {ends} in Physical (L2)")] = lambda: self.show_in(self.view, keys)
        if any(key in self.l3_view.items_by_key for key in keys):
            actions[menu.addAction(f"Show {ends} in Logical (L3)")] = lambda: self.show_in(
                self.l3_view, [key for key in keys if key in self.l3_view.items_by_key])
        actions[menu.addAction(f"Show {ends} in Devices")] = lambda: self.show_in(self.devices_table, keys)
        menu.addSeparator()
        actions[menu.addAction("Copy")] = lambda: QApplication.clipboard().setText("\n".join(
            "\t".join(self.links_table.item(row, column).text() for column in range(self.links_table.columnCount()))
            for row in rows))
        chosen = menu.exec_(self.links_table.viewport().mapToGlobal(position))
        if chosen in actions:
            actions[chosen]()

    def show_node_menu(self, key, position):
        """Right-click on the logical view's subnets and traceroute hops."""
        node = self.l3_nodes.get(key)
        if node is None:
            return
        menu = QMenu(self)
        actions = {}
        if node.kind == l3.SUBNET:
            actions[menu.addAction("Sweep This Subnet")] = lambda: self.sweep_subnet(node.label)
            actions[menu.addAction("Copy Subnet")] = lambda: QApplication.clipboard().setText(node.label)
        elif node.kind == l3.HOP and key != l3.SELF:
            actions = self.host_actions.add_to(menu, node.label)
            menu.addSeparator()
            actions[menu.addAction("Crawl from Here")] = lambda: self.crawl_from(node.label)
            actions[menu.addAction("Copy Address")] = lambda: QApplication.clipboard().setText(node.label)
        if not actions:
            return
        chosen = menu.exec_(position)
        if chosen in actions:
            actions[chosen]()

    # ----------------------------------------------------------------- Adding and removing hosts

    def selected_table_hosts(self):
        if self.network_map is None:
            return []
        rows = sorted({index.row() for index in self.hosts_table.selectionModel().selectedRows()
                       if not self.hosts_table.isRowHidden(index.row())})  # Not rows a filter hides (Ctrl+A)
        return [self.network_map.hosts[self.hosts_table.item(row, 0).data_object] for row in rows]

    def show_hosts_table_menu(self, position):
        if self.network_map is None or self.worker is not None:
            return
        item = self.hosts_table.itemAt(position)
        if item is not None and not self.hosts_table.item(item.row(), 0).isSelected():
            self.hosts_table.selectRow(item.row())
        hosts = self.selected_table_hosts() if item is not None else []
        menu = QMenu(self)
        actions = {}
        if len(hosts) == 1:
            host = hosts[0]
            if host.ip:
                actions = self.host_actions.add_to(menu, host.ip)
                menu.addSeparator()
            actions[menu.addAction("Show on Map")] = lambda: self.show_on_map("host", item.row())
            actions[menu.addAction("Edit Host...")] = lambda: self.edit_host(host)
        actions[menu.addAction("Add Host...")] = lambda: self.add_host(hosts[0].device if hosts else None,
                                                                        hosts[0].port if hosts else "")
        if hosts:
            label = "Delete Host" if len(hosts) == 1 else f"Delete {len(hosts)} Hosts"
            actions[menu.addAction(label)] = lambda: self.delete_hosts(hosts)
        chosen = menu.exec_(self.hosts_table.viewport().mapToGlobal(position))
        if chosen in actions:
            actions[chosen]()

    def show_port_menu(self, key, port, position):
        """Right-click on a port's box of hosts on the map."""
        if self.worker is not None:
            return
        hosts = self.network_map.hosts_by_port(key).get(port, []) if self.network_map else []
        menu = QMenu(self)
        actions = {}
        if len(hosts) == 1 and hosts[0].ip:
            actions = self.host_actions.add_to(menu, hosts[0].ip)
            menu.addSeparator()
        if len(hosts) == 1:
            actions[menu.addAction("Edit Host...")] = lambda: self.edit_host(hosts[0])
        actions[menu.addAction("Add Host on This Port...")] = lambda: self.add_host(key, port)
        if hosts:
            label = "Delete Host" if len(hosts) == 1 else f"Delete the {len(hosts)} Hosts on This Port"
            actions[menu.addAction(label)] = lambda: self.delete_hosts(hosts)
        chosen = menu.exec_(position)
        if chosen in actions:
            actions[chosen]()

    def add_host(self, device=None, port=""):
        if self.network_map is None or not self.network_map.devices:
            return
        dialog = HostDialog(self.network_map, device=device, port=port, parent=self)
        if dialog.exec_() == QDialog.Accepted:
            host = dialog.values()
            self.network_map.hosts.append(host)
            self.hosts_changed(host)
            set_hint(self.status_label, f"Added {host.name or host.ip or host.mac} on "
                     f"{self.network_map.devices[host.device].label} {host.port}. It's kept when you map again.",
                     "success")

    def edit_host(self, host):
        dialog = HostDialog(self.network_map, host=host, parent=self)
        if dialog.exec_() == QDialog.Accepted:
            edited = dialog.values()
            self.network_map.hosts[self.network_map.hosts.index(host)] = edited
            self.hosts_changed(edited)

    def delete_hosts(self, hosts):
        if not hosts or self.network_map is None or self.worker is not None:
            return
        found = sum(1 for host in hosts if not host.manual)
        what = hosts[0].name or hosts[0].ip or hosts[0].mac if len(hosts) == 1 else f"these {len(hosts)} hosts"
        text = f"Delete {what} from the map?"
        if found:
            text += ("\n\nHosts the crawl found come back the next time you map, if they're still plugged in."
                     if len(hosts) > 1 else "\n\nIt comes back the next time you map, if it's still plugged in.")
        if QMessageBox.question(self, "Delete Hosts", text, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        doomed = {id(host) for host in hosts}
        self.network_map.hosts = [host for host in self.network_map.hosts if id(host) not in doomed]
        self.hosts_changed()
        set_hint(self.status_label, f"Deleted {len(hosts)} host{'' if len(hosts) == 1 else 's'}.", "info")

    def hosts_changed(self, show=None):
        """Redraw after hosts were added, edited or deleted, keeping the view where it was, and save."""
        network_map = self.network_map
        network_map.hosts.sort(key=lambda host: (network_map.devices[host.device].label.lower(),
                                                 port_key(host.port), host.mac))
        network_map.positions = self.view.positions()
        network_map.l3_positions = self.l3_view.positions()
        expanded = {key for key, item in self.view.items_by_key.items() if item.expanded}
        scroll = (self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value())
        self.show_map(network_map, self.map_path)
        for key in expanded | ({show.device} if show else set()):
            if key in self.view.items_by_key and self.view.items_by_key[key].host_count:
                self.view.items_by_key[key].set_expanded(True)
        self.view.update_scene_rect()
        self.view.horizontalScrollBar().setValue(scroll[0])
        self.view.verticalScrollBar().setValue(scroll[1])
        try:
            self.map_path = store.save(network_map, self.map_path) if self.map_path else store.save(network_map)
        except OSError as error:
            QMessageBox.warning(self, "Save Network Map", f"Couldn't save the map:\n\n{error}")

    def sweep_subnet(self, subnet):
        """Fill in the subnet on the Sweep page; sweeping needs a deliberate Sweep there."""
        self.window.navigator.setCurrentWidget(self.window.sweep_tab)
        self.window.sweep_tab.subnet_input.setText(subnet)

    def put_at_top(self, key):
        self.network_map.root = key
        self.network_map.positions = {}
        self.tabs.setCurrentWidget(self.view)
        self.show_map(self.network_map, self.map_path, fit=True)
        self.save_positions()

    # ----------------------------------------------------------------- Monitoring

    def on_monitor_toggled(self, on):
        if on and self.network_map is None:
            self.monitor_check.setChecked(False)
            return
        if on:
            self.monitor.start(self.interval_combo.currentData())
        else:
            self.monitor.stop()
        self.show_statuses()

    def show_statuses(self):
        """After a poll: the dots on both maps, the Status column, the summary and the details showing."""
        for view in (self.view, self.l3_view):
            view.set_statuses(self.monitor.status)
        summary = self.monitor.summary()
        self.monitor_label.setText(summary)
        self.monitor_label.setStyleSheet(f"color: {COLORS['error' if ' down' in summary else 'success']};")
        if self.network_map is not None:
            column = export.DEVICE_COLUMNS.index("Status")
            for row in range(self.devices_table.rowCount()):
                item = self.devices_table.item(row, column)
                key = self.devices_table.item(row, 0).data_object
                if item is not None and item.text() != self.monitor.status_text(key):
                    item.setText(self.monitor.status_text(key))
            self.table_filters[self.devices_table].apply()  # A filter on Status follows the changes
            selected = self.current_view().scene().selectedItems()
            if len(selected) == 1 and getattr(selected[0], "device", None) is not None:
                self.show_details(("device", selected[0].key))

    def keep_history(self, entries):
        """Status changes go into the map's history, saved with it."""
        if self.network_map is None:
            return
        self.network_map.status_log = (self.network_map.status_log + entries)[-monitor.HISTORY_LIMIT:]
        self.save_timer.start()

    # ----------------------------------------------------------------- Comparing

    def fill_compare_menu(self):
        self.compare_menu.clear()
        current = Path(self.map_path).resolve() if self.map_path else None
        for path in store.recent(limit=store.RECENT_LIMIT + 1):
            if path.resolve() != current:
                self.compare_menu.addAction(f"With {path.stem}", lambda path=path: self.compare_with(path))
        self.compare_menu.addSeparator()
        self.compare_menu.addAction("With Another Map...", self.compare_with_file)

    def compare_with_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Compare with Map", str(store.maps_dir()), MAP_FILTER)
        if path:
            self.compare_with(Path(path))

    def compare_with(self, path):
        if self.network_map is None:
            return
        try:
            older = store.load(path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Compare Maps", f"Couldn't open {path.name}:\n\n{error}")
            return
        changes = diff.compare(older, self.network_map)
        if self.compare_dialog is not None:
            self.compare_dialog.close()
        self.compare_dialog = CompareDialog(changes, path.stem, self)
        self.compare_dialog.show_change.connect(self.show_change)
        self.compare_dialog.finished.connect(lambda _: self.view.set_highlights({}))
        colors = {}
        for change in changes:
            if change.what == diff.DEVICE and change.device:
                colors[change.device] = COLORS["success"] if change.change == diff.ADDED else COLORS["warning"]
        self.view.set_highlights(colors)
        self.tabs.setCurrentWidget(self.view)
        self.compare_dialog.show()
        set_hint(self.status_label, f"Compared with {path.stem}: {len(changes)} difference"
                 f"{'' if len(changes) == 1 else 's'}. New devices are ringed in green, changed ones in amber.",
                 "info")

    def show_change(self, change):
        self.tabs.setCurrentWidget(self.view)
        if change.mac:
            host = next((host for host in self.network_map.hosts if host.mac == change.mac), None)
            if host is not None:
                self.view.show_host(host)
                return
        if change.device:
            self.view.show_device(change.device)

    # ----------------------------------------------------------------- Files

    def open_map(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Network Map", str(store.maps_dir()), MAP_FILTER)
        if path:
            self.open_path(Path(path))

    def open_path(self, path):
        try:
            network_map = store.load(path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Open Network Map", f"Couldn't open {path.name}:\n\n{error}")
            return
        self.show_map(network_map, path, fit=True)
        set_hint(self.status_label, f"Opened {path.name} (mapped {network_map.started.replace('T', ' ')}).", "info")

    def fill_recent_menu(self):
        self.recent_menu.clear()
        maps = store.recent()
        if not maps:
            self.recent_menu.addAction("No saved maps").setEnabled(False)
        for path in maps:
            self.recent_menu.addAction(path.stem, lambda path=path: self.open_path(path))

    def save_map_as(self):
        if self.network_map is None:
            return
        start = str(self.map_path or store.maps_dir() / store.default_name(self.network_map))
        path, _ = QFileDialog.getSaveFileName(self, "Save Network Map", start, MAP_FILTER)
        if not path:
            return
        self.network_map.positions = self.view.positions()
        try:
            self.map_path = store.save(self.network_map, path)
        except OSError as error:
            QMessageBox.critical(self, "Save Network Map", f"Couldn't save the map:\n\n{error}")
            return
        set_hint(self.status_label, f"Saved {self.map_path.name}.", "success")

    def export_path(self, title, extension, file_filter):
        name = Path(self.map_path).stem if self.map_path else "Network map"
        path, _ = QFileDialog.getSaveFileName(self, title, str(Path.home() / f"{name}{extension}"), file_filter)
        return Path(path) if path else None

    def export_png(self):
        path = self.export_path("Export Picture", ".png", "PNG pictures (*.png)")
        if path and not self.current_view().render_image().save(str(path)):
            QMessageBox.critical(self, "Export Picture", f"Couldn't save {path}.")
        elif path:
            self.window.show_status(f"Saved the map as {path}.")

    def export_svg(self):
        path = self.export_path("Export Drawing", ".svg", "SVG drawings (*.svg)")
        if path:
            self.current_view().render_svg(path)
            self.window.show_status(f"Saved the map as {path}.")

    def export_drawio(self):
        path = self.export_path("Export for draw.io", ".drawio", "draw.io files (*.drawio)")
        if not path:
            return
        try:
            if self.current_view() is self.l3_view:
                text = export.drawio_graph([(key, node.label, node.device.kind if node.device else node.kind,
                                             node.kind in (l3.HOP, l3.STAR)) for key, node in self.l3_nodes.items()],
                                           l3.l3_graph(self.network_map)[1], self.l3_view.positions(), "Logical map")
            else:
                text = export.drawio(self.network_map, self.view.positions())
            path.write_text(text, encoding="utf-8")
        except OSError as error:
            QMessageBox.critical(self, "Export for draw.io", f"Couldn't save the file:\n\n{error}")
            return
        self.window.show_status(f"Saved {path}. Open it in draw.io (or import it into Visio).")

    def export_csv(self, which):
        columns, rows = {"devices": (export.DEVICE_COLUMNS,
                                     lambda network_map: export.device_rows(network_map, self.monitor.status_text)),
                         "links": (export.LINK_COLUMNS, export.link_rows),
                         "hosts": (export.HOST_COLUMNS, export.host_rows)}[which]
        path = self.export_path(f"Export {which.title()}", f" {which}.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            export.write_csv(path, columns, rows(self.network_map))
        except OSError as error:
            QMessageBox.critical(self, "Export", f"Couldn't save the file:\n\n{error}")
            return
        self.window.show_status(f"Saved {path}.")

    def update_buttons(self):
        running = self.worker is not None
        has_map = self.network_map is not None
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        for widget in (self.save_button, self.export_button, self.compare_button, self.arrange_button):
            widget.setEnabled(has_map and not running)  # Not while the map is being drawn from a crawl
        for widget in (self.open_button, self.recent_button):
            widget.setEnabled(not running)
        for widget in (self.fit_button, self.hosts_check):
            widget.setEnabled(has_map or running)
        self.monitor_check.setEnabled(has_map or self.monitor_check.isChecked())
        here = self.tabs.currentWidget()
        self.find_input.setEnabled(has_map or running or here is self.crawl_progress.tab)


def fill_table(table, rows, ip_columns=(), keys=None):
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for row_number, row in enumerate(rows):
        for column, text in enumerate(row):
            sort_key = ip_sort_key(text) if column in ip_columns else None
            data = keys[row_number] if keys is not None and column == 0 else None
            table.setItem(row_number, column, SortableTableItem(text, sort_key, data))
    table.setSortingEnabled(True)


def device_html(network_map, key, state=None):
    """state: the device's monitor.DeviceStatus while it's monitored."""
    device = network_map.devices.get(key)
    if device is None:
        return ""
    escape = html.escape
    parts = [f"<h3>{escape(device.label)}</h3>",
             f"<p>{escape(KIND_NAMES.get(device.kind, device.kind))}"
             + (f" &middot; {escape(device.platform)}" if device.platform else "") + "</p><table>"]
    rows = [("Status", monitor_status_text(state)), ("Management IP", device.mgmt_ip),
            ("Found by", SOURCE_NAMES.get(device.source, device.source)),
            ("Hops from start", str(device.hops)), ("Addresses", ", ".join(device.addresses)),
            ("Problem", device.error)]
    for label, value in rows:
        if value:
            parts.append(f"<tr><td><b>{escape(label)}</b>&nbsp;</td><td>{escape(value)}</td></tr>")
    parts.append("</table>")
    links = network_map.links_of(key)
    if links:
        parts.append("<h4>Links</h4><table>")
        for link in sorted(links, key=lambda link: link.port_on(key)):
            other = network_map.devices[link.other(key)]
            parts.append(f"<tr><td>{escape(link.port_on(key))}&nbsp;</td><td>&rarr; {escape(other.label)} "
                         f"{escape(link.port_on(other.key))}</td></tr>")
        parts.append("</table>")
    ports = network_map.hosts_by_port(key)
    if ports:
        count = sum(len(hosts) for hosts in ports.values())
        parts.append(f"<h4>Hosts ({count})</h4><table>")
        for port, hosts in ports.items():
            what = ", ".join(host.name or host.ip or host.mac for host in hosts) if len(hosts) <= 3 \
                else f"{len(hosts)} hosts"
            parts.append(f"<tr><td>{escape(port)}&nbsp;</td><td>{escape(what)}</td></tr>")
        parts.append("</table>")
    if device.interfaces_l3:
        parts.append("<h4>IP Interfaces</h4><table>")
        for address, prefix, port in device.interfaces_l3:
            parts.append(f"<tr><td>{escape(short_port(port))}&nbsp;</td><td>{escape(address)}/{prefix}</td></tr>")
        parts.append("</table>")
    routes = [route for route in device.routes if route[1]]  # Connected ones are the interfaces above
    if routes:
        parts.append(f"<h4>Routes ({len(routes)}{'+' if device.routes_truncated else ''})</h4><table>")
        for destination, next_hop, port, protocol in routes[:ROUTES_SHOWN]:
            parts.append(f"<tr><td>{escape(destination)}&nbsp;</td><td>via {escape(next_hop)} "
                         f"({escape(protocol)})</td></tr>")
        parts.append("</table>")
        if len(routes) > ROUTES_SHOWN:
            parts.append(f"<p>...and {len(routes) - ROUTES_SHOWN} more.</p>")
    if device.sys_descr:
        parts.append(f"<h4>Description</h4><p>{escape(device.sys_descr).replace(chr(10), '<br>')}</p>")
    return "".join(parts)


def monitor_status_text(state):
    if state is None:
        return ""
    if state.status == monitor.UNKNOWN:
        return "Being checked"
    since = datetime.datetime.fromtimestamp(state.since).strftime("%H:%M:%S")
    if state.status == monitor.DOWN:
        return f"Down for {monitor.duration_text(time.time() - state.since)} (since {since})"
    rtt = f", {'<1' if state.rtt < 1 else state.rtt} ms" if state.rtt is not None else ""
    return f"Up{rtt} (since {since})"


def subnet_html(network_map, subnet):
    escape = html.escape
    members, hosts = l3.subnet_details(network_map, subnet)
    parts = [f"<h3>{escape(subnet)}</h3><h4>Devices with an address in it</h4><table>"]
    for label, port, address in members:
        parts.append(f"<tr><td>{escape(label)}&nbsp;</td><td>{escape(port)} {escape(address)}</td></tr>")
    parts.append("</table>")
    routed = [(device.label, route) for device in network_map.devices.values() for route in device.routes
              if route[1] and route[0] == subnet]
    for label, (destination, next_hop, port, protocol) in routed[:ROUTES_SHOWN]:
        parts.append(f"<p>{escape(label)} routes it via {escape(next_hop)} ({escape(protocol)})</p>")
    if hosts:
        parts.append(f"<h4>Hosts on the map ({len(hosts)})</h4><table>")
        for host in hosts[:ROUTES_SHOWN]:
            device = network_map.devices.get(host.device)
            parts.append(f"<tr><td>{escape(host.ip)}&nbsp;</td><td>{escape(host.name or host.mac)} on "
                         f"{escape(device.label if device else host.device)} {escape(host.port)}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


def node_html(network_map, node):
    """A hop traceroute found, an unanswered hop, or this computer: which traces went through it."""
    escape = html.escape
    parts = [f"<h3>{escape(node.label)}</h3>", f"<p>{escape(node.detail)}</p>" if node.detail else ""]
    through = [item for item in network_map.traces
               if node.key == l3.SELF or node.label in item.hops or node.key.startswith(f"star:{item.target}:")]
    if through:
        parts.append("<h4>Traceroutes</h4>")
        for item in through:
            path = " &rarr; ".join(escape(hop) or "*" for hop in item.hops)
            parts.append(f"<p><b>{escape(item.target)}</b> ({escape(item.reason)}"
                         f"{'' if item.reached else ', not reached'}): {path}</p>")
    return "".join(parts)


def port_html(network_map, key, port):
    escape = html.escape
    device = network_map.devices.get(key)
    hosts = network_map.hosts_by_port(key).get(port, [])
    parts = [f"<h3>{escape(device.label if device else key)} {escape(port)}</h3>",
             f"<p>{len(hosts)} host{'' if len(hosts) == 1 else 's'}</p><table>"]
    for host in hosts:
        details = [host.ip, host.name, host.vendor or host.platform, f"VLAN {host.vlan}" if host.vlan else "",
                   "(added by hand)" if host.manual else "", host.note]
        parts.append(f"<tr><td>{escape(host.mac)}&nbsp;</td><td>"
                     f"{escape('  '.join(part for part in details if part))}</td></tr>")
    parts.append("</table>")
    return "".join(parts)
