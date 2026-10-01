"""The drawing on the Network Map page: devices as boxes you can drag, links with the port at each end, each
switch's hosts behind a badge that opens into one box per port, and sites and buildings as boxes round their
devices that can be collapsed into one."""
import math
import time

from PyQt5.QtCore import QLineF, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QStyleOptionGraphicsItem

from ..netmap.l3 import HOP, STAR, SUBNET
from ..netmap.layout import GROUP_PAD, GROUP_TITLE, NODE_HEIGHT, NODE_WIDTH
from ..netmap.monitor import DOWN, UNKNOWN, UP, duration_text
from ..netmap.model import AP, BUILDING, FIREWALL, GROUP_KINDS, KIND_NAMES, NO_SNMP, ROUTER, SHARED_PORT_HOSTS, SNMP, \
    SOURCE_NAMES, SWITCH, UNREACHABLE
from .theme import COLORS

KIND_COLORS = {SWITCH: COLORS["link"], ROUTER: COLORS["accent"], FIREWALL: "#ff9f43", AP: "#c792ea"}
STATUS_COLORS = {UP: COLORS["success"], DOWN: COLORS["error"], UNKNOWN: COLORS["muted"]}
KIND_TAGS = {SWITCH: "SW", ROUTER: "RTR", FIREWALL: "FW", AP: "AP"}
STRIP_WIDTH = 34
BADGE_HEIGHT = 20
PORT_WIDTH = 210
PORT_HEADER = 18
LINE_HEIGHT = 14
HOST_LINES = 6  # Hosts listed in a port's box; the rest are counted (and in its tooltip and the Hosts tab)
PORT_COLUMNS = 4
PORT_GAP = 12
PROTOCOL_NAMES = {"cdp": "CDP", "lldp": "LLDP", "l3": "address on the subnet", "icmp": "traceroute"}
ZOOM_STEP = 1.15
MIN_ZOOM, MAX_ZOOM = 0.05, 4.0
LABEL_MIN_ZOOM = 0.5  # Port labels are left off below this zoom
COLLAPSED_WIDTH, COLLAPSED_HEIGHT = 200, 64
GROUP_TAGS = {BUILDING: "BLDG"}


def small_font(scale=0.85, bold=False):
    font = QFont()
    font.setPointSizeF(max(6.0, font.pointSizeF() * scale))
    font.setBold(bold)
    return font


def elided(text, font, width):
    return QFontMetrics(font).elidedText(text, Qt.ElideRight, int(width))


def host_line(host):
    """A host in its port's box: its name and address (or MAC), then its VLAN."""
    names = [part for part in (host.name, host.ip) if part] or [host.mac or "?"]
    return "  ".join(names), f"VLAN {host.vlan}" if host.vlan else "VLAN ?"


def port_height(hosts):
    """A port box's height: a header, a line per host (up to HOST_LINES), and a "more" line."""
    lines = min(len(hosts), HOST_LINES) + (1 if len(hosts) > HOST_LINES else 0)
    return PORT_HEADER + lines * LINE_HEIGHT + 6


def host_tooltip(port, hosts):
    lines = [f"{port}:"]
    for host in hosts[:40]:
        parts = [host.mac, host.ip, host.name, host.vendor, f"VLAN {host.vlan}" if host.vlan else "",
                 "(added by hand)" if host.manual else "", host.note]
        lines.append("  " + "  ".join(part for part in parts if part))
    if len(hosts) > 40:
        lines.append(f"  ...and {len(hosts) - 40} more (see the Hosts tab)")
    return "\n".join(lines)


class NodeItem(QGraphicsItem):
    """Something on the map that links join and the user can drag: a device, or on the logical view a subnet or a
    hop traceroute found."""

    def __init__(self, key, label, view):
        super().__init__()
        self.key, self.label, self.view = key, label, view
        self.links = []
        self.port_items = []
        self.host_count = 0
        self.expanded = False
        self.highlight = None  # Colour of a ring drawn round it (Compare's added and changed devices)
        self.group_item = None  # The GroupItem it's directly in
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(2)

    def set_highlight(self, color):
        self.highlight = color
        self.update()

    def draw_highlight(self, painter, rect, radius):
        if self.highlight:
            ring = QPainterPath()
            ring.addRoundedRect(rect.adjusted(-6, -6, 6, 6), radius + 4, radius + 4)
            color = QColor(self.highlight)
            color.setAlpha(170)
            painter.setPen(QPen(color, 4))
            painter.drawPath(ring)

    def center(self):
        return self.pos()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for link in self.links:
                link.update_position()
            if self.group_item is not None and not self.view.groups_suspended:
                self.group_item.update_rect()
        elif change == QGraphicsItem.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self.view.begin_node_drag(self)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self.view.node_dragged(self)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.view.on_item_moved()


