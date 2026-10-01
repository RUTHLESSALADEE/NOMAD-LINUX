"""Exporting a map: tables as CSV, and the drawing as a draw.io file (which draw.io and Visio can open)."""
import csv
from xml.sax.saxutils import quoteattr

from .layout import NODE_HEIGHT, NODE_WIDTH
from .model import KIND_NAMES, SOURCE_NAMES

DEVICE_COLUMNS = ["Name", "Status", "Management IP", "Kind", "Platform", "Found by", "Links", "Hosts", "Addresses",
                  "Description", "Problem"]
LINK_COLUMNS = ["Device", "Port", "Neighbor", "Neighbor Port", "Seen by"]
HOST_COLUMNS = ["MAC Address", "IP Address", "Vendor", "Name", "Switch", "Port", "VLAN", "Found by", "Note"]

DRAWIO_STYLES = {
    "switch": "fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "router": "fillColor=#d5e8d4;strokeColor=#82b366;",
    "firewall": "fillColor=#ffe6cc;strokeColor=#d79b00;",
    "ap": "fillColor=#e1d5e7;strokeColor=#9673a6;",
    "subnet": "fillColor=#f5f5f5;strokeColor=#6c8ebf;arcSize=50;",
}


def device_rows(network_map, status_of=lambda key: ""):
    """status_of(key) gives the Status column (from monitoring: Up, Down...), "" when it isn't monitored."""
    hosts = {}
    for host in network_map.hosts:
        hosts[host.device] = hosts.get(host.device, 0) + 1
    rows = []
    for device in sorted(network_map.devices.values(), key=lambda device: device.label.lower()):
        rows.append([device.label, status_of(device.key), device.mgmt_ip, KIND_NAMES.get(device.kind, device.kind),
                     device.platform, SOURCE_NAMES.get(device.source, device.source),
                     str(len(network_map.links_of(device.key))), str(hosts.get(device.key, "")),
                     ", ".join(device.addresses), device.sys_descr.splitlines()[0] if device.sys_descr else "",
                     device.error])
    return rows


def sorted_links(network_map):
    """Links in the order the Links table and its CSV list them."""
    devices = network_map.devices
    return sorted(network_map.links, key=lambda link: (devices[link.a].label.lower(), link.a_port))


def link_rows(network_map):
    devices = network_map.devices
    return [[devices[link.a].label, link.a_port, devices[link.b].label, link.b_port,
             " + ".join(protocol.upper() for protocol in link.protocols)] for link in sorted_links(network_map)]


def host_rows(network_map):
    devices = network_map.devices
    return [[host.mac, host.ip, host.vendor, host.name, devices[host.device].label, host.port,
             str(host.vlan or ""), "Added by hand" if host.manual else "Crawl", host.note]
            for host in network_map.hosts]


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)


def drawio(network_map, positions):
    """An uncompressed draw.io (mxGraph) file with each device where it is on the map and a labelled edge per
    link."""
    nodes = []
    for device in sorted(network_map.devices.values(), key=lambda device: device.key):
        label = device.label + (f"\n{device.mgmt_ip}" if device.mgmt_ip and device.mgmt_ip != device.label else "")
        nodes.append((device.key, label, device.kind, device.source != "snmp"))
    return drawio_graph(nodes, network_map.links, positions)


def drawio_graph(nodes, links, positions, name="Network map"):
    """draw.io XML for [(key, label, kind, dashed)] and Links, with nodes centred on positions."""
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    ids = {}
    for number, (key, label, kind, dashed) in enumerate(nodes):
        ids[key] = f"d{number}"
        x, y = positions.get(key, (0, 0))
        style = "rounded=1;whiteSpace=wrap;html=0;" + DRAWIO_STYLES.get(kind, "") + ("dashed=1;" if dashed else "")
        cells.append(f'<mxCell id="{ids[key]}" value={quoteattr(label)} style={quoteattr(style)} vertex="1" '
                     f'parent="1"><mxGeometry x="{x - NODE_WIDTH / 2:.0f}" y="{y - NODE_HEIGHT / 2:.0f}" '
                     f'width="{NODE_WIDTH}" height="{NODE_HEIGHT}" as="geometry"/></mxCell>')
    for number, link in enumerate(links):
        if link.a not in ids or link.b not in ids:
            continue
        label = " - ".join(port for port in (link.a_port, link.b_port) if port)
        style = "endArrow=none;html=0;fontSize=9;" + ("dashed=1;" if link.protocols == ["icmp"] else "")
        cells.append(f'<mxCell id="l{number}" value={quoteattr(label)} style={quoteattr(style)} '
                     f'edge="1" parent="1" source="{ids[link.a]}" target="{ids[link.b]}">'
                     '<mxGeometry relative="1" as="geometry"/></mxCell>')
    return (f'<mxfile host="NOMAD"><diagram name={quoteattr(name)}><mxGraphModel><root>\n'
            + "\n".join(cells) + "\n</root></mxGraphModel></diagram></mxfile>\n")
