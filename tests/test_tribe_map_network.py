"""A tribe map keeps the IPAM network it's of: saved, sent to the server, and there when it's opened again."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from netmap_fakes import build_network  # noqa: E402
from PyQt5.QtWidgets import QInputDialog  # noqa: E402
from test_netmap_watch_tab import app, crawl, make_tab, server, tribe_for, wait_for  # noqa: E402,F401


def test_tribe_map_keeps_its_network_after_reopening(app, server, tmp_path, monkeypatch):
    network = build_network()
    (tmp_path / "a").mkdir()
    alice = make_tab(network, tmp_path / "a", monkeypatch, tribe_for(server, tmp_path, "alice"))
    alice.on_crawled(crawl(network))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("HQ", True))
    alice.share_with_tribe()
    map_id = alice.tribe_map_id
    alice.set_ipam_network("team:abc")
    assert alice.tribe.maps.pending_count() == 1
    assert wait_for(app, lambda: alice.tribe.maps.pending_count() == 0, 15)  # Reached the server
    alice.shutdown()
    again = make_tab(network, tmp_path / "a", monkeypatch, tribe_for(server, tmp_path, "alice"))
    maps = again.tribe.ensure()
    wait_for(app, lambda: bool(maps.maps()), 15)
    again.open_tribe_map(map_id, quiet=True)
    assert again.network_map.ipam_network == "team:abc"
    again.shutdown()
