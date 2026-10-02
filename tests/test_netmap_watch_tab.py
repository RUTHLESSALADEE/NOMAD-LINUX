"""Watching for new devices and tribe maps on the Network Map page."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from netmap_fakes import build_network  # noqa: E402
from PyQt5.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402
from test_netmap_tab import Window  # noqa: E402
from test_netmap_watch import NEW_PC, plug_in_switch  # noqa: E402

from nomad.netmap import export, store, watch  # noqa: E402
from nomad.netmap.crawl import CrawlSettings, Crawler  # noqa: E402
from nomad.ui import netmap_tab  # noqa: E402
from nomad.ui.netmap_tribe import TribeSync  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def make_tab(network, tmp_path, monkeypatch, tribe=None):
    monkeypatch.setattr(store, "maps_dir", lambda: tmp_path)
    page = netmap_tab.NetworkMapTab(Window())
    page.resize(1200, 800)
    page.overrides = [("10.0.0.12/32", "secret")]
    page.watcher.client_factory = network.client
    page.watcher.crawl = lambda network_map, options, seeds, should_stop: watch.crawl_from(
        network_map, options, seeds, network.client, network.ping, network.echo, should_stop)
    page.watcher.listen_check.setChecked(False)  # Not port 514 in tests
    if tribe is not None:
        page.tribe.shutdown()
        page.tribe = tribe
        tribe.synced.connect(page.on_tribe_synced)
        tribe.status_changed.connect(page.update_tribe_label)
    return page


def crawl(network):
    return Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")], trace=False),
                   client_factory=network.client, pinger=network.ping, echo=network.echo).run()


def idle(page):
    watcher = page.watcher
    return watcher.signature_thread is None and watcher.refresh_thread is None and not watcher.to_refresh


def test_watching_adds_a_new_switch_tagged_new(app, tmp_path, monkeypatch):
    network = build_network()
    page = make_tab(network, tmp_path, monkeypatch)
    try:
        page.on_crawled(crawl(network))
        page.watch_check.setChecked(True)
        assert page.watcher.running
        assert wait_for(app, lambda: idle(page) and page.watcher.last_neighbors)
        assert page.network_map.news == {}  # Nothing new on a fresh map
        plug_in_switch(network)
        page.watcher.poll_neighbors()
        assert wait_for(app, lambda: "acc3" in page.network_map.devices and idle(page))
        assert "device:acc3" in page.network_map.news and f"host:{NEW_PC}" in page.network_map.news
        assert page.view.items_by_key["acc3"].news_text == "NEW"
        assert page.view.items_by_key["acc3"].news_text and page.view.new_hosts == {NEW_PC}
        column = export.DEVICE_COLUMNS.index("New")
        new_rows = [page.devices_table.item(row, 0).text() for row in range(page.devices_table.rowCount())
                    if page.devices_table.item(row, column).text()]
        assert new_rows == ["acc3.corp.example"]
        assert "1 new" not in page.watch_label.text() and "new" in page.watch_label.text()
        assert any("New device: acc3" in line for line in page.watcher.lines)
        saved = store.load(page.map_path)
        assert "device:acc3" in saved.news  # Saved with the map
        page.mark_seen(page.news_of_devices(["acc3"]))
        assert page.network_map.news == {} and page.view.items_by_key["acc3"].news_text == ""
    finally:
        page.shutdown()


def test_triggered_switch_is_read_after_the_delay(app, tmp_path, monkeypatch):
    network = build_network()
    page = make_tab(network, tmp_path, monkeypatch)
    try:
        page.on_crawled(crawl(network))
        page.watch_check.setChecked(True)
        assert wait_for(app, lambda: idle(page) and page.watcher.last_neighbors)
        plug_in_switch(network)
        page.watcher.queue.delay = 0
        from nomad.syslog import parse_message
        page.watcher.on_syslog(parse_message("<189>53: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/7, changed "
                                             "state to up", "10.0.0.11"))
        page.watcher.on_syslog(parse_message("<189>53: hello", "10.9.9.9"))
        page.watcher.tick()
        assert wait_for(app, lambda: "acc3" in page.network_map.devices and idle(page))
        assert any("port GigabitEthernet1/0/7 up: reading it" in line for line in page.watcher.lines)
    finally:
        page.shutdown()


# --------------------------------------------------------------------- Tribe maps through a real server

@pytest.fixture
def server(tmp_path):
    from nomad.ipam.server import IpamServer
    server = IpamServer(tmp_path / "server", host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    yield server
    server.stop()
    thread.join(10)


def tribe_for(server, tmp_path, user):
    from nomad.ipam.client import TeamClient
    from nomad.netmap.tribe import TribeMaps
    from test_ipam_server import key_for
    key = key_for(server)
    return TribeSync(key_loader=lambda: key, maps_factory=lambda key: TribeMaps(
        key.server_id, TeamClient(key, user=user, computer=user.upper()), tmp_path / f"{user}-maps.db"))


def test_tribe_map_shared_between_two_pages(app, server, tmp_path, monkeypatch):
    network = build_network()
    alice = make_tab(network, tmp_path / "a", monkeypatch, tribe_for(server, tmp_path, "alice"))
    bob = make_tab(network, tmp_path / "b", monkeypatch, tribe_for(server, tmp_path, "bob"))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    try:
        alice.communities = ["public", "s3cret"]
        alice.on_crawled(crawl(network))
        monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("HQ", True))
        alice.share_with_tribe()
        assert alice.tribe_map_id is not None and alice.map_path is None
        assert "Tribe map HQ" in alice.tribe_label.text()

        maps = bob.tribe.ensure()
        assert wait_for(app, lambda: [item["name"] for item in maps.maps()] == ["HQ"])
        bob.open_tribe_map(maps.maps()[0]["id"])
        assert set(bob.network_map.devices) == set(alice.network_map.devices)
        assert bob.communities == ["public", "s3cret"]  # The map's community strings came with it

        # Bob moves a device; Alice sees it
        bob.view.items_by_key["core"].setPos(1234, 567)
        bob.save_positions()
        assert wait_for(app, lambda: tuple(alice.network_map.positions.get("core", ())) == (1234.0, 567.0), 15)
        assert alice.view.items_by_key["core"].pos().x() == 1234

        # Watching on Alice's computer found something: Bob sees it tagged NEW
        alice.network_map.news["device:acc1"] = {"when": "2026-10-02T10:00:00", "where": "", "by": "x"}
        alice.write_map(alice.network_map, None)
        assert wait_for(app, lambda: "device:acc1" in bob.network_map.news, 15)
        assert bob.view.items_by_key["acc1"].news_text == "NEW"

        # Deleted by Alice: Bob keeps a copy as a file
        monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
        alice.delete_tribe_map()
        assert alice.tribe_map_id is None and alice.map_path is not None
        assert wait_for(app, lambda: bob.tribe_map_id is None, 15)
        assert bob.map_path is not None and "deleted" in bob.status_label.text()
    finally:
        alice.shutdown()
        bob.shutdown()


def test_watching_stands_by_while_another_computer_watches(app, server, tmp_path, monkeypatch):
    network = build_network()
    page = make_tab(network, tmp_path, monkeypatch, tribe_for(server, tmp_path, "alice"))
    try:
        page.on_crawled(crawl(network))
        monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("HQ", True))
        page.share_with_tribe()
        bobs_sync = tribe_for(server, tmp_path, "bob")
        other = bobs_sync.ensure()
        assert other.lease(page.tribe_map_id, "bob-service", kind="service")["yours"]
        page.watch_check.setChecked(True)
        assert wait_for(app, lambda: page.watcher.standing_by)
        assert "BOB" in page.watcher.standing_by and "service" in page.watcher.standing_by
        assert "watched by" in page.watch_label.text()
        other.lease(page.tribe_map_id, "bob-service", release=True)
        page.watcher.lease_checked = 0
        page.watcher.tick()
        assert wait_for(app, lambda: not page.watcher.standing_by)
        bobs_sync.shutdown()
    finally:
        page.shutdown()
