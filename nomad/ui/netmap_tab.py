"""Network Map page: crawl switches, routers and firewalls over SNMP from a starting device, and draw what's
connected to what (CDP/LLDP), with the hosts on each switch port (MAC and ARP tables)."""
import html
import ipaddress
import json
import logging
import socket
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, \
    QPushButton, QSplitter, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget

from ..netmap import export, store
from ..netmap.crawl import CrawlSettings, Crawler
from ..netmap.layout import merge_positions
from ..netmap.model import FIREWALL, KIND_NAMES, NO_SNMP, ROUTER, SNMP, SOURCE_NAMES, SWITCH, UNREACHABLE
from ..snmp import V2C
from ..terminal.credentials import CredentialError, protect, unprotect
from .common import SortableTableItem, StoppableThread, read_only_table, set_hint
from .host_menu import HostActions
from .netmap_dialogs import CommunitiesDialog, ScopeDialog
from .netmap_view import MapView
from .theme import accent_button

log = logging.getLogger(__name__)

MAP_FILTER = f"Network maps (*{store.EXTENSION})"
KIND_WEIGHTS = {FIREWALL: 3, ROUTER: 2, SWITCH: 1}  # Breaks ties when choosing the top device
SAVE_DELAY_MS = 1000
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
            network_map = Crawler(self.settings, should_stop=lambda: self.stopping,
                                  progress=self.progress.emit).run()
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
                                     "MAC tables for hosts.")
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
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("Find a device or host: name, IP, MAC or vendor")
        self.find_input.setClearButtonEnabled(True)
        self.fit_button = QPushButton("Fit")
        self.fit_button.setToolTip("Zoom to show the whole map. Scroll to zoom, drag the background to move around.")
        self.arrange_button = QPushButton("Re-arrange")
        self.arrange_button.setToolTip("Lay the map out again, forgetting where devices were dragged to.")
        for widget in (self.open_button, self.recent_button, self.save_button, self.export_button):
            tools.addWidget(widget)
        tools.addSpacing(16)
        tools.addWidget(self.find_input, 1)
        tools.addWidget(self.fit_button)
        tools.addWidget(self.arrange_button)
        layout.addLayout(tools)

        self.tabs = QTabWidget()
        self.view = MapView()
        self.devices_table = read_only_table(export.DEVICE_COLUMNS)
        self.links_table = read_only_table(export.LINK_COLUMNS)
        self.hosts_table = read_only_table(export.HOST_COLUMNS)
        self.tabs.addTab(self.view, "Physical (L2)")
        self.tabs.addTab(self.devices_table, "Devices")
        self.tabs.addTab(self.links_table, "Links")
        self.tabs.addTab(self.hosts_table, "Hosts")
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
        self.fit_button.clicked.connect(self.view.fit)
        self.arrange_button.clicked.connect(self.rearrange)
        self.view.selection_changed.connect(self.show_details)
        self.view.positions_changed.connect(self.save_timer.start)
        self.view.context_requested.connect(self.show_device_menu)
        self.devices_table.itemDoubleClicked.connect(lambda item: self.show_on_map("device", item.row()))
        self.hosts_table.itemDoubleClicked.connect(lambda item: self.show_on_map("host", item.row()))

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
        splitter = settings.value("netmap/splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)
        last = settings.value("netmap/last_map", "", str)
        if last and Path(last).is_file():
            try:
                self.show_map(store.load(last), Path(last), fit=True)
            except (OSError, ValueError) as error:
                log.warning("Couldn't reopen the last network map %s: %s", last, error)

    def shutdown(self):
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
        dialog = ScopeDialog(self.scope, self.max_hops, self.max_devices, self.collect_hosts, self)
        if dialog.exec_() == QDialog.Accepted:
            self.scope, self.max_hops, self.max_devices, self.collect_hosts = dialog.values()

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
                                 version=self.version, timeout=self.timeout, collect_hosts=self.collect_hosts)
        self.worker = CrawlThread(settings, self)
        self.worker.progress.connect(lambda message: set_hint(self.status_label, message, "info"))
        self.worker.crawled.connect(self.on_crawled)
        self.worker.failed.connect(lambda message: set_hint(self.status_label, message, "error"))
        self.worker.finished.connect(self.on_thread_finished)
        set_hint(self.status_label, f"Asking {', '.join(seeds)}...", "info")
        self.window.set_busy("netmap", "Mapping the network")
        self.worker.start()
        self.update_buttons()

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            set_hint(self.status_label, "Stopping: finishing the devices being read...", "warning")

    def on_thread_finished(self):
        self.worker = None
        self.window.clear_busy("netmap")
        self.update_buttons()

    def on_crawled(self, network_map):
        if self.network_map is not None:  # Keep where the user put devices that are still there
            network_map.positions = {key: position for key, position in self.network_map.positions.items()
                                     if key in network_map.devices}
            network_map.root = self.network_map.root if self.network_map.root in network_map.devices else ""
        path = None
        try:
            path = store.save(network_map)
        except OSError as error:
            log.warning("Couldn't save the network map: %s", error)
        self.show_map(network_map, path, fit=True)
        snmp_count = sum(1 for device in network_map.devices.values() if device.source == SNMP)
        problems = sum(1 for device in network_map.devices.values() if device.source in (NO_SNMP, UNREACHABLE))
        message = (f"{'Stopped' if network_map.stopped else 'Done'}: {len(network_map.devices)} devices "
                   f"({snmp_count} read over SNMP), {len(network_map.links)} links, {len(network_map.hosts)} hosts.")
        if problems:
            message += f" {problems} didn't answer SNMP (dashed or red; select one to see why)."
        if path:
            message += f" Saved as {path.name}."
        set_hint(self.status_label, message, "warning" if network_map.stopped or problems else "success")

    # ----------------------------------------------------------------- Showing a map

    def show_map(self, network_map, path=None, fit=False, relayout=False):
        self.network_map, self.map_path = network_map, path
        if relayout:
            network_map.positions = {}
        nodes = list(network_map.devices)
        edges = [(link.a, link.b) for link in network_map.links]
        positions = merge_positions(nodes, edges, network_map.positions, root=network_map.root or None,
                                    weight=lambda key: KIND_WEIGHTS.get(network_map.devices[key].kind, 0))
        network_map.positions = positions
        self.view.set_map(network_map, positions)
        self.fill_tables()
        self.show_details(None)
        if fit or relayout:
            self.view.request_fit()
        self.update_buttons()

    def rearrange(self):
        if self.network_map is not None:
            self.show_map(self.network_map, self.map_path, relayout=True)
            self.save_positions()

    def save_positions(self):
        if self.network_map is None or self.map_path is None:
            return
        self.network_map.positions = self.view.positions()
        try:
            store.save(self.network_map, self.map_path)
        except OSError as error:
            log.warning("Couldn't save the network map's layout: %s", error)

    def fill_tables(self):
        network_map = self.network_map
        self.device_keys = [device.key for device in sorted(network_map.devices.values(),
                                                            key=lambda device: device.label.lower())]
        fill_table(self.devices_table, export.device_rows(network_map), ip_columns={1}, keys=self.device_keys)
        fill_table(self.links_table, export.link_rows(network_map))
        fill_table(self.hosts_table, export.host_rows(network_map), ip_columns={1},
                   keys=list(range(len(network_map.hosts))))

    def show_on_map(self, kind, row):
        item = (self.devices_table if kind == "device" else self.hosts_table).item(row, 0)
        if item is None or self.network_map is None:
            return
        self.tabs.setCurrentWidget(self.view)
        if kind == "device":
            self.view.show_device(item.data_object)
        else:
            self.view.show_host(self.network_map.hosts[item.data_object])

    def find(self):
        text = self.find_input.text().strip()
        if not text:
            return
        self.tabs.setCurrentWidget(self.view)
        if not self.view.find(text):
            set_hint(self.status_label, f"Nothing on the map matches '{text}'.", "warning")

    def show_details(self, selection):
        network_map = self.network_map
        if network_map is None or selection is None:
            if network_map is None:
                text = ("<p>Start from a core switch or your gateway. Each device's CDP and LLDP neighbors are read "
                        "over SNMP, then theirs, until the whole network (within the scope) is mapped.</p>"
                        "<p>Double-click a switch to show its hosts by port. Right-click a device for SSH, ping, "
                        "SNMP and more.</p>")
            else:
                text = "<p>Select a device to see its details.</p>"
            self.details.setHtml(text)
            return
        if selection[0] == "device":
            self.details.setHtml(device_html(network_map, selection[1]))
        else:
            self.details.setHtml(port_html(network_map, selection[1], selection[2]))

    def show_device_menu(self, key, position):
        device = self.network_map.devices.get(key) if self.network_map else None
        if device is None:
            return
        menu = QMenu(self)
        actions = self.host_actions.add_to(menu, device.mgmt_ip) if device.mgmt_ip else {}
        menu.addSeparator()
        if device.mgmt_ip:
            actions[menu.addAction("Crawl from Here")] = lambda: self.crawl_from(device.mgmt_ip)
        actions[menu.addAction("Put at the Top")] = lambda: self.put_at_top(key)
        item = self.view.items_by_key.get(key)
        if item is not None and item.host_count:
            label = "Hide Hosts" if item.expanded else "Show Hosts"
            actions[menu.addAction(label)] = lambda: self.view.toggle_hosts(item)
        menu.addSeparator()
        actions[menu.addAction("Copy Name")] = lambda: QApplication.clipboard().setText(device.label)
        if device.mgmt_ip:
            actions[menu.addAction("Copy Address")] = lambda: QApplication.clipboard().setText(device.mgmt_ip)
        chosen = menu.exec_(position)
        if chosen in actions:
            actions[chosen]()

    def put_at_top(self, key):
        self.network_map.root = key
        self.rearrange()

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
        if path and not self.view.render_image().save(str(path)):
            QMessageBox.critical(self, "Export Picture", f"Couldn't save {path}.")
        elif path:
            self.window.show_status(f"Saved the map as {path}.")

    def export_svg(self):
        path = self.export_path("Export Drawing", ".svg", "SVG drawings (*.svg)")
        if path:
            self.view.render_svg(path)
            self.window.show_status(f"Saved the map as {path}.")

    def export_drawio(self):
        path = self.export_path("Export for draw.io", ".drawio", "draw.io files (*.drawio)")
        if not path:
            return
        try:
            path.write_text(export.drawio(self.network_map, self.view.positions()), encoding="utf-8")
        except OSError as error:
            QMessageBox.critical(self, "Export for draw.io", f"Couldn't save the file:\n\n{error}")
            return
        self.window.show_status(f"Saved {path}. Open it in draw.io (or import it into Visio).")

    def export_csv(self, which):
        columns, rows = {"devices": (export.DEVICE_COLUMNS, export.device_rows),
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
        for widget in (self.save_button, self.export_button, self.fit_button, self.arrange_button, self.find_input):
            widget.setEnabled(has_map)


def fill_table(table, rows, ip_columns=(), keys=None):
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for row_number, row in enumerate(rows):
        for column, text in enumerate(row):
            sort_key = ip_sort_key(text) if column in ip_columns else None
            data = keys[row_number] if keys is not None and column == 0 else None
            table.setItem(row_number, column, SortableTableItem(text, sort_key, data))
    table.setSortingEnabled(True)


def device_html(network_map, key):
    device = network_map.devices.get(key)
    if device is None:
        return ""
    escape = html.escape
    parts = [f"<h3>{escape(device.label)}</h3>",
             f"<p>{escape(KIND_NAMES.get(device.kind, device.kind))}"
             + (f" &middot; {escape(device.platform)}" if device.platform else "") + "</p><table>"]
    rows = [("Management IP", device.mgmt_ip), ("Found by", SOURCE_NAMES.get(device.source, device.source)),
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
    if device.sys_descr:
        parts.append(f"<h4>Description</h4><p>{escape(device.sys_descr).replace(chr(10), '<br>')}</p>")
    return "".join(parts)


def port_html(network_map, key, port):
    escape = html.escape
    device = network_map.devices.get(key)
    hosts = network_map.hosts_by_port(key).get(port, [])
    parts = [f"<h3>{escape(device.label if device else key)} {escape(port)}</h3>",
             f"<p>{len(hosts)} host{'' if len(hosts) == 1 else 's'}</p><table>"]
    for host in hosts:
        details = [host.ip, host.name, host.vendor or host.platform, f"VLAN {host.vlan}" if host.vlan else ""]
        parts.append(f"<tr><td>{escape(host.mac)}&nbsp;</td><td>"
                     f"{escape('  '.join(part for part in details if part))}</td></tr>")
    parts.append("</table>")
    return "".join(parts)
