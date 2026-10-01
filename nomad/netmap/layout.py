"""Placing a map's devices: layers by hop distance from the top device, ordered to keep links from crossing.

Devices with a single link (access points, a lone router) sit in a small grid under the device they hang off, so a
switch with thirty access points doesn't make its layer thirty wide. Positions are the centers of the devices.

The same layers can run left to right, or the devices go in a grid or in rings round the top device; with groups
(sites and buildings), each group is laid out on its own and the groups' boxes are tiled. Align and distribute
line up devices the user chose.
"""
import math

NODE_WIDTH, NODE_HEIGHT = 170, 58
H_GAP, V_GAP = 40, 120
LEAF_COLUMNS = 4
LEAF_GAP = 26
COMPONENT_GAP = 160
SWEEPS = 4
PEER_SHARE = 0.6  # Share of the top device's links a neighbor needs to sit beside it
PEER_MIN_LINKS = 3
RING_GAP = 230  # Between a circle's rings
GRID_ROW = NODE_HEIGHT + 50  # Leaves room for a switch's hosts badge
GROUP_PAD = 24  # Space inside a group's box round what's in it
GROUP_TITLE = 26  # A group's title bar
GROUP_GAP = 90  # Between groups' boxes when arranged by group
DISTRIBUTE_MIN_GAP = 20

# Arrangements
TOP_DOWN, LEFT_RIGHT, GRID, CIRCLE = "top-down", "left-right", "grid", "circle"
STYLE_NAMES = {TOP_DOWN: "Top to Bottom", LEFT_RIGHT: "Left to Right", GRID: "Grid", CIRCLE: "Circle"}
# Lining up
LEFT, CENTER, RIGHT, TOP, MIDDLE, BOTTOM = "left", "center", "right", "top", "middle", "bottom"
HORIZONTAL, VERTICAL = "horizontal", "vertical"


def leaf_block(count):
    """(width, height) of the grid of count single-link devices."""
    if not count:
        return 0, 0
    columns = min(count, LEAF_COLUMNS)
    rows = math.ceil(count / columns)
    return columns * (NODE_WIDTH + H_GAP) - H_GAP, rows * (NODE_HEIGHT + LEAF_GAP)


def neighbors_of(nodes, edges):
    adjacent = {node: set() for node in nodes}
    for a, b in edges:
        if a in adjacent and b in adjacent and a != b:
            adjacent[a].add(b)
            adjacent[b].add(a)
    return adjacent


def components(adjacent):
    seen, found = set(), []
    for start in sorted(adjacent):
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            group.append(node)
            for other in adjacent[node]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        found.append(group)
    return sorted(found, key=lambda group: (-len(group), min(group)))


