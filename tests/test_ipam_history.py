import datetime
import time

import pytest

from nomad.ipam.history import address_history, network_as_of, network_history, subnet_history
from nomad.ipam.store import RESERVED, USED, IpamStore


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    store = IpamStore(str(tmp_path / "ipam.db"), user="alice")
    yield store
    store.close()


def test_address_history_follows_the_address(store):
    network = store.add_network("Lab")
    store.add_subnet(network.id, "10.0.0.0/24", "LAN")
    store.set_address(network.id, "10.0.0.5", USED, "sw1", mac="AA-BB-CC-00-00-05")
    store.user = "bob"
    store.set_address(network.id, "10.0.0.5", RESERVED, "sw1-core", mac="AA-BB-CC-00-00-05")
    store.free_address(network.id, "10.0.0.5")
    store.set_address(network.id, "10.0.0.5", USED, "printer")  # A new record at the same address
    store.set_address(network.id, "10.0.0.6", USED, "other")

    events = address_history(store, network.id, "10.0.0.5")
    assert [(event.action, event.who) for event in events] == [
        ("Recorded", "bob"), ("Marked free", "bob"), ("Changed", "bob"), ("Recorded", "alice")]  # Newest first
    assert events[3].details == "Used, sw1, AA-BB-CC-00-00-05"
    assert events[2].details == "Name: sw1 → sw1-core; Status: Used → Reserved"
    assert events[1].details == "was Reserved, sw1-core, AA-BB-CC-00-00-05"
    assert events[0].subject == "10.0.0.5" and len(events[0].local_time) == 16


def test_subnet_and_network_history(store):
    network = store.add_network("Lab")
    subnet = store.add_subnet(network.id, "10.0.0.0/24", "LAN")
    store.update_subnet(subnet.id, gateway="10.0.0.1", fields={"VLAN": "10"})
    store.set_address(network.id, "10.0.0.9", USED, "host")
    other = store.add_network("Other")
    store.add_subnet(other.id, "10.0.0.0/24", "Elsewhere")  # Same range, another network: not included

    [changed, added] = subnet_history(store, subnet.id)
    assert added.action == "Added" and added.subject == "10.0.0.0/24 (LAN)"
    assert changed.details == "Gateway: (none) → 10.0.0.1; Details: (none) → VLAN 10"
    feed = network_history(store, network.id)
    assert [(event.entity, event.action) for event in feed] == [
        ("addresses", "Recorded"), ("subnets", "Changed"), ("subnets", "Added"), ("networks", "Added")]
    assert network_history(store, network.id, since="2999-01-01") == []


def test_network_as_it_was(store):
    network = store.add_network("Lab")
    store.add_subnet(network.id, "10.0.0.0/24", "LAN")
    store.set_address(network.id, "10.0.0.5", USED, "sw1")
    before = utc_now()
    time.sleep(1.1)  # The log's times are to the second
    store.set_address(network.id, "10.0.0.5", USED, "sw1-renamed")
    store.set_address(network.id, "10.0.0.6", USED, "added-later")
    store.add_subnet(network.id, "10.0.1.0/24", "Added later")

    snapshot = network_as_of(store, network.id, before)
    assert [subnet.name for subnet in snapshot.subnets(network.id)] == ["LAN"]
    assert {address.ip: address.name for address in snapshot.addresses(network.id)} == {"10.0.0.5": "sw1"}
    assert snapshot.network(network.id).name == "Lab"
    assert network_as_of(store, network.id, "2000-01-01T00:00:00+00:00") is None  # Didn't exist yet
