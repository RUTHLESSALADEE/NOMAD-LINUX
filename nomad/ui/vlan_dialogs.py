"""VLAN dialogs: editing VLAN domains and VLANs, bringing in a network map's VLANs, linking subnets whose names say
which VLAN they're in, and resolving VLAN changes the server refused."""
from dataclasses import dataclass, replace

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, \
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTableWidget, \
    QTableWidgetItem, QVBoxLayout

from ..ipam.roles import NOT_IN_VLANS
from ..ipam.store import IpamError, VlanDomain
from ..ipam.vlan_compare import ACTION_LABELS, ADD, LINK, MISSING, RENAME, apply_vlan_changes, compare_with_map
from ..ipam.vlans import ACTIVE, MAX_VLAN, STATUSES, check_number, holding_subnet, name_problem, \
    network_for_gateways, range_for, suggested_links
from ..netmap import vlans as map_vlans
from .common import set_hint
from .ipam_dialogs import FieldsEditor, _EditDialog
from .theme import COLORS

ACTION_COLORS = {ADD: "success", RENAME: "warning", LINK: "link", MISSING: "muted"}
NEW_DOMAIN = "new"


@dataclass
class Source:
    """Where VLAN domains are kept: the tribe's (through the IPAM server) or this computer's."""
    key: str  # "team" or "local", as the IPAM page names them
    label: str  # "Tribe" or "Local"
    ipam: object  # TeamStore or IpamStore: its networks and subnets
    vlans: object  # TeamVlanStore or VlanStore
    can_edit_domains: bool = True  # Tribe domains need the server
    can_edit_vlans: bool = True


def _item(text, color=None, tooltip=None):
    item = QTableWidgetItem(text)
    item.setToolTip(tooltip or text)
    if color:
        item.setForeground(QColor(COLORS[color]))
    return item


def _table(columns):
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setWordWrap(True)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _tick(chosen, data, enabled=True):
    tick = QTableWidgetItem()
    flags = Qt.ItemIsSelectable | (Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if enabled else Qt.NoItemFlags)
    tick.setFlags(flags)
    tick.setCheckState(Qt.Checked if chosen and enabled else Qt.Unchecked)
    tick.setData(Qt.UserRole, data)
    return tick


def fill_network_combo(combo, ipam, selected=""):
    combo.clear()
    combo.addItem("(none: no subnets to link)", "")
    for network in ipam.networks():
        combo.addItem(network.name, network.id)
    index = combo.findData(selected)
    combo.setCurrentIndex(max(index, 0))


class RangesEditor(QTableWidget):
    """A domain's ranges: first, last and what the block is for. Always an empty row to type a new one into."""

    def __init__(self, ranges):
        super().__init__(0, 3)
        self.setHorizontalHeaderLabels(["First", "Last", "For"])
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setMinimumHeight(120)
        for item in ranges:
            self.add_row(str(item["first"]), str(item["last"]), item.get("name", ""))
        self.add_row()

    def add_row(self, first="", last="", name=""):
        row = self.rowCount()
        self.insertRow(row)
        for column, text in enumerate((first, last, name)):
            self.setItem(row, column, QTableWidgetItem(text))

    def ranges(self):
        """[{"first", "last", "name"}]; rows without a first number are dropped. Raises IpamError for a bad one."""
        found = []
        for row in range(self.rowCount()):
            first, last, name = ((self.item(row, column).text() if self.item(row, column) else "").strip()
                                 for column in range(3))
            if not first:
                continue
            found.append({"first": check_number(first), "last": check_number(last or first), "name": name})
        return found

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        last = self.rowCount() - 1
        if last < 0 or (self.item(last, 0) and self.item(last, 0).text()):
            self.add_row()


