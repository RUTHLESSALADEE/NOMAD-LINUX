import socket
import struct

import pytest

from nomad.dnsclient import TYPE_A, DnsAnswer, benchmark_server, build_query, forward_reverse, parse_response, \
    read_name, reverse_name


def answer_record(name_pointer, record_type, rdata):
    return struct.pack(">HHHIH", name_pointer, record_type, 1, 300, len(rdata)) + rdata


def response(transaction_id, rcode=0, records=()):
    query = build_query(transaction_id, "www.example.com", TYPE_A)
    header = struct.pack(">HHHHHH", transaction_id, 0x8180 | rcode, 1, len(records), 0, 0)
    return header + query[12:] + b"".join(records)


def test_build_query():
    query = build_query(0xBEEF, "www.example.com.", TYPE_A)
    assert query[:4] == b"\xbe\xef\x01\x00"
    assert query[12:] == b"\x03www\x07example\x03com\x00\x00\x01\x00\x01"


def test_parse_answers_with_compression():
    cname = b"\x03cdn\xc0\x10"  # cdn.example.com, pointing at "example.com" in the question
    records = [answer_record(0xC00C, 5, cname), answer_record(0xC00C, 1, socket.inet_aton("93.184.216.34"))]
    answer = parse_response(response(7, records=records), 7)
    assert answer.rcode_name == "NOERROR"
    assert answer.records == [("CNAME", "cdn.example.com"), ("A", "93.184.216.34")]
    assert answer.values("A") == ["93.184.216.34"]


def test_parse_errors():
    assert parse_response(response(7, rcode=3)).rcode_name == "NXDOMAIN"
    with pytest.raises(ValueError):
        parse_response(response(7), 8)
    with pytest.raises(ValueError):
        parse_response(build_query(7, "example.com", TYPE_A))  # A query, not a response
    with pytest.raises(ValueError):
        parse_response(b"\x00\x07\x81")
    looping = b"\x00" * 12 + b"\xc0\x0c"
    with pytest.raises(ValueError):
        read_name(looping, 12)


def test_reverse_name():
    assert reverse_name("192.168.1.10") == "10.1.168.192.in-addr.arpa"
    assert reverse_name("fe80::1%11").endswith("ip6.arpa")


def test_benchmark_counts_nxdomain_as_answered():
    replies = iter([DnsAnswer(0, 12.0), DnsAnswer(3, 4.0), TimeoutError(), DnsAnswer(2, 3.0)])

    def ask(server, name, record_type, timeout):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = benchmark_server("10.0.0.53", ["a.example", "b.example"], rounds=2, ask=ask)
    assert result.sent == 4 and result.answered == 2
    assert result.first_rtt == 12.0 and result.average == 8.0 and result.median == 8.0
    assert result.failures == {"timed out": 1, "SERVFAIL": 1}
    assert result.status == "timed out, SERVFAIL"


def test_benchmark_all_failed():
    def ask(server, name, record_type, timeout):
        raise OSError("10.0.0.1 isn't running a DNS server (port 53 is closed).")

    result = benchmark_server("10.0.0.1", ["a.example"], rounds=2, ask=ask)
    assert result.answered == 0 and result.status.startswith("Failed:") and "×2" in result.status


def test_forward_reverse():
    records = {
        ("host.example", "A"): [("A", "10.0.0.5"), ("A", "10.0.0.6")],
        ("5.0.0.10.in-addr.arpa", "PTR"): [("PTR", "host.example")],
        ("6.0.0.10.in-addr.arpa", "PTR"): [("PTR", "other.example")],
        ("other.example", "A"): [("A", "10.9.9.9")],
    }
    names = {1: "A", 28: "AAAA", 12: "PTR"}

    def ask(server, name, record_type, timeout):
        return DnsAnswer(0, 1.0, records.get((name, names[record_type]), []))

    addresses, checks = forward_reverse("host.example", "10.0.0.53", ask=ask)
    assert addresses == ["10.0.0.5", "10.0.0.6"]
    assert [(check.address, check.matches) for check in checks] == [("10.0.0.5", True), ("10.0.0.6", False)]
    with pytest.raises(ValueError, match="no A or AAAA"):
        forward_reverse("missing.example", "10.0.0.53", ask=ask)


def test_query_against_a_local_server():
    from nomad.dnsclient import query
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        import nomad.dnsclient as dnsclient
        original = dnsclient.DNS_PORT
        dnsclient.DNS_PORT = port
        try:
            import threading

            def serve():
                data, client = server.recvfrom(512)
                transaction_id = struct.unpack(">H", data[:2])[0]
                reply = struct.pack(">HHHHHH", transaction_id, 0x8180, 1, 1, 0, 0) + data[12:] + \
                    answer_record(0xC00C, 1, socket.inet_aton("10.1.2.3"))
                server.sendto(reply, client)

            threading.Thread(target=serve, daemon=True).start()
            answer = query("127.0.0.1", "test.example", TYPE_A, 2000)
        finally:
            dnsclient.DNS_PORT = original
    assert answer.values("A") == ["10.1.2.3"] and answer.rtt >= 0


def test_forward_reverse_reports_a_silent_server():
    def ask(server, name, record_type, timeout):
        raise TimeoutError(f"{server} didn't answer within {timeout} ms.")

    with pytest.raises(ValueError, match="didn't answer"):
        forward_reverse("host.example", "10.0.0.53", ask=ask)
