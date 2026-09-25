import ipaddress

import pytest

from nomad.subnet import address_kind, describe, host_range, parse_subnet, prefix_for_hosts, reverse_zone, split


@pytest.mark.parametrize("text, network", [("192.168.1.77/24", "192.168.1.0/24"),
                                           ("10.1.2.3 255.255.252.0", "10.1.0.0/22"),
                                           ("10.1.2.3/255.255.255.128", "10.1.2.0/25"),
                                           ("172.16.5.4", "172.16.5.0/24"),
                                           ("2001:db8::1/48", "2001:db8::/48"), ("fe80::1%11", "fe80::/64")])
def test_parse_subnet(text, network):
    assert str(parse_subnet(text)[1]) == network


@pytest.mark.parametrize("text, message", [("", "Enter an address"), ("192.168.1.300/24", "not an IPv4"),
                                           ("10.0.0.1/33", "isn't a valid prefix"),
                                           ("10.0.0.1 255.0.255.0", "isn't a valid prefix"),
                                           ("10.0.0.1 / 24 / 8", "too many parts")])
def test_parse_subnet_rejects(text, message):
    with pytest.raises(ValueError, match=message):
        parse_subnet(text)


def test_describe_ipv4():
    rows = dict(describe(*parse_subnet("192.168.1.77/26")))
    assert rows["Network"] == "192.168.1.64/26" and rows["Netmask"] == "255.255.255.192"
    assert rows["Wildcard mask"] == "0.0.0.63" and rows["Broadcast"] == "192.168.1.127"
    assert (rows["First host"], rows["Last host"], rows["Usable hosts"]) == ("192.168.1.65", "192.168.1.126", "62")
    assert rows["Address type"] == "Private" and rows["Reverse DNS zone"] == "1.168.192.in-addr.arpa"
    assert dict(describe(*parse_subnet("192.168.1.64/26")))["Note"].startswith("This is the network address")


def test_point_to_point_and_host_routes():
    assert host_range(ipaddress.ip_network("10.0.0.0/31"))[2] == 2
    assert host_range(ipaddress.ip_network("10.0.0.5/32"))[2] == 1
    assert dict(describe(*parse_subnet("10.0.0.0/31")))["Broadcast"].startswith("None")


def test_address_kinds():
    assert address_kind(ipaddress.ip_address("100.64.1.1")).startswith("Shared")
    assert address_kind(ipaddress.ip_address("169.254.3.4")) == "Link-local"
    assert address_kind(ipaddress.ip_address("8.8.8.8")) == "Public"


def test_split():
    subnets, total = split(ipaddress.ip_network("10.0.0.0/22"), 24)
    assert total == 4 and [str(subnet) for subnet in subnets] == ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24",
                                                                  "10.0.3.0/24"]
    subnets, total = split(ipaddress.ip_network("10.0.0.0/8"), 30, limit=10)
    assert total == 2 ** 22 and len(subnets) == 10
    with pytest.raises(ValueError, match="Choose a prefix"):
        split(ipaddress.ip_network("10.0.0.0/24"), 24)


def test_prefix_for_hosts():
    assert prefix_for_hosts(50) == 26 and prefix_for_hosts(62) == 26 and prefix_for_hosts(63) == 25
    assert prefix_for_hosts(1) == 32 and prefix_for_hosts(2) == 31


def test_reverse_zone():
    assert reverse_zone(ipaddress.ip_network("10.20.0.0/16")) == "20.10.in-addr.arpa"
    assert reverse_zone(ipaddress.ip_network("10.0.0.0/7")) == "10.in-addr.arpa"
