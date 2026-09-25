import gzip

import pytest

from nomad import oui
from nomad.oui import RANDOMIZED_VENDOR, format_mac, is_multicast_mac, is_randomized_mac, normalize_mac, vendor

VENDORS = {"001B63": "Apple, Inc.", "3C5AB4": "Google, Inc."}


@pytest.mark.parametrize("text, expected", [("00:1b:63:84:45:e6", "00-1B-63-84-45-E6"),
                                            ("001B.6384.45E6", "00-1B-63-84-45-E6"),
                                            ("00-1B-63-84-45", ""), ("", ""), (None, "")])
def test_format_mac(text, expected):
    assert format_mac(text) == expected


def test_vendor_lookup():
    assert vendor("00-1B-63-84-45-E6", VENDORS) == "Apple, Inc."
    assert vendor("3c:5a:b4:00:00:01", VENDORS) == "Google, Inc."
    assert vendor("00-00-5E-00-01-01", VENDORS) == ""


def test_multicast_and_randomized_macs():
    assert is_multicast_mac("FF-FF-FF-FF-FF-FF") and is_multicast_mac("01-00-5E-00-00-FB")
    assert vendor("01-00-5E-00-00-FB", VENDORS) == ""
    assert is_randomized_mac("DA-A1-19-00-00-01") and not is_randomized_mac("00-1B-63-84-45-E6")
    assert vendor("DA-A1-19-00-00-01", VENDORS) == RANDOMIZED_VENDOR


def test_bundled_registry_loads():
    assert oui.DATA_FILE.is_file(), "Run: python -m nomad.oui build oui.csv"
    assert vendor("00-50-56-00-00-01") == "VMware, Inc."
    assert len(oui._load()) > 30000


def test_build_from_ieee_csv(tmp_path):
    source = tmp_path / "oui.csv"
    source.write_text('Registry,Assignment,Organization Name,Organization Address\n'
                      'MA-L,001B63,"Apple, Inc.","1 Infinite Loop Cupertino CA US 95014 "\n'
                      'MA-L,3c5ab4,"Google,   Inc.",Somewhere\n'
                      'MA-M,AABBCCD,Too Specific,Somewhere\n', encoding="utf-8")
    output = tmp_path / "oui.txt.gz"
    assert oui.build(source, output) == 2
    with gzip.open(output, "rt", encoding="utf-8") as file:
        assert oui.parse_registry(file) == {"001B63": "Apple, Inc.", "3C5AB4": "Google, Inc."}


def test_missing_registry_gives_blank_vendors(monkeypatch, tmp_path):
    monkeypatch.setattr(oui, "DATA_FILE", tmp_path / "missing.txt.gz")
    monkeypatch.setattr(oui, "_vendors", None)
    assert vendor("00-1B-63-84-45-E6") == ""
    assert normalize_mac("00-1B-63-84-45-E6") == "001B638445E6"
