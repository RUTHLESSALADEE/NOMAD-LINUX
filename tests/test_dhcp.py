import socket
import struct

from nomad.dhcp import BOOTREPLY, HEADER, MAGIC_COOKIE, REQUESTED, Offer, assess, build_discover, describe_option, \
    parse_offer, parse_options

XID = b"\x12\x34\x56\x78"


def option(code, value):
    return bytes([code, len(value)]) + value


def offer_packet(server="192.168.1.1", address="192.168.1.50", xid=XID, message_type=2, giaddr="0.0.0.0",
                 siaddr="0.0.0.0", sname=b"", boot_file=b"", extra=b""):
    header = HEADER.pack(BOOTREPLY, 1, 6, 0, xid, 0, 0x8000, bytes(4), socket.inet_aton(address),
                         socket.inet_aton(siaddr), socket.inet_aton(giaddr), bytes(16), sname, boot_file)
    options = option(53, bytes([message_type])) + option(54, socket.inet_aton(server))
    options += option(1, socket.inet_aton("255.255.255.0"))
    options += option(3, socket.inet_aton("192.168.1.1"))
    options += option(6, socket.inet_aton("192.168.1.1") + socket.inet_aton("8.8.8.8"))
    options += option(51, struct.pack("!I", 86400 + 3600 * 2 + 60 * 5))
    options += option(15, b"office.lan") + bytes([0, 0])  # Pads are skipped
    return header + MAGIC_COOKIE + options + extra + bytes([255])


def test_build_discover():
    packet = build_discover("00-11-22-33-44-55", XID)
    fields = HEADER.unpack_from(packet)
    assert fields[0] == 1 and fields[4] == XID and fields[6] == 0x8000  # Request, our ID, broadcast replies
    assert fields[11][:6] == bytes.fromhex("001122334455")
    assert packet[HEADER.size:HEADER.size + 4] == MAGIC_COOKIE
    assert packet[HEADER.size + 4:HEADER.size + 7] == bytes([53, 1, 1]) and packet.endswith(b"\xff")
    requested = dict(parse_options(packet[HEADER.size + 4:]))[55]
    assert list(requested) == REQUESTED and {42, 43, 44, 66, 121, 150, 252} <= set(requested)


def test_parse_offer():
    offer = parse_offer(offer_packet(), XID, "192.168.1.1")
    assert (offer.server, offer.address, offer.subnet_mask) == ("192.168.1.1", "192.168.1.50", "255.255.255.0")
    assert offer.routers == ["192.168.1.1"] and offer.dns == ["192.168.1.1", "8.8.8.8"]
    assert offer.lease_text == "1d 2h 5m" and offer.domain == "office.lan" and offer.relay == ""
    relayed = parse_offer(offer_packet(server="10.0.0.5", giaddr="192.168.1.1"), XID, "192.168.1.1")
    assert relayed.server == "10.0.0.5" and relayed.relay == "192.168.1.1"


def test_other_packets_are_ignored():
    assert parse_offer(offer_packet(xid=b"\0\0\0\0"), XID) is None  # Someone else's transaction
    assert parse_offer(offer_packet(message_type=5), XID) is None  # An ACK, not an offer
    assert parse_offer(b"\x02" * 100, XID) is None


def test_every_option_is_kept_and_described():
    extra = (option(42, socket.inet_aton("10.0.0.123")) + option(44, socket.inet_aton("10.0.0.2"))
             + option(46, b"\x08") + option(58, struct.pack("!I", 43200)) + option(66, b"10.0.0.9")
             + option(67, b"pxelinux.0") + option(150, socket.inet_aton("10.0.0.9"))
             + option(121, bytes([24, 10, 1, 2]) + socket.inet_aton("192.168.1.254")
                      + bytes([0]) + socket.inet_aton("192.168.1.1"))
             + option(119, b"\x03lab\x07example\x03com\x00\x04corp\xc0\x04")
             + option(43, b"\x01\x04\xc0\xa8\x01\x0a") + option(252, b"http://wpad/wpad.dat\n")
             + option(199, b"custom"))
    offer = parse_offer(offer_packet(siaddr="10.0.0.9", sname=b"bootsrv", boot_file=b"boot.bin", extra=extra), XID)
    details = {name: value for _, name, value in offer.details()}
    assert details["Next server (siaddr)"] == "10.0.0.9" and details["Server host name (sname)"] == "bootsrv"
    assert details["Boot file (file)"] == "boot.bin"
    assert details["NTP servers"] == "10.0.0.123" and details["NetBIOS name servers (WINS)"] == "10.0.0.2"
    assert details["NetBIOS node type"] == "H-node (WINS, then broadcast)"
    assert details["Renewal time (T1)"] == "12h 0m" and details["Lease time"] == "1d 2h 5m"
    assert details["TFTP server name"] == "10.0.0.9" and details["Boot file name"] == "pxelinux.0"
    assert details["TFTP server addresses (Cisco)"] == "10.0.0.9"
    assert details["Classless static routes"] == "10.1.2.0/24 via 192.168.1.254; 0.0.0.0/0 via 192.168.1.1"
    assert details["Domain search list"] == "lab.example.com, corp.example.com"
    assert details["Vendor-specific information"] == "01 04 c0 a8 01 0a"
    assert details["Option 199"] == "custom"
    assert [code for code, _, _ in offer.details() if code != ""][:2] == [53, 54]  # In the order the server sent


def test_split_and_overloaded_options():
    # An option split in two (RFC 3396) is joined; option 52 = 3 moves more options into the file and sname fields
    split = option(6, socket.inet_aton("10.0.0.1")) + option(6, socket.inet_aton("10.0.0.2"))
    overload_file = option(66, b"tftp.lab") + bytes([255])
    overload_sname = option(67, b"image.bin") + bytes([255])
    offer = parse_offer(offer_packet(extra=split + option(52, b"\x03"), sname=overload_sname,
                                     boot_file=overload_file), XID)
    details = {name: value for _, name, value in offer.details()}
    assert details["DNS servers"] == "192.168.1.1, 8.8.8.8, 10.0.0.1, 10.0.0.2"
    assert details["TFTP server name"] == "tftp.lab" and details["Boot file name"] == "image.bin"
    assert "Boot file (file)" not in details and "Server host name (sname)" not in details


def test_malformed_options_fall_back_to_hex():
    assert describe_option(121, bytes([40, 1, 2, 3])) == ("Classless static routes", "28 01 02 03")
    assert describe_option(119, b"\x05ab") == ("Domain search list", "05 61 62")
    assert describe_option(1, b"\x01\x02") == ("Subnet mask", "01 02")
    assert describe_option(2, struct.pack("!i", -18000)) == ("Time offset", "-18000 seconds (UTC-5h)")
    assert parse_options(b"\x06") == []  # A code with no length byte


def offer(server):
    return Offer(server, server, "192.168.1.50")


def test_assess():
    assert assess([], "")[0] == "warning"
    assert assess([offer("192.168.1.1")], "192.168.1.1")[0] == "success"
    assert assess([offer("192.168.1.1")], "")[0] == "success"
    kind, message = assess([offer("192.168.1.1"), offer("192.168.1.99")], "192.168.1.1")
    assert kind == "error" and "192.168.1.99" in message and "rogue" in message
    kind, message = assess([offer("192.168.1.1"), offer("192.168.1.99")], "")
    assert kind == "error" and "2 DHCP servers" in message
