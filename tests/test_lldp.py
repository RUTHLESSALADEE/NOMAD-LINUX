import struct

from nomad.lldp import neighbors_from_pcapng, parse_frame, read_pcapng

SWITCH_MAC = bytes.fromhex("00259c112233")


def tlv(kind, value):
    return struct.pack(">H", kind << 9 | len(value)) + value


def lldp_frame(vlan_tag=False):
    payload = (tlv(1, b"\x04" + SWITCH_MAC) + tlv(2, b"\x05Gi1/0/24") + tlv(3, struct.pack(">H", 120)) +
               tlv(4, b"Office desk 12") + tlv(5, b"core-sw-01") + tlv(6, b"Cisco IOS 15.2") +
               tlv(7, struct.pack(">HH", 0x14, 0x04)) +  # Bridge and router capable; bridge enabled
               tlv(8, bytes([5, 1]) + bytes([10, 0, 0, 2]) + b"\x02\x00\x00\x00\x01\x00") +
               tlv(127, b"\x00\x80\xc2\x01" + struct.pack(">H", 20)) +
               tlv(127, b"\x00\x80\xc2\x03" + struct.pack(">HB", 20, 5) + b"Staff") +
               tlv(127, b"\x00\x12\xbb\x02\x01" + struct.pack(">I", (30 << 9) | (5 << 6) | 46)[1:]) +
               tlv(0, b""))
    header = bytes.fromhex("0180c200000e") + SWITCH_MAC
    if vlan_tag:
        header += struct.pack(">HH", 0x8100, 20)
    return header + struct.pack(">H", 0x88CC) + payload


def cdp_frame():
    def cdp_tlv(kind, value):
        return struct.pack(">HH", kind, len(value) + 4) + value
    address = struct.pack(">I", 1) + bytes([1, 1, 0xCC]) + struct.pack(">H", 4) + bytes([10, 0, 0, 3])
    payload = (bytes([2, 180, 0, 0]) + cdp_tlv(1, b"access-sw-02.lab") + cdp_tlv(2, address) +
               cdp_tlv(3, b"FastEthernet0/7") + cdp_tlv(4, struct.pack(">I", 0x28)) +
               cdp_tlv(5, b"Cisco IOS Software, C2960\nVersion 15.0") + cdp_tlv(6, b"cisco WS-C2960-24TT-L") +
               cdp_tlv(0x0A, struct.pack(">H", 10)) + cdp_tlv(0x0B, b"\x01") +
               cdp_tlv(0x0E, b"\x01" + struct.pack(">H", 110)))
    llc = bytes.fromhex("AAAA0300000C2000")
    return bytes.fromhex("01000ccccccc") + SWITCH_MAC + struct.pack(">H", len(llc) + len(payload)) + llc + payload


def test_parse_lldp():
    neighbor = parse_frame(lldp_frame(), "Ethernet")
    assert neighbor.protocol == "LLDP" and neighbor.interface == "Ethernet"
    assert neighbor.device_id == "00-25-9C-11-22-33" and neighbor.source_mac == "00-25-9C-11-22-33"
    assert (neighbor.system_name, neighbor.port_id, neighbor.port_description) == \
        ("core-sw-01", "Gi1/0/24", "Office desk 12")
    assert neighbor.ttl == 120 and neighbor.capabilities == ["Bridge"]
    assert neighbor.management_addresses == ["10.0.0.2"]
    assert (neighbor.native_vlan, neighbor.voice_vlan, neighbor.vlan_names) == ("20", "30", ["20 Staff"])
    assert neighbor.name == "core-sw-01"


def test_parse_vlan_tagged_lldp():
    assert parse_frame(lldp_frame(vlan_tag=True)).port_id == "Gi1/0/24"


def test_parse_cdp():
    neighbor = parse_frame(cdp_frame())
    assert neighbor.protocol == "CDP" and neighbor.device_id == "access-sw-02.lab"
    assert neighbor.port_id == "FastEthernet0/7" and neighbor.platform == "cisco WS-C2960-24TT-L"
    assert neighbor.capabilities == ["Switch", "IGMP"] and neighbor.management_addresses == ["10.0.0.3"]
    assert (neighbor.native_vlan, neighbor.voice_vlan, neighbor.duplex) == ("10", "110", "Full")
    assert neighbor.name == "access-sw-02.lab" and neighbor.ttl == 180


def test_other_frames_are_ignored():
    arp = bytes.fromhex("ffffffffffff") + SWITCH_MAC + struct.pack(">H", 0x0806) + bytes(28)
    assert parse_frame(arp) is None and parse_frame(b"\x00" * 10) is None


def block(block_type, body):
    body += b"\0" * (-len(body) % 4)
    length = len(body) + 12
    return struct.pack("<II", block_type, length) + body + struct.pack("<I", length)


def pcapng(frames, interface_name="Ethernet 2"):
    section = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    name = interface_name.encode()
    options = struct.pack("<HH", 2, len(name)) + name + b"\0" * (-len(name) % 4) + struct.pack("<HH", 0, 0)
    interfaces = block(1, struct.pack("<HHI", 1, 0, 0) + options)
    packets = b"".join(block(6, struct.pack("<IIIII", 0, 0, 0, len(frame), len(frame)) + frame) for frame in frames)
    return section + interfaces + packets


def test_read_pcapng():
    data = pcapng([lldp_frame(), b"\x01" * 60, cdp_frame(), lldp_frame()])
    frames = list(read_pcapng(data))
    assert len(frames) == 4 and frames[0][0] == "Ethernet 2"
    neighbors = neighbors_from_pcapng(data)
    assert sorted(neighbor.protocol for neighbor in neighbors) == ["CDP", "LLDP"]  # Repeats are merged
    assert all(neighbor.interface == "Ethernet 2" for neighbor in neighbors)


def test_truncated_pcapng_stops_cleanly():
    data = pcapng([lldp_frame()])
    assert neighbors_from_pcapng(data[:-10]) == []
    assert list(read_pcapng(b"")) == []
