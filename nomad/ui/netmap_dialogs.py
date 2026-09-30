"""Network Map settings: the SNMP community strings to try, and how far the crawl may go."""
import ipaddress

from PyQt5.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, \
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, \
    QTableWidgetItem, QVBoxLayout

from ..netmap.crawl import parse_networks
from ..snmp import VERSIONS, community_is_valid


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
    def __init__(self, scope, max_hops, max_devices, collect_hosts, parent=None):
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
        form.addRow("Hops:", self.hops_input)
        form.addRow("Devices to ask, at most:", self.devices_input)
        form.addRow(self.hosts_check)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        """(scope lines, max hops, max devices, collect hosts). Raises ValueError for a bad subnet."""
        lines = [line.strip() for line in self.scope_input.toPlainText().splitlines() if line.strip()]
        parse_networks(lines)
        return lines, self.hops_input.value(), self.devices_input.value(), self.hosts_check.isChecked()

    def accept(self):
        try:
            self.values()
        except ValueError as error:
            QMessageBox.warning(self, "Crawl Scope", str(error))
            return
        super().accept()