class DeviceItem(NodeItem):
    def __init__(self, device, host_ports, view):
        super().__init__(device.key, device.label, view)
        self.device, self.host_ports = device, host_ports
        self.monitor_state = None  # A monitor.DeviceStatus while the device is monitored
        self.rect = QRectF(-NODE_WIDTH / 2, -NODE_HEIGHT / 2, NODE_WIDTH, NODE_HEIGHT)
        self.host_count = sum(len(hosts) for hosts in host_ports.values())
        self.badge = QRectF(-45, NODE_HEIGHT / 2 + 4, 90, BADGE_HEIGHT) if self.host_count else QRectF()
        tip = [device.label, KIND_NAMES.get(device.kind, device.kind), device.mgmt_ip, device.platform,
               SOURCE_NAMES.get(device.source, device.source), device.error]
        if self.host_count:
            tip.append(f"{self.host_count} hosts: double-click to show them by port")
        self.setToolTip("\n".join(part for part in tip if part))

    def boundingRect(self):
        return self.rect.adjusted(-9, -9, 9, 9).united(self.badge.adjusted(-2, -2, 2, 2))

    def paint(self, painter, option, widget=None):
        device = self.device
        color = QColor(KIND_COLORS.get(device.kind, COLORS["muted"]))
        painter.setRenderHint(QPainter.Antialiasing)
        self.draw_highlight(painter, self.rect, 7)
        outline = QColor(COLORS["error"]) if device.source == UNREACHABLE else color
        pen = QPen(outline, 3 if self.isSelected() else 1.6)
        if self.isSelected():
            pen.setColor(QColor(COLORS["accent_hover"]))
        if device.source != SNMP:
            pen.setStyle(Qt.DashLine)
        path = QPainterPath()
        path.addRoundedRect(self.rect, 7, 7)
        painter.fillPath(path, QColor(COLORS["panel"]))
        strip = QPainterPath()
        strip.addRoundedRect(QRectF(self.rect.left(), self.rect.top(), STRIP_WIDTH, self.rect.height()), 7, 7)
        painter.save()
        painter.setClipRect(QRectF(self.rect.left(), self.rect.top(), STRIP_WIDTH, self.rect.height()))
        faded = QColor(outline)
        faded.setAlpha(60)
        painter.fillPath(strip, faded)
        painter.restore()
        painter.setPen(pen)
        painter.drawPath(path)

        painter.setPen(QColor(outline))
        painter.setFont(small_font(0.75, bold=True))
        painter.drawText(QRectF(self.rect.left(), self.rect.top(), STRIP_WIDTH, self.rect.height()), Qt.AlignCenter,
                         KIND_TAGS.get(device.kind, "?"))
        state = self.monitor_state
        if state is not None and state.status == DOWN:  # Tint the box: it's the one to look at
            tint = QColor(COLORS["error"])
            tint.setAlpha(45)
            painter.fillPath(path, tint)
        left = self.rect.left() + STRIP_WIDTH + 6
        width = self.rect.right() - left - (18 if state is not None else 5)
        name_font = small_font(0.95, bold=True)
        painter.setFont(name_font)
        painter.setPen(QColor(COLORS["error"] if device.source == UNREACHABLE else COLORS["text"]))
        painter.drawText(QRectF(left, self.rect.top() + 4, width, 18), Qt.AlignLeft | Qt.AlignVCenter,
                         elided(device.label, name_font, width))
        detail_font = small_font(0.8)
        painter.setFont(detail_font)
        painter.setPen(QColor(COLORS["muted"]))
        address = device.mgmt_ip if device.mgmt_ip != device.label else ""
        if state is not None and state.status == UP and state.rtt is not None:
            address = f"{address}  ·  {'<1' if state.rtt < 1 else state.rtt} ms".strip(" ·")
        lines = [(address, COLORS["muted"])]
        if state is not None and state.status == DOWN:
            lines.append((f"Down for {duration_text(time.time() - state.since)}", COLORS["error"]))
        elif device.source in (NO_SNMP, UNREACHABLE):
            lines.append((SOURCE_NAMES[device.source], COLORS["warning" if device.source == NO_SNMP else "error"]))
        else:
            lines.append((device.platform or (device.sys_descr.splitlines()[0] if device.sys_descr else ""),
                          COLORS["muted"]))
        for row, (line, line_color) in enumerate((line, color) for line, color in lines if line):
            painter.setPen(QColor(line_color))
            painter.drawText(QRectF(left, self.rect.top() + 22 + row * 15, width, 15), Qt.AlignLeft | Qt.AlignVCenter,
                             elided(line, detail_font, width))

        if state is not None:
            self.draw_status_dot(painter, state)

        if self.host_count:
            painter.setPen(QPen(QColor(COLORS["border"]), 1))
            badge = QPainterPath()
            badge.addRoundedRect(self.badge, BADGE_HEIGHT / 2, BADGE_HEIGHT / 2)
            painter.fillPath(badge, QColor(COLORS["panel_alt"]))
            painter.drawPath(badge)
            painter.setPen(QColor(COLORS["muted"]))
            painter.setFont(small_font(0.78))
            arrow = "▴" if self.expanded else "▾"
            painter.drawText(self.badge, Qt.AlignCenter, f"{arrow} {self.host_count} host"
                             f"{'' if self.host_count == 1 else 's'}")

    def mouseDoubleClickEvent(self, event):
        self.view.toggle_hosts(self)
        event.accept()

    def draw_status_dot(self, painter, state):
        """Monitoring: green when it answers ping, red when it's down, an empty ring until it's been checked."""
        center = QPointF(self.rect.right() - 10, self.rect.top() + 10)
        color = QColor(STATUS_COLORS[state.status])
        painter.setPen(QPen(color, 1.5))
        painter.setBrush(color if state.status != UNKNOWN else Qt.NoBrush)
        painter.drawEllipse(center, 4.5, 4.5)
        painter.setBrush(Qt.NoBrush)

    def set_expanded(self, expanded):
        if expanded == self.expanded:
            return
        self.expanded = expanded
        for item in self.port_items:
            self.scene().removeItem(item)
        self.port_items = []
        if expanded:
            ports = list(self.host_ports.items())
            columns = min(len(ports), PORT_COLUMNS)
            rows = [ports[start:start + columns] for start in range(0, len(ports), columns)]
            row_heights = [max(port_height(hosts) for _, hosts in row) for row in rows]
            width = columns * (PORT_WIDTH + PORT_GAP) - PORT_GAP
            top = self.clear_space(width, sum(row_heights) + PORT_GAP * len(rows))
            for row, row_height in zip(rows, row_heights):
                for column, (port, hosts) in enumerate(row):
                    item = HostPortItem(self, port, hosts)
                    item.setPos(-width / 2 + PORT_WIDTH / 2 + column * (PORT_WIDTH + PORT_GAP), top)
                    item.set_anchor()
                    self.port_items.append(item)
                top += row_height + PORT_GAP
        self.update()
        if self.group_item is not None:
            self.group_item.update_rect()

    def clear_space(self, width, height):
        """How far below the device (in its coordinates) a block of port boxes fits without covering other
        devices (such as the access points under a switch) or other switches' port boxes."""
        top = NODE_HEIGHT / 2 + BADGE_HEIGHT + 24
        others = [item.sceneBoundingRect() for item in self.scene().items()
                  if isinstance(item, DeviceItem) and item is not self]
        others += [item.mapRectToScene(item.rect).adjusted(-PORT_GAP, -PORT_GAP, PORT_GAP, PORT_GAP)
                   for item in self.scene().items()  # Other switches' hosts, when several are open
                   if isinstance(item, HostPortItem) and item.parentItem() is not self]
        for _ in range(40):
            block = QRectF(self.pos().x() - width / 2, self.pos().y() + top, width, height)
            if not any(block.intersects(other) for other in others):
                break
            top += NODE_HEIGHT / 2
        return top


