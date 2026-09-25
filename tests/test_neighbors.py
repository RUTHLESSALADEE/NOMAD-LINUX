from nomad.neighbors import MacHistory, Neighbor, build_delete_neighbor_script, parse_neighbors, shared_macs

GATEWAY_MAC = "00-1E-14-D9-A3-BF"


def neighbor(address, mac, state="Reachable", interface="11", family=4):
    return Neighbor(interface, "Ethernet", address, mac, state, family)


def test_parse_neighbors():
    data = {
        "neighbors": [
            {"InterfaceIndex": 11, "InterfaceAlias": "Ethernet", "IPAddress": "192.168.1.1",
             "LinkLayerAddress": "001E14D9A3BF", "State": 5, "AddressFamily": 2},
            {"InterfaceIndex": 11, "InterfaceAlias": "Ethernet", "IPAddress": "192.168.1.9",
             "LinkLayerAddress": "00-00-00-00-00-00", "State": 0, "AddressFamily": 2},
            {"InterfaceIndex": 11, "InterfaceAlias": "Ethernet", "IPAddress": "fe80::1%11",
             "LinkLayerAddress": "00-1E-14-D9-A3-BF", "State": 4, "AddressFamily": 23},
            {"InterfaceIndex": 11, "IPAddress": "not an address", "State": 5, "AddressFamily": 2},
        ],
        "addresses": {"InterfaceAlias": "Ethernet", "IPAddress": "192.168.1.50", "AddressState": 2},
    }
    table = parse_neighbors(data)
    assert [(item.address, item.mac, item.state, item.family) for item in table.neighbors] == [
        ("192.168.1.1", GATEWAY_MAC, "Reachable", 4),
        ("192.168.1.9", "", "Unreachable", 4),
        ("fe80::1%11", GATEWAY_MAC, "Stale", 6),
    ]
    assert table.neighbors[0].vendor.startswith("Cisco")
    assert table.duplicate_addresses == [("Ethernet", "192.168.1.50")]
    assert parse_neighbors(None).neighbors == []


def test_multicast_entries():
    assert neighbor("224.0.0.22", "01-00-5E-00-00-16", "Permanent").is_multicast
    assert neighbor("192.168.1.255", "FF-FF-FF-FF-FF-FF", "Permanent").is_multicast
    assert not neighbor("192.168.1.1", GATEWAY_MAC).is_multicast


def test_shared_macs():
    neighbors = [neighbor("192.168.1.1", GATEWAY_MAC), neighbor("192.168.1.66", GATEWAY_MAC),
                 neighbor("192.168.1.9", "00-11-22-33-44-55"),
                 neighbor("192.168.1.10", "00-11-22-33-44-55", interface="12"),  # Another network
                 neighbor("192.168.1.255", "FF-FF-FF-FF-FF-FF", "Permanent"),
                 neighbor("192.168.1.254", "FF-FF-FF-FF-FF-FF", "Permanent")]
    assert shared_macs(neighbors) == {("11", GATEWAY_MAC): ["192.168.1.1", "192.168.1.66"]}


def test_mac_history_notices_changes():
    history = MacHistory()
    first = neighbor("192.168.1.20", "00-11-22-33-44-55")
    assert history.update([first]) == []
    second = neighbor("192.168.1.20", "66-77-88-99-AA-BB")
    changed = history.update([second])
    assert changed == [(second, "00-11-22-33-44-55")]
    assert history.earlier_macs(second) == ["00-11-22-33-44-55"]
    back = neighbor("192.168.1.20", "00-11-22-33-44-55")
    history.update([back])
    assert history.earlier_macs(back) == ["66-77-88-99-AA-BB"]
    assert history.update([neighbor("192.168.1.20", "")]) == []  # Unresolved entries aren't changes


def test_delete_script_quotes_address():
    script = build_delete_neighbor_script(neighbor("fe80::1%11", GATEWAY_MAC, family=6))
    assert script == "Remove-NetNeighbor -InterfaceIndex 11 -IPAddress 'fe80::1' -Confirm:$false"
