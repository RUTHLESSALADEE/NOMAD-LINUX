"""Talking to the NOMAD IPAM server: the tribe key, the HTTPS connection (pinned to the server's certificate), syncing
a local copy of the tribe's data, and TeamStore, which the IPAM page uses like a local IpamStore.

Reads come from the local copy, so they work offline. Edits go straight to the server, which checks them against
what the client last saw (raising ConflictError if someone else got there first) and replies with the changed rows,
which are applied to the copy at once; other changes arrive with the next sync.
"""
import contextlib
import http.client
import json
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..system import app_data_dir, log_dir
from .server import ADMIN, KEY_FILE_FORMAT, TEAM, ConflictError, fingerprint, fingerprint_of_file, load_config, \
    server_dir
from .store import IpamError, IpamStore, current_user, parse_address

log = logging.getLogger(__name__)

SETTINGS_FILE = "ipam-team.json"
COPY_FILE = "ipam-team.db"
CONNECT_SECONDS = 4
READ_SECONDS = 60  # A first sync or an import can take a while to send


class ServerUnreachable(IpamError):
    """No answer from the server (the laptop is offline, or the server is down)."""


class OldServerError(IpamError):
    """The server is an older NOMAD that doesn't know this request (it needs updating)."""


class TeamKeyError(IpamError):
    """The tribe key file is missing, damaged, or no longer accepted."""