class HostPortItem(QGraphicsItem):
    """One switch port's hosts, shown when the switch's hosts are opened. A child of the switch, so it moves with it."""

    def __init__(self, parent, port, hosts):
        super().__init__(parent)
        self.port, self.hosts = port, hosts
        self.rect = QRectF(-PORT_WIDTH / 2, 0, PORT_WIDTH, port_height(hosts))  # Hangs from its top middle
        self.anchor = QPointF()
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setToolTip(host_tooltip(port, hosts))

    def set_anchor(self):
        """Where the connector meets the switch's badge, in this item's coordinates."""
        self.prepareGeometryChange()
        self.anchor = QPointF(-self.pos().x(), -self.pos().y() + NODE_HEIGHT / 2 + BADGE_HEIGHT + 4)

    def boundingRect(self):
        return self.rect.adjusted(-2, -2, 2, 2).united(QRectF(self.anchor, QSizeF(1, 1)).adjusted(-2, -2, 2, 2))

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(COLORS["border"]), 1, Qt.DotLine))
        painter.drawLine(QPointF(0, self.rect.top()), self.anchor)
        shared = len(self.hosts) > SHARED_PORT_HOSTS
        pen = QPen(QColor(COLORS["accent_hover"] if self.isSelected() else
                          COLORS["warning"] if shared else COLORS["border"]), 2 if self.isSelected() else 1)
        if all(host.manual for host in self.hosts):
            pen.setStyle(Qt.DashLine)  # Only hosts added by hand: not seen by the crawl
        path = QPainterPath()
        path.addRoundedRect(self.rect, 5, 5)
        painter.fillPath(path, QColor(COLORS["panel_alt"]))
        painter.setPen(pen)
        painter.drawPath(path)
        left, width = self.rect.left() + 6, PORT_WIDTH - 12
        bold = small_font(0.78, bold=True)
        painter.setFont(bold)
        painter.setPen(QColor(COLORS["text"]))
        header = QRectF(left, self.rect.top() + 2, width, PORT_HEADER - 2)
        painter.drawText(header, Qt.AlignLeft | Qt.AlignVCenter, self.port)
        if len(self.hosts) > 1:
            painter.setFont(small_font(0.72))
            painter.setPen(QColor(COLORS["warning"] if shared else COLORS["muted"]))
            painter.drawText(header, Qt.AlignRight | Qt.AlignVCenter,
                             f"{len(self.hosts)} hosts" + (": unmanaged switch?" if shared else ""))
        regular, vlan_font = small_font(0.74), small_font(0.7, bold=True)
        vlan_width = QFontMetrics(vlan_font).horizontalAdvance("VLAN 4094") + 4
        top = self.rect.top() + PORT_HEADER
        for host in self.hosts[:HOST_LINES]:
            name, vlan = host_line(host)
            font = QFont(regular)
            font.setItalic(host.manual)  # Added by hand
            painter.setFont(font)
            painter.setPen(QColor(COLORS["muted"] if host.manual else COLORS["text"]))
            painter.drawText(QRectF(left, top, width - vlan_width, LINE_HEIGHT), Qt.AlignLeft | Qt.AlignVCenter,
                             elided(name, font, width - vlan_width - 4))
            painter.setFont(vlan_font)
            painter.setPen(QColor(COLORS["link"] if host.vlan else COLORS["muted"]))
            painter.drawText(QRectF(left + width - vlan_width, top, vlan_width, LINE_HEIGHT),
                             Qt.AlignRight | Qt.AlignVCenter, vlan)
            top += LINE_HEIGHT
        if len(self.hosts) > HOST_LINES:
            painter.setFont(regular)
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(left, top, width, LINE_HEIGHT), Qt.AlignLeft | Qt.AlignVCenter,
                             f"+ {len(self.hosts) - HOST_LINES} more (see the tooltip or the Hosts tab)")


class SimpleNodeItem(NodeItem):
    """A subnet, a router only traceroute found, an unanswered hop (*) or this computer, on the logical view."""

    def __init__(self, node, view):
        super().__init__(node.key, node.label, view)
        self.node = node
        if node.kind == SUBNET:
            self.rect = QRectF(-NODE_WIDTH / 2 + 10, -18, NODE_WIDTH - 20, 36)
        elif node.kind == STAR:
            self.rect = QRectF(-16, -16, 32, 32)
        else:
            self.rect = QRectF(-NODE_WIDTH / 2 + 20, -22, NODE_WIDTH - 40, 44)
        self.setToolTip("\n".join(part for part in (node.label, node.detail) if part))

    def boundingRect(self):
        return self.rect.adjusted(-9, -9, 9, 9)

    def paint(self, painter, option, widget=None):
        node = self.node
        painter.setRenderHint(QPainter.Antialiasing)
        radius = self.rect.height() / 2 if node.kind in (SUBNET, STAR) else 6
        self.draw_highlight(painter, self.rect, radius)
        path = QPainterPath()
        path.addRoundedRect(self.rect, radius, radius)
        painter.fillPath(path, QColor(COLORS["panel_alt"] if node.kind == SUBNET else COLORS["panel"]))
        color = QColor(COLORS["accent_hover"] if self.isSelected() else
                       COLORS["link"] if node.kind == SUBNET else COLORS["muted"])
        pen = QPen(color, 3 if self.isSelected() else 1.4)
        if node.kind in (HOP, STAR):
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawPath(path)
        bold = small_font(0.9, bold=True)
        painter.setFont(bold)
        painter.setPen(QColor(COLORS["text"]))
        if node.kind == STAR or not node.detail:
            painter.drawText(self.rect, Qt.AlignCenter, elided(node.label, bold, self.rect.width() - 10))
            return
        painter.drawText(self.rect.adjusted(5, 3, -5, -self.rect.height() / 2), Qt.AlignCenter,
                         elided(node.label, bold, self.rect.width() - 10))
        detail = small_font(0.75)
        painter.setFont(detail)
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(self.rect.adjusted(5, self.rect.height() / 2, -5, -3), Qt.AlignCenter,
                         elided(node.detail, detail, self.rect.width() - 10))