def layout_component(group, adjacent, root=None, weight=lambda node: 0):
    """Positions for one connected group, with its top-left corner near (0, 0). Returns ({node: (x, y)}, width)."""
    if len(group) == 1:
        return {group[0]: (NODE_WIDTH / 2, NODE_HEIGHT / 2)}, NODE_WIDTH
    if root not in group:
        root = max(group, key=lambda node: (len(adjacent[node]), weight(node), node))
    # A core pair: neighbors of the top device with nearly as many links go on the top layer beside it
    peers = sorted(node for node in adjacent[root] if len(adjacent[root]) >= PEER_MIN_LINKS
                   and len(adjacent[node]) >= max(PEER_MIN_LINKS, PEER_SHARE * len(adjacent[root])))
    depth = {node: 0 for node in [root] + peers}
    order = [root] + peers
    for node in order:  # Breadth first
        for other in sorted(adjacent[node]):
            if other not in depth:
                depth[other] = depth[node] + 1
                order.append(other)

    leaves = {}  # Parent -> [leaf]
    if len(group) > 2:
        for node in group:
            if depth[node] and len(adjacent[node]) == 1:
                parent = next(iter(adjacent[node]))
                if len(adjacent[parent]) > 1 or parent == root:
                    leaves.setdefault(parent, []).append(node)
    leaf_set = {leaf for items in leaves.values() for leaf in items}
    layers = {}
    for node in order:
        if node not in leaf_set:
            layers.setdefault(depth[node], []).append(node)
    levels = [layers[level] for level in sorted(layers)]

    # Order each layer by the average position of its links in the layer above (then below), a few times over
    for sweep in range(SWEEPS):
        indices = list(range(1, len(levels))) if sweep % 2 == 0 else list(range(len(levels) - 2, -1, -1))
        for index in indices:
            reference = levels[index - 1] if sweep % 2 == 0 else levels[index + 1]
            place = {node: position for position, node in enumerate(reference)}
            keys = {}
            for position, node in enumerate(levels[index]):
                linked = [place[other] for other in adjacent[node] if other in place]
                keys[node] = sum(linked) / len(linked) if linked else position
            levels[index].sort(key=keys.get)

    positions, width = {}, 0
    slot_widths = [[max(NODE_WIDTH, leaf_block(len(leaves.get(node, [])))[0]) for node in level] for level in levels]
    width = max(sum(widths) + H_GAP * (len(widths) - 1) for widths in slot_widths)
    y = NODE_HEIGHT / 2
    for level, widths in zip(levels, slot_widths):
        x = (width - (sum(widths) + H_GAP * (len(widths) - 1))) / 2
        tallest = 0
        for node, slot in zip(level, widths):
            center = x + slot / 2
            positions[node] = (center, y)
            children = sorted(leaves.get(node, []))
            block_width, block_height = leaf_block(len(children))
            tallest = max(tallest, block_height)
            columns = min(len(children), LEAF_COLUMNS) or 1
            # Off the top layer (a WAN router, a firewall): above it, clear of the links to the layers below
            above = level is levels[0] and len(levels) > 1
            for number, child in enumerate(children):
                row, column = divmod(number, columns)
                offset = (NODE_HEIGHT + LEAF_GAP) * (row + 1)
                positions[child] = (center - block_width / 2 + NODE_WIDTH / 2 + column * (NODE_WIDTH + H_GAP),
                                    y - offset if above else y + offset)
            x += slot + H_GAP
        if level is levels[0] and len(levels) > 1:
            tallest = 0
        y += NODE_HEIGHT + (tallest + LEAF_GAP if tallest else 0) + V_GAP
    return positions, width


def circle_component(group, adjacent, root=None, weight=lambda node: 0):
    """Rings round the top device by hops from it, each device near the ones it links to on the ring inside.
    Returns ({node: (x, y)}, width) with the top-left corner near (0, 0)."""
    if root not in group:
        root = max(group, key=lambda node: (len(adjacent[node]), weight(node), node))
    depth, order = {root: 0}, [root]
    for node in order:
        for other in sorted(adjacent[node]):
            if other not in depth:
                depth[other] = depth[node] + 1
                order.append(other)
    rings = {}
    for node in order:
        rings.setdefault(depth[node], []).append(node)
    angles, radii, radius = {root: -math.pi / 2}, [0], 0
    for level in range(1, len(rings)):
        ring = rings[level]
        wanted = {}
        for node in ring:  # The circular mean of where its links on the ring inside are
            inside = [angles[other] for other in adjacent[node] if other in angles]
            wanted[node] = math.atan2(sum(map(math.sin, inside)), sum(map(math.cos, inside))) if inside else 0
        ring.sort(key=lambda node: (wanted[node] + math.pi / 2) % (2 * math.pi))
        radius = max(radius + RING_GAP, len(ring) * (NODE_WIDTH + H_GAP) / (2 * math.pi))
        radii.append(radius)
        # The first ring starts at the top; two go either side, as the boxes are wider than they're tall
        start = wanted[ring[0]] if level > 1 else -math.pi / 2 + (math.pi / 2 if len(ring) == 2 else 0)
        for number, node in enumerate(ring):
            angles[node] = start + 2 * math.pi * number / len(ring)
    center = (radius + NODE_WIDTH / 2, radius + NODE_HEIGHT / 2)
    positions = {node: (center[0] + radii[depth[node]] * math.cos(angles[node]),
                        center[1] + radii[depth[node]] * math.sin(angles[node])) for node in order}
    return positions, 2 * radius + NODE_WIDTH