@dataclass
class TeamKey:
    server_id: str
    hosts: list
    port: int
    fingerprint: str
    secret: str
    role: str = TEAM

    @classmethod
    def from_dict(cls, data):
        try:
            if data.get("format") != KEY_FILE_FORMAT:
                raise ValueError
            key = cls(str(data["server_id"]), [str(host) for host in data["hosts"]], int(data["port"]),
                      str(data["fingerprint"]).lower(), str(data["secret"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TeamKeyError("That isn't a NOMAD tribe key file (or it's from a newer version of NOMAD).") from None
        if not key.hosts or len(key.fingerprint) != 64:
            raise TeamKeyError("That tribe key file is incomplete.")
        return key


def read_key_file(path):
    try:
        return TeamKey.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise TeamKeyError(f"Couldn't read {Path(path).name}: {error}") from None


def admin_key(directory=None):
    """The key for managing the server from the server itself, or None if this isn't the server (or NOMAD isn't
    running as administrator, which reading the server's folder needs)."""
    directory = Path(directory or server_dir())
    try:
        config = load_config(directory)
        return TeamKey(config["server_id"], ["127.0.0.1"], int(config["port"]),
                       fingerprint_of_file(directory / "cert.pem"), config["admin_secret"], ADMIN)
    except (OSError, KeyError, ValueError):
        return None


# --------------------------------------------------------------------- Saved connection (the secret encrypted)

def settings_path():
    return app_data_dir() / SETTINGS_FILE


def save_key(key, path=None):
    """Remember the tribe key for this Windows account (the secret encrypted with DPAPI)."""
    from ..terminal.credentials import protect
    data = {"format": KEY_FILE_FORMAT, "server_id": key.server_id, "hosts": key.hosts, "port": key.port,
            "fingerprint": key.fingerprint, "secret": protect(key.secret)}
    Path(path or settings_path()).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_saved_key(path=None):
    """The remembered tribe key, or None."""
    from ..terminal.credentials import CredentialError, unprotect
    path = Path(path or settings_path())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["secret"] = unprotect(data["secret"])
        return TeamKey.from_dict(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, CredentialError, TeamKeyError) as error:
        log.warning("Couldn't read the saved tribe key: %s", error)
        return None


def forget_key(path=None):
    Path(path or settings_path()).unlink(missing_ok=True)


def copy_path():
    return log_dir() / COPY_FILE  # %LOCALAPPDATA%: a cache of the server's data, so it needn't roam


# --------------------------------------------------------------------- HTTPS, pinned to the server's certificate

class TeamClient:
    def __init__(self, key, user=None, computer=None):
        self.key = key
        self.user = user or current_user()
        self.computer = computer or socket.gethostname()
        self.last_host = None

    def _connect(self, host):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False  # Trust comes from the pinned fingerprint instead
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(host, self.key.port, timeout=CONNECT_SECONDS, context=context)
        connection.connect()
        presented = fingerprint(connection.sock.getpeercert(binary_form=True))
        if presented != self.key.fingerprint:
            connection.close()
            raise IpamError(f"The server at {host} isn't the one in the tribe key file (its certificate is "
                            "different). Ask for a new tribe key file if the server was rebuilt.")
        connection.sock.settimeout(READ_SECONDS)
        return connection

    def request(self, method, path, body=None):
        hosts = [self.last_host] + [host for host in self.key.hosts if host != self.last_host] if self.last_host \
            else list(self.key.hosts)
        errors = []
        for host in hosts:
            try:
                connection = self._connect(host)
            except (OSError, ssl.SSLError) as error:
                errors.append(f"{host}: {getattr(error, 'strerror', None) or error}")
                continue
            try:
                data = None if body is None else json.dumps(body).encode("utf-8")
                headers = {"Authorization": f"Bearer {self.key.secret}", "X-NOMAD-User": self.user,
                           "X-NOMAD-Computer": self.computer, "Content-Type": "application/json"}
                connection.request(method, path, data, headers)
                response = connection.getresponse()
                payload = response.read()
            except (OSError, http.client.HTTPException) as error:
                errors.append(f"{host}: {error}")
                continue
            finally:
                connection.close()
            self.last_host = host
            try:
                reply = json.loads(payload or b"{}")
            except ValueError:
                raise IpamError(f"The server sent an unreadable reply (HTTP {response.status}).") from None
            if response.status == 409:
                raise ConflictError(reply.get("error", "Someone else changed it first."))
            if response.status == 401:  # The key itself was refused (whatever the server's wording)
                raise TeamKeyError(reply.get("error", "The tribe key isn't accepted."))
            if response.status == 404:
                raise OldServerError(f"The IPAM server doesn't know {path.split('?')[0]}: it's running an older "
                                     "version of NOMAD.")
            if response.status >= 400:
                raise IpamError(reply.get("error", f"The server refused that (HTTP {response.status})."))
            return reply
        raise ServerUnreachable("Can't reach the IPAM server (" + "; ".join(errors) + ").")

    def status(self):
        return self.request("GET", "/api/status")

    def changes(self, since):
        return self.request("GET", f"/api/changes?since={int(since)}")

    def wait(self, since, timeout=25):
        """Wait (up to `timeout` seconds) for a revision after `since`; returns the latest revision."""
        return self.request("GET", f"/api/wait?since={int(since)}&timeout={timeout}")["revision"]

    def fetch_all_changes(self, since):
        """Every change after `since`, following `more` (safe to call on a worker thread). Returns (status, items,
        revision)."""
        status = self.status()
        items, revision = [], since
        if status["server_id"] != self.key.server_id:
            raise TeamKeyError("The server has changed (it was set up again). Ask for the new tribe key file.")
        while True:
            reply = self.changes(revision)
            items.extend(reply["items"])
            revision = reply["revision"]
            if not reply.get("more"):
                return status, items, revision

    def edit(self, action, **arguments):
        return self.request("POST", "/api/edit", dict(arguments, action=action))

    def import_networks(self, plans):
        return self.request("POST", "/api/import", {"plans": plans})


# --------------------------------------------------------------------- The copy, and the store the UI uses

class TeamStore:
    """The tribe's IPAM data for the UI: IpamStore's reading methods from the local copy, and its changing methods
    sent to the server. Use on the UI thread (the copy's SQLite connection belongs to it)."""

    def __init__(self, key, path=None, client=None):
        self.key = key
        self.client = client or TeamClient(key)
        self.copy = IpamStore(str(path or copy_path()))
        self.online = False
        self.last_error = ""
        self.key_rejected = False  # The server refused the tribe key (a new key file is needed)
        if self.copy.get_meta("server_id") not in ("", key.server_id):
            self.reset_copy()

    @property
    def admin(self):
        return self.key.role == ADMIN

    @property
    def user(self):
        return self.client.user

    def close(self):
        self.copy.close()

    def reset_copy(self):
        """Empty the copy (for a different server), so the next sync fetches everything."""
        with self.copy.transaction():
            for table in ("networks", "subnets", "addresses", "changes"):
                self.copy.db.execute(f"DELETE FROM {table}")
            self.copy.set_meta("revision", 0)
            self.copy.set_meta("server_id", self.key.server_id)

    @property
    def revision(self):
        return int(self.copy.get_meta("revision", "0") or 0)

    @property
    def last_sync(self):
        """When the copy last caught up with the server (seconds since the epoch), or 0."""
        return float(self.copy.get_meta("last_sync", "0") or 0)

    def apply_sync(self, items, revision, status=None):
        """Apply what fetch_all_changes brought (on the UI thread)."""
        self.copy.apply_rows(items)
        with self.copy.transaction():
            self.copy.set_meta("revision", revision)
            self.copy.set_meta("server_id", self.key.server_id)
            self.copy.set_meta("last_sync", time.time())
            if status and status.get("name"):
                self.copy.set_meta("server_name", status["name"])
        self.online, self.last_error, self.key_rejected = True, "", False

    @property
    def server_name(self):
        """The server's computer name (from its last answer), or the first name in the tribe key."""
        return self.copy.get_meta("server_name") or self.key.hosts[0]

    @property
    def server_address(self):
        """"host:port" as last reached (or as first listed in the tribe key)."""
        return f"{self.client.last_host or self.key.hosts[0]}:{self.key.port}"

    def sync(self):
        """Fetch and apply every change now (blocking; the UI does the fetch on a worker thread instead)."""
        status, items, revision = self.client.fetch_all_changes(self.revision)
        self.apply_sync(items, revision, status)
        return len(items)

    def _send(self, action, **arguments):
        try:
            reply = self.client.edit(action, **arguments)
        except ServerUnreachable as error:
            self.online, self.last_error = False, str(error)
            raise ServerUnreachable("The IPAM server can't be reached, so tribe networks can't be changed right now "
                                    "(changing them offline comes in a later version).") from None
        self.online = True
        self.copy.apply_rows(reply["items"])
        return reply

    # ----------------------------------------------------------------- Reading, from the copy

    def __getattr__(self, name):
        if name in ("networks", "network", "network_named", "subnets", "subnet", "subnet_for", "addresses",
                    "address", "count_addresses", "next_free", "search"):
            return getattr(self.copy, name)
        raise AttributeError(name)

    @contextlib.contextmanager
    def transaction(self):
        yield  # Each change goes to the server on its own

    # ----------------------------------------------------------------- Changing, through the server

    def set_address(self, network_id, ip, status="used", name="", mac="", description="", fields=None):
        current = self.copy.address(network_id, ip)
        self._send("set_address", network_id=network_id, ip=str(parse_address(ip)), status=status, name=name,
                   mac=mac, description=description, fields=fields or {},
                   expected_version=current.version if current else None)
        return self.copy.address(network_id, ip)

    def free_address(self, network_id, ip):
        current = self.copy.address(network_id, ip)
        if current is not None:
            self._send("free_address", network_id=network_id, ip=str(parse_address(ip)),
                       expected_version=current.version)

    def add_subnet(self, network_id, cidr, name="", gateway="", description="", fields=None):
        reply = self._send("add_subnet", network_id=network_id, cidr=cidr, name=name, gateway=gateway,
                           description=description, fields=fields or {})
        created = [item["row"]["id"] for item in reply["items"] if item["entity"] == "subnets"]
        return self.copy.subnet(created[-1])

    def update_subnet(self, subnet_id, **changes):
        self._send("update_subnet", subnet_id=subnet_id, changes=changes,
                   expected_version=self.copy.subnet(subnet_id).version)
        return self.copy.subnet(subnet_id)

    def delete_subnet(self, subnet_id, with_addresses=False):
        self._send("delete_subnet", subnet_id=subnet_id, with_addresses=with_addresses,
                   expected_version=self.copy.subnet(subnet_id).version)

    def add_network(self, name, description="", fields=None):
        reply = self._send("add_network", name=name, description=description, fields=fields or {})
        created = [item["row"]["id"] for item in reply["items"] if item["entity"] == "networks"]
        return self.copy.network(created[-1])

    def update_network(self, network_id, **changes):
        self._send("update_network", network_id=network_id, changes=changes,
                   expected_version=self.copy.network(network_id).version)
        return self.copy.network(network_id)

    def delete_network(self, network_id):
        self._send("delete_network", network_id=network_id, expected_version=self.copy.network(network_id).version)

    def import_networks(self, plans):
        try:
            reply = self.client.import_networks(plans)
        except ServerUnreachable:
            self.online = False
            raise
        self.copy.apply_rows(reply["items"])
        return [self.copy.network(network_id) for network_id in reply["networks"]]
