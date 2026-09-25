import ipaddress

from nomad.dnsclient import ServerResult
from nomad.icmp import PingStats
from nomad.mtr import HopRow
from nomad.neighbors import Neighbor, NeighborTable
from nomad.report import INFO, OK, PROBLEM, WARNING, Finding, Report, evaluate_adapter, evaluate_arp, \
    evaluate_dns, evaluate_gateway, evaluate_internet, evaluate_mtu, evaluate_trace, loss_start, render_html, \
    render_text
from nomad.snapshot import Adapter

GATEWAY_MAC = "00-1E-14-D9-A3-BF"


def adapter(**changes):
    values = dict(index="1", name="Ethernet", status="Up", dhcp=True, is_adapter=True, speed_bps=10 ** 9,
                  ipv4=[ipaddress.ip_interface("192.168.1.10/24")], gateways4=["192.168.1.1"], dns4=["192.168.1.1"])
    values.update(changes)
    return Adapter(**values)


def stats(sent, rtts):
    result = PingStats()
    result.sent, result.received, result.rtts = sent, len(rtts), list(rtts)
    return result


def test_adapter_findings():
    assert evaluate_adapter(adapter()).status == OK
    assert evaluate_adapter(adapter(status="Disconnected")).status == PROBLEM
    apipa = evaluate_adapter(adapter(ipv4=[ipaddress.ip_interface("169.254.4.5/16")]))
    assert apipa.status == PROBLEM and "DHCP" in apipa.summary
    no_gateway = evaluate_adapter(adapter(gateways4=[], dns4=[]))
    assert no_gateway.status == WARNING and "gateway" in no_gateway.summary and "DNS" in no_gateway.summary


def test_gateway_findings():
    assert evaluate_gateway("192.168.1.1", stats(10, [1] * 10), None).status == OK
    assert evaluate_gateway("192.168.1.1", stats(10, [1] * 9), GATEWAY_MAC).status == WARNING
    assert evaluate_gateway("192.168.1.1", stats(10, [1] * 4), GATEWAY_MAC).status == PROBLEM
    blocks_ping = evaluate_gateway("192.168.1.1", stats(10, []), GATEWAY_MAC)
    assert blocks_ping.status == WARNING and "answered ARP" in blocks_ping.summary
    assert evaluate_gateway("192.168.1.1", stats(10, []), None).status == PROBLEM
    assert evaluate_gateway("192.168.1.1", stats(10, [40] * 10), None).status == WARNING  # Slow for a LAN


def server(address, rtts=(), failures=None):
    result = ServerResult(address)
    result.rtts = list(rtts)
    result.failures = failures or {}
    result.sent = len(result.rtts) + sum(result.failures.values())
    return result


def test_dns_findings():
    assert evaluate_dns([server("10.0.0.53", [5, 7])]).status == OK
    assert evaluate_dns([server("10.0.0.53", [5]), server("10.0.0.54", failures={"timed out": 2})]).status == WARNING
    assert evaluate_dns([server("10.0.0.53", failures={"timed out": 2})]).status == PROBLEM
    offline = evaluate_dns([server("10.0.0.53", failures={"SERVFAIL": 4})])
    assert offline.status == WARNING and "without internet" in offline.summary
    assert evaluate_dns([server("10.0.0.53", [400])]).status == WARNING
    assert evaluate_dns([]).status == WARNING


def test_offline_network_is_info_not_a_fault():
    finding = evaluate_internet({"8.8.8.8": stats(4, [])}, {"1.1.1.1:443": False}, False)
    assert finding.status == INFO and "No internet access" in finding.summary
    assert evaluate_internet({"8.8.8.8": stats(4, [20] * 4)}, {"1.1.1.1:443": True}, True).status == OK
    assert evaluate_internet({"8.8.8.8": stats(4, [20] * 4)}, {}, False).status == WARNING
    assert evaluate_trace("8.8.8.8", [], False, "").status == INFO


def hop(ttl, address, loss, average=10.0):
    sent = 10
    received = sent - int(loss / 10)
    return HopRow(ttl, address, (), sent, received, average, average if received else None, average, average,
                  0.0, "", False)


def test_loss_start_ignores_routers_that_just_skip_probes():
    rows = [hop(1, "10.0.0.1", 0), hop(2, "10.0.0.2", 50), hop(3, "10.0.0.3", 0), hop(4, "8.8.8.8", 0)]
    assert loss_start(rows) is None
    rows = [hop(1, "10.0.0.1", 0), hop(2, "10.0.0.2", 30), hop(3, "", 100), hop(4, "8.8.8.8", 30)]
    assert loss_start(rows) == 2
    finding = evaluate_trace("8.8.8.8", rows, True, "table")
    assert finding.status == WARNING and "hop 2" in finding.summary and finding.preformatted == "table"
    assert evaluate_trace("8.8.8.8", [hop(1, "10.0.0.1", 0), hop(2, "8.8.8.8", 0)], True, "").status == OK


def test_mtu_findings():
    assert evaluate_mtu("8.8.8.8", 1500, "").status == OK
    assert evaluate_mtu("8.8.8.8", 1420, "").status == WARNING
    assert evaluate_mtu("8.8.8.8", None, "Stopped.").status == INFO


def test_arp_findings_only_count_the_local_subnet():
    local = [ipaddress.ip_network("192.168.1.0/24")]
    neighbors = [Neighbor("1", "Ethernet", "192.168.1.1", GATEWAY_MAC, "Reachable", 4),
                 Neighbor("1", "Ethernet", "8.8.8.8", GATEWAY_MAC, "Stale", 4)]
    assert evaluate_arp(NeighborTable(neighbors), {"192.168.1.1"}, local).status == OK
    neighbors.append(Neighbor("1", "Ethernet", "192.168.1.66", GATEWAY_MAC, "Reachable", 4))
    spoofed = evaluate_arp(NeighborTable(neighbors), {"192.168.1.1"}, local)
    assert spoofed.status == WARNING and "192.168.1.66" in spoofed.details[0]
    duplicate = evaluate_arp(NeighborTable(neighbors[:1], [("Ethernet", "192.168.1.10")]), {"192.168.1.1"}, local)
    assert duplicate.status == PROBLEM


def test_render_html_escapes_and_summarizes():
    report = Report("Ethernet <1>", "PC&1", "2026-09-24 10:00:00",
                    [Finding("Adapter", OK, "Fine."), Finding("DNS", WARNING, "Slow <script>", ["a & b"]),
                     Finding("Traceroute", OK, "Done.", preformatted="Hop  Host\n  1  <router>")])
    page = render_html(report)
    assert "<script>" not in page and "Slow &lt;script&gt;" in page and "a &amp; b" in page
    assert "Ethernet &lt;1&gt;" in page and "&lt;router&gt;" in page
    assert report.status == WARNING and "Some things need a look." in page
    text = render_text(report)
    assert "[Warning] DNS: Slow <script>" in text and "    a & b" in text