class DomainDialog(_EditDialog):
    def __init__(self, parent, source, domain=None, name="", vtp_domain="", network_id=""):
        super().__init__(parent, "Edit VLAN Domain" if domain else f"New {source.label} VLAN Domain")
        self.source, self.domain = source, domain
        self.name_input = QLineEdit(domain.name if domain else name)
        self.name_input.setPlaceholderText("Where VLAN numbers are unique: a VTP domain, or a site's switches")
        self.network_combo = QComboBox()
        fill_network_combo(self.network_combo, source.ipam, domain.network_id if domain else network_id)
        self.network_combo.setToolTip("The IPAM network whose subnets these VLANs carry. Only read: linking a "
                                      "subnet to a VLAN changes nothing in IPAM.")
        self.vtp_input = QLineEdit(domain.vtp_domain if domain else vtp_domain)
        self.vtp_input.setPlaceholderText("Its switches' VTP domain, to match what a network map finds (optional)")
        self.description_input = QLineEdit(domain.description if domain else "")
        self.ranges_editor = RangesEditor(domain.ranges if domain else [])
        self.ranges_editor.setToolTip("Blocks of VLAN numbers set aside for a purpose (such as 100-199 for users): "
                                      "Next Free picks from one.")
        self.fields_editor = FieldsEditor(domain.fields if domain else {})
        self.form.addRow("Name:", self.name_input)
        self.form.addRow("IPAM network:", self.network_combo)
        self.form.addRow("VTP domain:", self.vtp_input)
        self.form.addRow("Description:", self.description_input)
        self.form.addRow("Ranges:", self.ranges_editor)
        self.form.addRow("Details:", self.fields_editor)
        self.finish_layout()

    def apply(self):
        values = dict(name=self.name_input.text(), network_id=self.network_combo.currentData() or "",
                      vtp_domain=self.vtp_input.text(), description=self.description_input.text().strip(),
                      ranges=self.ranges_editor.ranges(), fields=self.fields_editor.fields())
        if self.domain is None:
            return self.source.vlans.add_domain(**values)
        return self.source.vlans.update_domain(self.domain.id, **values)


class SubnetChecklist(QListWidget):
    """Subnets to tick: a click anywhere on a row ticks or unticks it (not only on its box)."""

    def __init__(self):
        super().__init__()
        self.pressed_state = None
        self.itemPressed.connect(self.remember_state)
        self.itemClicked.connect(self.toggle)

    def remember_state(self, item):
        self.pressed_state = item.checkState()

    def toggle(self, item):
        if not item.flags() & Qt.ItemIsEnabled or not item.flags() & Qt.ItemIsUserCheckable:
            return
        if item.checkState() == self.pressed_state:  # Clicked beside the box: the box didn't take it
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)


