import pytest

from nomad import capture, pktmon
from nomad.capture import Capture, CaptureFilter, filter_arguments, validate_filter
from nomad.pktmon import PktmonBusy


def test_validate_filter():
    assert validate_filter(" 10.0.0.5 ", " 443 ", "TCP") == CaptureFilter("10.0.0.5", "443", "TCP")
    assert validate_filter("10.0.0.7/24", "", "Any").address == "10.0.0.0/24"
    assert validate_filter("", "", "Any").empty


@pytest.mark.parametrize("address, port, protocol, message", [
    ("10.0.0", "", "Any", "not an IP address"), ("", "0", "Any", "port number"), ("", "http", "TCP", "port number"),
    ("", "53", "ICMP", "doesn't use ports"), ("", "", "Carrier pigeon", "Unknown protocol")])
def test_validate_filter_rejects(address, port, protocol, message):
    with pytest.raises(ValueError, match=message):
        validate_filter(address, port, protocol)


def test_filter_arguments():
    assert filter_arguments(CaptureFilter()) is None
    assert filter_arguments(CaptureFilter("10.0.0.5", "443", "TCP")) == ["-t", "TCP", "-i", "10.0.0.5", "-p", "443"]
    assert filter_arguments(CaptureFilter(protocol="ARP")) == ["-d", "ARP"]
    assert CaptureFilter("10.0.0.5", "53", "UDP").describe() == "UDP to or from 10.0.0.5 port 53"


class FakePktmon:
    def __init__(self):
        self.calls = []

    def __call__(self, *arguments, timeout=120):
        self.calls.append(arguments)
        if arguments[0] == "etl2pcap":
            with open(arguments[3], "wb") as file:
                file.write(b"pcapng")
        return ""


def test_capture_runs_pktmon_and_releases_the_lock(monkeypatch, tmp_path):
    fake = FakePktmon()
    monkeypatch.setattr(pktmon, "pktmon", fake)
    monkeypatch.setattr(pktmon, "find_pktmon", lambda: "pktmon.exe")
    job = Capture(CaptureFilter("10.0.0.5", "", "Any"), whole_packets=False, file_size_mb=64)
    job.start()
    with pytest.raises(PktmonBusy):  # Switch Port can't start while this capture runs
        pktmon.acquire("finding the switch port")
    job.stop()
    output = tmp_path / "out.pcapng"
    assert job.save(str(output)) == 6
    job.cleanup()
    started = next(call for call in fake.calls if call[0] == "start")
    assert "--pkt-size" in started and started[started.index("--pkt-size") + 1] == "128"
    assert started[started.index("--file-size") + 1] == "64"
    assert ("filter", "add", capture.FILTER_NAME, "-i", "10.0.0.5") in fake.calls
    assert fake.calls[-1] == ("filter", "remove")
    assert pktmon.lock.acquire(blocking=False)  # Released
    pktmon.lock.release()


def test_failed_start_cleans_up(monkeypatch):
    def failing(*arguments, timeout=120):
        if arguments[0] == "start":
            raise OSError("Access is denied.")
        return ""
    monkeypatch.setattr(pktmon, "pktmon", failing)
    monkeypatch.setattr(pktmon, "find_pktmon", lambda: "pktmon.exe")
    job = Capture(CaptureFilter())
    with pytest.raises(OSError):
        job.start()
    assert not pktmon.lock.locked() and job.folder is None
