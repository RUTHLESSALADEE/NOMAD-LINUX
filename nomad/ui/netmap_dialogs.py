"""Network Map settings: the SNMP community strings to try, and how far the crawl may go."""
import ipaddress

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, \
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, \
    QTableWidgetItem, QVBoxLayout

from ..netmap import diff
from ..netmap.crawl import parse_networks
from ..netmap.model import Host, port_sort_key, short_port
from ..oui import format_mac, vendor
from ..snmp import VERSIONS, community_is_valid
from .theme import COLORS

CHANGE_COLORS = {diff.ADDED: "success", diff.REMOVED: "error", diff.CHANGED: "warning", diff.MOVED: "link"}


class CommunitiesDialog(QDialog):
    def __init__(self, communities, overrides, version, timeout, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SNMP Community Strings")
        self.resize(520, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Community strings to try on each device, in order (one per line). The first one "
                                "that answers is used for that device."))
        self.communities_input = QPlainTextEdit("\n".join(communities))
        self.communities_input.setTabChangesFocus(True)
        layout.addWidget(self.communities_input, 1)

        layout.addWidget(QLabel("Per-subnet community strings, tried first for addresses in the subnet:"))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Subnet", "Community"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for subnet, community in overrides:
            self.add_row(subnet, community)
        layout.addWidget(self.table, 1)
        row_buttons = QHBoxLayout()
        add_button = QPushButton("Add")
        remove_button = QPushButton("Remove")
        add_button.clicked.connect(lambda: self.add_row("", "", edit=True))
        remove_button.clicked.connect(self.remove_rows)
        row_buttons.addWidget(add_button)
        row_buttons.addWidget(remove_button)
        row_buttons.addStretch()
        layout.addLayout(row_buttons)

        form = QFormLayout()
        self.version_combo = QComboBox()
        self.version_combo.addItems(list(VERSIONS))
        self.version_combo.setCurrentText(next((name for name, value in VERSIONS.items() if value == version), "v2c"))
        self.timeout_input = QSpinBox()
        self.timeout_input.setRange(200, 20000)
        self.timeout_input.setSingleStep(500)
        self.timeout_input.setValue(timeout)
        self.timeout_input.setSuffix(" ms")
        self.timeout_input.setToolTip("How long to wait for each SNMP answer. Devices that don't answer are tried "
                                      "with each community string, so a long timeout slows the crawl down.")
        form.addRow("SNMP version:", self.version_combo)
        form.addRow("Timeout:", self.timeout_input)
        layout.addLayout(form)
        note = QLabel("Saved encrypted for your Windows account.")
        note.setEnabled(False)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_row(self, subnet, community, edit=False):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(subnet))
        self.table.setItem(row, 1, QTableWidgetItem(community))
        if edit:
            self.table.editItem(self.table.item(row, 0))

    def remove_rows(self):
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def values(self):
        """(communities, overrides, version, timeout). Raises ValueError for anything that isn't valid."""
        communities = [line.strip() for line in self.communities_input.toPlainText().splitlines() if line.strip()]
        if not communities:
            raise ValueError("Enter at least one community string, such as public.")
        overrides = []
        for row in range(self.table.rowCount()):
            subnet = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            community = (self.table.item(row, 1).text() if self.table.item(row, 1) else "").strip()
            if not subnet and not community:
                continue
            try:
                subnet = str(ipaddress.ip_network(subnet, strict=False))
            except ValueError:
                raise ValueError(f"'{subnet}' isn't a subnet. Use CIDR notation, such as 10.20.0.0/16.") from None
            if not community:
                raise ValueError(f"Enter the community string for {subnet}.")
            overrides.append((subnet, community))
        for community in communities + [community for _, community in overrides]:
            if not community_is_valid(community):
                raise ValueError("Community strings can't be longer than 255 bytes or contain control characters.")
        return communities, overrides, VERSIONS[self.version_combo.currentText()], self.timeout_input.value()

    def accept(self):
        try:
            self.values()
        except ValueError as error:
            QMessageBox.warning(self, "SNMP Community Strings", str(error))
            return
        super().accept()