class VlanDialog(_EditDialog):
    """interfaces_of(number): the open network map's VLAN interfaces for a VLAN number, as [(address, prefix, port,
    device name)], to point out the subnets they're in. roles_of(network id): {CIDR: roles.RoleInfo} for its subnets,
    so the ones that aren't in VLANs (point-to-point links, loopbacks, tunnels...) are left out of the list at first."""

    def __init__(self, parent, source, domain, vlan=None, number=None, interfaces_of=None, roles_of=None):
        super().__init__(parent, f"Edit VLAN {vlan.vlan}" if vlan else "New VLAN")
        self.source, self.domain, self.vlan = source, domain, vlan
        self.interfaces_of = interfaces_of or (lambda number: [])
        self.roles_of = roles_of or (lambda network_id: {})
        self.number_input = QSpinBox()
        self.number_input.setRange(1, MAX_VLAN)
        self.number_input.setValue(vlan.vlan if vlan else number or source.vlans.next_free(domain.id) or 1)
        self.number_input.setEnabled(vlan is None)
        if vlan is not None:
            self.number_input.setToolTip("To change the number, add a new VLAN and delete this one.")
        self.range_label = QLabel()
        number_row = QHBoxLayout()
        number_row.addWidget(self.number_input)
        number_row.addWidget(self.range_label, 1)
        self.name_input = QLineEdit(vlan.name if vlan else "")
        self.name_input.setPlaceholderText("As the switches should call it, such as USERS")
        self.name_hint = QLabel()
        self.name_hint.setWordWrap(True)
        self.status_combo = QComboBox()
        for key, label in STATUSES.items():
            self.status_combo.addItem(label, key)
        self.status_combo.setCurrentIndex(max(self.status_combo.findData(vlan.status if vlan else ACTIVE), 0))
        self.network_combo = QComboBox()  # Only for a domain without an IPAM network
        fill_network_combo(self.network_combo, source.ipam)
        self.network_combo.setToolTip("The domain has no IPAM network yet: choose the one whose subnets its VLANs "
                                      "carry (the domain is given it when you press OK).")
        self.network_combo.setVisible(not domain.network_id)
        self.subnet_hint = QLabel()
        self.subnet_hint.setWordWrap(True)
        self.subnet_list = SubnetChecklist()
        self.subnet_list.setMinimumHeight(160)
        self.others_check = QCheckBox()
        self.others_check.setToolTip("Point-to-point links, loopbacks, tunnels, routed ports' subnets and containers "
                                     "aren't usually in a VLAN (Subnet Placement's Role and Scope says what each "
                                     "is for).")
        self.carried = set(vlan.subnets) if vlan else set()
        self.fill_subnets()
        self.description_input = QLineEdit(vlan.description if vlan else "")
        self.fields_editor = FieldsEditor(vlan.fields if vlan else {})
        self.form.addRow("VLAN:", number_row)
        self.form.addRow("Name:", self.name_input)
        self.form.addRow("", self.name_hint)
        self.form.addRow("Status:", self.status_combo)
        if not domain.network_id:
            self.form.addRow("IPAM network:", self.network_combo)
        self.form.addRow("Subnets:", self.subnet_hint)
        self.form.addRow("", self.subnet_list)
        self.form.addRow("", self.others_check)
        self.form.addRow("Description:", self.description_input)
        self.form.addRow("Details:", self.fields_editor)
        self.finish_layout()
        self.number_input.valueChanged.connect(self.on_number_changed)
        self.name_input.textChanged.connect(self.show_name_problem)
        self.network_combo.currentIndexChanged.connect(self.refill_subnets)
        self.subnet_list.itemChanged.connect(self.show_ticked)
        self.others_check.toggled.connect(self.refill_subnets)
        self.show_range()
        self.show_name_problem()

    def network_id(self):
        return self.domain.network_id or self.network_combo.currentData() or ""

    def refill_subnets(self):
        self.carried = set(self.chosen_subnets()) | (self.carried - self.listed)
        self.subnet_list.blockSignals(True)
        self.subnet_list.clear()
        self.fill_subnets()
        self.subnet_list.blockSignals(False)
        self.show_ticked()

    def fill_subnets(self):
        """The domain's network's subnets, ticked when this VLAN carries them (ones in another VLAN can't be), and
        the ones holding its VLAN interfaces on the open map first."""
        others = {cidr: vlan for vlan in self.source.vlans.vlans(self.domain.id)
                  if self.vlan is None or vlan.vlan != self.vlan.vlan for cidr in vlan.subnets}
        subnets = []
        if self.network_id():
            try:
                subnets = self.source.ipam.subnets(self.network_id())
            except IpamError:
                subnets = []
        self.listed = {subnet.cidr for subnet in subnets}
        interfaces = {}  # Subnet CIDR -> what the map has in it
        for address, prefix, port, device in self.interfaces_of(self.number_input.value()):
            subnet = holding_subnet(subnets, address)
            if subnet is not None:
                interfaces.setdefault(subnet.cidr, []).append(f"{address}/{prefix} on {device} {port}")
        roles = self.roles_of(self.network_id()) if subnets else {}
        not_vlans = [subnet for subnet in subnets if subnet.cidr in roles and roles[subnet.cidr].role in NOT_IN_VLANS
                     and subnet.cidr not in self.carried and subnet.cidr not in interfaces]
        self.others_check.setText(f"Also list the {len(not_vlans)} subnet{'s' if len(not_vlans) != 1 else ''} that "
                                  "aren't VLANs' (point-to-point links, loopbacks, tunnels...)")
        self.others_check.setVisible(bool(not_vlans))
        if not self.others_check.isChecked():
            subnets = [subnet for subnet in subnets if subnet not in not_vlans]
        if not subnets:
            item = QListWidgetItem("Choose the domain's IPAM network above to tick its subnets."
                                   if not self.network_id() else "The domain's network has no subnets.")
            item.setFlags(Qt.NoItemFlags)
            self.subnet_list.addItem(item)
        for subnet in sorted(subnets, key=lambda subnet: subnet.cidr not in interfaces):
            other = others.get(subnet.cidr)
            text = f"{subnet.cidr}  {subnet.name}".strip()
            if other is not None:
                text += f"  (VLAN {other.vlan}'s)"
            role = roles.get(subnet.cidr)
            if role is not None and role.role in NOT_IN_VLANS:
                text += f"  [{role.name}]"
            if subnet.cidr in interfaces:
                text += f"  ← VLAN interface on the map: {', '.join(interfaces[subnet.cidr][:2])}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, subnet.cidr)
            item.setFlags(Qt.ItemIsUserCheckable | (Qt.ItemIsEnabled if other is None else Qt.NoItemFlags))
            item.setCheckState(Qt.Checked if subnet.cidr in self.carried else Qt.Unchecked)
            item.setToolTip("\n".join([f"Gateway {subnet.gateway}" if subnet.gateway else ""]
                                      + interfaces.get(subnet.cidr, [])).strip())
            self.subnet_list.addItem(item)
        for cidr in sorted(self.carried - self.listed):  # No longer in IPAM: kept unless unticked
            item = QListWidgetItem(f"{cidr}  (not a subnet in IPAM now)")
            item.setData(Qt.UserRole, cidr)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked)
            self.subnet_list.addItem(item)
        self.show_ticked()

    def show_ticked(self, *_):
        ticked = len(self.chosen_subnets())
        self.subnet_hint.setText(f"Tick the subnets this VLAN carries ({ticked} ticked). Linking one changes nothing "
                                 "in IPAM.")

    def chosen_subnets(self):
        return [self.subnet_list.item(row).data(Qt.UserRole) for row in range(self.subnet_list.count())
                if self.subnet_list.item(row).data(Qt.UserRole)
                and self.subnet_list.item(row).checkState() == Qt.Checked]

    def on_number_changed(self):
        self.show_range()
        self.refill_subnets()  # Its VLAN interfaces on the map are another number's

    def show_range(self):
        number = self.number_input.value()
        block = range_for(self.domain, number)
        taken = self.vlan is None and self.source.vlans.vlan(self.domain.id, number) is not None
        text = f"in {block['first']}-{block['last']} {block['name']}".strip() if block else ""
        if taken:
            set_hint(self.range_label, f"VLAN {number} is recorded already: edit it instead.", "error")
        else:
            self.range_label.setText(text)
            self.range_label.setStyleSheet("")

    def show_name_problem(self):
        problem = name_problem(self.name_input.text().strip())
        self.name_hint.setVisible(bool(problem))
        if problem:
            set_hint(self.name_hint, problem, "warning")

    def apply(self):
        number = self.number_input.value()
        if self.vlan is None and self.source.vlans.vlan(self.domain.id, number) is not None:
            raise IpamError(f"VLAN {number} is recorded already: edit it instead.")
        subnets = self.chosen_subnets()
        if not self.domain.network_id and self.network_id():
            if not self.source.can_edit_domains:
                raise IpamError("The domain can't be given a network while the IPAM server can't be reached.")
            self.domain = self.source.vlans.update_domain(self.domain.id, network_id=self.network_id())
        return self.source.vlans.set_vlan(self.domain.id, number, self.name_input.text(),
                                          self.status_combo.currentData(), subnets,
                                          self.description_input.text().strip(), self.fields_editor.fields())


