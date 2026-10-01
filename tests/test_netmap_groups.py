"""Network Map: sites, buildings and rooms, the other arrangements, and lining devices up."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import xml.etree.ElementTree as ElementTree  # noqa: E402

import pytest  # noqa: E402
from netmap_fakes import build_network  # noqa: E402
from PyQt5.QtCore import QPointF  # noqa: E402

from nomad.netmap import export, store  # noqa: E402
from nomad.netmap.crawl import CrawlSettings, Crawler  # noqa: E402
from nomad.netmap.layout import CIRCLE, GRID, GROUP_PAD, GROUP_TITLE, HORIZONTAL, LEFT, LEFT_RIGHT, MIDDLE, \
    NODE_HEIGHT, NODE_WIDTH, STYLE_NAMES, TOP_DOWN, align, arrange, arrange_in_place, distribute  # noqa: E402
from nomad.netmap.model import BUILDING, ROOM, SITE, Device, NetworkMap  # noqa: E402
from nomad.ui.netmap_dialogs import GroupDialog  # noqa: E402
from nomad.ui.netmap_view import GroupItem, LinkItem  # noqa: E402
from test_netmap_tab import Window, app, tab  # noqa: E402,F401


def no_overlaps(positions):
    points = list(positions.items())
    for index, (key, (x, y)) in enumerate(points):
        for other, (other_x, other_y) in points[index + 1:]:
            assert abs(x - other_x) >= NODE_WIDTH or abs(y - other_y) >= NODE_HEIGHT, (key, other, positions)


def box(positions, keys):
    """The box arrange() leaves round a group's devices: (left, top, right, bottom)."""
    return (min(positions[key][0] for key in keys) - NODE_WIDTH / 2 - GROUP_PAD,
            min(positions[key][1] for key in keys) - NODE_HEIGHT / 2 - GROUP_PAD - GROUP_TITLE,
            max(positions[key][0] for key in keys) + NODE_WIDTH / 2 + GROUP_PAD,
            max(positions[key][1] for key in keys) + NODE_HEIGHT / 2 + GROUP_PAD)


def apart(a, b):
    return a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


@pytest.fixture
def crawled():
    network = build_network()
    return Crawler(CrawlSettings(seeds=["10.0.0.1"], overrides=[("10.0.0.12/32", "secret")]),
                   client_factory=network.client, pinger=network.ping, echo=network.echo).run()


def campus():
    nodes = ["core", "dist1", "dist2"] + [f"a{number}" for number in range(8)] + ["lone"]
    edges = [("core", "dist1"), ("core", "dist2")] + [("dist1" if number < 4 else "dist2", f"a{number}")
                                                      for number in range(8)]
    return nodes, edges


# ----------------------------------------------------------------- The model


def test_groups_in_the_model():
    network_map = NetworkMap(devices={key: Device(key) for key in ("a", "b", "c", "d")})
    site = network_map.new_group("Head Office")
    network_map.set_group(["a"], site.key)
    building = network_map.new_group("Building 2", BUILDING, site.key)
    network_map.set_group(["b", "c"], building.key)
    assert (site.key, building.key) == ("g1", "g2") and building.parent == site.key
    assert [group.name for group in network_map.group_path("b")] == ["Head Office", "Building 2"]
    assert network_map.device_group_label("b") == "Head Office / Building 2"
    assert network_map.device_group_label("a") == "Head Office" and network_map.device_group_label("d") == ""
    assert sorted(network_map.members(site.key)) == ["a", "b", "c"]
    assert network_map.members(site.key, deep=False) == ["a"]

    again = NetworkMap.from_json(network_map.to_json())  # Saved with the map
    assert [(group.name, group.kind, group.parent) for group in again.groups] == [
        ("Head Office", SITE, ""), ("Building 2", BUILDING, "g1")]
    assert again.group_of == network_map.group_of

    network_map.set_group(["b", "c"], "")  # Emptied: the building goes, the site stays (it has a)
    assert [group.name for group in network_map.groups] == ["Head Office"]
    network_map.set_group(["b"], network_map.new_group("Annex", BUILDING, site.key).key)
    network_map.remove_group(site.key)  # Its building stands on its own; its devices are in no group
    assert [(group.name, group.parent) for group in network_map.groups] == [("Annex", "")]
    assert network_map.group_of == {"b": network_map.groups[0].key}


