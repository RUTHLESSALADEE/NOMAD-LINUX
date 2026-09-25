import ipaddress

import pytest

from nomad.wol import WakeTarget, destinations, magic_packet, targets_from_json, targets_to_json, validate_broadcast


def test_magic_packet():
    packet = magic_packet("00:11:22:33:44:55")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("001122334455") and packet[-6:] == bytes.fromhex("001122334455")
    with pytest.raises(ValueError, match="not a MAC address"):
        magic_packet("00-11-22")


def test_destinations_cover_each_local_subnet():
    addresses = [ipaddress.ip_interface("192.168.1.10/24"), ipaddress.ip_interface("10.0.0.5/30"),
                 ipaddress.ip_interface("172.16.0.1/32")]  # A /32 has no broadcast
    assert destinations(addresses, "10.20.30.255") == [
        ("192.168.1.10", "255.255.255.255", 9), ("192.168.1.10", "192.168.1.255", 9),
        ("10.0.0.5", "255.255.255.255", 9), ("10.0.0.5", "10.0.0.7", 9), (None, "10.20.30.255", 9)]
    assert destinations([]) == [(None, "255.255.255.255", 9)]


def test_validate_broadcast():
    assert validate_broadcast(" 10.0.0.255 ") == "10.0.0.255" and validate_broadcast("") == ""
    with pytest.raises(ValueError):
        validate_broadcast("10.0.0")


def test_saved_targets_round_trip_and_skip_bad_entries():
    targets = [WakeTarget("Lab PC", "00-11-22-33-44-55", "10.0.0.255")]
    assert targets_from_json(targets_to_json(targets)) == targets
    assert targets_from_json('[{"name": "x", "mac": "nope"}, 5, {"name": "y", "mac": "001122334455"}]') == [
        WakeTarget("y", "00-11-22-33-44-55", "")]
    assert targets_from_json("not json") == [] and targets_from_json(None) == []