class NextFreeDialog(QDialog):
    """Pick where to take the next free VLAN number from: one of the domain's ranges, or anywhere."""

    def __init__(self, parent, source, domain):
        super().__init__(parent)
        self.setWindowTitle("Next Free VLAN")
        self.source, self.domain = source, domain
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"The lowest VLAN number {domain.name} hasn't recorded, from:"))
        self.combo = QComboBox()
        self.combo.addItem(f"Anywhere (1-{MAX_VLAN})", (1, MAX_VLAN))
        for block in domain.ranges:
            self.combo.addItem(f"{block['first']}-{block['last']} {block['name']}".strip(),
                               (block["first"], block["last"]))
        layout.addWidget(self.combo)
        self.result_label = QLabel()
        layout.addWidget(self.result_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        layout.addWidget(buttons)
        self.combo.currentIndexChanged.connect(self.show_number)
        if domain.ranges:
            self.combo.setCurrentIndex(1)
        self.show_number()

    def number(self):
        first, last = self.combo.currentData()
        return self.source.vlans.next_free(self.domain.id, first, last)

    def show_number(self):
        number = self.number()
        self.ok_button.setEnabled(number is not None)
        if number is None:
            set_hint(self.result_label, "Every number there is recorded.", "error")
        else:
            set_hint(self.result_label, f"Next free: VLAN {number}", "success")


class MapImportDialog(QDialog):
    """Bring a network map's VLANs into a domain: pick which VTP domain's VLANs and which domain (or a new one),
    then tick what to bring in."""

    COLUMNS = ["", "VLAN", "Change", "What", "Seen", "Note"]

    def __init__(self, parent, sources, network_map, map_name="", domain_key=None):
        super().__init__(parent)
        self.sources = {source.key: source for source in sources}
        self.network_map = network_map
        self.found = map_vlans.map_vlans(network_map)
        self.changes = []
        self.made = 0
        self.domain_made = None  # (source key, domain id) of the domain changed or made
        self.map_name = map_name
        self.setWindowTitle("Bring in a Network Map's VLANs")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.resize(1150, 640)
        layout = QVBoxLayout(self)
        intro = QLabel(f"VLANs the map{f' <b>{map_name}</b>' if map_name else ''} found on its switches, compared "
                       "with a VLAN domain. Ticked changes are made in the domain. Subnets are only linked when IPAM "
                       "has them; nothing in IPAM changes.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        top = QHBoxLayout()
        self.vtp_combo = QComboBox()
        for domain in map_vlans.domains(network_map) or [""]:
            count = sum(1 for item in self.found if item.domain == domain)
            self.vtp_combo.addItem(f"{domain or '(switches without a VTP domain)'} ({count} VLANs)", domain)
        self.domain_combo = QComboBox()
        self.domain_combo.setMinimumWidth(260)
        top.addWidget(QLabel("VLANs found in:"))
        top.addWidget(self.vtp_combo)
        top.addSpacing(16)
        top.addWidget(QLabel("Bring into:"))
        top.addWidget(self.domain_combo, 1)
        layout.addLayout(top)
        self.new_row = QHBoxLayout()
        self.new_name = QLineEdit()
        self.new_network = QComboBox()
        self.new_label = QLabel("New domain's name:")
        self.new_row.addWidget(self.new_label)
        self.new_row.addWidget(self.new_name, 1)
        self.network_label = QLabel("Its IPAM network:")
        self.new_network.setToolTip("The IPAM network whose subnets these VLANs carry: their VLAN interfaces are "
                                    "linked to the subnets holding their addresses. Suggested: the network holding "
                                    "most of them.")
        self.new_row.addWidget(self.network_label)
        self.new_row.addWidget(self.new_network, 1)
        layout.addLayout(self.new_row)
        bulk = QHBoxLayout()
        bulk.addStretch()
        for label, state in (("Tick All", True), ("Untick All", False)):
            button = QPushButton(label)
            button.clicked.connect(self.tick_all if state else self.untick_all)
            bulk.addWidget(button)
        layout.addLayout(bulk)
        self.table = _table(self.COLUMNS)
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
        self.vtp_combo.currentIndexChanged.connect(self.on_vtp_chosen)
        self.domain_combo.currentIndexChanged.connect(self.on_domain_chosen)
        self.new_network.currentIndexChanged.connect(self.compare)
        self.table.itemChanged.connect(self.on_ticked)
        self.preferred = domain_key
        self.on_vtp_chosen()

    def fill_domains(self):
        """Existing domains (those matching the VTP domain first), then a new one for each place."""
        vtp = self.vtp_combo.currentData() or ""
        self.domain_combo.blockSignals(True)
        self.domain_combo.clear()
        entries = []
        for source in self.sources.values():
            matching = {domain.id for domain in source.vlans.domains_for_vtp(vtp)}
            for domain in source.vlans.domains():
                entries.append((domain.id not in matching, f"{domain.name} ({source.label})",
                                (source.key, domain.id)))
        for _, label, data in sorted(entries, key=lambda entry: (entry[0], entry[1].lower())):
            self.domain_combo.addItem(label, data)
        for source in self.sources.values():
            if source.can_edit_domains:
                self.domain_combo.addItem(f"New {source.label.lower()} domain...", (source.key, NEW_DOMAIN))
        wanted = self.preferred if self.preferred is not None else self.domain_combo.itemData(0)
        index = self.domain_combo.findData(wanted)
        self.domain_combo.setCurrentIndex(max(index, 0))
        self.domain_combo.blockSignals(False)

    def on_vtp_chosen(self):
        self.fill_domains()
        self.new_name.setText(self.vtp_combo.currentData() or (f"{self.map_name} VLANs" if self.map_name else ""))
        self.on_domain_chosen()

    def found_here(self):
        """The map's VLANs in the VTP domain chosen."""
        vtp = self.vtp_combo.currentData() or ""
        return [item for item in self.found if item.domain == vtp]

    def chosen(self):
        """(Source, VlanDomain) chosen; a new domain is one with no id yet. A domain without an IPAM network has the
        one chosen for it here (given it when the changes are applied)."""
        data = self.domain_combo.currentData()
        if data is None:
            return None, None
        source = self.sources[data[0]]
        if data[1] == NEW_DOMAIN:
            return source, VlanDomain("", self.new_name.text().strip(), self.new_network.currentData() or "",
                                      self.vtp_combo.currentData() or "")
        domain = source.vlans.domain(data[1])
        if not domain.network_id and self.new_network.isVisibleTo(self):
            domain = replace(domain, network_id=self.new_network.currentData() or "")
        return source, domain

    def on_domain_chosen(self):
        data = self.domain_combo.currentData()
        if data is None:
            return
        source = self.sources[data[0]]
        new = data[1] == NEW_DOMAIN
        needs_network = new or not source.vlans.domain(data[1]).network_id
        if needs_network:  # Suggest the network holding most of the VLAN interfaces found
            self.new_network.blockSignals(True)
            fill_network_combo(self.new_network, source.ipam)
            gateways = [gateway for item in self.found_here() for gateway in item.gateways]
            suggested = network_for_gateways(source.ipam, gateways)
            if suggested is not None:
                self.new_network.setCurrentIndex(max(self.new_network.findData(suggested.id), 0))
            self.new_network.blockSignals(False)
        for widget in (self.new_label, self.new_name):
            widget.setVisible(new)
        self.network_label.setText("Its IPAM network:" if new else "The domain has no IPAM network; give it:")
        for widget in (self.network_label, self.new_network):
            widget.setVisible(needs_network)
        self.compare()

    def compare(self):
        source, domain = self.chosen()
        self.changes = compare_with_map(source.vlans, source.ipam, domain, self.found_here()) \
            if domain is not None else []
        self.fill()

    def fill(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.changes))
        for row, change in enumerate(self.changes):
            self.table.setItem(row, 0, _tick(change.chosen, change, change.can_apply))
            self.table.setItem(row, 1, _item(str(change.vlan)))
            self.table.setItem(row, 2, _item(ACTION_LABELS[change.action], ACTION_COLORS[change.action]))
            self.table.setItem(row, 3, _item(change.describe()))
            self.table.setItem(row, 4, _item(change.detail, "muted"))
            self.table.setItem(row, 5, _item(change.note, "muted"))
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        for column, widest in ((3, 380), (4, 260)):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), widest))
        self.update_state()

    def on_ticked(self, item):
        if item.column() == 0:
            item.data(Qt.UserRole).chosen = item.checkState() == Qt.Checked
            self.update_state()

    def tick_all(self):
        self.set_all(True)

    def untick_all(self):
        self.set_all(False)

    def set_all(self, state):
        for change in self.changes:
            if change.can_apply:
                change.chosen = state
        self.fill()

    def update_state(self):
        source, domain = self.chosen()
        ticked = sum(1 for change in self.changes if change.chosen and change.can_apply)
        domain_change = domain is not None and (not domain.id or self.network_to_give(source, domain))
        allowed = source is not None and (source.can_edit_domains if domain_change else source.can_edit_vlans)
        self.apply_button.setEnabled(bool(ticked) and allowed)
        if not self.changes:
            set_hint(self.status_label, "Nothing to bring in: the domain already has these VLANs." if self.found
                     else "The map has no VLANs.", "info")
        elif not allowed:
            set_hint(self.status_label, "The IPAM server can't be reached, so a tribe domain can't be made or given "
                                        "a network now.", "warning")
        else:
            set_hint(self.status_label, f"{ticked} of {len(self.changes)} ticked.", "info")

    @staticmethod
    def network_to_give(source, domain):
        """The network chosen here for an existing domain that has none, or ""."""
        return domain.network_id if domain.id and not source.vlans.domain(domain.id).network_id else ""

    def apply(self):
        source, domain = self.chosen()
        try:
            if not domain.id:
                domain = source.vlans.add_domain(domain.name or "Map VLANs", domain.network_id, domain.vtp_domain)
            elif self.network_to_give(source, domain):
                domain = source.vlans.update_domain(domain.id, network_id=domain.network_id)
            self.made = apply_vlan_changes(source.vlans, domain.id, self.changes)
        except IpamError as error:
            set_hint(self.status_label, f"Not brought in: {error}", "error")
            return
        self.domain_made = (source.key, domain.id)
        self.accept()


