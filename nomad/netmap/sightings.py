"""Where each MAC address has been seen (MAC Finder's history): from the Network Map's crawls and MAC Finder's live
lookups, kept in an SQLite file in the app's folder.

A MAC seen again on the same switch port just stretches the time it's known to have been there; seen somewhere else,
it gets a new row. So the history of a MAC reads as the places it's been, each from when to when, however many
times it was looked for.
"""
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ..oui import format_mac, normalize_mac
from .macfind import IP, MAC_FULL, MAC_PART
from .model import port_key

log = logging.getLogger(__name__)

FILE_NAME = "mac_history.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY,
    mac TEXT NOT NULL,
    switch TEXT NOT NULL,
    switch_ip TEXT NOT NULL DEFAULT '',
    port TEXT NOT NULL DEFAULT '',
    vlan INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sightings_mac ON sightings (mac, first_seen);
"""
COLUMNS = "id, mac, switch, switch_ip, port, vlan, ip, name, first_seen, last_seen, source"


@dataclass
class Sighting:
    id: int
    mac: str  # AA-BB-CC-DD-EE-FF
    switch: str
    switch_ip: str
    port: str
    vlan: int
    ip: str
    name: str
    first_seen: str  # ISO times
    last_seen: str
    source: str  # "map" (a crawl, and the map's name) or "live"

    def same_place(self, switch, port):
        return self.switch.casefold() == (switch or "").casefold() and port_key(self.port) == port_key(port)


def default_path():
    from ..system import app_data_dir
    return app_data_dir() / FILE_NAME


class SightingLog:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self.lock = threading.Lock()  # Recording from a worker thread while the page reads
        self.ready = False

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        if not self.ready:
            db.executescript(SCHEMA)
            self.ready = True
        return db

    def record(self, locations, source_name=""):
        """Note where MACs were seen: Locations (macfind's) with a switch, each at its own time. Returns how many
        moved (were seen somewhere new)."""
        rows = [location for location in locations if location.switch and normalize_mac(location.mac)
                and location.when]
        if not rows:
            return 0
        moved = 0
        with self.lock:
            db = self.connect()
            try:
                with db:
                    for location in rows:
                        moved += self.record_one(db, location, source_name)
            finally:
                db.close()
        return moved

    @staticmethod
    def record_one(db, location, source_name):
        digits = normalize_mac(location.mac)
        when = location.when
        source = location.source + (f": {source_name}" if source_name else "")
        spans = [Sighting(*row) for row in db.execute(
            f"SELECT {COLUMNS} FROM sightings WHERE mac = ? ORDER BY first_seen, id", (digits,))]
        before = [span for span in spans if span.first_seen <= when]
        latest = before[-1] if before else None
        if latest is not None and latest.same_place(location.switch, location.port):
            if when > latest.last_seen:  # Newer: what it was like then is the latest
                db.execute("UPDATE sightings SET last_seen = ?, ip = ?, name = ?, vlan = ?, switch_ip = ?, "
                           "source = ? WHERE id = ?",
                           (when, location.ip or latest.ip, location.name or latest.name,
                            location.vlan or latest.vlan, location.switch_ip or latest.switch_ip, source, latest.id))
            return 0
        if latest is None and spans and spans[0].same_place(location.switch, location.port):
            db.execute("UPDATE sightings SET first_seen = ? WHERE id = ?", (when, spans[0].id))  # Seen there earlier
            return 0
        db.execute("INSERT INTO sightings (mac, switch, switch_ip, port, vlan, ip, name, first_seen, last_seen, "
                   "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (digits, location.switch, location.switch_ip or "", location.port or "", location.vlan or 0,
                    location.ip or "", location.name or "", when, when, source))
        return 1 if spans else 0

    def history(self, mac):
        """Where a MAC has been: [Sighting], newest first."""
        digits = normalize_mac(mac)
        if not digits or not self.path.exists():
            return []
        with self.lock:
            db = self.connect()
            try:
                rows = db.execute(f"SELECT {COLUMNS} FROM sightings WHERE mac = ? ORDER BY last_seen DESC, id DESC",
                                  (digits,)).fetchall()
            finally:
                db.close()
        return [self.sighting(row) for row in rows]

    def latest(self, query, limit=500):
        """The last place each MAC that matches a search (macfind.Query: a whole or part of a MAC, an IP address or
        a name) was seen: [Sighting], newest first."""
        if not self.path.exists():
            return []
        if query.kind == MAC_FULL:
            where, value = "mac = ?", query.digits
        elif query.kind == MAC_PART:
            where, value = "mac LIKE ?", f"%{query.digits}%"
        elif query.kind == IP:
            where, value = "ip = ?", query.address
        else:
            where, value = "name LIKE ?", f"%{query.text.rstrip('.')}%"
        with self.lock:
            db = self.connect()
            try:
                rows = db.execute(f"SELECT {COLUMNS} FROM sightings WHERE {where} ORDER BY last_seen DESC, id DESC "
                                  f"LIMIT ?", (value, limit * 4)).fetchall()
            finally:
                db.close()
        found, seen = [], set()
        for row in rows:
            if row[1] not in seen:
                seen.add(row[1])
                found.append(self.sighting(row))
        return found[:limit]

    def count(self):
        """(MACs, rows) in the history."""
        if not self.path.exists():
            return 0, 0
        with self.lock:
            db = self.connect()
            try:
                return tuple(db.execute("SELECT COUNT(DISTINCT mac), COUNT(*) FROM sightings").fetchone())
            finally:
                db.close()

    def clear(self, mac=None):
        """Forget one MAC's history, or (with none) everything."""
        if not self.path.exists():
            return
        with self.lock:
            db = self.connect()
            try:
                with db:
                    if mac:
                        db.execute("DELETE FROM sightings WHERE mac = ?", (normalize_mac(mac),))
                    else:
                        db.execute("DELETE FROM sightings")
            finally:
                db.close()

    @staticmethod
    def sighting(row):
        sighting = Sighting(*row)
        sighting.mac = format_mac(sighting.mac)
        return sighting