class GroupItem(QGraphicsItem):
    """A site or building: a box round its devices (and a site's buildings) with its name on a title bar, or when
    collapsed one box standing for all of them. Drag the title bar to move everything in it; double-click it to
    collapse or expand. Clicks inside the box (off the title bar) go to the background, so it can still be dragged
    to move around."""

    def __init__(self, group, view):
        super().__init__()
        self.group, self.key, self.view = group, group.key, view
        self.members = []  # DeviceItems directly in it
        self.children = []  # The GroupItems of a site's buildings
        self.parent_group = None
        self.links = []  # LinkItems drawn to it while it's collapsed
        self.rect = QRectF()
        self.frozen = False  # Kept still while devices are dragged, so they can be dropped out of it
        self.drop_target = False
        self.drag_from = None
        self.dragged = False
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setZValue(-1 if group.kind == BUILDING else -2)

    @property
    def label(self):
        return self.group.name

    def all_members(self):
        """Its devices and its buildings' devices."""
        return self.members + [item for child in self.children for item in child.all_members()]

    def center(self):
        return self.rect.center()

    def member_rect(self, item):
        rect = item.sceneBoundingRect()
        if item.port_items:  # Its hosts' boxes, while they're showing
            rect = rect.united(item.mapRectToScene(item.childrenBoundingRect()))
        return rect

    def expanded_rect(self):
        """The box round everything in it, as if it weren't collapsed."""
        area = QRectF()
        for item in self.members:
            area = area.united(self.member_rect(item))
        for child in self.children:
            area = area.united(child.expanded_rect() if not child.group.collapsed else child.rect)
        if area.isNull():
            return area
        return area.adjusted(-GROUP_PAD, -GROUP_PAD - GROUP_TITLE, GROUP_PAD, GROUP_PAD)

    def update_rect(self, propagate=True):
        if self.frozen:
            return
        if self.group.collapsed:
            items = self.all_members()
            area = QRectF()
            for item in items:
                area = area.united(QRectF(item.pos().x() - NODE_WIDTH / 2, item.pos().y() - NODE_HEIGHT / 2,
                                          NODE_WIDTH, NODE_HEIGHT))
            rect = QRectF(0, 0, COLLAPSED_WIDTH, COLLAPSED_HEIGHT)
            rect.moveCenter(area.center())
        else:
            rect = self.expanded_rect()
        if rect != self.rect:
            self.prepareGeometryChange()
            self.rect = rect
            for link in self.links:
                link.update_position()
        if propagate and self.parent_group is not None:
            self.parent_group.update_rect()

    def title_rect(self):
        return self.rect if self.group.collapsed else QRectF(self.rect.left(), self.rect.top(), self.rect.width(),
                                                             GROUP_TITLE)

    def shape(self):
        path = QPainterPath()
        path.addRect(self.title_rect())
        return path

    def boundingRect(self):
        return self.rect.adjusted(-8, -8, 8, 8)

    def set_drop_target(self, on):
        if on != self.drop_target:
            self.drop_target = on
            self.update()

    def summary(self):
        members = self.all_members()
        down = sum(1 for item in members if item.monitor_state is not None and item.monitor_state.status == DOWN)
        text = f"{len(members)} device{'' if len(members) == 1 else 's'}"
        if self.children:
            text += f", {len(self.children)} building{'' if len(self.children) == 1 else 's'}"
        return text, down

    def paint(self, painter, option, widget=None):
        if self.rect.isNull():
            return
        painter.setRenderHint(QPainter.Antialiasing)
        building = self.group.kind == BUILDING
        color = QColor(COLORS["link"] if building else COLORS["accent"])
        selected = self.isSelected()
        text, down = self.summary()
        if self.group.collapsed:
            self.paint_collapsed(painter, color, selected, text, down)
            return
        path = QPainterPath()
        path.addRoundedRect(self.rect, 10, 10)
        fill = QColor(color)
        fill.setAlpha(22 if building else 14)
        painter.fillPath(path, fill)
        border = QColor(COLORS["success"] if self.drop_target else COLORS["accent_hover"] if selected else color)
        if not (self.drop_target or selected):
            border.setAlpha(150)
        pen = QPen(border, 2.5 if self.drop_target or selected else 1.4)
        if self.drop_target:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawPath(path)
        title = self.title_rect()
        bar = QPainterPath()
        bar.addRoundedRect(title, 10, 10)
        painter.save()
        painter.setClipRect(title)
        fill.setAlpha(60 if building else 45)
        painter.fillPath(bar, fill)
        painter.restore()
        name_font = small_font(0.95, bold=True)
        painter.setFont(name_font)
        painter.setPen(QColor(COLORS["text"]))
        name = f"▾ {self.group.name}"
        name_width = min(QFontMetrics(name_font).horizontalAdvance(name) + 4, title.width() - 20)
        painter.drawText(QRectF(title.left() + 10, title.top(), name_width, title.height()),
                         Qt.AlignLeft | Qt.AlignVCenter, elided(name, name_font, name_width))
        detail = small_font(0.78)
        painter.setFont(detail)
        rest = QRectF(title.left() + 18 + name_width, title.top(), title.width() - name_width - 28, title.height())
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(rest, Qt.AlignLeft | Qt.AlignVCenter,
                         elided(f"{GROUP_KINDS.get(self.group.kind, '')}  ·  {text}", detail, rest.width()))
        if down:
            painter.setPen(QColor(COLORS["error"]))
            painter.drawText(rest, Qt.AlignRight | Qt.AlignVCenter, f"{down} down")

    def paint_collapsed(self, painter, color, selected, text, down):
        """A stack of boxes: the top one names the group and says what's in it."""
        for offset in (8, 4):
            back = QPainterPath()
            back.addRoundedRect(self.rect.translated(offset, offset), 8, 8)
            painter.fillPath(back, QColor(COLORS["panel_alt"]))
            painter.setPen(QPen(QColor(COLORS["border"]), 1))
            painter.drawPath(back)
        path = QPainterPath()
        path.addRoundedRect(self.rect, 8, 8)
        painter.fillPath(path, QColor(COLORS["panel"]))
        strip_rect = QRectF(self.rect.left(), self.rect.top(), STRIP_WIDTH + 6, self.rect.height())
        strip = QPainterPath()
        strip.addRoundedRect(strip_rect, 8, 8)
        faded = QColor(color)
        faded.setAlpha(60)
        painter.save()
        painter.setClipRect(strip_rect)
        painter.fillPath(strip, faded)
        painter.restore()
        if down:
            tint = QColor(COLORS["error"])
            tint.setAlpha(45)
            painter.fillPath(path, tint)
        painter.setPen(QPen(QColor(COLORS["accent_hover"]) if selected else color, 3 if selected else 1.8))
        painter.drawPath(path)
        painter.setPen(color)
        painter.setFont(small_font(0.72, bold=True))
        painter.drawText(strip_rect, Qt.AlignCenter, GROUP_TAGS.get(self.group.kind, "SITE"))
        left = strip_rect.right() + 6
        width = self.rect.right() - left - 6
        name_font = small_font(0.95, bold=True)
        painter.setFont(name_font)
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(QRectF(left, self.rect.top() + 5, width, 18), Qt.AlignLeft | Qt.AlignVCenter,
                         elided(self.group.name, name_font, width))
        detail = small_font(0.8)
        painter.setFont(detail)
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(QRectF(left, self.rect.top() + 24, width, 15), Qt.AlignLeft | Qt.AlignVCenter,
                         elided(text, detail, width))
        painter.setPen(QColor(COLORS["error"] if down else COLORS["muted"]))
        painter.drawText(QRectF(left, self.rect.top() + 40, width, 15), Qt.AlignLeft | Qt.AlignVCenter,
                         f"{down} down" if down else "▸ double-click to expand")

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        if not event.modifiers() & Qt.ControlModifier:
            self.scene().clearSelection()
        self.setSelected(not self.isSelected() if event.modifiers() & Qt.ControlModifier else True)
        self.drag_from, self.dragged = event.scenePos(), False
        event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_from is None:
            return
        delta = event.scenePos() - self.drag_from
        self.drag_from = event.scenePos()
        self.dragged = True
        self.view.move_group(self, delta)

    def mouseReleaseEvent(self, event):
        self.drag_from = None
        if self.dragged:
            self.view.on_item_moved()

    def mouseDoubleClickEvent(self, event):
        self.view.set_collapsed(self, not self.group.collapsed)
        event.accept()


