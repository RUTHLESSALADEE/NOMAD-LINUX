"""The network map's VLANs tab: every VLAN the crawl found (by VTP domain), where it goes, and what looks wrong in how
VLANs are set up (a native VLAN that differs at the two ends of a trunk, say). A VLAN can be highlighted on the
physical view: the switches and links that carry it stay bright, the rest fade."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHeaderView, QHBoxLayout, QLabel, QMenu, QPushButton, QSplitter, QVBoxLayout, QWidget

from ..netmap import vlans
from .common import SortableTableItem, read_only_table
from .theme import COLORS

VLAN_COLUMNS = ["Domain", "VLAN", "Name", "Switches", "Access Ports", "Trunk Ports", "Gateways", "Hosts"]
CHECK_COLUMNS = ["Severity", "VLAN", "Where", "What"]
SEVERITY_COLORS = {vlans.ERROR: "error", vlans.WARNING: "warning", vlans.INFO: "muted"}
NO_DOMAIN = "(no VTP domain)"


def domain_text(domain):
    return domain or NO_DOMAIN


class VlanPanel(QWidget):
    highlight_requested = pyqtSignal(int, str)  # VLAN, VTP domain
    show_requested = pyqtSignal(str, str)  # Device key, port ("" for the device)
    add_to_database_requested = pyqtSignal()
    read_requested = pyqtSignal()  # Read VLANs Again
    vlans_page_requested = pyqtSignal(int, str)  # VLAN, VTP domain: show it on Manage > VLANs

    def __init__(self, parent=None):
        super().__init__(parent)
        self.network_map = None
        self.items = []
        self.findings = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        buttons = QHBoxLayout()
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.highlight_button = QPushButton("Highlight on Map")
        self.highlight_button.setToolTip("Show the VLAN selected on the physical view: the switches that have it and "
                                         "the links carrying it stay bright, everything else fades (double-click a "
                                         "VLAN does the same).")
        self.database_button = QPushButton("Add to VLAN Database...")
        self.database_button.setToolTip("Bring the VLANs found here into Manage > VLANs: you see what's new, missing "
                                        "or named differently there, and choose what to bring in.")
        self.vlans_page_button = QPushButton("Show on VLANs Page")
        self.vlans_page_button.setToolTip("Go to the VLAN on Manage > VLANs (in the domain of its VTP domain, or of "
                                          "the map's IPAM network).")
        self.read_button = QPushButton("Read VLANs Again")
        self.read_button.setToolTip("Read the VLANs of every switch on the map again (just their VLANs, not their "
                                    "neighbors or hosts): quicker than mapping again, and gives a map made before "
                                    "NOMAD read VLANs its VLANs. What changed goes in the Watch log.")
        buttons.addWidget(self.summary_label, 1)
        buttons.addWidget(self.read_button)
        buttons.addWidget(self.highlight_button)
        buttons.addWidget(self.vlans_page_button)
        buttons.addWidget(self.database_button)
        layout.addLayout(buttons)
        self.table = read_only_table(VLAN_COLUMNS)
        self.checks = read_only_table(CHECK_COLUMNS)
        self.checks.setWordWrap(False)
        self.checks.setToolTip("Double-click one to show the switch on the map.")
        self.checks_label = QLabel()
        self.expand_checks_button = QPushButton("Expand Notes and Warnings")
        self.expand_checks_button.setCheckable(True)
        self.expand_checks_button.toggled.connect(self.expand_checks)
        checks_heading = QHBoxLayout()
        checks_heading.addWidget(self.checks_label, 1)
        checks_heading.addWidget(self.expand_checks_button)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addLayout(checks_heading)
        lower_layout.addWidget(self.checks, 1)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.highlight_button.clicked.connect(self.highlight_selected)
        self.database_button.clicked.connect(self.add_to_database)
        self.read_button.clicked.connect(self.read_again)
        self.vlans_page_button.clicked.connect(self.show_on_vlans_page)
        self.table.itemDoubleClicked.connect(self.highlight_selected)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_vlan_menu)
        self.checks.itemDoubleClicked.connect(self.show_finding)
        self.checks.setContextMenuPolicy(Qt.CustomContextMenu)
        self.checks.customContextMenuRequested.connect(self.show_checks_menu)
        self.set_map(None)

    def set_map(self, network_map):
        self.network_map = network_map
        self.items = vlans.map_vlans(network_map) if network_map is not None else []
        self.findings = vlans.check_map(network_map) if network_map is not None else []
        devices = network_map.devices if network_map is not None else {}
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            gateways = ", ".join(f"{gateway.address}/{gateway.prefix} on {devices[gateway.device].label}"
                                 + (" (guessed from its name)" if gateway.guessed else "")
                                 for gateway in item.gateways if gateway.device in devices)
            names = item.name + (f" (+{len(item.names) - 1} other name{'s' if len(item.names) > 2 else ''})"
                                 if len(item.names) > 1 else "")
            values = [(domain_text(item.domain), None), (str(item.vlan), item.vlan), (names, None),
                      (str(len(item.switches)), len(item.switches)), (str(len(item.access_ports)),
                                                                      len(item.access_ports)),
                      (str(len(item.trunk_ports)), len(item.trunk_ports)), (gateways, None),
                      (str(item.hosts), item.hosts)]
            for column, (text, sort_key) in enumerate(values):
                self.table.setItem(row, column, SortableTableItem(text, sort_key, item if column == 0 else None))
        self.table.setSortingEnabled(True)
        self.checks.setSortingEnabled(False)
        self.checks.setRowCount(len(self.findings))
        order = {vlans.ERROR: 0, vlans.WARNING: 1, vlans.INFO: 2}
        for row, finding in enumerate(self.findings):
            where = devices[finding.device].label if finding.device in devices else ""
            if finding.port:
                where += f" {finding.port}"
            cells = [SortableTableItem(vlans.SEVERITY_LABELS[finding.severity], order[finding.severity], finding),
                     SortableTableItem(str(finding.vlan) if finding.vlan else "", finding.vlan),
                     SortableTableItem(where), SortableTableItem(finding.text)]
            cells[0].setForeground(QColor(COLORS[SEVERITY_COLORS[finding.severity]]))
            cells[3].setToolTip(finding.text)
            for column, cell in enumerate(cells):
                self.checks.setItem(row, column, cell)
        self.checks.setSortingEnabled(True)
        switches = sum(1 for device in devices.values() if device.vlans)
        domains = vlans.domains(network_map) if network_map is not None else []
        if network_map is None:
            self.summary_label.setText("")
        elif not self.items:
            self.summary_label.setText("No VLANs on this map. They're read from switches over SNMP when the map is "
                                       "crawled. A map made before NOMAD read VLANs has none: Read VLANs Again.")
        else:
            named = ", ".join(domain_text(domain) for domain in domains[:4])
            self.summary_label.setText(f"{len(self.items)} VLAN{'s' if len(self.items) != 1 else ''} on {switches} "
                                       f"switch{'es' if switches != 1 else ''}"
                                       + (f", VTP domain{'s' if len(domains) > 1 else ''} {named}" if domains else ""))
        problems = sum(1 for finding in self.findings if finding.severity != vlans.INFO)
        self.checks_label.setText(f"Checks: {len(self.findings)} finding{'s' if len(self.findings) != 1 else ''}"
                                  + (f", {problems} worth fixing" if problems else "")
                                  if self.findings else "Checks: nothing wrong found in how VLANs are set up.")
        self.update_buttons()

    def selected_item(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        cell = self.table.item(rows.pop(), 0)
        return cell.data_object if cell is not None else None

    def expand_checks(self, expanded):
        self.checks.setWordWrap(expanded)
        header = self.checks.verticalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents if expanded else QHeaderView.Fixed)
        if not expanded:
            for row in range(self.checks.rowCount()):
                self.checks.setRowHeight(row, header.defaultSectionSize())
        self.expand_checks_button.setText("Collapse Notes and Warnings" if expanded else "Expand Notes and Warnings")

    def update_buttons(self):
        self.highlight_button.setEnabled(self.selected_item() is not None)
        self.vlans_page_button.setEnabled(self.selected_item() is not None)
        self.database_button.setEnabled(bool(self.items))
        self.read_button.setEnabled(self.network_map is not None and self.read_button.text() == "Read VLANs Again")

    def highlight_selected(self, *_):
        item = self.selected_item()
        if item is not None:
            self.highlight_requested.emit(item.vlan, item.domain)

    def show_vlan_menu(self, position):
        cell = self.table.itemAt(position)
        if cell is None:
            return
        self.table.selectRow(cell.row())
        menu = QMenu(self)
        actions = {}
        for button in (self.highlight_button, self.vlans_page_button, self.database_button):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            actions[action] = button.click
        chosen = menu.exec_(self.table.viewport().mapToGlobal(position))
        if chosen in actions:
            actions[chosen]()

    def show_checks_menu(self, position):
        cell = self.checks.itemAt(position)
        if cell is None:
            return
        self.checks.selectRow(cell.row())
        finding = self.checks.item(cell.row(), 0).data_object
        menu = QMenu(self)
        show = menu.addAction("Show on Map")
        show.setEnabled(finding is not None and bool(finding.device))
        expand = menu.addAction(self.expand_checks_button.text())
        chosen = menu.exec_(self.checks.viewport().mapToGlobal(position))
        if chosen is show and show.isEnabled():
            self.show_finding(cell)
        elif chosen is expand:
            self.expand_checks_button.click()

    def show_on_vlans_page(self):
        item = self.selected_item()
        if item is not None:
            self.vlans_page_requested.emit(item.vlan, item.domain)

    def read_again(self):
        self.read_requested.emit()

    def set_reading(self, reading):
        self.read_button.setEnabled(not reading and self.network_map is not None)
        self.read_button.setText("Reading VLANs..." if reading else "Read VLANs Again")

    def add_to_database(self):
        self.add_to_database_requested.emit()

    def show_finding(self, cell):
        finding = self.checks.item(cell.row(), 0).data_object
        if finding is not None and finding.device:
            self.show_requested.emit(finding.device, finding.port)