def test_rooms_in_buildings():
    network_map = NetworkMap(devices={key: Device(key) for key in ("a", "b", "c")})
    site = network_map.new_group("Head Office")
    building = network_map.new_group("Building 2", BUILDING, site.key)
    room = network_map.new_group("Room 114", ROOM, building.key)
    network_map.set_group(["a", "b"], room.key)
    network_map.set_group(["c"], building.key)
    assert [group.name for group in network_map.group_path("a")] == ["Head Office", "Building 2", "Room 114"]
    assert network_map.device_group_label("a") == "Head Office / Building 2 / Room 114"
    assert sorted(network_map.members(site.key)) == ["a", "b", "c"]  # Down through the building to its rooms
    assert sorted(network_map.members(building.key)) == ["a", "b", "c"]
    assert network_map.members(building.key, deep=False) == ["c"]
    assert [group.name for group in NetworkMap.from_json(network_map.to_json()).groups] == [
        "Head Office", "Building 2", "Room 114"]

    network_map.set_group(["c"], "")  # The building holds only a room now, and the site only it: both stay
    assert [group.name for group in network_map.groups] == ["Head Office", "Building 2", "Room 114"]
    assert network_map.new_group("Desk", ROOM, site.key).parent == ""  # A room can't be straight in a site
    network_map.remove_group(room.key)  # Its devices go to its building
    assert network_map.group_of == {"a": building.key, "b": building.key}
    network_map.remove_group(building.key)
    assert network_map.group_of == {"a": site.key, "b": site.key}


def test_groups_carried_to_a_new_crawl():
    older = NetworkMap(devices={key: Device(key) for key in ("a", "b")})
    site = older.new_group("Site")
    older.set_group(["a", "b"], site.key)
    newer = NetworkMap(devices={"a": Device("a"), "c": Device("c")})
    newer.carry_groups(older)
    assert newer.group_of == {"a": site.key} and newer.groups[0] is not site
    gone = NetworkMap(devices={"c": Device("c")})
    gone.carry_groups(older)
    assert gone.groups == [] and gone.group_of == {}


# ----------------------------------------------------------------- Arranging


@pytest.mark.parametrize("style", list(STYLE_NAMES))
def test_every_arrangement_places_everything_clear(style):
    nodes, edges = campus()
    positions = arrange(nodes, edges, style)
    assert set(positions) == set(nodes)
    no_overlaps(positions)


def test_arrangements_differ_in_shape():
    nodes, edges = campus()
    top_down = arrange(nodes, edges, TOP_DOWN)
    left_right = arrange(nodes, edges, LEFT_RIGHT)
    assert top_down["core"][1] < top_down["dist1"][1] < top_down["a0"][1]  # Layers down the page
    assert left_right["core"][0] < left_right["dist1"][0] < left_right["a0"][0]  # Layers across it
    circle = arrange(nodes, edges, CIRCLE)
    center = circle["core"]
    distance = {key: ((x - center[0]) ** 2 + (y - center[1]) ** 2) ** 0.5 for key, (x, y) in circle.items()}
    assert distance["dist1"] == pytest.approx(distance["dist2"]) and distance["a0"] > distance["dist1"]
    grid = arrange(nodes, edges, GRID)
    assert len({y for _, y in grid.values()}) > 1 and len({x for x, _ in grid.values()}) > 1


@pytest.mark.parametrize("style", [TOP_DOWN, GRID, CIRCLE])
def test_arranging_by_group_keeps_each_group_in_its_own_box(style):
    nodes, edges = campus()
    path_of = {"a0": ["north"], "a1": ["north"], "a2": ["north", "n-b1"], "a3": ["north", "n-b1"],
               "a4": ["south"], "a5": ["south"], "a6": ["south"], "a7": ["south"]}
    positions = arrange(nodes, edges, style, path_of=path_of)
    no_overlaps(positions)
    north, south, building = box(positions, ["a0", "a1", "a2", "a3"]), box(positions, ["a4", "a5", "a6", "a7"]), \
        box(positions, ["a2", "a3"])
    assert apart(north, south)
    assert north[0] <= building[0] and building[2] <= north[2] and north[1] < building[1]  # Inside its site
    for key in ("core", "dist1", "dist2", "lone"):  # Devices in no group are outside both
        point = positions[key]
        for group in (north, south):
            assert not (group[0] <= point[0] <= group[2] and group[1] <= point[1] <= group[3])