class LinkSuggestionsDialog(QDialog):
    """Subnets of the domain's network whose names (or details) say which VLAN they're in: tick the ones to link.
    The subnets themselves are left exactly as they are."""

    def __init__(self, parent, source, domain, roles=None):
        super().__init__(parent)
        self.source, self.domain = source, domain
        self.made = 0
        roles = roles or {}  # {CIDR: roles.RoleInfo}: subnets that aren't VLANs' aren't suggested
        self.setWindowTitle(f"Link Subnets to {domain.name}'s VLANs")
        self.resize(900, 480)
        layout = QVBoxLayout(self)
        intro = QLabel("These subnets say which VLAN they're in, by name (such as Vlan 6) or in a detail. Ticked ones "
                       "are linked to that VLAN (added to the domain if it isn't there yet). IPAM isn't changed: the "
                       "subnets keep their names, and export to the workbook as before.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.suggestions = suggested_links(source.ipam, source.vlans, domain,
                                           skip={cidr for cidr, role in roles.items() if role.role in NOT_IN_VLANS})
        self.table = _table(["", "VLAN", "Subnet", "Why"])
        self.table.setRowCount(len(self.suggestions))
        names = {subnet.cidr: subnet.name for subnet in source.ipam.subnets(domain.network_id)} \
            if domain.network_id else {}
        for row, (number, cidr, why) in enumerate(self.suggestions):
            self.table.setItem(row, 0, _tick(True, row))
            self.table.setItem(row, 1, _item(str(number)))
            self.table.setItem(row, 2, _item(f"{cidr}  {names.get(cidr, '')}".strip()))
            self.table.setItem(row, 3, _item(why, "muted"))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        if not self.suggestions:
            set_hint(self.status_label, "No subnet in the network names a VLAN that it isn't linked to already."
                     if domain.network_id else "The domain has no IPAM network (Edit Domain to choose one).", "info")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.apply_button = buttons.addButton("Link Ticked Subnets", QDialogButtonBox.AcceptRole)
        self.apply_button.setProperty("accent", True)
        self.apply_button.setEnabled(bool(self.suggestions) and source.can_edit_vlans)
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self):
        chosen = {}
        for row, (number, cidr, _) in enumerate(self.suggestions):
            if self.table.item(row, 0).checkState() == Qt.Checked:
                chosen.setdefault(number, []).append(cidr)
        items = []
        for number, cidrs in sorted(chosen.items()):
            current = self.source.vlans.vlan(self.domain.id, number)
            items.append({"vlan": number, "name": current.name if current else "",
                          "status": current.status if current else ACTIVE,
                          "subnets": (list(current.subnets) if current else []) + cidrs,
                          "description": current.description if current else "",
                          "fields": dict(current.fields) if current else {}})
        try:
            if items:
                self.source.vlans.set_vlans(self.domain.id, items)
        except IpamError as error:
            set_hint(self.status_label, f"Not linked: {error}", "error")
            return
        self.made = sum(len(cidrs) for cidrs in chosen.values())
        self.accept()


