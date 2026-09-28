import socket
import time

import pytest

from nomad.syslog import SyslogReceiver, format_line, parse_message, split_tcp_frames


@pytest.mark.parametrize("raw, expected", [
    ("<34>Oct 11 22:14:15 mymachine su: 'su root' failed on /dev/pts/8",
     ("Critical", "auth", "mymachine", "su", "'su root' failed on /dev/pts/8")),
    ('<165>1 2003-10-11T22:14:15.003Z host.example.com evntslog - ID47 [exampleSDID@32473 iut="3"] An event',
     ("Notice", "local4", "host.example.com", "evntslog", "An event")),
    ("<189>53: *Mar  1 00:01:02.123: %LINK-3-UPDOWN: Interface Gi0/1, changed state to up",
     ("Notice", "local7", "", "", "53: *Mar  1 00:01:02.123: %LINK-3-UPDOWN: Interface Gi0/1, changed state to up")),
    ("<189>Sep 25 10:00:00 10.0.0.1 53: %SYS-5-CONFIG_I: Configured from console",
     ("Notice", "local7", "10.0.0.1", "", "53: %SYS-5-CONFIG_I: Configured from console")),
    ("<13>sshd[1234]: Accepted password for admin", ("Notice", "user", "", "sshd", "Accepted password for admin")),
    ("<30>2026-09-25T10:00:00+00:00 ap1 hostapd: wlan0: STA associated",
     ("Info", "daemon", "ap1", "hostapd", "wlan0: STA associated")),
    ("no priority at all\n", ("Info", "", "", "", "no priority at all")),
    ("<999>out of range", ("Info", "", "", "", "<999>out of range")),
])
def test_parse_message(raw, expected):
    message = parse_message(raw, "10.0.0.9")
    assert (message.severity_name, message.facility, message.host, message.app, message.message) == expected


def test_format_line():
    message = parse_message("<11>Oct 11 22:14:15 fw1 kernel: link down", "10.0.0.1")
    line = format_line(message)
    assert line.endswith("10.0.0.1 ERROR user fw1 kernel: link down")


def test_split_tcp_frames():
    messages, rest = split_tcp_frames(b"11 <13>hello A<13>line one\n<13>line two\r\n<13>partial")
    assert messages == [b"<13>hello A", b"<13>line one", b"<13>line two"] and rest == b"<13>partial"
    messages, rest = split_tcp_frames(b"20 <13>too short")
    assert messages == [] and rest == b"20 <13>too short"  # Waits for the rest of the frame


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", port))  # Make sure TCP is free on the same number too
        return port


def test_receiver_udp_and_tcp():
    received = []
    port = free_port()
    receiver = SyslogReceiver(received.append, "127.0.0.1", port, tcp=True)
    receiver.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(b"<14>Sep 25 10:00:00 sw1 app: over udp", ("127.0.0.1", port))
        with socket.create_connection(("127.0.0.1", port)) as tcp:
            tcp.sendall(b"<14>over tcp one\n16 <14>over tcp two")
        deadline = time.monotonic() + 3
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        receiver.stop()
    assert sorted((message.protocol, message.message) for message in received) == [
        ("TCP", "over tcp one"), ("TCP", "over tcp two"), ("UDP", "over udp")]
    assert all(message.source == "127.0.0.1" for message in received)


def test_port_in_use():
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", port))
        with pytest.raises(OSError):
            SyslogReceiver(lambda message: None, "127.0.0.1", port).start()