def test_arranging_some_keeps_them_where_they_were():
    nodes, edges = campus()
    where = {key: (1000 + index * 7, 500 - index * 3) for index, key in enumerate(nodes[:5])}
    placed = arrange_in_place(where, edges, TOP_DOWN)
    assert set(placed) == set(where)
    no_overlaps(placed)
    assert min(x for x, _ in placed.values()) == pytest.approx(min(x for x, _ in where.values()))
    assert min(y for _, y in placed.values()) == pytest.approx(min(y for _, y in where.values()))


def test_align_and_distribute():
    positions = {"a": (0, 0), "b": (500, 40), "c": (220, 400)}
    left = align(positions, LEFT)
    assert {x for x, _ in left.values()} == {0} and left["c"][1] == 400
    middle = align(positions, MIDDLE)
    assert {y for _, y in middle.values()} == {200}
    narrow = align({"a": (0, 0), "subnet": (300, 0)}, LEFT, {"subnet": (100, 36)})
    assert narrow["subnet"][0] - 50 == narrow["a"][0] - NODE_WIDTH / 2  # By their left edges
    spread = distribute(positions, HORIZONTAL)
    xs = sorted(x for x, _ in spread.values())
    assert xs[0] == 0 and xs[2] == 500 and xs[1] == pytest.approx(250)
    stacked = distribute({"a": (0, 0), "b": (0, 0), "c": (0, 0)}, HORIZONTAL)  # On top of each other: spread out
    no_overlaps(stacked)


# ----------------------------------------------------------------- On the page


def make_site(tab, keys, name="North", kind=SITE, parent=""):
    group = tab.network_map.new_group(name, kind, parent)
    tab.network_map.set_group(keys, group.key)
    tab.groups_edited()
    return group


def test_group_box_round_its_devices(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)[:2]
    group = make_site(tab, keys)
    item = tab.view.group_items[group.key]
    assert isinstance(item, GroupItem) and len(item.members) == 2
    for key in keys:
        assert item.rect.contains(tab.view.items_by_key[key].sceneBoundingRect())
    column = export.DEVICE_COLUMNS.index("Group")
    groups = {tab.devices_table.item(row, 0).data_object: tab.devices_table.item(row, column).text()
              for row in range(tab.devices_table.rowCount())}
    assert {key for key, text in groups.items() if text == "North"} == set(keys)
    assert store.load(tab.map_path).group_of == {key: group.key for key in keys}  # Saved

    tab.view.items_by_key[keys[0]].moveBy(400, 300)  # The box follows its devices
    assert item.rect.contains(tab.view.items_by_key[keys[0]].sceneBoundingRect())

    tab.view.show_devices([])
    tab.view.scene().clearSelection()
    item.setSelected(True)
    assert "North" in tab.details.toPlainText() and "Site" in tab.details.toPlainText()


def test_collapsing_a_group_draws_its_links_to_one_box(tab, crawled):
    tab.on_crawled(crawled)
    core = next(key for key in crawled.devices if key.startswith("core"))
    others = [key for key in crawled.devices if key != core]
    group = make_site(tab, others, "Access")
    item = tab.view.group_items[group.key]
    tab.view.set_collapsed(item, True)
    assert not any(tab.view.items_by_key[key].isVisible() for key in others)
    links = [link for link in tab.view.scene().items() if isinstance(link, LinkItem)]
    assert links and all(item in (link.a_item, link.b_item) for link in links)
    assert len(links) == 1  # Every link from core into the group, drawn as one line
    assert tab.view.find(others[0].split(".")[0])  # Finding a device in it opens it
    assert not group.collapsed and tab.view.items_by_key[others[0]].isVisible()
    tab.view.set_collapsed(item, True)
    tab.save_positions()
    assert store.load(tab.map_path).groups[0].collapsed  # Remembered


