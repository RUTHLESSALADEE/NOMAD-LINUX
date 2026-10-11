"""Packets for the capture viewer's tests, built byte by byte: a little of every protocol it decodes, and the trouble
its problem finder looks for."""
import ipaddress
import struct

from nomad import erspan

PC, ROUTER, SWITCH, SERVER2 = "00:50:56:aa:00:09", "00:1e:14:d9:a3:bf", "00:1e:14:00:00:01", "00:50:56:aa:00:22"
BROADCAST = "ff:ff:ff:ff:ff:ff"


def mac(text):
    return bytes.fromhex(text.replace(":", ""))


def ip(text):
    return ipaddress.ip_address(text).packed


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ethernet(source, destination, ethertype, payload, vlan=None):
    tag = struct.pack("!HH", 0x8100, vlan) if vlan is not None else b""
    return mac(destination) + mac(source) + tag + struct.pack("!H", ethertype) + payload


def ipv4(source, destination, protocol, payload, ttl=64, ident=1, flags=0x4000):
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, flags, ttl, protocol, 0, ip(source),
                         ip(destination))
    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    return header + payload


def udp(source_port, destination_port, payload):
    return struct.pack("!HHHH", source_port, destination_port, 8 + len(payload), 0) + payload


def tcp(source_port, destination_port, seq, ack, flags, payload=b"", window=64240, options=b""):
    options += b"\0" * (-len(options) % 4)
    offset = (20 + len(options)) // 4
    return struct.pack("!HHIIHHHH", source_port, destination_port, seq, ack, (offset << 12) | flags, window, 0, 0) + \
        options + payload


SYN, ACK, PSH, FIN, RST = 0x002, 0x010, 0x008, 0x001, 0x004
SYN_OPTIONS = b"\x02\x04\x05\xb4\x01\x03\x03\x08\x04\x02"  # MSS 1460, WS 8, SACK permitted


def frame_ip(source_mac, destination_mac, source, destination, protocol, payload, **kwargs):
    vlan = kwargs.pop("vlan", None)
    return ethernet(source_mac, destination_mac, 0x0800, ipv4(source, destination, protocol, payload, **kwargs), vlan)


def arp(source_mac, destination_mac, opcode, sender_mac, sender_ip, target_mac, target_ip):
    body = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, opcode, mac(sender_mac), ip(sender_ip), mac(target_mac),
                       ip(target_ip))
    return ethernet(source_mac, destination_mac, 0x0806, body + b"\0" * 18)


def dns_name(name):
    return b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\0"


def dns(ident, name, response=False, rcode=0, answer=None, kind=1):
    flags = (0x8180 | rcode) if response else 0x0100
    body = struct.pack("!HHHHHH", ident, flags, 1, 1 if answer else 0, 0, 0) + dns_name(name) + struct.pack("!HH", kind,
                                                                                                          1)
    if answer:
        body += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + ip(answer)
    return body


def dhcp(message, xid, client_mac, yours="0.0.0.0", server=None, hostname=None):
    body = struct.pack("!BBBBIHH", 1 if message in (1, 3) else 2, 1, 6, 0, xid, 0, 0x8000)
    body += ip("0.0.0.0") + ip(yours) + ip("0.0.0.0") + ip("0.0.0.0") + mac(client_mac) + b"\0" * 10
    body += b"\0" * 192 + b"\x63\x82\x53\x63"
    body += bytes([53, 1, message])
    if server:
        body += bytes([54, 4]) + ip(server)
    if hostname:
        body += bytes([12, len(hostname)]) + hostname.encode()
    return body + b"\xff"


def bpdu(root_mac, bridge_mac, flags=0x3C, root_priority=32768 + 10, cost=4):
    body = struct.pack("!HBBB", 0, 2, 2, flags) + struct.pack("!H", root_priority) + mac(root_mac) + \
        struct.pack("!I", cost) + struct.pack("!H", root_priority) + mac(bridge_mac) + struct.pack("!H", 0x8001) + \
        struct.pack("!HHHH", 0, 20 * 256, 2 * 256, 15 * 256) + b"\0"
    llc = b"\x42\x42\x03"
    return mac("01:80:c2:00:00:00") + mac(bridge_mac) + struct.pack("!H", len(llc + body)) + llc + body


def cdp(device, port, native_vlan=10, duplex=1):
    def tlv(kind, value):
        return struct.pack("!HH", kind, 4 + len(value)) + value
    body = b"\x02\xb4\x00\x00" + tlv(1, device.encode()) + tlv(3, port.encode()) + tlv(6, b"cisco WS-C3650") + \
        tlv(10, struct.pack("!H", native_vlan)) + tlv(11, bytes([duplex])) + \
        tlv(2, struct.pack("!I", 1) + b"\x01\x01\xcc" + struct.pack("!H", 4) + ip("10.18.0.2"))
    snap = b"\xaa\xaa\x03\x00\x00\x0c\x20\x00"
    return mac("01:00:0c:cc:cc:cc") + mac(SWITCH) + struct.pack("!H", len(snap + body)) + snap + body


