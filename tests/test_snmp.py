import socket
import threading

import pytest

from nomad import snmp
from nomad.snmp import COUNTER32, END_OF_MIB_VIEW, GAUGE32, GET, GET_BULK, GET_NEXT, INTEGER, NULL, OBJECT_ID, \
    OCTET_STRING, RESPONSE, SEQUENCE, TIMETICKS, V1, V2C, SnmpClient, SnmpError, Value, build_request, decode_oid, \
    encode_integer, encode_oid, format_value, interface_summary, oid_name, parse_oid, parse_response, read_tlv, tlv

MIB = {
    (1, 3, 6, 1, 2, 1, 1, 1, 0): Value(OCTET_STRING, b"Cisco IOS Software, C2960"),
    (1, 3, 6, 1, 2, 1, 1, 3, 0): Value(TIMETICKS, 8640012345),
    (1, 3, 6, 1, 2, 1, 1, 5, 0): Value(OCTET_STRING, b"access-sw-02"),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 2, 1): Value(OCTET_STRING, b"GigabitEthernet0/1"),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 2, 2): Value(OCTET_STRING, b"GigabitEthernet0/2"),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 3, 1): Value(INTEGER, 6),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 3, 2): Value(INTEGER, 6),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 5, 1): Value(GAUGE32, 1000000000),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 5, 2): Value(GAUGE32, 100000000),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 6, 1): Value(OCTET_STRING, bytes.fromhex("0011223344aa")),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 7, 1): Value(INTEGER, 1),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 7, 2): Value(INTEGER, 1),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 1): Value(INTEGER, 1),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 2): Value(INTEGER, 2),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 14, 1): Value(COUNTER32, 0),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 14, 2): Value(COUNTER32, 1234),
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1, 1): Value(OCTET_STRING, b"Gi0/1"),
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1, 2): Value(OCTET_STRING, b"Gi0/2"),
    (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 18, 1): Value(OCTET_STRING, b"Uplink to core"),
}


