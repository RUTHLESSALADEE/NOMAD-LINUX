"""Unwrapping ERSPAN captures: types I, II and III, fragments put back together, duplicates dropped, pcap and
pcapng read, and only the session (and switch) asked for kept."""
import struct

import pytest

from nomad import erspan

SWITCH, PC, OTHER = bytes([10, 0, 0, 1]), bytes([10, 0, 10, 9]), bytes([10, 0, 0, 2])


def inner_frame(number, size=60):
    """An Ethernet frame mirrored: numbered in its payload so each can be told apart."""
    payload = struct.pack("!I", number) + b"x" * (size - 18)
    return b"\x00\x11\x22\x33\x44\x55" + b"\x00\x50\x56\xaa\x00\x05" + b"\x08\x00" + payload


def gre_type2(frame, session=12, sequence=1):
    header = struct.pack("!HHI", 0x1000, erspan.ERSPAN_II, sequence)
    span = struct.pack("!HHI", (1 << 12) | 10, session, 0)
    return header + span + frame


def gre_type1(frame):
    return struct.pack("!HH", 0, erspan.ERSPAN_II) + frame


def gre_type3(frame, session=7, subheader=True):
    header = struct.pack("!HHI", 0x1000, erspan.ERSPAN_III, 1)
    word3 = struct.pack("!HBB", 0, 0, 1 if subheader else 0)
    span = struct.pack("!HHI", (2 << 12), session, 123456) + word3 + (b"\x00" * 8 if subheader else b"")
    return header + span + frame


def ipv4(payload, source=SWITCH, ident=1, protocol=erspan.GRE, offset=0, more=False):
    flags = (0x2000 if more else 0) | (offset // 8)
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, flags, 64, protocol, 0, source, PC)
    return header + payload


def ethernet(ip, vlan=None):
    tag = struct.pack("!HH", 0x8100, vlan) if vlan else b""
    return b"\xaa" * 6 + b"\xbb" * 6 + tag + b"\x08\x00" + ip


def write_pcapng(path, packets):
    with open(path, "wb") as file:
        file.write(erspan.header_blocks("test"))
        for timestamp, packet in packets:
            file.write(erspan.packet_block(timestamp, packet))


def frames_in(path):
    return [packet for _, _, packet in erspan.read_packets(path)]


def test_type_two_is_unwrapped_and_other_traffic_left_out(tmp_path):
    source, output = tmp_path / "c.pcapng", tmp_path / "u.pcapng"
    write_pcapng(source, [(1.0, ethernet(ipv4(gre_type2(inner_frame(1))))),
                          (1.1, ethernet(ipv4(b"\x00" * 8, protocol=17, ident=2))),  # UDP: not ERSPAN
                          (1.2, ethernet(ipv4(gre_type2(inner_frame(2), sequence=2), ident=3), vlan=10))])
    result = erspan.unwrap(source, output)
    assert result.frames == 2 and result.other == 1
    assert result.sessions == {12: 2} and result.senders == {"10.0.0.1": 2}
    assert frames_in(output) == [inner_frame(1), inner_frame(2)]
    times = [timestamp for _, timestamp, _ in erspan.read_packets(output)]
    assert times == pytest.approx([1.0, 1.2])
    assert "2 mirrored frames from 10.0.0.1" in result.summary()


def test_types_one_and_three(tmp_path):
    source, output = tmp_path / "c.pcapng", tmp_path / "u.pcapng"
    write_pcapng(source, [(1.0, ethernet(ipv4(gre_type1(inner_frame(1))))),
                          (2.0, ethernet(ipv4(gre_type3(inner_frame(2)), ident=2))),
                          (3.0, ethernet(ipv4(gre_type3(inner_frame(3), subheader=False), ident=3)))])
    result = erspan.unwrap(source, output)
    assert frames_in(output) == [inner_frame(1), inner_frame(2), inner_frame(3)]
    assert result.sessions == {0: 1, 7: 2}


def test_fragments_are_put_back_together_and_duplicates_dropped(tmp_path):
    big = gre_type2(inner_frame(9, size=1514))
    first, second = big[:1480], big[1480:]
    one = ethernet(ipv4(first, ident=77, more=True))
    two = ethernet(ipv4(second, ident=77, offset=1480))
    source, output = tmp_path / "c.pcapng", tmp_path / "u.pcapng"
    write_pcapng(source, [(1.0, two), (1.0001, two), (1.001, one),  # Out of order, and one captured twice
                          (2.0, ethernet(ipv4(first, ident=78, more=True)))])  # Its second piece never came
    result = erspan.unwrap(source, output)
    assert frames_in(output) == [inner_frame(9, size=1514)]
    assert result.duplicates == 1 and result.incomplete == 1


def test_only_the_session_and_switch_asked_for(tmp_path):
    source, output = tmp_path / "c.pcapng", tmp_path / "u.pcapng"
    write_pcapng(source, [(1.0, ethernet(ipv4(gre_type2(inner_frame(1), session=12)))),
                          (1.1, ethernet(ipv4(gre_type2(inner_frame(2), session=13), ident=2))),
                          (1.2, ethernet(ipv4(gre_type2(inner_frame(3), session=12), source=OTHER, ident=3)))])
    result = erspan.unwrap(source, output, erspan_id=12, sender="10.0.0.1")
    assert frames_in(output) == [inner_frame(1)] and result.left_out == 2
    empty = erspan.unwrap(source, output, erspan_id=99)
    assert empty.frames == 0 and "for that session" in empty.summary()


def test_classic_pcap_and_raw_ip(tmp_path):
    source, output = tmp_path / "c.pcap", tmp_path / "u.pcapng"
    packet = ipv4(gre_type2(inner_frame(1)))
    with open(source, "wb") as file:
        file.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))
        file.write(struct.pack("<IIII", 5, 250000, len(packet), len(packet)) + packet)
    result = erspan.unwrap(source, output)
    assert result.frames == 1 and frames_in(output) == [inner_frame(1)]
    assert [timestamp for _, timestamp, _ in erspan.read_packets(output)] == pytest.approx([5.25])


def test_not_a_capture(tmp_path):
    path = tmp_path / "x.pcapng"
    path.write_bytes(b"hello, this isn't a capture file at all")
    with pytest.raises(erspan.CaptureError):
        erspan.unwrap(path, tmp_path / "u.pcapng")
    assert erspan.unwrapped_path("C:/x/NOMAD capture.pcapng") == "C:/x/NOMAD capture (unwrapped).pcapng"
