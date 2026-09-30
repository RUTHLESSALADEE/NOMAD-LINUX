"""The drawing on the Network Map page: devices as boxes you can drag, links with the port at each end, and each
switch's hosts behind a badge that opens into one box per port."""
import math

from PyQt5.QtCore import QLineF, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QStyleOptionGraphicsItem

from ..netmap.l3 import HOP, STAR, SUBNET
from ..netmap.layout import NODE_HEIGHT, NODE_WIDTH
from ..netmap.model import AP, FIREWALL, KIND_NAMES, NO_SNMP, ROUTER, SHARED_PORT_HOSTS, SNMP, SOURCE_NAMES, SWITCH, \
    UNREACHABLE
from .theme import COLORS

KIND_COLORS = {SWITCH: COLORS["link"], ROUTER: COLORS["accent"], FIREWALL: "#ff9f43", AP: "#c792ea"}
KIND_TAGS = {SWITCH: "SW", ROUTER: "RTR", FIREWALL: "FW", AP: "AP"}
STRIP_WIDTH = 34
BADGE_HEIGHT = 20
PORT_WIDTH, PORT_HEIGHT = 150, 44
PORT_COLUMNS = 4
PORT_GAP = 12
PROTOCOL_NAMES = {"cdp": "CDP", "lldp": "LLDP", "l3": "address on the subnet", "icmp": "traceroute"}
ZOOM_STEP = 1.15
MIN_ZOOM, MAX_ZOOM = 0.05, 4.0
LABEL_MIN_ZOOM = 0.5  # Port labels are left off below this zoom


def small_font(scale=0.85, bold=False):
    font = QFont()
    font.setPointSizeF(max(6.0, font.pointSizeF() * scale))
    font.setBold(bold)
    return font


def elided(text, font, width):
    return QFontMetrics(font).elidedText(text, Qt.ElideRight, int(width))


def host_summary(hosts):
    """How a port's hosts are described in its box: the one host, or how many."""
    if len(hosts) == 1:
        host = hosts[0]
        return host.name or host.ip or host.mac, host.vendor or host.platform
    if len(hosts) > SHARED_PORT_HOSTS:
        return f"{len(hosts)} hosts", "unmanaged switch or hypervisor?"
    return f"{len(hosts)} hosts", ", ".join(host.name or host.ip or host.mac for host in hosts)


def host_tooltip(port, hosts):
    lines = [f"{port}:"]
    for host in hosts[:40]:
        parts = [host.mac, host.ip, host.name, host.vendor, f"VLAN {host.vlan}" if host.vlan else ""]
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

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for link in self.links:
                link.update_position()
        elif change == QGraphicsItem.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.view.on_item_moved()


class DeviceItem(NodeItem):
    def __init__(self, device, host_ports, view):
        super().__init__(device.key, device.label, view)
        self.device, self.host_ports = device, host_ports
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
        left = self.rect.left() + STRIP_WIDTH + 6
        width = self.rect.right() - left - 5
        name_font = small_font(0.95, bold=True)
        painter.setFont(name_font)
        painter.setPen(QColor(COLORS["error"] if device.source == UNREACHABLE else COLORS["text"]))
        painter.drawText(QRectF(left, self.rect.top() + 4, width, 18), Qt.AlignLeft | Qt.AlignVCenter,
                         elided(device.label, name_font, width))
        detail_font = small_font(0.8)
        painter.setFont(detail_font)
        painter.setPen(QColor(COLORS["muted"]))
        lines = [(device.mgmt_ip if device.mgmt_ip != device.label else "", COLORS["muted"])]
        if device.source in (NO_SNMP, UNREACHABLE):
            lines.append((SOURCE_NAMES[device.source], COLORS["warning" if device.source == NO_SNMP else "error"]))
        else:
            lines.append((device.platform or (device.sys_descr.splitlines()[0] if device.sys_descr else ""),
                          COLORS["muted"]))
        for row, (line, line_color) in enumerate((line, color) for line, color in lines if line):
            painter.setPen(QColor(line_color))
            painter.drawText(QRectF(left, self.rect.top() + 22 + row * 15, width, 15), Qt.AlignLeft | Qt.AlignVCenter,
                             elided(line, detail_font, width))

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
            rows = math.ceil(len(ports) / columns)
            width = columns * (PORT_WIDTH + PORT_GAP) - PORT_GAP
            top = self.clear_space(width, rows * (PORT_HEIGHT + PORT_GAP))
            for number, (port, hosts) in enumerate(ports):
                row, column = divmod(number, columns)
                item = HostPortItem(self, port, hosts)
                item.setPos(-width / 2 + PORT_WIDTH / 2 + column * (PORT_WIDTH + PORT_GAP),
                            top + PORT_HEIGHT / 2 + row * (PORT_HEIGHT + PORT_GAP))
                item.set_anchor()
                self.port_items.append(item)
        self.update()

    def clear_space(self, width, height):
        """How far below the device (in its coordinates) a block of port boxes fits without covering other
        devices, such as the access points under a switch."""
        top = NODE_HEIGHT / 2 + BADGE_HEIGHT + 24
        others = [item.sceneBoundingRect() for item in self.scene().items()
                  if isinstance(item, DeviceItem) and item is not self]
        for _ in range(40):
            block = QRectF(self.pos().x() - width / 2, self.pos().y() + top, width, height)
            if not any(block.intersects(other) for other in others):
                break
            top += PORT_HEIGHT + PORT_GAP
        return top


