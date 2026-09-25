import struct

import pytest

from nomad.netbios import build_query, encode_name, parse_response


def name_entry(name, suffix, group=False):
    return name.encode("ascii").ljust(15) + bytes([suffix]) + struct.pack(">H", 0x8400 if group else 0x0400)


def response(transaction_id, names, mac=b"\x00\x11\x22\x33\x44\x55", compressed=False):
    body = bytes([len(names)]) + b"".join(names) + mac + b"\0" * 40  # Statistics that follow the MAC
    header = struct.pack(">HHHHHH", transaction_id, 0x8400, 0, 1, 0, 0)
    question = b"\xc0\x0c" if compressed else encode_name("*")
    return header + question + struct.pack(">HHIH", 0x21, 1, 0, len(body)) + body


def test_encode_wildcard_name():
    encoded = encode_name("*")
    assert len(encoded) == 34 and encoded[0] == 32 and encoded[-1] == 0
    assert encoded[1:3] == b"CK"  # "*" is 0x2A
    assert encoded[3:33] == b"A" * 30


def test_build_query():
    query = build_query(0x1234)
    assert query[:2] == b"\x12\x34" and query[-4:] == b"\x00\x21\x00\x01"


@pytest.mark.parametrize("compressed", [False, True])
def test_parse_response(compressed):
    data = response(7, [name_entry("WORKGROUP", 0x00, group=True), name_entry("DESKTOP-42", 0x00),
                        name_entry("DESKTOP-42", 0x20)], compressed=compressed)
    status = parse_response(data, 7)
    assert status.name == "DESKTOP-42" and status.group == "WORKGROUP" and status.mac == "00-11-22-33-44-55"


def test_parse_response_without_mac():
    status = parse_response(response(1, [name_entry("NAS", 0x00)], mac=b"\0" * 6))
    assert status.name == "NAS" and status.mac == ""


def test_parse_rejects_other_responses():
    data = response(7, [name_entry("PC", 0x00)])
    with pytest.raises(ValueError):
        parse_response(data, 8)
    with pytest.raises(ValueError):
        parse_response(data[:20])
    with pytest.raises(ValueError):
        parse_response(build_query(7))  # A request, not a response
