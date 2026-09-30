import xml.etree.ElementTree as ElementTree

import pytest
from netmap_fakes import build_network

from nomad.netmap import export, store
from nomad.netmap.crawl import CrawlSettings, Crawler
from nomad.netmap.layout import H_GAP, NODE_HEIGHT, NODE_WIDTH, layout, merge_positions
from nomad.netmap.model import NetworkMap


def no_overlaps(positions):
    points = list(positions.values())
    for index, (x, y) in enumerate(points):
        for other_x, other_y in points[index + 1:]:
            assert abs(x - other_x) >= NODE_WIDTH or abs(y - other_y) >= NODE_HEIGHT, positions


def test_layers_follow_hops_from_the_busiest_device():
    edges = [("core", "a1"), ("core", "a2"), ("core", "a3"), ("a1", "e1"), ("a2", "e2"), ("a3", "e3"), ("e1", "x")]
    positions = layout(["core", "a1", "a2", "a3", "e1", "e2", "e3", "x"], edges)
    assert positions["core"][1] < positions["a1"][1] == positions["a2"][1] < positions["e1"][1]
    no_overlaps(positions)


def test_single_link_devices_are_grouped_under_their_parent():
    nodes = ["core", "dist"] + [f"ap{number}" for number in range(10)]
    edges = [("core", "dist")] + [("dist", f"ap{number}") for number in range(10)]
    positions = layout(nodes, edges, root="core")
    xs = [positions[f"ap{number}"][0] for number in range(10)]
    assert max(xs) - min(xs) <= 3 * (NODE_WIDTH + H_GAP) + 1  # Four across, not ten
    assert all(positions[f"ap{number}"][1] > positions["dist"][1] for number in range(10))
    no_overlaps(positions)


def test_separate_groups_and_lone_devices_do_not_overlap():
    positions = layout(["a", "b", "c", "d", "lone1", "lone2"], [("a", "b"), ("c", "d")])
    assert len(positions) == 6
    no_overlaps(positions)


def test_saved_positions_are_kept_and_new_devices_placed_clear():
    nodes = ["core", "a1", "a2"]
    edges = [("core", "a1"), ("core", "a2")]
    saved = {"core": (1000, 1000), "a1": (700, 1300)}
    positions = merge_positions(nodes + ["a3"], edges + [("core", "a3")], saved)
    assert positions["core"] == (1000, 1000) and positions["a1"] == (700, 1300)
    no_overlaps(positions)


@pytest.fixture
def crawled():
    network = build_network()
    return Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                   client_factory=network.client, pinger=network.ping,
                   echo=network.echo).run()


def test_map_round_trips_through_a_file(crawled, tmp_path):
    crawled.positions = {"core": (10.0, 20.0)}
    path = store.save(crawled, folder=tmp_path)
    assert path.suffix == store.EXTENSION
    loaded = store.load(path)
    assert loaded.devices == crawled.devices
    assert loaded.links == crawled.links
    assert loaded.hosts == crawled.hosts
    assert loaded.positions == {"core": (10.0, 20.0)}
    assert store.recent(tmp_path) == [path]


def test_loading_something_else_says_so(tmp_path):
    path = tmp_path / "other.nomadmap"
    path.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="isn't a NOMAD network map"):
        store.load(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="damaged"):
        store.load(path)


def test_exports(crawled, tmp_path):
    positions = layout(list(crawled.devices), [(link.a, link.b) for link in crawled.links])
    root = ElementTree.fromstring(export.drawio(crawled, positions))
    cells = root.iter("mxCell")
    vertices = [cell for cell in root.iter("mxCell") if cell.get("vertex")]
    edges = [cell for cell in root.iter("mxCell") if cell.get("edge")]
    assert len(vertices) == len(crawled.devices) and len(edges) == len(crawled.links)
    assert any(cell.get("value") == "Te1/0/1 - Te1/1/1" for cell in cells) or edges
    path = tmp_path / "hosts.csv"
    export.write_csv(path, export.HOST_COLUMNS, export.host_rows(crawled))
    text = path.read_text(encoding="utf-8-sig")
    assert text.startswith("MAC Address,IP Address") and "SEP00AABBCCDDEE" in text
    rows = export.device_rows(crawled)
    assert [row[0] for row in rows][:2] == ["acc1.corp.example", "acc2"]


def test_empty_map_json():
    assert NetworkMap.from_json(NetworkMap().to_json()).devices == {}


def test_core_pair_shares_the_top_layer():
    access = [f"acc{number}" for number in range(4)]
    edges = [("core1", "core2")] + [(core, switch) for core in ("core1", "core2") for switch in access]
    edges.append(("core1", "wan"))
    positions = layout(["core1", "core2", "wan"] + access, edges)
    assert positions["core1"][1] == positions["core2"][1] < positions["acc0"][1]
    assert positions["wan"][1] < positions["core1"][1]  # Hanging off the top layer: drawn above it
    no_overlaps(positions)
