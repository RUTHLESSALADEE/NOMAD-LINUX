"""Subnet Placement dialogs: how a subnet is treated (what it's for, advertised or local, one segment), and planning a
move."""
from PyQt5.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox

from ..ipam.placement import AUTO, SCOPES
from ..ipam.roles import ROLE_HELP, ROLE_NAMES
from ..ipam.store import IpamError
from ..ipam.vlans import MAX_VLAN
from .common import set_hint
from .ipam_dialogs import _EditDialog

ANY_DEVICE = ""


class ScopeDialog(_EditDialog):
    """What the subnet is for (its role), over what the map and IPAM suggest; advertised (one place only) or local
    (may repeat), over what the routing tables suggest; and whether its places are one L2 segment the map can't see."""

    def __init__(self, parent, placements, network_id, row):
        super().__init__(parent, f"How to Treat {row.cidr}")
        self.placements, self.network_id, self.row = placements, network_id, row
        role = row.role
        self.role_combo = QComboBox()
        self.role_combo.addItem(f"Automatic (now {ROLE_NAMES[role.detected].lower()})", AUTO)
        for key, label in ROLE_NAMES.items():
            self.role_combo.addItem(f"{label}: {ROLE_HELP[key]}", key)
        self.role_combo.setCurrentIndex(max(self.role_combo.findData(role.set.role if role.set else AUTO), 0))
        self.role_detected = QLabel(f"The map and IPAM suggest {ROLE_NAMES[role.detected].lower()}: "
                                    f"{role.detected_why}.")
        self.role_detected.setWordWrap(True)
        can_set_role = getattr(placements, "can_change_roles", True)
        self.role_combo.setEnabled(can_set_role)
        if not can_set_role:
            self.role_combo.setToolTip("The IPAM server needs updating before it can keep roles.")
        current = row.placement
        self.scope_combo = QComboBox()
        for key, label in SCOPES.items():
            self.scope_combo.addItem(label, key)
        self.scope_combo.setCurrentIndex(max(self.scope_combo.findData(current.scope if current else AUTO), 0))
        self.detected = QLabel(f"The routing tables say: {row.scope_why}." if not current or current.scope == AUTO
                               else f"Now: {row.scope_why}.")
        self.detected.setWordWrap(True)
        self.segment_check = QCheckBox("Its places are one L2 segment the map can't see (an unmanaged switch, or a "
                                       "link CDP and LLDP don't show): don't count them as separate places")
        self.segment_check.setChecked(bool(current and current.one_segment))
        self.note_input = QLineEdit(current.note if current else "")
        self.note_input.setPlaceholderText("Why (shown with it), such as: reused for printers at every site")
        self.form.addRow("It's for:", self.role_combo)
        self.form.addRow("", self.role_detected)
        self.form.addRow("Treat it as:", self.scope_combo)
        self.form.addRow("", self.detected)
        self.form.addRow("", self.segment_check)
        self.form.addRow("Note:", self.note_input)
        self.finish_layout()

    def apply(self):
        """Save what changed (each is its own change, with its own history)."""
        current, role = self.row.placement, self.role_combo.currentData()
        if role != (self.row.role.set.role if self.row.role.set else AUTO):
            self.placements.set_role(self.network_id, self.row.cidr, role)
        wanted = (self.scope_combo.currentData(), self.segment_check.isChecked(), self.note_input.text().strip())
        if wanted != ((current.scope, current.one_segment, current.note) if current else (AUTO, False, "")):
            return self.placements.set_placement(self.network_id, self.row.cidr, *wanted)
        return current