class LinkItem(QGraphicsItem):
    """The links between two devices: one line, with the ports at each end (and "×2" for a port-channel's
    members). Either end can be a collapsed group, standing in for the devices in it."""

    def __init__(self, a_item, b_item, links, ends, label_of):
        """ends: for each link, its (device on a_item's side, device on b_item's side)."""
        super().__init__()
        self.a_item, self.b_item, self.links, self.ends = a_item, b_item, links, ends
        self.line = QLineF()
        self.setZValue(0)
        self.traced = all(link.protocols == ["icmp"] for link in links)
        self.setToolTip("\n".join(
            f"{label_of(a)} {link.port_on(a)}  —  {label_of(b)} {link.port_on(b)}  "
            f"({' + '.join(PROTOCOL_NAMES.get(protocol, protocol) for protocol in link.protocols)})"
            for link, (a, b) in zip(links, ends)))
        a_item.links.append(self)
        b_item.links.append(self)
        self.update_position()

    def ports_text(self, item):
        if isinstance(item, GroupItem):
            return ""  # Which device in it is in the tooltip
        side = 0 if item is self.a_item else 1
        ports = [port for port in (link.port_on(ends[side]) for link, ends in zip(self.links, self.ends)) if port]
        return ", ".join(ports) if len(ports) <= 2 else f"{len(ports)} ports"

    def update_position(self):
        self.prepareGeometryChange()
        self.line = QLineF(self.a_item.center(), self.b_item.center())

    def boundingRect(self):
        return QRectF(self.line.p1(), self.line.p2()).normalized().adjusted(-90, -20, 90, 20)

    def label_point(self, from_start):
        length = self.line.length()
        if length < 1:
            return self.line.p1()
        # Just outside the device's box, along the line
        direction = QPointF(self.line.dx() / length, self.line.dy() / length)
        horizontal = abs(direction.x()) * NODE_HEIGHT > abs(direction.y()) * NODE_WIDTH
        edge = (NODE_WIDTH / 2) / max(abs(direction.x()), 1e-6) if horizontal else \
            (NODE_HEIGHT / 2) / max(abs(direction.y()), 1e-6)
        distance = min(edge + 26, length / 2 - 10)
        start = self.line.p1() if from_start else self.line.p2()
        sign = 1 if from_start else -1
        return start + direction * distance * sign

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        count = len(self.links)
        pen = QPen(QColor(COLORS["muted"]), 3.2 if count > 1 else 1.5)
        if self.traced:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(self.line)
        if QStyleOptionGraphicsItem.levelOfDetailFromTransform(painter.worldTransform()) < LABEL_MIN_ZOOM:
            return  # Too small to read: zoom in to see the ports
        font = small_font(0.72)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        labels = [(self.label_point(True), self.ports_text(self.a_item)),
                  (self.label_point(False), self.ports_text(self.b_item))]
        if count > 1:
            labels.append((self.line.center(), f"×{count}"))
        for point, text in labels:
            if not text:
                continue
            box = QRectF(0, 0, metrics.horizontalAdvance(text) + 8, metrics.height() + 2)
            box.moveCenter(point)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["background"]))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor(COLORS["text"] if text.startswith("×") else COLORS["muted"]))
            painter.drawText(box, Qt.AlignCenter, text)


