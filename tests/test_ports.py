import socket
import threading

import pytest

from nomad.ports import CLOSED, FILTERED, OPEN, PORT_PRESETS, PortResult, check_port, parse_ports, scan_ports, \
    summarize


def test_parse_ports():
    assert parse_ports(" 443, 22 80;8000-8002, 22 ") == [22, 80, 443, 8000, 8001, 8002]
    assert parse_ports("1-65535")[-1] == 65535


@pytest.mark.parametrize("text, message", [("", "Enter one or more"), ("http", "not a port"),
                                           ("0", "from 1 to 65535"), ("70000", "from 1 to 65535"),
                                           ("100-90", "low to high"), ("1-2-3", "not a port")])
def test_parse_ports_rejects(text, message):
    with pytest.raises(ValueError, match=message):
        parse_ports(text)


def test_presets_parse():
    for _, ports in PORT_PRESETS:
        assert parse_ports(ports)


def test_filtered_ports_are_retried_and_reported_once():
    attempts = {}
    answers = {22: [OPEN], 23: [CLOSED], 80: [FILTERED, OPEN], 81: [FILTERED, FILTERED]}

    def probe(port):
        attempts[port] = attempts.get(port, 0) + 1
        return PortResult(port, answers[port][attempts[port] - 1])

    reported = []
    progress = []
    results = scan_ports(sorted(answers), probe, workers=4, passes=2, result=reported.append,
                         progress=lambda done, total: progress.append((done, total)))
    assert {port: result.state for port, result in results.items()} == {22: OPEN, 23: CLOSED, 80: OPEN,
                                                                         81: FILTERED}
    assert sorted(result.port for result in reported) == [22, 23, 80, 81]
    assert attempts == {22: 1, 23: 1, 80: 2, 81: 2}
    assert progress[-1] == (4, 4)


def test_stop_request():
    probed = []
    assert scan_ports([1, 2, 3], lambda port: probed.append(port), workers=2, should_stop=lambda: True) == {}
    assert probed == []


def test_summarize():
    results = [PortResult(1, OPEN), PortResult(2, FILTERED), PortResult(3, OPEN), PortResult(4, CLOSED)]
    assert summarize(results) == "2 open, 1 closed, 1 filtered"
    assert PortResult(3389, OPEN).service == "Remote Desktop"


def test_check_port_against_a_local_listener():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        accepted = threading.Thread(target=lambda: server.accept()[0].close(), daemon=True)
        accepted.start()
        result = check_port("127.0.0.1", 4, port, 2000)
        assert result.state == OPEN and result.rtt is not None
    closed = check_port("127.0.0.1", 4, port, 2000)  # Nothing listening now
    assert closed.state == CLOSED