class MoveDialog(_EditDialog):
    """Plan moving a subnet: from the VLAN it's linked to (and the device it's on), to another VLAN (and device)."""

    def __init__(self, parent, placements, vlans, network_id, row, network_map=None):
        super().__init__(parent, f"Move {row.cidr}")
        self.placements, self.vlans, self.network_id, self.row = placements, vlans, network_id, row
        self.domains = [domain for domain in vlans.domains() if domain.network_id in (network_id, "")]
        devices = network_map.devices if network_map is not None else {}
        intro = QLabel(f"<b>{row.cidr}</b>{f' ({row.subnet.name})' if row.subnet and row.subnet.name else ''}. "
                       "While it moves, the page expects it at the old place or the new one, and still flags it if "
                       "it's advertised from both. Complete Move relinks it on the VLANs page.")
        intro.setWordWrap(True)
        self.layout.insertWidget(0, intro)
        self.from_combo = QComboBox()
        for domain, vlan in row.planned:
            self.from_combo.addItem(f"VLAN {vlan.vlan} {vlan.name} in {domain.name}".replace("  ", " "),
                                    (domain.id, vlan.vlan))
        self.from_combo.addItem("(not linked to a VLAN)", ("", 0))
        self.from_device = QComboBox()
        self.from_device.addItem("(any device it's on)", ANY_DEVICE)
        for place in row.places:
            label = devices[place.device].label if place.device in devices else place.device
            if self.from_device.findData(place.device) < 0:
                self.from_device.addItem(f"{label} ({place.port})", place.device)
        if self.from_device.count() == 2:
            self.from_device.setCurrentIndex(1)
        self.to_domain = QComboBox()
        for domain in self.domains:
            self.to_domain.addItem(domain.name, domain.id)
        self.to_vlan = QSpinBox()
        self.to_vlan.setRange(1, MAX_VLAN)
        self.to_vlan_label = QLabel()
        to_row = QHBoxLayout()
        to_row.addWidget(self.to_domain, 1)
        to_row.addWidget(QLabel("VLAN"))
        to_row.addWidget(self.to_vlan)
        to_row.addWidget(self.to_vlan_label, 1)
        self.to_device = QComboBox()
        self.to_device.addItem("(any device)", ANY_DEVICE)
        for key, device in sorted(devices.items(), key=lambda item: item[1].label.lower()):
            if device.interfaces_l3 or device.vlans:
                self.to_device.addItem(device.label, key)
        self.to_device.setToolTip("The device it's moving to, when it matters: the check that it's done looks for it "
                                  "there (a VLAN number can be the same at two sites).")
        self.when_input = QLineEdit()
        self.when_input.setPlaceholderText("When (a date or change window), optional")
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("Why, or a change ticket, optional")
        self.form.addRow("From:", self.from_combo)
        self.form.addRow("On:", self.from_device)
        self.form.addRow("To:", to_row)
        self.form.addRow("On:", self.to_device)
        self.form.addRow("When:", self.when_input)
        self.form.addRow("Note:", self.note_input)
        self.finish_layout()
        self.to_domain.currentIndexChanged.connect(self.show_vlan)
        self.to_vlan.valueChanged.connect(self.show_vlan)
        if not self.domains:
            set_hint(self.error_label, "There's no VLAN domain for this network yet: make one on the VLANs page.",
                     "error")
        self.show_vlan()

    def show_vlan(self):
        domain_id = self.to_domain.currentData()
        vlan = self.vlans.vlan(domain_id, self.to_vlan.value()) if domain_id else None
        if vlan is None:
            self.to_vlan_label.setText("(new: added to the domain when the move is completed)")
        else:
            self.to_vlan_label.setText(vlan.name + (f", carrying {', '.join(vlan.subnets)}" if vlan.subnets else ""))

    def apply(self):
        if not self.domains:
            raise IpamError("There's no VLAN domain for this network yet: make one on the VLANs page.")
        from_domain, from_vlan = self.from_combo.currentData()
        return self.placements.plan_move(
            self.network_id, self.row.cidr, from_domain_id=from_domain, from_vlan=from_vlan,
            from_device=self.from_device.currentData(), to_domain_id=self.to_domain.currentData(),
            to_vlan=self.to_vlan.value(), to_device=self.to_device.currentData(), planned_for=self.when_input.text(),
            note=self.note_input.text())