def encode_value(value):
    if value.tag in (INTEGER,):
        return encode_integer(value.value)
    if value.tag in (COUNTER32, GAUGE32, TIMETICKS):
        raw = value.value.to_bytes(max(1, (value.value.bit_length() + 8) // 8), "big")
        return tlv(value.tag, raw)
    if value.tag == OCTET_STRING:
        return tlv(OCTET_STRING, value.value)
    if value.tag == OBJECT_ID:
        return encode_oid(value.value)
    return tlv(value.tag, b"")


class FakeAgent:
    """Answers GET, GETNEXT and GETBULK from MIB for one community, like a small switch would."""

    def __init__(self, community="public", mib=MIB):
        self.community, self.mib = community, dict(sorted(mib.items()))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.requests = []
        threading.Thread(target=self.serve, daemon=True).start()

    def next_after(self, oid):
        return next(((key, value) for key, value in self.mib.items() if key > oid), None)

    def serve(self):
        while True:
            try:
                data, client = self.sock.recvfrom(65535)
            except OSError:
                return
            _, message, _ = read_tlv(data, 0)
            _, version_raw, offset = read_tlv(message, 0)
            _, community, offset = read_tlv(message, offset)
            if community.decode() != self.community:
                continue  # Wrong community: real agents stay silent
            pdu_type, pdu, _ = read_tlv(message, offset)
            fields, offset = [], 0
            for _ in range(3):
                _, raw, offset = read_tlv(pdu, offset)
                fields.append(int.from_bytes(raw, "big", signed=True))
            _, varbinds, _ = read_tlv(pdu, offset)
            _, varbind, _ = read_tlv(varbinds, 0)
            _, oid_raw, _ = read_tlv(varbind, 0)
            oid = decode_oid(oid_raw)
            self.requests.append(pdu_type)
            version = int.from_bytes(version_raw, "big")
            results, error = [], 0
            if pdu_type == GET:
                results = [(oid, self.mib.get(oid, Value(0x81, None)))]
            elif pdu_type == GET_NEXT:
                found = self.next_after(oid)
                if found:
                    results = [found]
                elif version == V1:
                    error, results = 2, [(oid, Value(NULL, None))]  # noSuchName
                else:
                    results = [(oid, Value(END_OF_MIB_VIEW, None))]
            elif pdu_type == GET_BULK:
                current = oid
                for _ in range(fields[2]):
                    found = self.next_after(current)
                    if not found:
                        results.append((current, Value(END_OF_MIB_VIEW, None)))
                        break
                    results.append(found)
                    current = found[0]
            body = b"".join(tlv(SEQUENCE, encode_oid(key) + encode_value(value)) for key, value in results)
            pdu_out = tlv(RESPONSE, encode_integer(fields[0]) + encode_integer(error) + encode_integer(0) +
                          tlv(SEQUENCE, body))
            reply = tlv(SEQUENCE, encode_integer(version) + tlv(OCTET_STRING, community) + pdu_out)
            self.sock.sendto(reply, client)

    def close(self):
        self.sock.close()


@pytest.fixture
def agent():
    fake = FakeAgent()
    yield fake
    fake.close()


def test_ber_round_trips():
    for number in (0, 1, 127, 128, 255, 256, -1, -129, 2 ** 31 - 1):
        tag, raw, _ = read_tlv(encode_integer(number), 0)
        assert int.from_bytes(raw, "big", signed=True) == number
    oid = (1, 3, 6, 1, 4, 1, 311, 21, 13, 128, 16383, 2 ** 28)
    assert decode_oid(read_tlv(encode_oid(oid), 0)[1]) == oid
    long_value = tlv(OCTET_STRING, b"x" * 300)
    assert read_tlv(long_value, 0)[1] == b"x" * 300


def test_parse_oid():
    assert parse_oid(".1.3.6.1.2.1.1.5.0") == (1, 3, 6, 1, 2, 1, 1, 5, 0)
    for bad in ("", "1", "system", "1.3.x", "3.1", "1.40"):
        with pytest.raises(ValueError):
            parse_oid(bad)


def test_request_and_response_parsing():
    request = build_request(V2C, "public", GET_BULK, 42, [(1, 3, 6, 1)], max_repetitions=10)
    assert request[0] == SEQUENCE
    with pytest.raises(ValueError):
        parse_response(request)  # A request, not a response
    with pytest.raises(ValueError):
        parse_response(b"\x30\x05\x02")


def test_get(agent):
    client = SnmpClient("127.0.0.1", port=agent.port, timeout=1000)
    [(oid, value)] = client.get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    assert oid_name(oid) == "sysName.0" and format_value(oid, value) == "access-sw-02"
    [(oid, value)] = client.get([(1, 3, 6, 1, 2, 1, 1, 9, 0)])
    assert value.is_exception and format_value(oid, value) == "(No such instance)"


@pytest.mark.parametrize("version, request_type", [(V2C, GET_BULK), (V1, GET_NEXT)])
def test_walk_stays_inside_the_subtree(agent, version, request_type):
    client = SnmpClient("127.0.0.1", version=version, port=agent.port, timeout=1000)
    rows = list(client.walk((1, 3, 6, 1, 2, 1, 1), max_repetitions=2))
    assert [oid_name(oid) for oid, _ in rows] == ["sysDescr.0", "sysUpTime.0", "sysName.0"]
    assert format_value(*rows[1]) == "1000d 0h 2m 3s"
    assert set(agent.requests) == {request_type}
    everything = list(client.walk((1, 3, 6, 1)))  # Runs off the end of the MIB
    assert len(everything) == len(MIB)


def test_wrong_community_times_out(agent):
    client = SnmpClient("127.0.0.1", community="private", port=agent.port, timeout=200, retries=0)
    with pytest.raises(SnmpError, match="No answer"):
        client.get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])


def test_interface_summary(agent):
    rows = interface_summary(SnmpClient("127.0.0.1", port=agent.port, timeout=1000))
    first, second = rows
    assert (first.index, first.name, first.description, first.alias) == (1, "Gi0/1", "GigabitEthernet0/1",
                                                                          "Uplink to core")
    assert (first.kind, first.admin, first.oper, first.speed_mbps, first.mac) == ("ethernet", "up", "up", 1000,
                                                                                  "00-11-22-33-44-AA")
    assert (second.oper, second.speed_mbps, second.in_errors) == ("down", 100, 1234)


def test_formatting():
    assert format_value((1, 3, 6, 1, 2, 1, 2, 2, 1, 8, 5), Value(INTEGER, 7)) == "lowerLayerDown"
    assert format_value((1, 3, 6, 1, 2, 1, 1, 2, 0), Value(OBJECT_ID, (1, 3, 6, 1, 4, 1, 9, 1, 1208))) == \
        "cisco.1.1208 (1.3.6.1.4.1.9.1.1208)"
    assert format_value((1, 3, 6, 1, 9), Value(OCTET_STRING, b"\x00\x01\xff")) == "00 01 ff"
    assert oid_name((1, 3, 6, 1, 99)) == "1.3.6.1.99"
    assert snmp.community_is_valid("public") and not snmp.community_is_valid("")