def lldp(system, port):
    def tlv(kind, value):
        return struct.pack("!H", (kind << 9) | len(value)) + value
    body = tlv(1, b"\x04" + mac(SWITCH)) + tlv(2, b"\x05" + port.encode()) + tlv(3, struct.pack("!H", 120)) + \
        tlv(5, system.encode()) + tlv(4, b"uplink") + tlv(0, b"")
    return ethernet(SWITCH, "01:80:c2:00:00:0e", 0x88CC, body)


def hsrp(state, group=1, priority=110, virtual="10.0.10.1"):
    body = bytes([0, 0, state, 3, 10, priority, group, 0]) + b"cisco\0\0\0" + ip(virtual)
    return body


def tls_client_hello(server_name):
    name = server_name.encode()
    sni = struct.pack("!HHHBH", 0, len(name) + 5, len(name) + 3, 0, len(name)) + name
    extensions = sni
    hello = struct.pack("!H", 0x0303) + b"\x11" * 32 + b"\x00" + struct.pack("!H", 2) + b"\x13\x01" + b"\x01\x00" + \
        struct.pack("!H", len(extensions)) + extensions
    handshake = bytes([1]) + len(hello).to_bytes(3, "big") + hello
    return bytes([22]) + struct.pack("!HH", 0x0301, len(handshake)) + handshake


def snmp_get(community, request_id, oid=b"\x2b\x06\x01\x02\x01\x01\x05\x00"):
    def element(tag, value):
        return bytes([tag, len(value)]) + value
    varbind = element(0x30, element(0x06, oid) + b"\x05\x00")
    pdu = element(0xA0, element(0x02, bytes([request_id])) + element(0x02, b"\0") + element(0x02, b"\0") +
                  element(0x30, varbind))
    return element(0x30, element(0x02, b"\x01") + element(0x04, community.encode()) + pdu)


def erspan_wrap(frame, session=7, source="10.18.0.122", destination="10.0.10.9"):
    header = struct.pack("!HH", 0x1000, erspan.ERSPAN_II) + struct.pack("!I", 1)
    span = struct.pack("!HHI", 0x1000 | 10, session, 0)
    return frame_ip(ROUTER, PC, source, destination, erspan.GRE, header + span + frame)


