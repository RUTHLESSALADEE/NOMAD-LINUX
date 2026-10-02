"""This computer's copy of the tribe's maps, and the changes made here that the server hasn't got yet.

The copy keeps each map as the server last had it (base) plus this computer's changes not sent yet (pending, the
latest of each item). A map is read as base with pending on top, so it opens and changes offline; sync() sends the
pending changes, then fetches everyone else's, which land in base under any pending change to the same item (the
pending one is sent afterwards, so it wins: the last change made to an item is the one kept).

Thread-safe: the map page syncs on a worker thread while it reads and saves on the UI thread, and the Map Watcher
service uses it from its own.
"""
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..system import log_dir
from . import shared

log = logging.getLogger(__name__)

COPY_FILE = "maps-team.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_by TEXT, created TEXT,
                                 deleted INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS base (map_id INTEGER NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
                                 PRIMARY KEY (map_id, section, key));
CREATE TABLE IF NOT EXISTS pending (map_id INTEGER NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL, data TEXT,
                                    seq INTEGER NOT NULL, PRIMARY KEY (map_id, section, key));
CREATE TABLE IF NOT EXISTS secrets (map_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);
"""


def copy_path():
    return log_dir() / COPY_FILE  # %LOCALAPPDATA%: a cache of the server's maps, so it needn't roam


class TribeMaps:
    def __init__(self, server_id, client, path=None, protect=None, unprotect=None):
        """client: a TeamClient (or anything with its map methods). protect/unprotect: how community strings are
        kept in the copy (DPAPI for this Windows account in NOMAD, for this computer in the service)."""
        self.server_id, self.client = server_id, client
        self.path = Path(path or copy_path())
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.protect = protect or (lambda text: text)
        self.unprotect = unprotect or (lambda text: text)
        self.leases = {}  # Map id -> lease, from the last sync
        self.online, self.last_error = False, ""
        if self._meta("server_id") not in ("", server_id):  # Another server's maps: start again
            with self._transaction():
                for table in ("maps", "base", "pending", "secrets"):
                    self.db.execute(f"DELETE FROM {table}")
                self._set_meta("revision", 0)
        self._set_meta("server_id", server_id)

    def close(self):
        with self.lock:
            self.db.close()

    @contextmanager
    def _transaction(self):
        with self.lock, self.db:
            yield

    def _meta(self, name, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, name, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)", (name, str(value)))

    @property
    def revision(self):
        with self.lock:
            return int(self._meta("revision", "0") or 0)

    # ----------------------------------------------------------------- Reading

    def maps(self):
        """[{"id", "name", ...}] of the tribe's maps, by name."""
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM maps WHERE deleted = 0 ORDER BY name")]

    def map_info(self, map_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM maps WHERE id = ?", (int(map_id),)).fetchone()
            return dict(row) if row else None

    def _base(self, map_id):
        rows = self.db.execute("SELECT section, key, data FROM base WHERE map_id = ?", (int(map_id),))
        return {(row["section"], row["key"]): json.loads(row["data"]) for row in rows}

    def _pending(self, map_id):
        rows = self.db.execute("SELECT section, key, data FROM pending WHERE map_id = ? ORDER BY seq", (int(map_id),))
        return {(row["section"], row["key"]): None if row["data"] is None else json.loads(row["data"])
                for row in rows}

    def items(self, map_id):
        """{(section, key): data} of a map as this computer has it: the server's, with changes not sent on top."""
        with self.lock:
            items = self._base(map_id)
            for key, data in self._pending(map_id).items():
                if data is None:
                    items.pop(key, None)
                else:
                    items[key] = data
            return items

    def load(self, map_id):
        """(NetworkMap, settings) of a map."""
        items = self.items(map_id)
        return shared.build(items), shared.settings_of(items)

    def pending_count(self, map_id=None):
        with self.lock:
            if map_id is None:
                return self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            return self.db.execute("SELECT COUNT(*) FROM pending WHERE map_id = ?", (int(map_id),)).fetchone()[0]

    # ----------------------------------------------------------------- Changing

    def save(self, map_id, network_map, settings=None):
        """Note what changed on a map here, to send. Returns how many items changed."""
        with self._transaction():
            current = shared.flatten(network_map, settings if settings is not None
                                     else shared.settings_of(self.items(map_id)))
            changes = shared.diff(self.items(map_id), current)
            if not changes:
                return 0
            base = self._base(map_id)
            seq = (self.db.execute("SELECT MAX(seq) FROM pending").fetchone()[0] or 0) + 1
            for change in changes:
                key = (change["section"], change["key"])
                if base.get(key) == change["data"]:  # Back to the server's: nothing to send
                    self.db.execute("DELETE FROM pending WHERE map_id = ? AND section = ? AND key = ?",
                                    (int(map_id), *key))
                    continue
                self.db.execute("INSERT OR REPLACE INTO pending (map_id, section, key, data, seq) VALUES "
                                "(?, ?, ?, ?, ?)", (int(map_id), *key,
                                                    None if change["data"] is None else json.dumps(change["data"]),
                                                    seq))
                seq += 1
            return len(changes)

    def create(self, name, network_map, settings, secrets):
        """Share a map with the tribe (needs the server). Returns its id."""
        items = shared.flatten(network_map, settings)
        reply = self.client.map_request("create", name=name, changes=shared.diff({}, items), secrets=secrets)
        map_id = reply["map_id"]
        with self._transaction():
            self.db.execute("INSERT OR REPLACE INTO maps (id, name, revision) VALUES (?, ?, ?)",
                            (map_id, name.strip(), reply["revision"]))
            self.db.executemany("INSERT OR REPLACE INTO base (map_id, section, key, data) VALUES (?, ?, ?, ?)",
                                [(map_id, section, key, json.dumps(data)) for (section, key), data in items.items()])
            self._cache_secrets(map_id, secrets)
        return map_id

    def rename(self, map_id, name):
        self.client.map_request("rename", map_id=int(map_id), name=name)
        with self._transaction():
            self.db.execute("UPDATE maps SET name = ? WHERE id = ?", (name.strip(), int(map_id)))

    def delete(self, map_id):
        self.client.map_request("delete", map_id=int(map_id))
        self._forget(map_id)

    def _forget(self, map_id):
        with self._transaction():
            for table in ("base", "pending", "secrets"):
                self.db.execute(f"DELETE FROM {table} WHERE map_id = ?", (int(map_id),))
            self.db.execute("UPDATE maps SET deleted = 1 WHERE id = ?", (int(map_id),))

    # ----------------------------------------------------------------- Community strings

    def _cache_secrets(self, map_id, secrets):
        try:
            data = self.protect(json.dumps(secrets or {}))
        except Exception as error:  # DPAPI unavailable: they'll be fetched again next time
            log.warning("Couldn't keep a map's community strings: %s", error)
            return
        with self._transaction():
            self.db.execute("INSERT OR REPLACE INTO secrets (map_id, data) VALUES (?, ?)", (int(map_id), data))

    def secrets(self, map_id):
        """The map's community strings as last fetched ({} if never)."""
        with self.lock:
            row = self.db.execute("SELECT data FROM secrets WHERE map_id = ?", (int(map_id),)).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(self.unprotect(row["data"]))
        except Exception as error:
            log.warning("Couldn't read a map's community strings: %s", error)
            return {}

    def fetch_secrets(self, map_id):
        secrets = self.client.map_secrets(map_id)
        self._cache_secrets(map_id, secrets)
        return secrets

    def set_secrets(self, map_id, secrets):
        self.client.map_request("secrets", map_id=int(map_id), secrets=secrets)
        self._cache_secrets(map_id, secrets)

    # ----------------------------------------------------------------- Syncing

    def send_pending(self):
        """Send the changes made here (network: call on a worker thread). Returns how many were sent."""
        with self.lock:
            outgoing = {}
            for row in self.db.execute("SELECT * FROM pending ORDER BY seq"):
                outgoing.setdefault(row["map_id"], []).append(
                    {"section": row["section"], "key": row["key"],
                     "data": None if row["data"] is None else json.loads(row["data"])})
        sent = 0
        for map_id, changes in outgoing.items():
            try:
                self.client.map_request("push", map_id=map_id, changes=changes)
            except Exception as error:
                if "isn't on the server any more" in str(error):  # Deleted by someone: nothing to send it to
                    log.info("Dropping changes to a deleted tribe map: %s", error)
                    self._forget(map_id)
                    continue
                raise
            with self._transaction():
                for change in changes:
                    key = (map_id, change["section"], change["key"])
                    row = self.db.execute("SELECT data FROM pending WHERE map_id = ? AND section = ? AND key = ?",
                                          key).fetchone()
                    sent_data = None if change["data"] is None else json.dumps(change["data"])
                    if row is not None and row["data"] == sent_data:  # Not changed again meanwhile
                        self.db.execute("DELETE FROM pending WHERE map_id = ? AND section = ? AND key = ?", key)
                    if change["data"] is None:
                        self.db.execute("DELETE FROM base WHERE map_id = ? AND section = ? AND key = ?", key)
                    else:
                        self.db.execute("INSERT OR REPLACE INTO base VALUES (?, ?, ?, ?)", key + (sent_data,))
            sent += len(changes)
        return sent

    def apply_changes(self, payload, revision):
        """Take what map_changes brought. Returns {map id: (section, key) items changed} for the maps touched."""
        touched = {}
        with self._transaction():
            for item in payload.get("maps", []):
                self.db.execute("INSERT OR REPLACE INTO maps (id, name, created_by, created, deleted, revision) "
                                "VALUES (:id, :name, :created_by, :created, :deleted, :revision)", item)
                if item["deleted"]:
                    for table in ("base", "pending", "secrets"):
                        self.db.execute(f"DELETE FROM {table} WHERE map_id = ?", (item["id"],))
                touched.setdefault(item["id"], set())
            for item in payload.get("items", []):
                key = (item["map_id"], item["section"], item["key"])
                if item["data"] is None:
                    self.db.execute("DELETE FROM base WHERE map_id = ? AND section = ? AND key = ?", key)
                else:
                    self.db.execute("INSERT OR REPLACE INTO base VALUES (?, ?, ?, ?)",
                                    key + (json.dumps(item["data"]),))
                touched.setdefault(item["map_id"], set()).add((item["section"], item["key"]))
            self.db.execute("INSERT OR REPLACE INTO meta (name, value) VALUES ('revision', ?)", (str(revision),))
        self.leases = {int(map_id): lease for map_id, lease in payload.get("leases", {}).items()}
        return touched

    def sync(self):
        """Send what's pending, then fetch everyone's changes (network: call on a worker thread). Returns
        {map id: items changed} as apply_changes does."""
        try:
            self.send_pending()
            payload, revision = self.client.map_changes(self.revision)
        except Exception as error:
            self.online, self.last_error = False, str(error)
            raise
        self.online, self.last_error = True, ""
        return self.apply_changes(payload, revision)

    # ----------------------------------------------------------------- Watching

    def lease(self, map_id, holder, kind="gui", release=False, take=False):
        """Claim (renew, or release) the watching of a map. Returns the lease ({"yours": ...})."""
        reply = self.client.map_request("lease", map_id=int(map_id), holder=holder, kind=kind, release=release,
                                        take=take)
        lease = reply.get("lease") or {}
        with self.lock:
            if lease:
                self.leases[int(map_id)] = lease
            else:
                self.leases.pop(int(map_id), None)
        return lease
