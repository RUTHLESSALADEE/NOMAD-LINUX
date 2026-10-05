"""Move to Another Network (IP Addresses, a subnet's right-click): a subnet, its addresses and (if ticked) the subnets
inside it go to another IPAM network, with their role and placement; their VLAN links in the old network are dropped,
and a VLAN of the new network can be linked instead. Within the tribe's networks or this computer's, or from this
computer's into the tribe's (never the other way: nothing silently leaves the shared data)."""
import html
import logging

from PyQt5.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout

from ..ipam.network_move import check_target, inside, move, plan, to_tribe
from ..ipam.store import IpamError
from ..ipam.vlans import VlanStore
from .common import set_hint
from .theme import COLORS

log = logging.getLogger(__name__)

LOCAL, TEAM = "local", "team"
NO_VLAN = None


class MoveToNetworkDialog(QDialog):
    """page: the IP Addresses page (its stores); source and network_id: where the subnet is."""

    def __init__(self, parent, page, source, network_id, subnet):
        super().__init__(parent)
        self.page, self.source, self.network_id, self.subnet = page, source, network_id, subnet
        self.store = page.store_for(source)
        self.done = None
        self.setWindowTitle(f"Move {subnet.cidr} to Another Network")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.target_combo = QComboBox()
        here = self.store.network(network_id).name
        for key, store, label in self.targets():
            self.target_combo.addItem(label, key)
        form.addRow("From:", QLabel(html.escape(here)))
        form.addRow("To:", self.target_combo)
        nested = inside(subnet, self.store.subnets(network_id))
        self.nested_check = QCheckBox(f"Take the {len(nested)} subnet{'s' if len(nested) != 1 else ''} inside it "
                                      "along, with their addresses")
        self.nested_check.setChecked(True)
        self.nested_check.setToolTip("\n".join(f"{item.cidr} {item.name}".strip() for item in nested[:20]))
        self.nested_check.setVisible(bool(nested))
        form.addRow("", self.nested_check)
        self.vlan_combo = QComboBox()
        self.vlan_combo.setToolTip("Its VLAN links belong to the old network's VLAN domains, so they're dropped. Link "
                                   "it to a VLAN of the new network's instead (a new one is added to the domain).")
        form.addRow("VLAN there:", self.vlan_combo)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.move_button = buttons.addButton("Move", QDialogButtonBox.AcceptRole)
        self.move_button.setProperty("accent", True)
        buttons.accepted.connect(self.do_move)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.target_combo.currentIndexChanged.connect(self.refresh)
        self.nested_check.toggled.connect(self.refresh)
        self.refresh()

    def targets(self):
        """[(source:network id, store, label)]: the same source's other networks; this computer's can also go to the
        tribe's."""
        page, found = self.page, []
        sources = [(self.source, self.store)]
        if self.source == LOCAL and page.team is not None:
            sources.append((TEAM, page.team))
        for source, store in sources:
            label = "Tribe" if source == TEAM else "Local"
            for network in store.networks():
                if not (source == self.source and network.id == self.network_id):
                    found.append((f"{source}:{network.id}", store, f"{network.name}  ({label})"))
        return found

    def target(self):
        key = self.target_combo.currentData()
        if not key:
            return None, None, None
        source, _, network_id = key.partition(":")
        return source, self.page.store_for(source), network_id

    def refresh(self):
        source, store, network_id = self.target()
        problems = []
        try:
            self.plan = plan(self.store, self.network_id, self.subnet.cidr, self.nested_check.isChecked())
        except IpamError as error:
            self.plan = None
            problems.append(str(error))
        if store is None:
            problems.append("There's no other network to move it to.")
        elif self.plan is not None:
            problems += self.plan.problems
            try:
                problems += check_target(store, network_id, self.plan.cidrs)
            except IpamError as error:
                problems.append(str(error))
            if source == TEAM:
                team = self.page.team
                if not team.online:
                    problems.append("The IPAM server can't be reached: tribe networks change only while it can.")
                elif not team.server_moves_subnets:
                    problems.append("The IPAM server needs updating before it can move subnets (Tools > Tribe "
                                    "Management > Update Service, on the server).")
        self.fill_vlans(store, network_id)
        self.show_summary(source, store, network_id)
        if problems:
            set_hint(self.problem_label, " ".join(problems), "error")
        else:
            self.problem_label.setText("")
        self.move_button.setEnabled(not problems)

    def fill_vlans(self, store, network_id):
        """The new network's VLANs (and a new one with the old link's number, in each of its domains)."""
        self.vlan_combo.clear()
        self.vlan_combo.addItem("(don't link it to a VLAN)", NO_VLAN)
        if store is None:
            return
        vlans = VlanStore(getattr(store, "copy", store))
        old = sorted({vlan.vlan for _, vlan in (self.plan.links if self.plan else [])})
        choose = None
        for domain in vlans.domains():
            if domain.network_id != network_id:
                continue
            numbers = {vlan.vlan for vlan in vlans.vlans(domain.id)}
            for number in old:
                if number not in numbers:
                    self.vlan_combo.addItem(f"New VLAN {number} in {domain.name}", (domain.id, number))
                    choose = choose if choose is not None else self.vlan_combo.count() - 1
            for vlan in vlans.vlans(domain.id):
                self.vlan_combo.addItem(f"VLAN {vlan.vlan} {vlan.name} ({domain.name})".replace("  ", " "),
                                        (domain.id, vlan.vlan))
                if vlan.vlan in old and (choose is None or "New VLAN" in self.vlan_combo.itemText(choose)):
                    choose = self.vlan_combo.count() - 1  # The same number there: most likely
        self.vlan_combo.setCurrentIndex(choose or 0)
        self.vlan_combo.setEnabled(self.vlan_combo.count() > 1)

    def show_summary(self, source, store, network_id):
        move_plan = self.plan
        if move_plan is None or store is None:
            self.summary.setText("")
            return
        name = store.network(network_id).name
        parts = [f"Moves <b>{len(move_plan.subnets)}</b> subnet{'s' if len(move_plan.subnets) != 1 else ''} and "
                 f"<b>{len(move_plan.addresses)}</b> recorded address{'es' if len(move_plan.addresses) != 1 else ''} "
                 f"to <b>{html.escape(name)}</b>."]
        if move_plan.left:
            parts.append(f"{len(move_plan.left)} subnet{'s' if len(move_plan.left) != 1 else ''} inside it stay"
                         f"{'s' if len(move_plan.left) == 1 else ''}, with their addresses.")
        if move_plan.links:
            listed = ", ".join(f"VLAN {vlan.vlan} ({domain.name})" for domain, vlan in move_plan.links)
            parts.append(f"Its link to {html.escape(listed)} is dropped (that's the old network's).")
        if move_plan.roles or move_plan.placements:
            parts.append("Its role and how it's treated on Subnet Placement go with it.")
        if source == TEAM and self.source == LOCAL:
            parts.append(f"<span style='color:{COLORS['warning']}'>It leaves this computer's networks and is "
                         "shared with the tribe from now on.</span>")
        parts.append("Its history stays with the old network; the new one shows it added now.")
        self.summary.setText(" ".join(parts))

    def do_move(self):
        source, store, network_id = self.target()
        link = self.vlan_combo.currentData()
        take = self.nested_check.isChecked()
        try:
            if source == self.source and source == TEAM:
                store.move_subnet(self.network_id, self.subnet.cidr, network_id, take, link)
                self.done = self.plan  # Done on the server, as planned here
            elif source == self.source:
                self.done = move(store, self.network_id, self.subnet.cidr, network_id, take, link)
            else:
                self.done = to_tribe(self.store, store, self.network_id, self.subnet.cidr, network_id, take, link)
        except IpamError as error:
            set_hint(self.problem_label, f"Not moved: {error}", "error")
            log.info("Moving %s to another network failed: %s", self.subnet.cidr, error)
            return
        log.info("Moved %s to network %s", self.subnet.cidr, network_id)
        self.moved_to = f"{source}:{network_id}"
        self.accept()
