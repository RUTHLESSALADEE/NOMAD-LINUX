import copy

from netmap_fakes import PC1_MAC, PRINTER_MAC, build_network

from nomad.netmap import diff
from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.model import SNMP, UNREACHABLE, Host


def crawl():
    network = build_network()
    return Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")], trace=False),
                   client_factory=network.client, pinger=network.ping, echo=network.echo).run()


def test_same_map_has_no_changes():
    assert diff.compare(crawl(), crawl()) == []


def test_changes_between_maps():
    old = crawl()
    new = copy.deepcopy(old)
    # acc2 gone (with its link), a new switch, pa-fw1 stopped answering, a PC moved port, a printer unplugged
    del new.devices["acc2"]
    new.links = [link for link in new.links if "acc2" not in (link.a, link.b)]
    new.hosts = [host for host in new.hosts if host.device != "acc2" and host.mac != PRINTER_MAC]
    new.devices["acc3"] = copy.deepcopy(new.devices["acc1"])
    new.devices["acc3"].key, new.devices["acc3"].name, new.devices["acc3"].mgmt_ip = "acc3", "acc3", "10.0.0.13"
    new.devices["pa-fw1"].source = UNREACHABLE
    for host in new.hosts:
        if host.mac == PC1_MAC:
            host.port = "Gi1/0/9"
    new.hosts.append(Host(mac="AA-BB-CC-00-00-01", device="acc1", port="Gi1/0/20", ip="10.10.0.99"))
    assert old.devices["pa-fw1"].source == SNMP

    changes = [(change.change, change.what, change.name) for change in diff.compare(old, new)]
    assert changes[:3] == [(diff.CHANGED, diff.DEVICE, "pa-fw1"), (diff.ADDED, diff.DEVICE, "acc3"),
                           (diff.REMOVED, diff.DEVICE, "acc2")]
    assert (diff.REMOVED, diff.LINK, "core.corp.example Te1/0/2 - acc2 Eth1/49") in changes
    moved = [change for change in diff.compare(old, new) if change.change == diff.MOVED]
    assert len(moved) == 1 and moved[0].detail == "acc1.corp.example Gi1/0/5 -> acc1.corp.example Gi1/0/9"
    assert (diff.ADDED, diff.HOST, "10.10.0.99") in changes
    assert (diff.REMOVED, diff.HOST, "10.10.0.30") in changes  # The printer
    host_rows = [change for change in changes if change[1] == diff.HOST]
    assert host_rows[0][0] == diff.MOVED  # Moves first: they're the interesting ones
