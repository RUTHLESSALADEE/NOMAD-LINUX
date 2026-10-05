"""Subnet Placement page (Manage > Subnet Placement): where each subnet of a network is planned to be (the VLANs it's
linked to on the VLANs page), where the network map finds it, whether it's advertised (from the map's routing
tables, per VRF, or as someone set it), what it's for (its role: a VLAN, a point-to-point link, loopbacks, a tunnel...),
and what's wrong: an advertised subnet in two places, routers reaching it in different places, a local-only subnet
leaking into the routing tables, a subnet not where it was planned, a loopback address on two devices. Subnets
are moved here too: plan a move, start it, check it on the map, and complete it (which relinks it on the VLANs
page).

It reads the map open on the Network Map page (Read Routes Again brings its routing tables up to date) and the IP
Addresses page's databases, the tribe's or this computer's.
"""
import html
import logging

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, \
    QPushButton, QSplitter, QTableWidget, QTextBrowser, QVBoxLayout, QWidget

from ..ipam.placement import ADVERTISED, CANCELLED, DONE, IN_PROGRESS, LOCAL, MOVE_STATUSES, NOTE, OPEN, PROBLEM, \
    SEVERITY_LABELS, UNKNOWN, WARNING, PlacementStore, evaluate, move_check
from ..ipam.placement_team import TeamPlacementStore
from ..ipam.roles import ROLE_NAMES
from ..ipam.store import IpamError
from ..ipam.vlan_team import TeamVlanStore
from ..ipam.vlans import VlanStore
from ..netmap.placement import places, segment_text, vrf_text
from .common import SortableTableItem, set_hint
from .placement_dialogs import MoveDialog, ScopeDialog
from .table_filter import TableFilter
from .theme import COLORS

log = logging.getLogger(__name__)

COLUMNS = ["Subnet", "VRF", "IPAM Name", "Role", "Scope", "Planned (VLANs Page)", "On the Map", "Advertised",
           "Status", "Move"]
COL_ROLE, COL_SCOPE, COL_STATUS = 3, 4, 8
SHOW = [("Everything", None), ("Problems and warnings", "issues"), ("Advertised", ADVERTISED), ("Local", LOCAL),
        ("Moving", "moving"), ("On the map, not in this network", "outside")]
SEVERITY_COLORS = {PROBLEM: "error", WARNING: "warning", NOTE: "muted"}
SCOPE_LABELS = {ADVERTISED: "Advertised", LOCAL: "Local", UNKNOWN: "Unknown"}
LOCAL_SOURCE, TEAM_SOURCE = "local", "team"


class PlacementTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.rows = []
        self.source_key, self.network_id = None, None
        self.saved_network = ""
        self.stale = True  # Something changed while the page was hidden: worked out again when it's shown
        self.waiting = None  # Reading the routes again for this page: "read", or (VRF, subnet) to check its move
        self.init_ui()
        window.ipam_tab.tribe_synced.connect(self.on_data_changed)
        page = getattr(window, "netmap_tab", None)
        if page is not None:
            page.map_shown.connect(self.on_data_changed)
            page.routes_read.connect(self.on_routes_read)

    # ----------------------------------------------------------------- Layout

    def init_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Network:"))
        self.network_combo = QComboBox()
        self.network_combo.setMinimumWidth(240)
        self.network_combo.setToolTip("The IPAM network whose subnets to check. Advertised subnets must be unique "
                                      "within it (and within each VRF).")
        top.addWidget(self.network_combo)
        top.addWidget(QLabel("Show:"))
        self.show_combo = QComboBox()
        for label, key in SHOW:
            self.show_combo.addItem(label, key)
        top.addWidget(self.show_combo)
        top.addWidget(QLabel("Role:"))
        self.role_combo = QComboBox()
        self.role_combo.setToolTip("Only subnets with this role: what they're for, as set or as the map and IPAM "
                                   "suggest.")
        self.role_combo.addItem("Any", None)
        for key, label in ROLE_NAMES.items():
            self.role_combo.addItem(label, key)
        top.addWidget(self.role_combo)
        top.addStretch()
        self.map_label = QLabel()
        top.addWidget(self.map_label)
        self.read_button = QPushButton("Read Routes Again")
        self.read_button.setToolTip("Read the routing tables, VRFs, VLANs and interfaces of every device on the map "
                                    "again (not their neighbors or hosts), to see the network as it is now. What "
                                    "changed goes in the Network Map's Watch log.")
        top.addWidget(self.read_button)
        layout.addLayout(top)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText("Filter: subnet, name, VLAN, device or any column (Ctrl+F)")
        layout.addWidget(self.search_input)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_filter = TableFilter(self.table)
        self.table_filter.columns.widest = {5: 260, 6: 320}
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([850, 420])
        layout.addWidget(splitter, 1)
        buttons = QHBoxLayout()
        self.scope_button = QPushButton("Role and Scope...")
        self.scope_button.setToolTip("What it's for (a VLAN, a point-to-point link, loopbacks, a tunnel, other...), "
                                     "and advertised (must be in one place) or local (may be in several), over what "
                                     "the map suggests; or its places one L2 segment the map can't see.")
        self.move_button = QPushButton("Plan Move...")
        self.move_button.setProperty("accent", True)
        self.start_button = QPushButton("Start Move")
        self.check_button = QPushButton("Check Move")
        self.check_button.setToolTip("Read the routes again, then whether the map shows the move done: the subnet "
                                     "only at the new place, and every route to it leading there.")
        self.complete_button = QPushButton("Complete Move")
        self.complete_button.setToolTip("Mark the move done and relink the subnet on the VLANs page: out of the old "
                                        "VLAN, into the new one.")
        self.cancel_button = QPushButton("Cancel Move")
        self.map_button = QPushButton("Show on Map")
        self.ipam_button = QPushButton("Show in IPAM")
        for button in (self.scope_button, self.move_button, self.start_button, self.check_button,
                       self.complete_button, self.cancel_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.map_button)
        buttons.addWidget(self.ipam_button)
        layout.addLayout(buttons)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.network_combo.currentIndexChanged.connect(self.on_network_chosen)
        self.show_combo.currentIndexChanged.connect(self.fill_table)
        self.role_combo.currentIndexChanged.connect(self.fill_table)
        self.search_input.textChanged.connect(self.table_filter.set_text)
        self.table.itemSelectionChanged.connect(self.on_selection)
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.read_button.clicked.connect(self.read_routes_again)
        self.scope_button.clicked.connect(self.set_scope)
        self.move_button.clicked.connect(self.plan_move)
        self.start_button.clicked.connect(self.start_move)
        self.check_button.clicked.connect(self.check_move)
        self.complete_button.clicked.connect(self.complete_move)
        self.cancel_button.clicked.connect(self.cancel_move)
        self.map_button.clicked.connect(self.show_on_map)
        self.ipam_button.clicked.connect(self.show_in_ipam)
        self.update_buttons()

    # ----------------------------------------------------------------- Page interface

    def showEvent(self, event):
        super().showEvent(event)
        if self.window.ipam_tab.open_store():
            self.fill_networks()

    def focus_find(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def save_settings(self, settings):
        if self.network_id:
            settings.setValue("placement/network", f"{self.source_key}:{self.network_id}")

    def restore_settings(self, settings):
        self.saved_network = settings.value("placement/network", "", str)

    def shutdown(self):
        pass  # The stores are the IP Addresses page's

    def on_data_changed(self):
        """The tribe's data synced, or the map changed: work it out again now if the page is showing."""
        self.stale = True
        if self.isVisible():
            self.refresh()

    # ----------------------------------------------------------------- Where things are

    def stores(self, key=None):
        """(IPAM store, VlanStore-like, PlacementStore-like, whether placement can be changed now) for the tribe's
        data or this computer's."""
        page = self.window.ipam_tab
        key = key or self.source_key
        if key == TEAM_SOURCE and page.team is not None:
            team = page.team
            placements = TeamPlacementStore(team)
            return team, TeamVlanStore(team), placements, placements.can_change
        if page.local_store is None:
            return None, None, None, False
        return page.local_store, VlanStore(page.local_store), PlacementStore(page.local_store), True

    def network_map(self):
        page = getattr(self.window, "netmap_tab", None)
        return getattr(page, "network_map", None)

    def fill_networks(self):
        select = (f"{self.source_key}:{self.network_id}" if self.network_id else "") or self.saved_network
        page = self.window.ipam_tab
        self.network_combo.blockSignals(True)
        self.network_combo.clear()
        for key, label, store in ((TEAM_SOURCE, "Tribe", page.team), (LOCAL_SOURCE, "Local", page.local_store)):
            if store is not None:
                for network in store.networks():
                    self.network_combo.addItem(f"{network.name}  ({label})", f"{key}:{network.id}")
        index = self.network_combo.findData(select)
        if index < 0:
            index = self.best_network_index()
        if self.network_combo.count():
            self.network_combo.setCurrentIndex(max(index, 0))
        self.network_combo.blockSignals(False)
        self.on_network_chosen()

    def best_network_index(self):
        """With no network chosen before: the one holding most of the open map's subnets (else the first)."""
        network_map = self.network_map()
        if network_map is None:
            return 0
        on_map = {cidr for _, cidr in places(network_map)}
        best, best_count = 0, 0
        for index in range(self.network_combo.count()):
            key, _, network_id = self.network_combo.itemData(index).partition(":")
            ipam = self.stores(key)[0]
            count = sum(1 for subnet in ipam.subnets(network_id) if subnet.cidr in on_map) if ipam else 0
            if count > best_count:
                best, best_count = index, count
        return best

    def on_network_chosen(self):
        data = self.network_combo.currentData() or ":"
        self.source_key, _, network_id = data.partition(":")
        self.network_id = network_id or None
        self.refresh()

    def refresh(self):
        """Work everything out again: the plan, the map, and what's wrong."""
        self.stale = False
        ipam, vlans, placements, _ = self.stores()
        network_map = self.network_map()
        selected = self.selected_row()
        self.rows = []
        if ipam is not None and self.network_id:
            try:
                self.rows = evaluate(network_map, ipam, vlans, placements, self.network_id)
            except IpamError as error:  # The network was deleted meanwhile
                set_hint(self.status_label, str(error), "error")
        page = getattr(self.window, "netmap_tab", None)
        if network_map is None:
            self.map_label.setText("No network map open (open one on the Network Map page)")
        else:
            routers = sum(1 for device in network_map.devices.values() if device.routes or device.vrf_routes)
            self.map_label.setText(f"Map: {page.map_name()} · {routers} routing tables")
        self.read_button.setEnabled(network_map is not None)
        self.fill_table(select=(selected.vrf, selected.cidr) if selected else None)
        self.show_summary()

    def show_summary(self):
        if not self.network_id:
            self.summary_label.setText("No IPAM networks yet: add or import one on the IP Addresses page.")
            return
        problems = sum(1 for row in self.rows if row.severity == PROBLEM)
        warnings = sum(1 for row in self.rows if row.severity == WARNING)
        moving = sum(1 for row in self.rows if row.move is not None)
        advertised = sum(1 for row in self.rows if row.scope == ADVERTISED)
        parts = [f"{len(self.rows)} subnets", f"{advertised} advertised"]
        if problems:
            parts.append(f"<span style='color:{COLORS['error']}'>{problems} with problems</span>")
        if warnings:
            parts.append(f"<span style='color:{COLORS['warning']}'>{warnings} with warnings</span>")
        if moving:
            parts.append(f"{moving} moving")
        if not problems and not warnings and self.network_map() is not None:
            parts.append(f"<span style='color:{COLORS['success']}'>nothing wrong found</span>")
        self.summary_label.setText(" · ".join(parts))

    # ----------------------------------------------------------------- The table

    def shown_rows(self):
        mode, role = self.show_combo.currentData(), self.role_combo.currentData()
        rows = [row for row in self.rows if role is None or row.role.role == role]
        if mode == "issues":
            return [row for row in rows if row.severity in (PROBLEM, WARNING)]
        if mode in (ADVERTISED, LOCAL):
            return [row for row in rows if row.scope == mode]
        if mode == "moving":
            return [row for row in rows if row.move is not None]
        if mode == "outside":
            return [row for row in rows if row.found is not None and row.subnet is None and row.pool is None]
        return rows

    def fill_table(self, select=None):
        network_map = self.network_map()
        rows = self.shown_rows()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for number, row in enumerate(rows):
            planned = ", ".join(f"VLAN {vlan.vlan} {vlan.name} ({domain.name})".replace("  ", " ")
                                for domain, vlan in row.planned)
            found = "; ".join(segment_text(network_map, segment) for segment in row.found.segments) \
                if row.found is not None else ""
            routed = ""
            if row.found is not None and row.found.learned:
                routers = {route.device for route in row.found.learned}
                routed = f"{', '.join(row.found.protocols)} ({len(routers)})"
                if row.found.advertisers:
                    routed += " from " + ", ".join(network_map.devices[key].label for key in row.found.advertisers
                                                   if key in network_map.devices)
            scope = SCOPE_LABELS[row.scope] + (" (set)" if row.placement and row.placement.scope != "auto" else "")
            if row.placement and row.placement.one_segment:
                scope += ", one segment"
            status = SEVERITY_LABELS.get(row.severity, "OK")
            if row.severity == NOTE or not row.severity:
                status = "OK" if row.found is not None or network_map is None else "Not on the map"
            move = ""
            if row.move is not None:
                move = f"{MOVE_STATUSES[row.move.status]}: to VLAN {row.move.to_vlan}"
            name = row.subnet.name if row.subnet else (f"(in {row.pool.cidr} {row.pool.name})".replace(" )", ")")
                                                       if row.pool is not None else "")
            values = [(row.cidr, (row.network.version, int(row.network.network_address), row.network.prefixlen)),
                      (vrf_text(row.vrf), None), (name, None), (row.role.text, None), (scope, None), (planned, None),
                      (found, None), (routed, None), (status, None), (move, None)]
            for column, (text, sort_key) in enumerate(values):
                item = SortableTableItem(text, sort_key, row if column == 0 else None)
                item.setToolTip(row.role.why if column == COL_ROLE else text)
                if column == COL_STATUS and row.severity in (PROBLEM, WARNING):
                    item.setForeground(QColor(COLORS[SEVERITY_COLORS[row.severity]]))
                if column == COL_SCOPE and row.scope == UNKNOWN or column == COL_ROLE and row.role.role == "other":
                    item.setForeground(QColor(COLORS["muted"]))
                self.table.setItem(number, column, item)
        self.table.setSortingEnabled(True)
        self.table_filter.apply()
        if select is not None:
            for number in range(self.table.rowCount()):
                row = self.table.item(number, 0).data_object
                if (row.vrf, row.cidr) == select:
                    self.table.selectRow(number)
                    break
        self.on_selection()

    def selected_row(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        item = self.table.item(rows.pop(), 0)
        return item.data_object if item is not None else None

    def on_selection(self):
        row = self.selected_row()
        self.details.setHtml(self.row_html(row) if row is not None else self.help_html())
        self.update_buttons()

    def update_buttons(self):
        row = self.selected_row()
        _, _, _, can_change = self.stores()
        move = row.move if row is not None else None
        self.scope_button.setEnabled(row is not None and can_change)
        self.move_button.setEnabled(row is not None and can_change and move is None)
        self.start_button.setEnabled(can_change and move is not None and move.status == "planned")
        self.check_button.setEnabled(move is not None)
        self.complete_button.setEnabled(can_change and move is not None)
        self.cancel_button.setEnabled(can_change and move is not None)
        self.map_button.setEnabled(row is not None and row.found is not None)
        self.ipam_button.setEnabled(row is not None and row.subnet is not None)

    def show_menu(self, position):
        if self.selected_row() is None:
            return
        menu = QMenu(self)
        for button in (self.scope_button, self.move_button, self.start_button, self.check_button,
                       self.complete_button, self.cancel_button, self.map_button, self.ipam_button):
            action = menu.addAction(button.text(), button.click)
            action.setEnabled(button.isEnabled())
        menu.exec_(self.table.viewport().mapToGlobal(position))

    # ----------------------------------------------------------------- Details

    @staticmethod
    def help_html():
        return ("<p>Each subnet of the network, and each one a device on the open network map has an address in, "
                "per VRF.</p><p>Its <b>role</b> says what it's for: a VLAN's subnet, a point-to-point link or a "
                "tunnel (whose ends count as one place), loopbacks (each address on one device only), a routed "
                "port's, a container holding other subnets, or other (not known yet: nothing is expected of it). "
                "The map and IPAM suggest it, unless you set it (Role and Scope).</p>"
                "<p><b>Advertised</b> subnets (other devices have a route to them) must be in one place; "
                "<b>local</b> ones may be reused. The routing tables decide which, unless you set it (Role and "
                "Scope). A subnet only covered by a summary counts as local.</p>"
                "<p>To move a subnet to another VLAN or device: Plan Move, Start Move when the work begins, Read "
                "Routes Again once it's done on the switches, Check Move, then Complete Move, which relinks it on the "
                "VLANs page.</p>")

    def row_html(self, row):
        escape = html.escape
        network_map = self.network_map()
        devices = network_map.devices if network_map is not None else {}

        def label(key):
            return devices[key].label if key in devices else key

        parts = [f"<h3>{escape(row.cidr)}" + (f" &middot; VRF {escape(row.vrf)}" if row.vrf else "") + "</h3>"]
        if row.subnet is not None:
            details = ", ".join(part for part in (row.subnet.name, f"gateway {row.subnet.gateway}"
                                                  if row.subnet.gateway else "") if part)
            parts.append(f"<p>IPAM: {escape(details) or '(no name)'}</p>")
        elif row.pool is not None:
            parts.append(f"<p>IPAM: in the loopback subnet {escape(row.pool.cidr)} {escape(row.pool.name)}</p>")
        parts.append(f"<p><b>{escape(row.role.name)}</b>: {escape(row.role.why)}</p>")
        parts.append(f"<p><b>{SCOPE_LABELS[row.scope]}</b>: {escape(row.scope_why)}</p>")
        if row.findings:
            parts.append("<h4>Findings</h4>")
            for finding in row.findings:
                color = COLORS[SEVERITY_COLORS[finding.severity]]
                parts.append(f"<p><span style='color:{color}'>{SEVERITY_LABELS[finding.severity]}</span>: "
                             f"{escape(finding.text)}</p>")
        if row.planned:
            parts.append("<h4>Planned (VLANs page)</h4><p>" + "<br>".join(
                escape(f"VLAN {vlan.vlan} {vlan.name} in {domain.name}") for domain, vlan in row.planned) + "</p>")
        if row.found is not None:
            parts.append(f"<h4>On the map ({len(row.found.segments)} place{'s' if len(row.found.segments) != 1 else ''})"
                         "</h4><table>")
            for number, segment in enumerate(row.found.segments, start=1):
                for place in segment:
                    role = row.found.role(network_map, place.device)
                    parts.append(f"<tr><td>{number}&nbsp;</td><td>{escape(label(place.device))} {escape(place.port)}"
                                 f"&nbsp;</td><td>{escape(place.address)}/{place.prefix}&nbsp;</td>"
                                 f"<td>{escape(role)}</td></tr>")
            parts.append("</table>")
            if row.found.learned:
                parts.append(f"<h4>Routes to it ({len(row.found.learned)})</h4><table>")
                for route in sorted(row.found.learned, key=lambda route: label(route.device).lower())[:40]:
                    if route.origin is not None:
                        leads = f"place {route.origin + 1}"
                    elif route.beyond:
                        leads = f"beyond the map ({route.beyond})"
                    else:
                        leads = "?"
                    parts.append(f"<tr><td>{escape(label(route.device))}&nbsp;</td><td>{escape(route.protocol)} via "
                                 f"{escape(route.next_hop or route.port)}&nbsp;</td><td>&rarr; {escape(leads)}</td></tr>")
                parts.append("</table>")
            elif row.found.summaries:
                summary, keys = row.found.summaries[0]
                parts.append(f"<p>Covered by the summary {escape(summary)} on "
                             f"{escape(', '.join(label(key) for key in keys[:6]))}.</p>")
        if row.placement is not None:
            parts.append(f"<p>Treatment set by {escape(row.placement.modified_by)} "
                         f"({escape(row.placement.modified[:16].replace('T', ' '))} UTC)"
                         + (f": {escape(row.placement.note)}" if row.placement.note else "") + ".</p>")
        if row.move is not None:
            parts.append(self.move_html(row, row.move))
        _, _, placements, _ = self.stores()
        history = [move for move in placements.moves(self.network_id, row.cidr) if move.status not in OPEN] \
            if placements is not None and self.network_id else []
        if history:
            parts.append("<h4>Earlier moves</h4><p>" + "<br>".join(
                escape(f"{MOVE_STATUSES[move.status]} {move.finished[:10]}: {self.move_text(move)}"
                       f" ({move.modified_by})") for move in history[:10]) + "</p>")
        return "".join(parts)

    def domain_name(self, domain_id):
        _, vlans, _, _ = self.stores()
        try:
            return vlans.domain(domain_id).name if domain_id else ""
        except IpamError:
            return "(deleted domain)"

    def move_text(self, move):
        devices = self.network_map().devices if self.network_map() is not None else {}

        def end(domain_id, vlan, device):
            text = f"VLAN {vlan} in {self.domain_name(domain_id)}" if vlan else "(not linked)"
            if device:
                text += f" on {devices[device].label if device in devices else device}"
            return text

        return f"from {end(move.from_domain_id, move.from_vlan, move.from_device)} to " \
               f"{end(move.to_domain_id, move.to_vlan, move.to_device)}"

    def move_html(self, row, move):
        escape = html.escape
        done, why = move_check(row, move, self.network_map())
        color = COLORS["success" if done else "warning"]
        parts = [f"<h4>Move: {MOVE_STATUSES[move.status]}</h4>",
                 f"<p>{escape(self.move_text(move))}" + (f", {escape(move.planned_for)}" if move.planned_for else "")
                 + (f". {escape(move.note)}" if move.note else "") + "</p>",
                 f"<p>Planned by {escape(move.modified_by)}. On the map: <span style='color:{color}'>"
                 f"{'done' if done else 'not done'}</span> ({escape(why)}).</p>"]
        return "".join(parts)

    # ----------------------------------------------------------------- Actions

    def changed(self, message=""):
        """After a change: tribe changes reach the others through the sync; work the page out again."""
        if self.source_key == TEAM_SOURCE:
            self.window.ipam_tab.sync_now()
        self.refresh()
        if message:
            set_hint(self.status_label, message, "success")

    def report(self, error, action):
        QMessageBox.warning(self, "Not Changed", f"{action} wasn't made. {error}")
        if self.source_key == TEAM_SOURCE:
            self.window.ipam_tab.sync_now()

    def set_scope(self):
        row = self.selected_row()
        _, _, placements, _ = self.stores()
        if row is None:
            return
        if ScopeDialog(self, placements, self.network_id, row).exec_():
            self.changed(f"Saved the role and scope of {row.cidr}.")

    def plan_move(self):
        row = self.selected_row()
        _, vlans, placements, _ = self.stores()
        if row is None:
            return
        if MoveDialog(self, placements, vlans, self.network_id, row, self.network_map()).exec_():
            self.changed(f"Planned moving {row.cidr}. Start Move when the work begins.")

    def set_move_status(self, status, verb):
        row = self.selected_row()
        _, _, placements, _ = self.stores()
        if row is None or row.move is None:
            return
        try:
            placements.update_move(row.move.id, status=status)
        except IpamError as error:
            self.report(error, verb)
            return
        self.changed(f"{verb}: {row.cidr}.")

    def start_move(self):
        self.set_move_status(IN_PROGRESS, "Move started")

    def cancel_move(self):
        row = self.selected_row()
        if row is None or row.move is None or QMessageBox.question(
                self, "Cancel Move", f"Cancel moving {row.cidr}? It stays linked where it is on the VLANs page.") \
                != QMessageBox.Yes:
            return
        self.set_move_status(CANCELLED, "Move cancelled")

    def check_move(self):
        """Read the routes again (to see the network as it is now), then check the move when they're read."""
        row = self.selected_row()
        if row is None or row.move is None:
            return
        page = getattr(self.window, "netmap_tab", None)
        if page is not None and (page.reading_routes() or page.read_routes_again()):
            self.waiting = (row.vrf, row.cidr)
            set_hint(self.status_label, f"Reading the routes of the devices on the map to check moving {row.cidr} "
                                        "(progress is on the Network Map page)...", "info")
            return
        self.report_check(row, " (on the map as it was last read: its routes couldn't be read again now)")

    def report_check(self, row, note=""):
        done, why = move_check(row, row.move, self.network_map())
        set_hint(self.status_label, f"{row.cidr}: {'the move looks done' if done else 'not done yet'}: {why}{note}.",
                 "success" if done else "warning")

    def on_routes_read(self, read, failed):
        """Read Routes Again finished: say so if this page asked for it, or check the move it was read for."""
        waiting, self.waiting = self.waiting, None
        if waiting is None:
            return
        self.refresh()
        note = f" ({failed} device{'s' if failed != 1 else ''} didn't answer SNMP)" if failed else ""
        if waiting == "read":
            set_hint(self.status_label, f"Read the routes of {read} device{'s' if read != 1 else ''}{note}.",
                     "warning" if failed else "success")
            return
        row = next((row for row in self.rows if (row.vrf, row.cidr) == waiting), None)
        if row is None or row.move is None:
            set_hint(self.status_label, f"Read the routes again{note}, but {waiting[1]} has no open move to check now.",
                     "warning")
            return
        self.report_check(row, note)

    def complete_move(self):
        row = self.selected_row()
        _, _, placements, _ = self.stores()
        if row is None or row.move is None:
            return
        done, why = move_check(row, row.move, self.network_map())
        question = f"Complete moving {row.cidr} {self.move_text(row.move)}? It's relinked on the VLANs page."
        if not done:
            question = f"The map doesn't show the move done ({why}).\n\n{question}"
        if QMessageBox.question(self, "Complete Move", question) != QMessageBox.Yes:
            return
        try:
            placements.complete_move(row.move.id)
        except IpamError as error:
            self.report(error, "Completing the move")
            return
        self.changed(f"Moved {row.cidr}: {MOVE_STATUSES[DONE].lower()}, and relinked on the VLANs page.")

    def read_routes_again(self):
        page = getattr(self.window, "netmap_tab", None)
        if page is not None and page.read_routes_again():
            self.waiting = "read"
            set_hint(self.status_label, "Reading the routes of the devices on the map: the page updates when it's "
                                        "done (progress is on the Network Map page).", "info")

    def show_on_map(self):
        row = self.selected_row()
        page = getattr(self.window, "netmap_tab", None)
        if row is None or row.found is None or page is None:
            return
        self.window.navigator.setCurrentWidget(page)
        vlans = row.map_vlans
        if vlans:
            page.highlight_vlan(vlans[0])
        page.tabs.setCurrentWidget(page.view)
        page.view.show_devices(sorted({place.device for place in row.places}))

    def show_in_ipam(self):
        row = self.selected_row()
        if row is not None and row.subnet is not None:
            self.window.ipam_tab.go_to_subnet(self.source_key, self.network_id, row.cidr)