class VlanRefusedDialog(QDialog):
    """VLAN changes made offline that the IPAM server refused (someone else changed that VLAN first): record the same
    VLAN under the next free number instead, or discard it."""

    COLUMNS = ["Domain", "VLAN", "Change", "Made", "Why it was refused"]

    def __init__(self, parent, vlans):
        super().__init__(parent)
        self.vlans = vlans
        self.setWindowTitle("Refused VLAN Changes")
        self.resize(1000, 420)
        layout = QVBoxLayout(self)
        intro = QLabel("These VLAN changes were made while the IPAM server couldn't be reached. When they were sent, "
                       "someone else had already changed those VLANs, so the server kept theirs. For each, record the "
                       "same VLAN under the next free number (in the same range), or discard it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = _table(self.COLUMNS)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.table, 1)
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        buttons = QHBoxLayout()
        self.next_free_button = QPushButton("Use Next Free Number...")
        self.next_free_button.setProperty("accent", True)
        self.discard_button = QPushButton("Discard")
        close_button = QPushButton("Close")
        buttons.addWidget(self.next_free_button)
        buttons.addWidget(self.discard_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.next_free_button.clicked.connect(self.use_next_free)
        self.discard_button.clicked.connect(self.discard)
        close_button.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.fill()

    def fill(self):
        self.entries = self.vlans.refused()
        domains = {domain.id: domain.name for domain in self.vlans.domains()}
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            data = entry["data"]
            change = f"{data.get('name') or '(no name)'}" if entry["action"] == "set_vlan" else "Delete"
            values = [domains.get(entry["domain_id"], "(deleted domain)"), str(entry["vlan"]), change,
                      entry["made"][:16].replace("T", " "), entry["error"]]
            for column, value in enumerate(values):
                self.table.setItem(row, column, _item(value))
        for column in range(4):
            self.table.resizeColumnToContents(column)
        self.table.resizeRowsToContents()
        if self.entries:
            self.table.selectRow(0)
        self.update_buttons()

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.entries[rows[0].row()] if rows else None

    def update_buttons(self):
        entry = self.selected()
        self.discard_button.setEnabled(entry is not None)
        self.next_free_button.setEnabled(entry is not None and entry["action"] == "set_vlan")

    def use_next_free(self):
        entry = self.selected()
        try:
            domain = self.vlans.domain(entry["domain_id"])
        except IpamError:
            set_hint(self.message_label, "Its domain was deleted.", "error")
            return
        block = range_for(domain, entry["vlan"])
        first, last = (block["first"], block["last"]) if block else (1, MAX_VLAN)
        number = self.vlans.next_free(domain.id, first, last)
        if number is None:
            set_hint(self.message_label, f"Every number in {first}-{last} is recorded.", "error")
            return
        name = entry["data"].get("name") or "(no name)"
        if QMessageBox.question(self, "Use Next Free Number",
                                f"Record {name} as VLAN {number} instead of {entry['vlan']}?") != QMessageBox.Yes:
            return
        try:
            self.vlans.set_vlan(domain.id, number, **entry["data"])
        except IpamError as error:
            set_hint(self.message_label, f"Couldn't record it: {error}", "error")
            return
        self.vlans.discard(entry["seq"])
        set_hint(self.message_label, f"Recorded {name} as VLAN {number}.", "success")
        self.fill()

    def discard(self):
        entry = self.selected()
        self.vlans.discard(entry["seq"])
        set_hint(self.message_label, f"Discarded the change to VLAN {entry['vlan']}.", "info")
        self.fill()

