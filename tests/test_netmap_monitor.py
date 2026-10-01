from nomad.netmap import monitor
from nomad.netmap.model import NetworkMap
from nomad.netmap.monitor import DOWN, UNKNOWN, UP, StatusTracker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_down_only_after_two_missed_polls_and_up_at_once():
    clock = Clock()
    tracker = StatusTracker(clock=clock)
    assert [(change.key, change.status) for change in tracker.update({"a": 3, "b": None})] == [("a", UP)]
    assert tracker.get("b").status == UNKNOWN  # One miss: not down yet
    clock.now += 30
    changes = tracker.update({"a": None, "b": None})
    assert [(change.key, change.status, change.lasted) for change in changes] == [("b", DOWN, None)]
    assert tracker.get("a").status == UP  # One lost ping doesn't flap it
    clock.now += 30
    changes = tracker.update({"a": None, "b": 7})
    assert [(change.key, change.status, change.lasted) for change in changes] == [("a", DOWN, 60), ("b", UP, 30)]
    assert tracker.get("b").rtt == 7
    assert tracker.counts() == {UP: 1, DOWN: 1, UNKNOWN: 0}
    tracker.forget_others({"a"})
    assert set(tracker.devices) == {"a"}


def test_poll_pings_everything():
    seen = []

    def pinger(address):
        seen.append(address)
        return 2 if address.endswith(".1") else None
    assert monitor.poll({"core": "10.0.0.1", "rtr": "10.0.0.254"}, pinger) == {"core": 2, "rtr": None}
    assert sorted(seen) == ["10.0.0.1", "10.0.0.254"]
    assert monitor.poll({}, pinger) == {}


def test_log_text():
    down = monitor.Change("rtr1", DOWN, 0, 3725)
    assert monitor.change_text(down, "rtr1", "10.0.0.254") == \
        "rtr1 (10.0.0.254) is down: no answer to ping (it had been up for 1 h 2 min)"
    up = monitor.Change("rtr1", UP, 0, 192, 4)
    assert monitor.change_text(up, "rtr1", "10.0.0.254") == \
        "rtr1 (10.0.0.254) is up again, after being down for 3 min 12 s (4 ms)"
    assert monitor.duration_text(45) == "45 s" and monitor.duration_text(2 * 86400 + 3600) == "2 days 1 h"


def test_history_is_saved_with_the_map():
    network_map = NetworkMap()
    network_map.status_log = [["2026-09-30T14:02:11", "rtr1", "rtr1", DOWN, "rtr1 is down"]]
    assert NetworkMap.from_json(network_map.to_json()).status_log == network_map.status_log