def test_dragging_devices_in_and_out_of_a_group(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)
    inside, outside = keys[:2], keys[2]
    group = make_site(tab, inside)
    view, item = tab.view, tab.view.group_items[group.key]
    newcomer = view.items_by_key[outside]

    def drag(device_item, point):
        view.scene().clearSelection()
        device_item.setSelected(True)
        view.begin_node_drag(device_item)
        device_item.setPos(point)
        view.node_dragged(device_item)
        view.on_item_moved()

    drag(newcomer, item.rect.center() + QPointF(0, 10))
    assert tab.network_map.group_of.get(outside) == group.key
    assert "Moved" in tab.status_label.text()
    item = view.group_items[group.key]
    leaving = view.items_by_key[inside[0]]
    drag(leaving, QPointF(item.rect.right() + 2000, item.rect.bottom() + 2000))
    assert inside[0] not in tab.network_map.group_of

    # Dragging the whole group's devices together moves the group rather than taking them out
    item = view.group_items[group.key]
    members = item.all_members()
    view.scene().clearSelection()
    for member in members:
        member.setSelected(True)
    view.begin_node_drag(members[0])
    for member in members:
        member.moveBy(3000, 0)
    view.on_item_moved()
    assert {key for key, value in tab.network_map.group_of.items() if value == group.key} == \
        {member.key for member in members}


def test_buildings_in_sites_and_ungrouping(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)
    site = make_site(tab, keys[:3], "Campus")
    building = make_site(tab, keys[1:3], "Library", BUILDING, site.key)
    site_item, building_item = tab.view.group_items[site.key], tab.view.group_items[building.key]
    assert building_item.parent_group is site_item and site_item.children == [building_item]
    assert site_item.rect.contains(building_item.rect)
    assert len(site_item.all_members()) == 3
    tab.ungroup(site.key)
    assert [group.name for group in tab.network_map.groups] == ["Library"]
    assert keys[0] not in tab.network_map.group_of


def test_rooms_on_the_page(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)
    site = make_site(tab, keys[:4], "Campus")
    building = make_site(tab, keys[1:4], "Library", BUILDING, site.key)
    room = make_site(tab, keys[2:4], "Server Room", ROOM, building.key)
    items = tab.view.group_items
    assert items[room.key].parent_group is items[building.key] and items[building.key].children == [items[room.key]]
    assert items[building.key].rect.contains(items[room.key].rect)
    assert items[site.key].rect.contains(items[building.key].rect)
    assert items[room.key].zValue() > items[building.key].zValue() > items[site.key].zValue()
    assert "1 room" in items[building.key].summary()[0]
    tab.view.set_collapsed(items[site.key], True)  # The room is hidden inside the collapsed site
    assert not items[room.key].isVisible()
    assert tab.view.show_group(room.key) and items[room.key].isVisible()
    tab.rearrange(TOP_DOWN)
    assert items[building.key].rect.contains(items[room.key].rect)
    assert [box[0].kind for box in tab.view.group_boxes()] == [SITE, BUILDING, ROOM]
    tab.ungroup(room.key)
    assert "its building" in tab.status_label.text()
    assert {tab.network_map.group_of[key] for key in keys[1:4]} == {building.key}


def test_group_dialog_offers_rooms(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)
    site = make_site(tab, keys[:2], "Campus")
    building = make_site(tab, keys[1:2], "Library", BUILDING, site.key)
    dialog = GroupDialog(tab.network_map, count=1, inside=building.key)  # In a building: most likely a room
    assert dialog.kind_radios[ROOM].isChecked() and dialog.inside_combo.currentData() == building.key
    assert dialog.inside_combo.itemText(dialog.inside_combo.currentIndex()) == "Campus / Library"
    dialog.name_input.setText("Room 2")
    assert dialog.values() == ("Room 2", ROOM, building.key)
    dialog.kind_radios[BUILDING].setChecked(True)  # Buildings go in sites
    assert [dialog.inside_combo.itemData(index) for index in range(dialog.inside_combo.count())] == ["", site.key]
    dialog.kind_radios[SITE].setChecked(True)
    assert not dialog.inside_combo.isEnabled() and dialog.values() == ("Room 2", SITE, "")