class ScopeDialog(QDialog):
    def __init__(self, scope, max_hops, max_devices, collect_hosts, trace, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crawl Scope")
        self.resize(460, 380)
        layout = QVBoxLayout(self)
        label = QLabel("Only ask devices in these subnets (one per line). Leave empty for any private address "
                       "(10.x, 172.16-31.x, 192.168.x). Devices outside still appear as neighbors.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.scope_input = QPlainTextEdit("\n".join(scope))
        self.scope_input.setPlaceholderText("10.0.0.0/8")
        self.scope_input.setTabChangesFocus(True)
        layout.addWidget(self.scope_input, 1)
        form = QFormLayout()
        self.hops_input = QSpinBox()
        self.hops_input.setRange(0, 50)
        self.hops_input.setValue(max_hops)
        self.hops_input.setToolTip("How many links away from the starting devices to go. 0 reads only the "
                                   "starting devices.")
        self.devices_input = QSpinBox()
        self.devices_input.setRange(1, 10000)
        self.devices_input.setValue(max_devices)
        self.devices_input.setToolTip("Stop asking new devices after this many.")
        self.hosts_check = QCheckBox("Read MAC and ARP tables to show the hosts on each switch port")
        self.hosts_check.setChecked(collect_hosts)
        self.trace_check = QCheckBox("Traceroute to what SNMP can't show (for the logical view)")
        self.trace_check.setToolTip("After the crawl, trace from this computer to devices that didn't answer SNMP, "
                                    "next hops that aren't on the map, and static routes' destinations.")
        self.trace_check.setChecked(trace)
        form.addRow("Hops:", self.hops_input)
        form.addRow("Devices to ask, at most:", self.devices_input)
        form.addRow(self.hosts_check)
        form.addRow(self.trace_check)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        """(scope lines, max hops, max devices, collect hosts, trace). Raises ValueError for a bad subnet."""
        lines = [line.strip() for line in self.scope_input.toPlainText().splitlines() if line.strip()]
        parse_networks(lines)
        return (lines, self.hops_input.value(), self.devices_input.value(), self.hosts_check.isChecked(),
                self.trace_check.isChecked())

    def accept(self):
        try:
            self.values()
        except ValueError as error:
            QMessageBox.warning(self, "Crawl Scope", str(error))
            return
        super().accept()


class CompareDialog(QDialog):
    """What changed since an earlier map. Double-click a row to see it on the map; not modal, so the map can be
    looked at alongside."""
    show_change = pyqtSignal(object)  # diff.Change

    def __init__(self, changes, older_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Compared with {older_name}")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(760, 480)
        self.changes = changes
        layout = QVBoxLayout(self)
        counts = {}
        for change in changes:
            counts[(change.what, change.change)] = counts.get((change.what, change.change), 0) + 1
        summary = ", ".join(f"{count} {what.lower()}{'' if count == 1 else 's'} {change.lower()}"
                            for (what, change), count in sorted(counts.items())) or "No differences."
        label = QLabel(f"Since {older_name}: {summary}")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.churn_check = QCheckBox("Show hosts that appeared or went away (computers are turned on and off, so "
                                     "these are often not a real change)")
        self.churn_check.toggled.connect(self.fill)
        layout.addWidget(self.churn_check)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Change", "What", "Name", "Details"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(self.on_double_click)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fill()

    def shown_changes(self):
        churn = self.churn_check.isChecked()
        return [change for change in self.changes if churn or change.what != diff.HOST
                or change.change not in (diff.ADDED, diff.REMOVED)]

    def fill(self):
        self.shown = self.shown_changes()
        self.table.setRowCount(len(self.shown))
        for row, change in enumerate(self.shown):
            for column, text in enumerate((change.change, change.what, change.name, change.detail)):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setForeground(QColor(COLORS[CHANGE_COLORS.get(change.change, "text")]))
                self.table.setItem(row, column, item)

    def on_double_click(self, item):
        change = self.shown[item.row()]
        if change.device or change.mac:
            self.show_change.emit(change)


class HostDialog(QDialog):
    """Add a host by hand (one that's off or unplugged while mapping), or edit one."""

    def __init__(self, network_map, host=None, device=None, port="", parent=None):
        super().__init__(parent)
        self.network_map, self.host = network_map, host
        self.setWindowTitle("Edit Host" if host else "Add Host")
        self.resize(420, 0)
        layout = QVBoxLayout(self)
        if host is None:
            note = QLabel("For a device that's turned off or unplugged: it's kept on the map, marked as added by "
                          "hand, and carried over when you map again.")
            note.setWordWrap(True)
            layout.addWidget(note)
        form = QFormLayout()
        self.device_combo = QComboBox()
        for item in sorted(network_map.devices.values(), key=lambda item: item.label.lower()):
            self.device_combo.addItem(item.label, item.key)
        chosen = host.device if host else device
        if chosen:
            self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(chosen)))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.device_combo.currentIndexChanged.connect(self.fill_ports)
        self.fill_ports()
        self.port_combo.setEditText(host.port if host else port)
        self.name_input = QLineEdit(host.name if host else "")
        self.ip_input = QLineEdit(host.ip if host else "")
        self.mac_input = QLineEdit(host.mac if host else "")
        self.mac_input.setPlaceholderText("Such as 00-1A-2B-3C-4D-5E")
        self.vlan_input = QSpinBox()
        self.vlan_input.setRange(0, 4094)
        self.vlan_input.setSpecialValueText("None")
        self.vlan_input.setValue(host.vlan if host else 0)
        self.note_input = QLineEdit(host.note if host else "")
        form.addRow("Switch:", self.device_combo)
        form.addRow("Port:", self.port_combo)
        form.addRow("Name:", self.name_input)
        form.addRow("IP address:", self.ip_input)
        form.addRow("MAC address:", self.mac_input)
        form.addRow("VLAN:", self.vlan_input)
        form.addRow("Note:", self.note_input)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def fill_ports(self):
        """The ports the chosen switch is known to have: its links and the ports hosts are on."""
        key = self.device_combo.currentData()
        text = self.port_combo.currentText()
        ports = {link.port_on(key) for link in self.network_map.links_of(key)}
        ports |= set(self.network_map.hosts_by_port(key))
        self.port_combo.clear()
        self.port_combo.addItems(sorted((port for port in ports if port), key=port_sort_key))
        self.port_combo.setEditText(text)

    def values(self):
        """The host as entered. Raises ValueError for anything that isn't valid."""
        key = self.device_combo.currentData()
        if not key:
            raise ValueError("Choose the switch it's plugged into.")
        port = short_port(self.port_combo.currentText().strip())
        if not port:
            raise ValueError("Enter the port it's plugged into, such as Gi1/0/12.")
        name, ip, mac_text = (self.name_input.text().strip(), self.ip_input.text().strip(),
                              self.mac_input.text().strip())
        if not (name or ip or mac_text):
            raise ValueError("Enter at least a name, an IP address or a MAC address.")
        if ip:
            try:
                ip = str(ipaddress.ip_address(ip))
            except ValueError:
                raise ValueError(f"'{ip}' isn't an IP address.") from None
        mac = format_mac(mac_text)
        if mac_text and not mac:
            raise ValueError(f"'{mac_text}' isn't a MAC address.")
        for other in self.network_map.hosts:
            if other is not self.host and mac and other.mac == mac:
                device = self.network_map.devices.get(other.device)
                raise ValueError(f"{mac} is already on the map, on {device.label if device else other.device} "
                                 f"{other.port}.")
        host = Host(mac=mac, device=key, port=port, ip=ip, vendor=vendor(mac) if mac else "",
                    vlan=self.vlan_input.value(), name=name, note=self.note_input.text().strip(),
                    manual=self.host.manual if self.host else True)
        if self.host is not None:
            host.platform = self.host.platform
        return host

    def accept(self):
        try:
            self.values()
        except ValueError as error:
            QMessageBox.warning(self, self.windowTitle(), str(error))
            return
        super().accept()
