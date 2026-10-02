from netmap_fakes import build_network

from nomad.netmap import shared
from nomad.netmap.crawl import CrawlSettings, Crawler


def crawled():
    network = build_network()
    settings = CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")])
    network_map = Crawler(settings, client_factory=network.client, pinger=network.ping, echo=network.echo).run()
    network_map.positions = {key: (float(index * 100), 0.0) for index, key in enumerate(network_map.devices)}
    site = network_map.new_group("HQ")
    network_map.set_group(["core", "acc1"], site.key)
    network_map.news["device:acc2"] = {"when": "2026-10-02T09:00:00", "where": "core Te1/0/2", "by": "me"}
    return network_map


def test_flatten_and_build_round_trip():
    network_map = crawled()
    items = shared.flatten(network_map, settings={"scope": ["10.0.0.0/8"]})
    rebuilt = shared.build(items)
    assert set(rebuilt.devices) == set(network_map.devices) and len(rebuilt.hosts) == len(network_map.hosts)
    assert rebuilt.group_of == network_map.group_of and rebuilt.news == network_map.news
    assert shared.settings_of(items) == {"scope": ["10.0.0.0/8"]}
    assert shared.flatten(rebuilt, settings={"scope": ["10.0.0.0/8"]}) == items


def test_diff_lists_changed_and_gone_items():
    network_map = crawled()
    base = shared.flatten(network_map)
    network_map.positions["core"] = (5.0, 5.0)
    network_map.remove_devices(["rtr1"])
    changes = shared.diff(base, shared.flatten(network_map))
    touched = {(change["section"], change["key"]): change["data"] for change in changes}
    assert touched[("position", "core")] == [5.0, 5.0]
    assert touched[("device", "rtr1")] is None
    assert ("device", "core") not in touched


def test_two_people_moving_different_devices_both_kept():
    base = shared.flatten(crawled())
    mine, theirs = shared.build(base), shared.build(base)
    mine.positions["core"] = (1.0, 1.0)
    theirs.positions["acc1"] = (2.0, 2.0)
    theirs.set_group(["acc2"], theirs.groups[0].key)
    incoming = {(change["section"], change["key"]): change["data"]
                for change in shared.diff(base, shared.flatten(theirs))}
    merged, new_base = shared.merge(shared.flatten(mine), base, incoming)
    result = shared.build(merged)
    assert result.positions["core"] == (1.0, 1.0) and result.positions["acc1"] == (2.0, 2.0)
    assert result.group_of["acc2"] == theirs.groups[0].key
    assert [change["key"] for change in shared.diff(new_base, merged)] == ["core"]  # Still to send: mine


def test_same_item_keeps_mine_until_sent():
    base = shared.flatten(crawled())
    mine = shared.build(base)
    mine.positions["core"] = (1.0, 1.0)
    merged, _ = shared.merge(shared.flatten(mine), base, {("position", "core"): [9.0, 9.0]})
    assert merged[("position", "core")] == [1.0, 1.0]


def test_store_create_push_changes(tmp_path):
    store = shared.MapStore(tmp_path / "maps.db")
    items = shared.flatten(crawled())
    changes = shared.diff({}, items)
    map_id, first = store.create("Head office", "alice", changes, {"communities": ["public"]})
    assert store.items(map_id) == items
    assert store.secrets(map_id) == {"communities": ["public"]}
    revision = store.push(map_id, [{"section": "position", "key": "core", "data": [3, 4]},
                                   {"section": "device", "key": "rtr1", "data": None}], "bob")
    payload, latest, more = store.changes_since(first)
    assert latest == revision and not more
    assert {(item["section"], item["key"]) for item in payload["items"]} == {("position", "core"), ("device", "rtr1")}
    assert ("device", "rtr1") not in store.items(map_id)
    payload, _, _ = store.changes_since(0)
    assert payload["maps"][0]["name"] == "Head office"


def test_store_pages_whole_revisions(tmp_path):
    store = shared.MapStore(tmp_path / "maps.db")
    map_id, _ = store.create("A", "me")
    for number in range(5):
        store.push(map_id, [{"section": "news", "key": f"host:{number}-{item}", "data": {}} for item in range(3)],
                   "me")
    seen, since, more = [], 0, True
    while more:
        payload, since, more = store.changes_since(since, limit=4)
        seen += [item["key"] for item in payload["items"]]
    assert len(seen) == 15 and len(set(seen)) == 15


def test_store_names_and_delete(tmp_path):
    import pytest
    store = shared.MapStore(tmp_path / "maps.db")
    map_id, _ = store.create("A", "me")
    with pytest.raises(shared.MapError):
        store.create("a", "me")
    store.rename(map_id, "B")
    store.delete(map_id)
    assert store.maps() == []
    with pytest.raises(shared.MapError):
        store.push(map_id, [{"section": "news", "key": "x", "data": {}}], "me")


def test_lease_held_then_taken(tmp_path):
    store = shared.MapStore(tmp_path / "maps.db")
    map_id, _ = store.create("A", "me")
    assert store.lease(map_id, "ws1-gui", "WS1", "gui", now=100)["yours"]
    other = store.lease(map_id, "ws2-gui", "WS2", "gui", now=110)
    assert not other["yours"] and other["computer"] == "WS1"
    assert store.lease(map_id, "ws3-svc", "WS3", "service", now=120, take=True)["yours"]  # A service takes over
    assert not store.lease(map_id, "ws1-gui", "WS1", "gui", now=130, take=True)["yours"]  # But not from a service
    assert store.lease(map_id, "ws1-gui", "WS1", "gui", now=120 + shared.LEASE_SECONDS + 1)["yours"]  # Ran out
    assert store.lease(map_id, "ws1-gui", "WS1", "gui", now=400, release=True) == {}
    assert store.leases(now=401) == {}