def test_rearranging_in_each_style_keeps_groups_apart(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)
    first, second = make_site(tab, keys[:2], "A"), make_site(tab, keys[2:4], "B")
    for style in STYLE_NAMES:
        tab.rearrange(style)
        a, b = tab.view.group_items[first.key].rect, tab.view.group_items[second.key].rect
        assert not a.intersects(b), style
    assert tab.arrange_style == list(STYLE_NAMES)[-1]
    tab.fill_arrange_menu()
    assert [action.text() for action in tab.arrange_menu.actions()][:4] == list(STYLE_NAMES.values())


def test_aligning_the_selection(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)[:3]
    tab.align_selected(tab.view, keys, LEFT)
    xs = {round(tab.view.items_by_key[key].pos().x(), 3) for key in keys}
    assert len(xs) == 1
    tab.align_selected(tab.view, keys, "vertical")
    ys = sorted(tab.view.items_by_key[key].pos().y() for key in keys)
    assert ys[1] - ys[0] >= NODE_HEIGHT and ys[2] - ys[1] >= NODE_HEIGHT


def test_drawio_export_has_the_group_boxes(tab, crawled):
    tab.on_crawled(crawled)
    make_site(tab, sorted(crawled.devices)[:2], "North & South")
    root = ElementTree.fromstring(export.drawio(tab.network_map, tab.view.positions(), tab.view.group_boxes()))
    boxes = [cell for cell in root.iter("mxCell") if cell.get("id", "").startswith("g")]
    assert [cell.get("value") for cell in boxes] == ["North & South"]


# ----------------------------------------------------------------- Undo and Redo


def test_undo_and_redo_moves(tab, crawled):
    tab.on_crawled(crawled)
    view = tab.view
    key = sorted(crawled.devices)[0]
    item = view.items_by_key[key]
    start = (item.pos().x(), item.pos().y())
    assert not tab.undo_button.isEnabled() and not tab.redo_button.isEnabled()
    view.begin_node_drag(item)
    item.setPos(start[0] + 500, start[1] + 300)
    view.on_item_moved()
    assert tab.undo_button.isEnabled()
    tab.undo()
    assert (item.pos().x(), item.pos().y()) == start
    assert tab.network_map.positions[key] == start
    assert "Undid" in tab.status_label.text()
    assert tab.redo_button.isEnabled() and not tab.undo_button.isEnabled()
    tab.redo()
    assert (item.pos().x(), item.pos().y()) == (start[0] + 500, start[1] + 300)

    # Aligning and re-arranging are steps too, and a new change forgets what was undone
    keys = sorted(crawled.devices)[:3]
    before = view.positions()
    tab.align_selected(view, keys, LEFT)
    tab.undo()
    assert view.positions() == before
    tab.rearrange(GRID)
    assert not tab.redo_button.isEnabled()
    tab.undo()
    assert view.positions() == before


def test_undo_dragging_into_a_group(tab, crawled):
    tab.on_crawled(crawled)
    keys = sorted(crawled.devices)
    group = make_site(tab, keys[:2])
    view = tab.view
    newcomer = view.items_by_key[keys[2]]
    start = QPointF(newcomer.pos())
    view.scene().clearSelection()
    newcomer.setSelected(True)
    view.begin_node_drag(newcomer)
    newcomer.setPos(view.group_items[group.key].rect.center() + QPointF(0, 10))
    view.on_item_moved()
    assert tab.network_map.group_of.get(keys[2]) == group.key
    tab.undo()  # The move and joining the group are one step
    assert keys[2] not in tab.network_map.group_of and newcomer.pos() == start
    assert view.items_by_key[keys[2]].group_item is None
    tab.undo()  # Making the site
    assert not tab.network_map.groups and not view.group_items
    tab.redo()
    assert [g.name for g in tab.network_map.groups] == ["North"]
    assert set(tab.network_map.members(tab.network_map.groups[0].key)) == set(keys[:2])


def test_undo_forgotten_with_another_map(tab, crawled):
    tab.on_crawled(crawled)
    tab.align_selected(tab.view, sorted(crawled.devices)[:3], LEFT)
    assert tab.undo_button.isEnabled()
    tab.show_map(store.load(tab.map_path), tab.map_path)
    assert not tab.undo_button.isEnabled()
