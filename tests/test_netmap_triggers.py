import socket
import time

from nomad.netmap import triggers
from nomad.snmp import (INTEGER, OCTET_STRING, SEQUENCE, TRAP_V1, Value, build_trap, encode_integer, encode_oid,
                        inform_response, parse_trap, tlv, IP_ADDRESS, TIMETICKS, RESPONSE, read_tlv, INFORM)
from nomad.syslog import SyslogHub, parse_message


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def v1_trap(generic, specific=0, enterprise=(1, 3, 6, 1, 4, 1, 9, 1, 2494), agent="10.0.0.11"):
    pdu = (encode_oid(enterprise) + tlv(IP_ADDRESS, socket.inet_aton(agent)) + encode_integer(generic)
           + encode_integer(specific) + encode_integer(1234, TIMETICKS) + tlv(SEQUENCE, b""))
    return tlv(SEQUENCE, encode_integer(0) + tlv(OCTET_STRING, b"public") + tlv(TRAP_V1, pdu))


def test_v1_link_up_trap():
    trap = parse_trap(v1_trap(3))
    assert trap.trap_oid == triggers.LINK_UP and trap.agent == "10.0.0.11" and trap.community == "public"
    assert triggers.trap_reason(trap) == "port up (trap)"


def test_v1_enterprise_trap():
    trap = parse_trap(v1_trap(6, 1, enterprise=(1, 3, 6, 1, 4, 1, 9, 9, 215, 2)))
    assert trap.trap_oid == triggers.CISCO_MAC_CHANGED


def test_v2c_trap_and_inform():
    data = build_trap("public", triggers.CISCO_MAC_CHANGED, [((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 5), Value(INTEGER, 5))])
    trap = parse_trap(data)
    assert trap.trap_oid == triggers.CISCO_MAC_CHANGED and not trap.inform
    assert ((1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 5), Value(INTEGER, 5)) in trap.varbinds
    inform = data.replace(bytes([0xA7]), bytes([INFORM]), 1)
    assert parse_trap(inform).inform
    _, message, _ = read_tlv(inform_response(inform), 0)
    _, _, offset = read_tlv(message, 0)
    _, _, offset = read_tlv(message, offset)
    assert message[offset] == RESPONSE


def test_not_a_trap():
    import pytest
    with pytest.raises(ValueError):
        parse_trap(b"\x30\x03\x02\x01\x01")


def test_syslog_reasons():
    def reason(text):
        return triggers.syslog_reason(parse_message(text, "10.0.0.11").raw)
    assert reason("<189>53: Oct  2 09:30:01: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/5, changed state to up") \
        == "port GigabitEthernet1/0/5 up"
    assert reason("<189>54: %LINEPROTO-5-UPDOWN: Line protocol on Interface Gi1/0/5, changed state to up") \
        == "port Gi1/0/5 up"
    assert reason("<189>55: %LINK-3-UPDOWN: Interface Gi1/0/5, changed state to down") == ""
    assert reason("<189>56: %SYS-5-CONFIG_I: Configured from console") == ""
    assert reason("<188>57: %CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch") == "CDP message"


def test_trigger_queue_merges_a_burst():
    now = [0.0]
    queue = triggers.TriggerQueue(delay=45, clock=lambda: now[0])
    queue.add("10.0.0.11", "port Gi1/0/5 up")
    now[0] = 10
    queue.add("10.0.0.11", "port Gi1/0/5 up")
    queue.add("10.0.0.11", "MAC address learned (trap)")
    queue.add("10.0.0.12", "port Eth1/1 up")
    now[0] = 44
    assert queue.due() == []
    now[0] = 46
    assert queue.due() == [("10.0.0.11", ["port Gi1/0/5 up", "MAC address learned (trap)"])]
    assert len(queue) == 1


def test_trap_receiver_over_udp():
    port = free_udp_port()
    received = []
    receiver = triggers.TrapReceiver(lambda trap, sender: received.append((trap, sender)), "127.0.0.1", port)
    receiver.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(build_trap("public", triggers.LINK_UP), ("127.0.0.1", port))
        deadline = time.monotonic() + 3
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        receiver.stop()
    assert received and received[0][0].trap_oid == triggers.LINK_UP and received[0][1] == "127.0.0.1"


class FakeReceiver:
    started = []

    def __init__(self, on_message, address, port, tcp):
        self.on_message, self.address, self.port, self.tcp = on_message, address, port, tcp
        self.running = False

    def start(self):
        self.running = True
        FakeReceiver.started.append(self)

    def stop(self):
        self.running = False


def test_syslog_hub_shares_one_port():
    FakeReceiver.started = []
    hub = SyslogHub(FakeReceiver)
    page, watcher = [], []
    hub.subscribe(page.append, 514, "10.1.1.5", tcp=False)
    hub.subscribe(watcher.append, 514, "10.1.1.5")
    assert len(FakeReceiver.started) == 1
    FakeReceiver.started[0].on_message("hello")
    assert page == ["hello"] and watcher == ["hello"]
    hub.subscribe(page.append, 514, "0.0.0.0", tcp=True)  # The page asks for more: started again with both
    assert len(FakeReceiver.started) == 2 and FakeReceiver.started[1].tcp
    assert FakeReceiver.started[1].address == "0.0.0.0" and not FakeReceiver.started[0].running
    hub.unsubscribe(page.append, 514)
    assert hub.listening(514)
    hub.unsubscribe(watcher.append, 514)
    assert not hub.listening(514) and not FakeReceiver.started[1].running