class MapView(QGraphicsView):
    selection_changed = pyqtSignal(object)  # ("device", key), ("node", key), ("port", key, port) or None
    positions_changed = pyqtSignal()
    context_requested = pyqtSignal(str, object)  # Node key, global position
    port_context_requested = pyqtSignal(str, str, object)  # Switch key, port, global position
    group_context_requested = pyqtSignal(str, object)  # Group key, global position
    groups_changed = pyqtSignal()  # A group was collapsed or expanded here
    devices_dropped = pyqtSignal(object)  # {device key: group key, or "" for none} after a drag in or out of one

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        # Drag the background to move around (or with the middle button); Shift and drag to select a group
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setFocusPolicy(Qt.StrongFocus)
        self.pan_from = None
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setBackgroundBrush(QColor(COLORS["background"]))
        self.items_by_key = {}
        self.link_items = []
        self.links = []
        self.group_items = {}
        self.groups_suspended = False  # Moving many devices at once: the groups' boxes are updated after
        self.groups_editable = True  # Dragging devices in and out of groups (not while crawling)
        self.drag = None  # Devices being dragged: {"moving": set of items, "start": {item: position}}
        self.network_map = None
        self.fit_pending = False
        self.auto_fit = False  # Fitted automatically and not zoomed or panned since: refit when resized
        self.scene().selectionChanged.connect(self.on_selection_changed)

    def set_map(self, network_map, positions):
        self.scene().clear()
        self.items_by_key, self.link_items, self.group_items, self.drag = {}, [], {}, None
        self.network_map, self.links = network_map, network_map.links
        self.groups_suspended = True
        for key, device in network_map.devices.items():
            item = DeviceItem(device, network_map.hosts_by_port(key), self)
            item.setPos(*positions.get(key, (0, 0)))
            self.scene().addItem(item)
            self.items_by_key[key] = item
        self.groups_suspended = False
        self.rebuild_groups()
        self.update_scene_rect()

    def set_graph(self, nodes, links, positions):
        """Show the logical view: l3.L3Nodes (devices drawn as on the physical view) and the links between them."""
        self.scene().clear()
        self.items_by_key, self.link_items, self.group_items, self.drag = {}, [], {}, None
        self.network_map, self.links = None, links
        for key, node in nodes.items():
            item = DeviceItem(node.device, {}, self) if node.device is not None else SimpleNodeItem(node, self)
            item.setPos(*positions.get(key, (0, 0)))
            self.scene().addItem(item)
            self.items_by_key[key] = item
        self.rebuild_links()
        self.update_scene_rect()

    def rebuild_links(self):
        """One line per pair of things showing: a device, or the collapsed group standing in for it."""
        for item in self.link_items:
            self.scene().removeItem(item)
        for item in list(self.items_by_key.values()) + list(self.group_items.values()):
            item.links = []
        self.link_items = []
        pairs = {}
        for link in self.links:
            if link.a not in self.items_by_key or link.b not in self.items_by_key:
                continue
            a_item, b_item = self.representative(link.a), self.representative(link.b)
            if a_item is b_item:
                continue  # Inside a collapsed group
            if id(a_item) > id(b_item):
                a_item, b_item = b_item, a_item
            ends = (link.a, link.b) if self.representative(link.a) is a_item else (link.b, link.a)
            entry = pairs.setdefault((id(a_item), id(b_item)), (a_item, b_item, [], []))
            entry[2].append(link)
            entry[3].append(ends)
        for a_item, b_item, links, ends in pairs.values():
            item = LinkItem(a_item, b_item, links, ends, lambda key: self.items_by_key[key].label)
            self.scene().addItem(item)
            self.link_items.append(item)

    # ----------------------------------------------------------------- Groups

    def rebuild_groups(self):
        """Draw the map's sites and buildings again (after they changed), and the links to collapsed ones."""
        for item in self.group_items.values():
            self.scene().removeItem(item)
        self.group_items, self.drag = {}, None
        for item in self.items_by_key.values():
            item.group_item = None
        if self.network_map is not None:
            for group in self.network_map.groups:
                item = GroupItem(group, self)
                self.scene().addItem(item)
                self.group_items[group.key] = item
            for item in self.group_items.values():
                item.parent_group = self.group_items.get(item.group.parent)
                if item.parent_group is not None:
                    item.parent_group.children.append(item)
            for key, group_key in self.network_map.group_of.items():
                item, group_item = self.items_by_key.get(key), self.group_items.get(group_key)
                if item is not None and group_item is not None:
                    item.group_item = group_item
                    group_item.members.append(item)
        self.apply_collapsed()

    def representative(self, key):
        """What's drawn for a device: itself, or the outermost collapsed group it's in."""
        item = self.items_by_key[key]
        shown, group = item, item.group_item
        while group is not None:
            if group.group.collapsed:
                shown = group
            group = group.parent_group
        return shown

    def apply_collapsed(self):
        for key, item in self.items_by_key.items():
            visible = self.representative(key) is item
            if not visible:
                item.setSelected(False)
            item.setVisible(visible)
        for item in self.group_items.values():
            parent = item.parent_group
            item.setVisible(parent is None or not parent.group.collapsed)
            # Collapsed, it stands in for a device: over the links to it rather than under everything
            item.setZValue(2 if item.group.collapsed else -1 if item.group.kind == BUILDING else -2)
        self.rebuild_links()
        self.update_groups()

    def set_collapsed(self, group_item, collapsed):
        group_item.group.collapsed = collapsed
        self.apply_collapsed()
        self.update_scene_rect()
        self.groups_changed.emit()

    def set_all_collapsed(self, collapsed):
        for item in self.group_items.values():
            item.group.collapsed = collapsed
        self.apply_collapsed()
        self.update_scene_rect()
        self.groups_changed.emit()

    def update_groups(self):
        """Fit every group's box round what's in it: buildings first, then the sites round them."""
        for item in sorted(self.group_items.values(), key=lambda item: item.parent_group is None):
            item.update_rect(propagate=False)

    def reveal(self, key):
        """Expand the collapsed groups hiding a device. Returns True if any were."""
        item = self.items_by_key.get(key)
        group = item.group_item if item is not None else None
        opened = False
        while group is not None:
            if group.group.collapsed:
                group.group.collapsed, opened = False, True
            group = group.parent_group
        if opened:
            self.apply_collapsed()
            self.update_scene_rect()
            self.groups_changed.emit()
        return opened

    def move_group(self, group_item, delta):
        self.groups_suspended = True
        for item in group_item.all_members():
            item.moveBy(delta.x(), delta.y())
        self.groups_suspended = False
        self.update_groups()

    def move_to(self, positions):
        """Move devices to {key: (x, y)}, as when the user drags them (the layout's saved)."""
        self.groups_suspended = True
        for key, (x, y) in positions.items():
            if key in self.items_by_key:
                self.items_by_key[key].setPos(x, y)
        self.groups_suspended = False
        self.update_groups()
        self.update_scene_rect()
        self.positions_changed.emit()

    def selected_keys(self):
        """The devices selected (on the logical view, any node), in the order they're drawn."""
        return [key for key, item in self.items_by_key.items() if item.isSelected() and item.isVisible()]

    def selected_group(self):
        groups = [item for item in self.scene().selectedItems() if isinstance(item, GroupItem)]
        return groups[0].key if len(groups) == 1 else None

    def sizes(self, keys):
        return {key: (self.items_by_key[key].rect.width(), self.items_by_key[key].rect.height()) for key in keys}

    def group_boxes(self):
        """[(group, (left, top, width, height))] for each group as if expanded (for draw.io), sites first."""
        boxes = []
        for item in sorted(self.group_items.values(), key=lambda item: item.parent_group is not None):
            rect = item.expanded_rect()
            if not rect.isNull():
                boxes.append((item.group, (rect.left(), rect.top(), rect.width(), rect.height())))
        return boxes

    def begin_node_drag(self, item):
        """Devices are about to be dragged: groups they're not all of stay still, so they can be dropped out."""
        if not self.group_items or not self.groups_editable:
            self.drag = None
            return
        moving = {other for other in self.scene().selectedItems() if isinstance(other, NodeItem)} | {item}
        self.drag = {"moving": moving, "start": {other: QPointF(other.pos()) for other in moving}}
        for group_item in self.group_items.values():
            group_item.frozen = not set(group_item.all_members()) <= moving

    def drop_target(self, point):
        """The innermost group showing whose box (as it was when the drag started) holds the point."""
        found = [item for item in self.group_items.values()
                 if item.frozen and item.isVisible() and not item.group.collapsed and item.rect.contains(point)]
        return min(found, key=lambda item: item.rect.width() * item.rect.height(), default=None)

    def node_dragged(self, item):
        if self.drag is None:
            return
        target = self.drop_target(item.pos())
        for group_item in self.group_items.values():
            group_item.set_drop_target(group_item is target and target is not item.group_item)

    def end_node_drag(self):
        """Devices dropped: into a group's box puts them in it, out of their group's box takes them out."""
        drag, self.drag = self.drag, None
        if drag is None:
            return {}
        changes = {}
        for item in drag["moving"]:
            if item.pos() == drag["start"][item] or not isinstance(item, DeviceItem):
                continue
            current = item.group_item
            if current is not None and not current.frozen:
                continue  # Its whole group moved with it
            target = self.drop_target(item.pos())
            if target is not current:
                changes[item.key] = target.key if target is not None else ""
        for group_item in self.group_items.values():
            group_item.frozen = False
            group_item.set_drop_target(False)
        self.update_groups()
        return changes

    def set_statuses(self, status_of):
        """Monitoring: status_of(key) gives a device's monitor.DeviceStatus, or None if it isn't monitored."""
        for key, item in self.items_by_key.items():
            if isinstance(item, DeviceItem):
                state = status_of(key)
                if state is not item.monitor_state or state is not None:
                    item.monitor_state = state
                    item.update()
        for item in self.group_items.values():
            item.update()  # How many in it are down

    def set_highlights(self, colors):
        """Ring the items in {key: colour}; clear the rest."""
        for key, item in self.items_by_key.items():
            item.set_highlight(colors.get(key))

    def update_scene_rect(self):
        rect = self.scene().itemsBoundingRect()
        self.setSceneRect(rect.adjusted(-2000, -2000, 2000, 2000))

    def positions(self):
        return {key: (item.pos().x(), item.pos().y()) for key, item in self.items_by_key.items()}

    # ----------------------------------------------------------------- Navigation

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        self.auto_fit = False
        if steps:
            self.zoom(ZOOM_STEP ** steps)
        event.accept()

    def zoom(self, factor):
        current = self.transform().m11()
        factor = max(MIN_ZOOM / current, min(MAX_ZOOM / current, factor))
        self.scale(factor, factor)

    def request_fit(self):
        """Fit the map to the view now if it's showing, or when it's next shown (it needs its real size)."""
        self.fit_pending = True
        if self.isVisible():
            QTimer.singleShot(0, self.fit)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.fit()

    def mousePressEvent(self, event):
        self.auto_fit = False
        if event.button() == Qt.MiddleButton:
            self.pan_from = event.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            # Drag the background to move around; Shift and drag to draw a box selecting what's in it
            boxing = bool(event.modifiers() & Qt.ShiftModifier) and self.itemAt(event.pos()) is None
            self.setDragMode(QGraphicsView.RubberBandDrag if boxing else QGraphicsView.ScrollHandDrag)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.pan_from is not None:
            delta = event.pos() - self.pan_from
            self.pan_from = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.pan_from is not None:
            self.pan_from = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if self.dragMode() == QGraphicsView.RubberBandDrag:
            self.setDragMode(QGraphicsView.ScrollHandDrag)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.SelectAll):
            for item in self.items_by_key.values():
                item.setSelected(item.isVisible())
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.fit_pending:
            QTimer.singleShot(0, self.fit)

    def fit(self):
        self.fit_pending = False
        self.auto_fit = True
        rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            return
        self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.KeepAspectRatio)
        if self.transform().m11() > 1.2:
            self.resetTransform()
            self.scale(1.2, 1.2)
            self.centerOn(rect.center())

    def show_device(self, key):
        item = self.items_by_key.get(key)
        if item is None:
            return False
        self.reveal(key)
        self.auto_fit = False
        self.scene().clearSelection()
        item.setSelected(True)
        self.centerOn(item)
        return True

    def show_devices(self, keys):
        """Select these devices (a link's two ends, say) and bring them into view."""
        items = [self.items_by_key[key] for key in keys if key in self.items_by_key]
        if not items:
            return False
        for key in keys:
            self.reveal(key)
        self.auto_fit = False
        self.scene().clearSelection()
        area = QRectF()
        for item in items:
            item.setSelected(True)
            area = area.united(item.sceneBoundingRect())
        if len(items) == 1:
            self.centerOn(items[0])
        else:
            self.ensureVisible(area, 40, 40)
            self.centerOn(area.center())
        return True

    def show_group(self, key):
        item = self.group_items.get(key)
        if item is None:
            return False
        parent = item.parent_group
        if parent is not None and parent.group.collapsed:
            parent.group.collapsed = False
            self.apply_collapsed()
            self.groups_changed.emit()
        self.auto_fit = False
        self.scene().clearSelection()
        item.setSelected(True)
        self.ensureVisible(item.rect, 40, 40)
        self.centerOn(item.rect.center())
        return True

    def show_host(self, host):
        """Open the host's switch and select its port."""
        item = self.items_by_key.get(host.device)
        if item is None:
            return False
        self.reveal(host.device)
        item.set_expanded(True)
        self.auto_fit = False
        for port_item in item.port_items:
            if port_item.port == host.port:
                self.scene().clearSelection()
                port_item.setSelected(True)
                self.centerOn(port_item)
                return True
        return self.show_device(host.device)

    def find(self, text):
        """Select the first device or host matching text (name, address, MAC, platform). Returns True if found."""
        text = text.strip().lower()
        if not text:
            return False
        compact = text.replace("-", "").replace(":", "").replace(".", "")
        for key, item in sorted(self.items_by_key.items()):
            device = getattr(item, "device", None)
            values = [item.label] + ([device.mgmt_ip, device.platform] + device.addresses if device else [])
            if any(text in value.lower() for value in values if value):
                return self.show_device(key)
        if self.network_map is None:
            return False
        for group_item in self.group_items.values():
            if text in group_item.group.name.lower():
                return self.show_group(group_item.key)
        for host in self.network_map.hosts:
            mac = host.mac.replace("-", "").lower()
            if (len(compact) >= 4 and compact in mac) or any(text in value.lower() for value in (host.ip, host.name,
                                                                                               host.vendor) if value):
                return self.show_host(host)
        return False

    # ----------------------------------------------------------------- Items talking back

    def set_all_hosts_shown(self, shown):
        """Open (or close) every switch's hosts."""
        for item in self.items_by_key.values():
            if item.host_count:
                item.set_expanded(shown)
        self.update_scene_rect()

    def toggle_hosts(self, item):
        if item.host_count:
            item.set_expanded(not item.expanded)
            self.update_scene_rect()

    def on_item_moved(self):
        changes = self.end_node_drag()
        self.update_scene_rect()
        self.positions_changed.emit()
        if changes:
            self.devices_dropped.emit(changes)

    def on_selection_changed(self):
        try:
            selected = self.scene().selectedItems()
        except RuntimeError:  # Scene being torn down
            return
        nodes = [item for item in selected if isinstance(item, NodeItem)]
        if not selected:
            self.selection_changed.emit(None)
        elif len(nodes) > 1 or (nodes and len(selected) > len(nodes)):
            self.selection_changed.emit(("many", len(nodes)))
        elif isinstance(selected[0], GroupItem):
            self.selection_changed.emit(("group", selected[0].key))
        elif isinstance(selected[0], DeviceItem):
            self.selection_changed.emit(("device", selected[0].key))
        elif isinstance(selected[0], SimpleNodeItem):
            self.selection_changed.emit(("node", selected[0].key))
        elif isinstance(selected[0], HostPortItem):
            self.selection_changed.emit(("port", selected[0].parentItem().device.key, selected[0].port))

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if isinstance(item, HostPortItem):
            if not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)
            self.port_context_requested.emit(item.parentItem().key, item.port, event.globalPos())
            return
        if isinstance(item, GroupItem):
            if not item.isSelected():
                self.scene().clearSelection()
                item.setSelected(True)
            self.group_context_requested.emit(item.key, event.globalPos())
            return
        while item is not None and not isinstance(item, NodeItem):
            item = item.parentItem()
        if item is None:
            super().contextMenuEvent(event)
            return
        if not item.isSelected():  # Keep a selection of several when right-clicking one of them
            self.scene().clearSelection()
            item.setSelected(True)
        self.context_requested.emit(item.key, event.globalPos())

    # ----------------------------------------------------------------- Pictures

    def render_image(self, scale=2.0):
        rect = self.scene().itemsBoundingRect().adjusted(-30, -30, 30, 30)
        size = QSize(max(1, math.ceil(rect.width() * scale)), max(1, math.ceil(rect.height() * scale)))
        image = QImage(size, QImage.Format_ARGB32)
        image.fill(QColor(COLORS["background"]))
        painter = QPainter(image)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.render_scene(painter, QRectF(0, 0, size.width(), size.height()), rect)
        painter.end()
        return image

    def render_svg(self, path):
        from PyQt5.QtSvg import QSvgGenerator  # Only needed for this export
        rect = self.scene().itemsBoundingRect().adjusted(-30, -30, 30, 30)
        generator = QSvgGenerator()
        generator.setFileName(str(path))
        generator.setSize(QSize(math.ceil(rect.width()), math.ceil(rect.height())))
        generator.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
        generator.setTitle("Network map")
        painter = QPainter(generator)
        painter.fillRect(QRectF(0, 0, rect.width(), rect.height()), QColor(COLORS["background"]))
        self.render_scene(painter, QRectF(0, 0, rect.width(), rect.height()), rect)
        painter.end()

    def render_scene(self, painter, target, source):
        selected = self.scene().selectedItems()
        self.scene().clearSelection()  # The picture shouldn't show what happened to be selected
        self.scene().render(painter, target, source)
        for item in selected:
            item.setSelected(True)
