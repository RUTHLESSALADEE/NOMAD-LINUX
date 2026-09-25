import socket

from nomad.connections import TCP, UDP, Connection, ipv4_from_dword, ipv6_text, list_connections, matches, \
    port_from_dword


def test_table_field_conversions():
    assert port_from_dword(socket.htons(443)) == 443
    assert ipv4_from_dword(int.from_bytes(socket.inet_aton("192.168.1.10"), "little")) == "192.168.1.10"
    assert ipv6_text(bytes(15) + b"\x01", 0) == "::1"
    link_local = bytes.fromhex("fe800000000000000000000000000001")
    assert ipv6_text(link_local, 11) == "fe80::1%11"


def test_filter_matches_ports_exactly():
    web = Connection(TCP, "192.168.1.10", 50000, "142.250.1.1", 443, "Established", 1234, "chrome.exe")
    assert matches(web, "443") and matches(web, "chrome 443") and matches(web, "ESTABLISHED")
    assert not matches(web, "4430") and not matches(web, "44")
    assert matches(web, "1234")  # PID
    dns = Connection(UDP, "0.0.0.0", 53, "", 0, "", 2000, "dns.exe")
    assert dns.listening and matches(dns, "53") and not matches(dns, "0 dns")


def test_lists_a_listening_socket_with_its_program():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        found = [connection for connection in list_connections()
                 if connection.protocol == TCP and connection.local_port == port]
    assert found and found[0].listening and found[0].local_address == "127.0.0.1"
    assert found[0].process.lower().startswith("python")