def layout(nodes, edges, root=None, weight=lambda node: 0, component=layout_component):
    """{node: (x, y)} for every node: connected groups side by side, largest first, lone devices in a row after."""
    adjacent = neighbors_of(nodes, edges)
    positions, x = {}, 0
    lone = []
    for group in components(adjacent):
        if len(group) == 1:
            lone.append(group[0])
            continue
        placed, width = component(group, adjacent, root, weight)
        for node, (node_x, node_y) in placed.items():
            positions[node] = (node_x + x, node_y)
        x += width + COMPONENT_GAP
    if lone:
        bottom = max((y for _, y in positions.values()), default=-V_GAP) + V_GAP + NODE_HEIGHT
        per_row = max(LEAF_COLUMNS, int(max(x, NODE_WIDTH) // (NODE_WIDTH + H_GAP)))
        for number, node in enumerate(sorted(lone)):
            row, column = divmod(number, per_row)
            positions[node] = (NODE_WIDTH / 2 + column * (NODE_WIDTH + H_GAP), bottom + row * (NODE_HEIGHT + LEAF_GAP))
    return positions


def overlaps(point, others):
    return any(abs(point[0] - x) < NODE_WIDTH + H_GAP / 2 and abs(point[1] - y) < NODE_HEIGHT + LEAF_GAP / 2
               for x, y in others)


def merge_positions(nodes, edges, saved, root=None, weight=lambda node: 0):
    """Keep where the user put devices; place new ones near their placed neighbors, clear of everything else."""
    fresh = layout(nodes, edges, root, weight)
    kept = {node: tuple(saved[node]) for node in nodes if node in saved}
    if not kept:
        return fresh
    adjacent = neighbors_of(nodes, edges)
    result = dict(kept)
    new_nodes = [node for node in nodes if node not in kept]
    bottom = max(y for _, y in kept.values()) + NODE_HEIGHT + V_GAP
    for node in sorted(new_nodes, key=lambda node: fresh[node][1]):
        anchors = [other for other in adjacent[node] if other in kept]
        if anchors:  # Move with the neighbor it was laid out beside
            anchor = anchors[0]
            point = (fresh[node][0] - fresh[anchor][0] + kept[anchor][0],
                     fresh[node][1] - fresh[anchor][1] + kept[anchor][1])
        else:
            point = (fresh[node][0], fresh[node][1] + bottom)
        while overlaps(point, result.values()):
            point = (point[0] + NODE_WIDTH + H_GAP, point[1])
        result[node] = point
    return result


# ----------------------------------------------------------------- Arranging


def arrange_flat(nodes, edges, style=TOP_DOWN, root=None, weight=lambda node: 0):
    """{node: (x, y)} in one of the arrangements, ignoring groups."""
    if style == CIRCLE:
        return layout(nodes, edges, root, weight, component=circle_component)
    positions = layout(nodes, edges, root, weight)
    if style == LEFT_RIGHT:  # The layers turned on their side, spaced for boxes that are wider than tall
        stretch = (NODE_WIDTH + H_GAP) / (NODE_HEIGHT + LEAF_GAP)
        return {node: (y * stretch, x / stretch) for node, (x, y) in positions.items()}
    if style == GRID:  # In the order the layers had them, so linked devices stay near each other
        order = sorted(positions, key=lambda node: (positions[node][1], positions[node][0], node))
        columns = max(1, round(math.sqrt(len(order) * 1.6 * GRID_ROW / (NODE_WIDTH + H_GAP))))
        return {node: (NODE_WIDTH / 2 + (number % columns) * (NODE_WIDTH + H_GAP),
                       NODE_HEIGHT / 2 + (number // columns) * GRID_ROW) for number, node in enumerate(order)}
    return positions


def normalized(positions):
    """The positions moved so the boxes' top-left corner is at (0, 0), and the (width, height) they take up."""
    if not positions:
        return {}, 0, 0
    left = min(x for x, _ in positions.values()) - NODE_WIDTH / 2
    top = min(y for _, y in positions.values()) - NODE_HEIGHT / 2
    moved = {node: (x - left, y - top) for node, (x, y) in positions.items()}
    return (moved, max(x for x, _ in moved.values()) + NODE_WIDTH / 2,
            max(y for _, y in moved.values()) + NODE_HEIGHT / 2)


def pack(sizes, gap=GROUP_GAP):
    """Tile blocks of [(width, height)] in rows, in order, about as wide as they're tall (a bit wider, like a
    screen), each row centered under the widest. Returns the top-left corner of each and the (width, height) of
    the lot."""
    if not sizes:
        return [], 0, 0
    area = sum((width + gap) * (height + gap) for width, height in sizes)
    limit = max(max(width for width, _ in sizes), math.sqrt(area * 1.6))
    rows, x, y, row_height = [[]], 0, 0, 0  # Rows of [index, x, y]
    for index, (width, height) in enumerate(sizes):
        if x and x + width > limit:
            rows.append([])
            x, y, row_height = 0, y + row_height + gap, 0
        rows[-1].append((index, x, y))
        row_height = max(row_height, height)
        x += width + gap
    widths = [max(x + sizes[index][0] for index, x, _ in row) for row in rows]
    corners = [None] * len(sizes)
    for row, row_width in zip(rows, widths):
        for index, x, y in row:
            corners[index] = (x + (max(widths) - row_width) / 2, y)
    return corners, max(widths), y + row_height


def arrange(nodes, edges, style=TOP_DOWN, root=None, weight=lambda node: 0, path_of=None, order=lambda key: key):
    """{node: (x, y)} in an arrangement. With path_of (node -> [site, building], or fewer, of group keys) each
    group is arranged on its own inside a box (GROUP_PAD round it, GROUP_TITLE above) and the boxes are tiled,
    the devices in no group first and then the groups in order(key) order."""
    path_of = path_of or {}
    if not any(path_of.get(node) for node in nodes):
        return arrange_flat(nodes, edges, style, root, weight)

    def place(members, level):
        direct = [node for node in members if len(path_of.get(node) or ()) <= level]
        inner = {}
        for node in members:
            if len(path_of.get(node) or ()) > level:
                inner.setdefault(path_of[node][level], []).append(node)
        blocks = []  # (positions, width, height)
        if direct:
            blocks.append(normalized(arrange_flat(direct, edges, style, root if root in direct else None, weight)))
        for key in sorted(inner, key=order):
            positions, width, height = place(inner[key], level + 1)
            blocks.append(({node: (x + GROUP_PAD, y + GROUP_PAD + GROUP_TITLE) for node, (x, y) in positions.items()},
                           width + 2 * GROUP_PAD, height + 2 * GROUP_PAD + GROUP_TITLE))
        corners, width, height = pack([(width, height) for _, width, height in blocks])
        placed = {}
        for (positions, _, _), (left, top) in zip(blocks, corners):
            placed.update({node: (x + left, y + top) for node, (x, y) in positions.items()})
        return placed, width, height

    return place(list(nodes), 0)[0]


def arrange_in_place(positions, edges, style=TOP_DOWN, root=None, weight=lambda node: 0, path_of=None,
                     order=lambda key: key):
    """Arrange just these devices ({node: (x, y)} where they are now), keeping the top-left corner of the lot
    where it was."""
    if not positions:
        return {}
    left = min(x for x, _ in positions.values()) - NODE_WIDTH / 2
    top = min(y for _, y in positions.values()) - NODE_HEIGHT / 2
    placed, _, _ = normalized(arrange(list(positions), edges, style, root, weight, path_of, order))
    return {node: (x + left, y + top) for node, (x, y) in placed.items()}


def align(positions, how, sizes=None):
    """Line up {node: (x, y)} by their left, center or right edges, or top, middle or bottom. sizes: {node:
    (width, height)} where they aren't all device-sized."""
    if not positions:
        return {}
    sizes = sizes or {}

    def half(node, axis):
        return sizes.get(node, (NODE_WIDTH, NODE_HEIGHT))[axis] / 2

    axis = 0 if how in (LEFT, CENTER, RIGHT) else 1
    low = min(point[axis] - half(node, axis) for node, point in positions.items())
    high = max(point[axis] + half(node, axis) for node, point in positions.items())
    result = {}
    for node, point in positions.items():
        if how in (LEFT, TOP):
            value = low + half(node, axis)
        elif how in (RIGHT, BOTTOM):
            value = high - half(node, axis)
        else:
            value = (low + high) / 2
        result[node] = (value, point[1]) if axis == 0 else (point[0], value)
    return result


def distribute(positions, direction, sizes=None):
    """Space {node: (x, y)} evenly across (horizontal) or down (vertical) between the first and the last, at
    least DISTRIBUTE_MIN_GAP apart so ones lined up on top of each other spread out."""
    if len(positions) < 2:
        return dict(positions)
    sizes = sizes or {}
    axis = 0 if direction == HORIZONTAL else 1
    nodes = sorted(positions, key=lambda node: (positions[node][axis], positions[node][1 - axis], node))
    extent = [sizes.get(node, (NODE_WIDTH, NODE_HEIGHT))[axis] for node in nodes]
    start = positions[nodes[0]][axis] - extent[0] / 2
    end = positions[nodes[-1]][axis] + extent[-1] / 2
    gap = max(DISTRIBUTE_MIN_GAP, (end - start - sum(extent)) / (len(nodes) - 1))
    result, edge = {}, start
    for node, size in zip(nodes, extent):
        result[node] = (edge + size / 2, positions[node][1]) if axis == 0 else (positions[node][0], edge + size / 2)
        edge += size + gap
    return result