def sample_packets():
    """[(seconds, frame)]: a capture with a bit of everything."""
    packets = []
    t = 0.0

    def add(frame, gap=0.01):
        nonlocal t
        t += gap
        packets.append((t, frame))

    add(arp(PC, BROADCAST, 1, PC, "10.0.10.9", "00:00:00:00:00:00", "10.0.10.1"))
    add(arp(ROUTER, PC, 2, ROUTER, "10.0.10.1", PC, "10.0.10.9"))
    # Duplicate IP: two MACs claim 10.0.10.50
    add(arp("00:11:22:33:44:55", BROADCAST, 1, "00:11:22:33:44:55", "10.0.10.50", "00:00:00:00:00:00", "10.0.10.1"))
    add(arp("00:11:22:33:44:66", BROADCAST, 1, "00:11:22:33:44:66", "10.0.10.50", "00:00:00:00:00:00", "10.0.10.1"))
    # DNS: answered, no such name, unanswered
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.0.53", 17, udp(50000, 53, dns(0x1111, "intranet.example.com"))))
    add(frame_ip(ROUTER, PC, "10.0.0.53", "10.0.10.9", 17,
                 udp(53, 50000, dns(0x1111, "intranet.example.com", True, answer="10.0.20.80"))), 0.02)
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.0.53", 17, udp(50001, 53, dns(0x2222, "nothing.example.com"))))
    add(frame_ip(ROUTER, PC, "10.0.0.53", "10.0.10.9", 17,
                 udp(53, 50001, dns(0x2222, "nothing.example.com", True, rcode=3))))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.0.54", 17, udp(50002, 53, dns(0x3333, "lost.example.com"))))
    # TCP: handshake, a request, a resend, a reset
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.80", 6, tcp(51000, 80, 1000, 0, SYN, options=SYN_OPTIONS)))
    add(frame_ip(ROUTER, PC, "10.0.20.80", "10.0.10.9", 6, tcp(80, 51000, 5000, 1001, SYN | ACK, window=29200,
                                                               options=SYN_OPTIONS)), 0.3)
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.80", 6, tcp(51000, 80, 1001, 5001, ACK, window=502)))
    request = b"GET /index.html HTTP/1.1\r\nHost: intranet.example.com\r\nUser-Agent: test\r\n\r\n"
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.80", 6, tcp(51000, 80, 1001, 5001, ACK | PSH, request, 502)))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.80", 6, tcp(51000, 80, 1001, 5001, ACK | PSH, request, 502)), 0.2)
    reply = b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"
    add(frame_ip(ROUTER, PC, "10.0.20.80", "10.0.10.9", 6, tcp(80, 51000, 5001, 1001 + len(request), ACK | PSH,
                                                               reply, 114)))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.80", 6, tcp(51000, 80, 1001 + len(request), 5001 + len(reply),
                                                               RST | ACK, window=0)))
    # A refused connection and an unanswered one
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.81", 6, tcp(51001, 3389, 7000, 0, SYN, options=SYN_OPTIONS)))
    add(frame_ip(ROUTER, PC, "10.0.20.81", "10.0.10.9", 6, tcp(3389, 51001, 0, 7001, RST | ACK, window=0)))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.82", 6, tcp(51002, 22, 9000, 0, SYN, options=SYN_OPTIONS)))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.82", 6, tcp(51002, 22, 9000, 0, SYN, options=SYN_OPTIONS)), 1.0)
    # TLS with a server name
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.20.90", 6, tcp(51003, 443, 100, 200, ACK | PSH,
                                                               tls_client_hello("portal.example.com"))))
    # DHCP from two servers
    add(ethernet(PC, BROADCAST, 0x0800, ipv4("0.0.0.0", "255.255.255.255", 17,
                                             udp(68, 67, dhcp(1, 0xABCD, PC, hostname="laptop9")))))
    add(frame_ip(ROUTER, BROADCAST, "10.0.10.1", "255.255.255.255", 17,
                 udp(67, 68, dhcp(2, 0xABCD, PC, "10.0.10.9", "10.0.0.10"))))
    add(frame_ip("00:aa:bb:cc:dd:ee", BROADCAST, "192.168.1.1", "255.255.255.255", 17,
                 udp(67, 68, dhcp(2, 0xABCD, PC, "192.168.1.100", "192.168.1.1"))))
    # ICMP: port unreachable about the DNS query
    original = ipv4("10.0.10.9", "10.0.0.54", 17, udp(50002, 53, b""))[:28]
    add(frame_ip(ROUTER, PC, "10.0.0.54", "10.0.10.9", 1, struct.pack("!BBHI", 3, 3, 0, 0) + original))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.0.1", 1, struct.pack("!BBHHH", 8, 0, 0, 1, 7) + b"abcdefgh"))
    # Layer 2: RSTP with topology change, CDP (half duplex), LLDP, VLAN tag, HSRP active from two routers, LACP
    add(bpdu(SWITCH, SWITCH, flags=0x3D))
    add(cdp("CLOSET-SW1", "GigabitEthernet1/0/24", duplex=0))
    add(lldp("CLOSET-SW1", "Gi1/0/24"))
    add(frame_ip(ROUTER, PC, "10.0.20.80", "10.0.10.9", 17, udp(5000, 5001, b"tagged"), vlan=20))
    add(frame_ip(ROUTER, "01:00:5e:00:00:02", "10.0.10.2", "224.0.0.2", 17, udp(1985, 1985, hsrp(16)), ttl=1))
    add(frame_ip(SERVER2, "01:00:5e:00:00:02", "10.0.10.3", "224.0.0.2", 17, udp(1985, 1985, hsrp(16, priority=100)),
                 ttl=1))
    lacp = bytes([1, 1, 1, 20]) + struct.pack("!H", 32768) + mac(SWITCH) + struct.pack("!HHHB", 1, 32768, 24, 0x3D) + \
        b"\0" * 3 + bytes([2, 20]) + b"\0" * 18 + bytes([3, 16]) + b"\0" * 14 + b"\0\0" + b"\0" * 50
    add(ethernet(SWITCH, "01:80:c2:00:00:02", 0x8809, lacp))
    add(frame_ip(PC, ROUTER, "10.0.10.9", "10.0.10.2", 17, udp(50010, 161, snmp_get("public", 5))))
    # Something mirrored to this PC over ERSPAN
    add(erspan_wrap(frame_ip("00:de:ad:00:00:01", "00:de:ad:00:00:02", "172.16.0.1", "172.16.0.2", 17,
                             udp(1234, 53, dns(0x4444, "mirrored.example.com")))))
    return packets


def write_pcapng(path, packets):
    with open(path, "wb") as output:
        output.write(erspan.header_blocks("NOMAD test capture"))
        for seconds, frame in packets:
            output.write(erspan.packet_block(1_790_000_000 + seconds, frame))
    return path


def write_pcap(path, packets):
    with open(path, "wb") as output:
        output.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for seconds, frame in packets:
            whole = 1_790_000_000 + seconds
            output.write(struct.pack("<IIII", int(whole), int(round((whole % 1) * 1e6)), len(frame), len(frame)))
            output.write(frame)
    return path