class HostPortItem(QGraphicsItem):
    """One switch port's hosts, shown when the switch's hosts are opened. A child of the switch, so it moves with it."""

    def __init__(self, parent, port, hosts):
        super().__init__(parent)
        self.port, self.hosts = port, hosts
        self.rect = QRectF(-PORT_WIDTH / 2, -PORT_HEIGHT / 2, PORT_WIDTH, PORT_HEIGHT)
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
        path = QPainterPath()
        path.addRoundedRect(self.rect, 5, 5)
        painter.fillPath(path, QColor(COLORS["panel_alt"]))
        painter.setPen(pen)
        painter.drawPath(path)
        title, note = host_summary(self.hosts)
        width = PORT_WIDTH - 10
        bold = small_font(0.78, bold=True)
        painter.setFont(bold)
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(QRectF(self.rect.left() + 5, self.rect.top() + 2, width, 14), Qt.AlignLeft | Qt.AlignVCenter,
                         elided(f"{self.port}  {title}", bold, width))
        regular = small_font(0.74)
        painter.setFont(regular)
        painter.setPen(QColor(COLORS["warning"] if shared else COLORS["muted"]))
        if len(self.hosts) == 1 and self.hosts[0].ip and title != self.hosts[0].ip:
            note = f"{self.hosts[0].ip}  {note}"
        painter.drawText(QRectF(self.rect.left() + 5, self.rect.top() + 17, width, 24), Qt.AlignLeft | Qt.TextWordWrap,
                         note)


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


class LinkItem(QGraphicsItem):
    """The links between two devices: one line, with the ports at each end (and "×2" for a port-channel's
    members)."""

    def __init__(self, a_item, b_item, links):
        super().__init__()
        self.a_item, self.b_item, self.links = a_item, b_item, links
        self.line = QLineF()
        self.setZValue(0)
        a, b = a_item.key, b_item.key
        self.traced = all(link.protocols == ["icmp"] for link in links)
        self.setToolTip("\n".join(
            f"{a_item.label} {link.port_on(a)}  —  {b_item.label} {link.port_on(b)}  "
            f"({' + '.join(PROTOCOL_NAMES.get(protocol, protocol) for protocol in link.protocols)})" for link in links))
        a_item.links.append(self)
        b_item.links.append(self)
        self.update_position()

    def ports_text(self, item):
        ports = [port for port in (link.port_on(item.key) for link in self.links) if port]
        return ", ".join(ports) if len(ports) <= 2 else f"{len(ports)} ports"

    def update_position(self):
        self.prepareGeometryChange()
        self.line = QLineF(self.a_item.pos(), self.b_item.pos())

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setBackgroundBrush(QColor(COLORS["background"]))
        self.items_by_key = {}
        self.link_items = []
        self.network_map = None
        self.fit_pending = False
        self.auto_fit = False  # Fitted automatically and not zoomed or panned since: refit when resized
        self.scene().selectionChanged.connect(self.on_selection_changed)

    def set_map(self, network_map, positions):
        self.scene().clear()
        self.items_by_key, self.link_items = {}, []
        self.network_map = network_map
        for key, device in network_map.devices.items():
            item = DeviceItem(device, network_map.hosts_by_port(key), self)
            item.setPos(*positions.get(key, (0, 0)))
            self.scene().addItem(item)
            self.items_by_key[key] = item
        self.add_links(network_map.links)
        self.update_scene_rect()

    def set_graph(self, nodes, links, positions):
        """Show the logical view: l3.L3Nodes (devices drawn as on the physical view) and the links between them."""
        self.scene().clear()
        self.items_by_key, self.link_items = {}, []
        self.network_map = None
        for key, node in nodes.items():
            item = DeviceItem(node.device, {}, self) if node.device is not None else SimpleNodeItem(node, self)
            item.setPos(*positions.get(key, (0, 0)))
            self.scene().addItem(item)
            self.items_by_key[key] = item
        self.add_links(links)
        self.update_scene_rect()

    def add_links(self, links):
        pairs = {}
        for link in links:
            if link.a in self.items_by_key and link.b in self.items_by_key:
                pairs.setdefault(tuple(sorted((link.a, link.b))), []).append(link)
        for (a, b), grouped in pairs.items():
            item = LinkItem(self.items_by_key[a], self.items_by_key[b], grouped)
            self.scene().addItem(item)
            self.link_items.append(item)

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
        super().mousePressEvent(event)

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
        self.auto_fit = False
        self.scene().clearSelection()
        item.setSelected(True)
        self.centerOn(item)
        return True

    def show_host(self, host):
        """Open the host's switch and select its port."""
        item = self.items_by_key.get(host.device)
        if item is None:
            return False
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
        for host in self.network_map.hosts:
            mac = host.mac.replace("-", "").lower()
            if (len(compact) >= 4 and compact in mac) or any(text in value.lower() for value in (host.ip, host.name,
                                                                                               host.vendor) if value):
                return self.show_host(host)
        return False

    # ----------------------------------------------------------------- Items talking back

    def toggle_hosts(self, item):
        if item.host_count:
            item.set_expanded(not item.expanded)
            self.update_scene_rect()

    def on_item_moved(self):
        self.update_scene_rect()
        self.positions_changed.emit()

    def on_selection_changed(self):
        try:
            selected = self.scene().selectedItems()
        except RuntimeError:  # Scene being torn down
            return
        if not selected:
            self.selection_changed.emit(None)
        elif isinstance(selected[0], DeviceItem):
            self.selection_changed.emit(("device", selected[0].key))
        elif isinstance(selected[0], SimpleNodeItem):
            self.selection_changed.emit(("node", selected[0].key))
        elif isinstance(selected[0], HostPortItem):
            self.selection_changed.emit(("port", selected[0].parentItem().device.key, selected[0].port))

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        while item is not None and not isinstance(item, NodeItem):
            item = item.parentItem()
        if item is None:
            super().contextMenuEvent(event)
            return
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
