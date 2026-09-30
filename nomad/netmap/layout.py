"""Placing a map's devices: layers by hop distance from the top device, ordered to keep links from crossing.

Devices with a single link (access points, a lone router) sit in a small grid under the device they hang off, so a
switch with thirty access points doesn't make its layer thirty wide. Positions are the centers of the devices.
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
            above = level is levels[0]  # Off the top layer (a WAN router, a firewall): above it, clear of its links
            for number, child in enumerate(children):
                row, column = divmod(number, columns)
                offset = (NODE_HEIGHT + LEAF_GAP) * (row + 1)
                positions[child] = (center - block_width / 2 + NODE_WIDTH / 2 + column * (NODE_WIDTH + H_GAP),
                                    y - offset if above else y + offset)
            x += slot + H_GAP
        if level is levels[0]:
            tallest = 0
        y += NODE_HEIGHT + (tallest + LEAF_GAP if tallest else 0) + V_GAP
    return positions, width


def layout(nodes, edges, root=None, weight=lambda node: 0):
    """{node: (x, y)} for every node: connected groups side by side, largest first, lone devices in a row after."""
    adjacent = neighbors_of(nodes, edges)
    positions, x = {}, 0
    lone = []
    for group in components(adjacent):
        if len(group) == 1:
            lone.append(group[0])
            continue
        placed, width = layout_component(group, adjacent, root, weight)
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
