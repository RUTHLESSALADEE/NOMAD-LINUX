"""The NOMAD IPAM server: the tribe's shared copy of the IPAM data, served over HTTPS to every NOMAD on the network.

It runs headless (normally as the "NOMAD IPAM Server" Windows service; see service.py) and keeps its files in
%ProgramData%\\NOMAD\\server: the database, its TLS certificate and key, config.json with the tribe and admin
secrets, the log and nightly backups. Only Administrators, SYSTEM and the service can read that folder.

Clients prove who they are with the tribe secret from the tribe key file (the key file also carries the server's
certificate fingerprint, which clients pin, so no certificate authority is needed). The admin secret is accepted
only from the server machine itself: it's what lets NOMAD's GUI there import spreadsheets and add or delete
networks. Every edit names the Windows user and computer it came from, for the change log.

API (JSON; "Authorization: Bearer <secret>"):
    GET  /api/status                  the server's id and name, latest revision, and the caller's role
    GET  /api/changes?since=N         rows changed after revision N (see IpamStore.changes_since)
    GET  /api/log?since=N             the change log after revision N (who changed what, when), for history
    GET  /api/wait?since=N            answers as soon as there's a revision after N (or after about 25 seconds
                                      without one): each laptop keeps one of these waiting, so it syncs the
                                      moment anyone changes anything
    POST /api/edit {"action": ...}    one change, checked against the version the client last saw
    POST /api/import {"plans": [...]} import prepared networks (admin only)
"""
import datetime
import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import logging.handlers
import os
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__
from .store import IpamError, IpamStore

log = logging.getLogger(__name__)

DEFAULT_PORT = 8443
KEY_FILE_SUFFIX = ".nomadkey"
KEY_FILE_FORMAT = 1
MAX_REQUEST_BYTES = 64 * 1024 * 1024  # An import of every page of a large workbook fits easily
BACKUP_KEEP_DAYS = 14
BACKUP_CHECK_SECONDS = 3600
TEAM, ADMIN = "team", "admin"
ADMIN_ONLY_ACTIONS = {"add_network", "delete_network"}
CERTIFICATE_YEARS = 20
MAX_WAIT_SECONDS = 55
API_LEVEL = 5  # 2 added /api/wait (instant sync), 3 /api/log (history), 4 loopback subnets, 5 sightings (last seen).
# Clients cope with servers below this


class ConflictError(IpamError):
    """Someone else changed it first; the message says who and what, and the client should sync."""


def server_dir():
    """%ProgramData%\\NOMAD\\server, where the server keeps everything."""
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "NOMAD" / "server"


def host_names():
    """This machine's names and IPv4 addresses, for the tribe key file (clients try each until one answers)."""
    names = []
    for name in (socket.getfqdn(), socket.gethostname()):
        if name and name.lower() not in (existing.lower() for existing in names):
            names.append(name)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not ipaddress.ip_address(address).is_loopback and address not in names:
                names.append(address)
    except OSError:
        pass
    return names


# --------------------------------------------------------------------- Setup: config, secrets, certificate

def load_config(directory=None):
    """The server's config.json (raises OSError if it's missing or can't be read, e.g. without admin rights)."""
    directory = Path(directory or server_dir())
    return json.loads((directory / "config.json").read_text(encoding="utf-8"))


def save_config(config, directory=None):
    directory = Path(directory or server_dir())
    temporary = directory / "config.json.tmp"
    temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.replace(temporary, directory / "config.json")


def set_up(directory=None, port=DEFAULT_PORT):
    """Create the server's folder, secrets and certificate if they don't exist yet; returns the config.

    Running it again keeps what's there, so reinstalling the service doesn't lock out any laptops.
    """
    directory = Path(directory or server_dir())
    directory.mkdir(parents=True, exist_ok=True)
    try:
        config = load_config(directory)
    except FileNotFoundError:
        config = {"server_id": uuid.uuid4().hex, "port": port, "team_secret": secrets.token_urlsafe(32),
                  "admin_secret": secrets.token_urlsafe(32), "backup_dir": str(directory / "backups"),
                  "backup_keep_days": BACKUP_KEEP_DAYS}
        save_config(config, directory)
        log.info("Created the IPAM server's settings in %s", directory)
    if not (directory / "cert.pem").exists() or not (directory / "key.pem").exists():
        create_certificate(directory)
    return config


def create_certificate(directory):
    """A self-signed certificate for this machine's names. Clients pin its fingerprint (from the tribe key file),
    so it never needs to be trusted by Windows or renewed by a certificate authority."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, socket.gethostname()),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NOMAD IPAM Server")])
    alternatives = []
    for host in host_names() + ["localhost", "127.0.0.1"]:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alternatives.append(x509.DNSName(host))
    today = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(today - datetime.timedelta(days=1))
                   .not_valid_after(today + datetime.timedelta(days=365 * CERTIFICATE_YEARS))
                   .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
                   .sign(key, hashes.SHA256()))
    directory = Path(directory)
    (directory / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                          serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
    (directory / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    log.info("Created the IPAM server's certificate (fingerprint %s)", fingerprint_of_file(directory / "cert.pem"))


def fingerprint(der_bytes):
    """A certificate's SHA-256 fingerprint as hex, as the tribe key file holds it."""
    return hashlib.sha256(der_bytes).hexdigest()


def fingerprint_of_file(path):
    return fingerprint(ssl.PEM_cert_to_DER_cert(Path(path).read_text(encoding="ascii")))


def team_key(config, directory=None):
    """The contents of the tribe key file: where the server is, how to recognise it, and the tribe secret."""
    directory = Path(directory or server_dir())
    return {"format": KEY_FILE_FORMAT, "server_id": config["server_id"], "hosts": host_names(),
            "port": config["port"], "fingerprint": fingerprint_of_file(directory / "cert.pem"),
            "secret": config["team_secret"]}


def write_team_key(path, config, directory=None):
    Path(path).write_text(json.dumps(team_key(config, directory), indent=2), encoding="utf-8")


def change_team_secret(directory=None):
    """A new tribe secret: every laptop is locked out until it gets the new tribe key file."""
    config = load_config(directory)
    config["team_secret"] = secrets.token_urlsafe(32)
    save_config(config, directory)
    return config


# --------------------------------------------------------------------- Handling requests

class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _clean(text, limit=64):
    return "".join(character for character in str(text or "") if character.isprintable())[:limit].strip()


class IpamServer:
    """The server: its database and the HTTPS listener. serve() runs until stop()."""

    def __init__(self, directory=None, host="", port=None):
        self.directory = Path(directory or server_dir())
        self.config = set_up(self.directory, DEFAULT_PORT if port is None else port)
        self.port = self.config["port"] if port is None else port
        self.store = IpamStore(str(self.directory / "ipam.db"), user="server", shared=True)
        self.stop_event = threading.Event()
        self.changed = threading.Condition()  # Notified after every change, to answer waiting laptops
        self.latest = self.store.revision()
        self.latest_sightings = self.store.sighting_seq()  # Sweeps' news, which laptops also wait for
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.directory / "cert.pem", self.directory / "key.pem")
        handler = type("Handler", (_Handler,), {"server_app": self})
        try:
            self.httpd = http.server.ThreadingHTTPServer((host, self.port), handler)
        except OSError as error:
            self.store.close()
            raise OSError(f"Couldn't listen on port {self.port}: {error.strerror or error}. Another program may be "
                          f"using it (see which with: netstat -ano | findstr :{self.port}).") from error
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]  # The actual port, when asked for any free one (0)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.backup_thread = threading.Thread(target=self._backups, name="IPAM backups", daemon=True)
        self.backup_lock = threading.Lock()  # One backup at a time (the nightly one and any asked for)

    @property
    def address(self):
        return self.httpd.server_address

    def serve(self):
        log.info("NOMAD %s IPAM server listening on port %s (server %s)", __version__, self.port,
                 self.config["server_id"])
        self.backup_thread.start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.httpd.server_close()
            with self.store.lock:
                self.store.close()
            log.info("IPAM server stopped")

    def stop(self):
        self.stop_event.set()
        with self.changed:
            self.changed.notify_all()  # Let waiting requests finish
        self.httpd.shutdown()

    def announce(self, revision=None, sightings=None):
        """Tell every waiting laptop there's a new revision (or new sightings)."""
        with self.changed:
            if revision is not None:
                self.latest = max(self.latest, revision)
            if sightings is not None:
                self.latest_sightings = max(self.latest_sightings, sightings)
            self.changed.notify_all()

    def wait(self, since, timeout, sightings_since=None):
        """Block until there's a revision after `since` (or sightings after `sightings_since`, when given), the
        timeout passes, or the server stops."""
        timeout = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
        with self.changed:
            self.changed.wait_for(lambda: self.latest > since or self.stop_event.is_set() or
                                  (sightings_since is not None and self.latest_sightings > sightings_since), timeout)
            return {"revision": self.latest, "sightings": self.latest_sightings}

    # ----------------------------------------------------------------- Requests

    def role_for(self, authorization, client_address):
        secret = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if secret and hmac.compare_digest(secret, self.config["admin_secret"]):
            if not ipaddress.ip_address(client_address).is_loopback:
                raise RequestError(403, "The admin key only works on the server itself.")
            return ADMIN
        if secret and hmac.compare_digest(secret, self.config["team_secret"]):
            return TEAM
        raise RequestError(401, "This tribe key isn't accepted. The tribe key may have been changed: ask for the "
                                "new tribe key file.")

    def status(self, role):
        with self.store.lock:
            revision = self.store.revision()
        return {"server_id": self.config["server_id"], "name": socket.gethostname(), "version": __version__,
                "api": API_LEVEL, "revision": revision, "sightings": self.latest_sightings, "role": role}

    def sightings(self, since):
        with self.store.lock:
            payload, seq, more = self.store.sightings_since(since)
        return dict(payload, seq=seq, more=more)

    def record_sightings(self, user, request):
        """A laptop's sweep results: kept (newest wins) and passed on to every laptop, but not in the history."""
        network_id = request["network_id"]
        # Who swept is who sent them, whatever the request says
        hosts = [{name: value for name, value in host.items() if name != "seen_by"} for host in request.get("hosts", [])]
        ranges = [{name: value for name, value in swept.items() if name != "swept_by"}
                  for swept in request.get("ranges", [])]
        with self.store.lock:
            self.store.network(network_id)  # Raises if it was deleted
            self.store.record_sightings(network_id, hosts, ranges, by=user)
            seq = self.store.sighting_seq()
        self.announce(sightings=seq)
        return {"seq": seq}

    def log(self, since):
        with self.store.lock:
            entries, revision, more = self.store.log_since(since)
        return {"entries": entries, "revision": revision, "more": more}

    def changes(self, since):
        with self.store.lock:
            items, revision, more = self.store.changes_since(since)
        return {"items": items, "revision": revision, "more": more}

    def edit(self, role, user, request):
        action = request.get("action")
        if action in ADMIN_ONLY_ACTIONS and role != ADMIN:
            raise RequestError(403, "Only the server can add or delete tribe networks.")
        method = getattr(self, f"_edit_{action}", None) if isinstance(action, str) else None
        if method is None:
            raise RequestError(400, f"Unknown change {action!r}.")
        with self.store.lock:
            self.store.user = user
            before = self.store.revision()
            with self.store.transaction():
                method(request)
            items, revision, _ = self.store.changes_since(before)
        log.info("%s by %s", action, user)
        self.announce(revision)
        return {"items": items, "revision": revision}

    def import_networks(self, role, user, plans):
        if role != ADMIN:
            raise RequestError(403, "Spreadsheets can only be imported on the server.")
        with self.store.lock:
            self.store.user = user
            before = self.store.revision()
            networks = self.store.import_networks(plans)
            items, revision, _ = self.store.changes_since(before, limit=10 ** 9)
        log.info("Import of %s by %s", ", ".join(network.name for network in networks), user)
        self.announce(revision)
        return {"items": items, "revision": revision, "networks": [network.id for network in networks]}

    # ----------------------------------------------------------------- Edits, each checked for conflicts

    def _address_row(self, network_id, ip):
        """The address's current row, or its latest deleted one (to say who freed it), or None."""
        from .store import ip_key, parse_address
        return self.store.db.execute("SELECT * FROM addresses WHERE network_id = ? AND sort_key = ? "
                                     "ORDER BY deleted, version DESC LIMIT 1",
                                     (network_id, ip_key(parse_address(ip)))).fetchone()

    @staticmethod
    def _when(row):
        return f"{row['modified'][:16].replace('T', ' ')} UTC"

    def _check_address(self, request):
        row = self._address_row(request["network_id"], request["ip"])
        expected = request.get("expected_version")
        live = row is not None and not row["deleted"]
        if expected is None and live:
            status = "reserved" if row["status"] == "reserved" else "in use"
            named = f" for {row['name']}" if row["name"] else ""
            raise ConflictError(f"{request['ip']} was just recorded as {status}{named} by {row['modified_by']} "
                                f"({self._when(row)}). Pick another address.")
        if expected is not None and not live:
            who = f" by {row['modified_by']} ({self._when(row)})" if row is not None else ""
            raise ConflictError(f"{request['ip']} was marked free{who} since you last synced.")
        if expected is not None and row["version"] != expected:
            raise ConflictError(f"{request['ip']} was changed by {row['modified_by']} ({self._when(row)}) since you "
                                "last synced. Check it again, then make your change.")

    def _check_version(self, table, item_id, expected, what):
        row = self.store.db.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise IpamError(f"That {what} doesn't exist on the server.")
        if row["deleted"]:
            raise ConflictError(f"That {what} was deleted by {row['modified_by']} ({self._when(row)}).")
        if expected is not None and row["version"] != expected:
            raise ConflictError(f"That {what} was changed by {row['modified_by']} ({self._when(row)}) since you last "
                                "synced. Check it again, then make your change.")

    def _edit_set_address(self, request):
        self._check_address(request)
        self.store.set_address(request["network_id"], request["ip"], request.get("status", "used"),
                               request.get("name", ""), request.get("mac", ""), request.get("description", ""),
                               request.get("fields"))

    def _edit_free_address(self, request):
        self._check_address(dict(request, expected_version=request.get("expected_version", -1)))
        self.store.free_address(request["network_id"], request["ip"])

    def _edit_add_subnet(self, request):
        self.store.add_subnet(request["network_id"], request["cidr"], request.get("name", ""),
                              request.get("gateway", ""), request.get("description", ""), request.get("fields"),
                              bool(request.get("loopbacks")))

    def _edit_update_subnet(self, request):
        self._check_version("subnets", request["subnet_id"], request.get("expected_version"), "subnet")
        self.store.update_subnet(request["subnet_id"], **request["changes"])

    def _edit_delete_subnet(self, request):
        self._check_version("subnets", request["subnet_id"], request.get("expected_version"), "subnet")
        self.store.delete_subnet(request["subnet_id"], with_addresses=bool(request.get("with_addresses")))

    def _edit_add_network(self, request):
        self.store.add_network(request["name"], request.get("description", ""), request.get("fields"))

    def _edit_update_network(self, request):
        self._check_version("networks", request["network_id"], request.get("expected_version"), "network")
        self.store.update_network(request["network_id"], **request["changes"])

    def _edit_delete_network(self, request):
        self._check_version("networks", request["network_id"], request.get("expected_version"), "network")
        self.store.delete_network(request["network_id"])

    # ----------------------------------------------------------------- Backups

    def backup_now(self):
        """Copy the database to the backup folder (one file a day), and remove backups past the keep limit."""
        with self.backup_lock:
            return self._backup()

    def _backup(self):
        folder = Path(self.config.get("backup_dir") or self.directory / "backups")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"ipam-{datetime.date.today().isoformat()}.db"
        temporary = target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}.tmp")
        with self.store.lock:
            destination = sqlite3.connect(temporary)
            try:
                self.store.db.backup(destination)
            finally:
                destination.close()
        os.replace(temporary, target)
        keep = datetime.timedelta(days=int(self.config.get("backup_keep_days", BACKUP_KEEP_DAYS)))
        for old in folder.glob("ipam-*.db"):
            try:
                day = datetime.date.fromisoformat(old.stem[5:])
            except ValueError:
                continue
            if datetime.date.today() - day > keep:
                old.unlink(missing_ok=True)
        log.info("Backed up the IPAM database to %s", target)
        return target

    def _backups(self):
        while not self.stop_event.is_set():
            folder = Path(self.config.get("backup_dir") or self.directory / "backups")
            if not (folder / f"ipam-{datetime.date.today().isoformat()}.db").exists():
                try:
                    self.backup_now()
                except (OSError, sqlite3.Error) as error:
                    log.error("Backup failed: %s", error)
            self.stop_event.wait(BACKUP_CHECK_SECONDS)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_app = None  # Set per server by IpamServer
    server_version = "NOMAD-IPAM"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.debug("%s %s", self.client_address[0], format % args)

    def _reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _user(self):
        user = _clean(self.headers.get("X-NOMAD-User")) or "unknown"
        computer = _clean(self.headers.get("X-NOMAD-Computer"))
        return f"{user} ({computer})" if computer else user

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            raise RequestError(413, "That request is too large.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise RequestError(400, "The request isn't valid JSON.") from None

    def _handle(self, method):
        app = self.server_app
        try:
            role = app.role_for(self.headers.get("Authorization", ""), self.client_address[0])
            url = urlparse(self.path)
            if method == "GET" and url.path == "/api/status":
                return self._reply(200, app.status(role))
            if method == "GET" and url.path == "/api/wait":
                query = parse_qs(url.query)
                sightings_since = query.get("sightings_since")
                return self._reply(200, app.wait(int(query.get("since", ["0"])[0]),
                                                 float(query.get("timeout", ["25"])[0]),
                                                 int(sightings_since[0]) if sightings_since else None))
            if method == "GET" and url.path == "/api/sightings":
                return self._reply(200, app.sightings(int(parse_qs(url.query).get("since", ["0"])[0])))
            if method == "POST" and url.path == "/api/sightings":
                return self._reply(200, app.record_sightings(self._user(), self._body()))
            if method == "GET" and url.path == "/api/log":
                return self._reply(200, app.log(int(parse_qs(url.query).get("since", ["0"])[0])))
            if method == "GET" and url.path == "/api/changes":
                since = int(parse_qs(url.query).get("since", ["0"])[0])
                return self._reply(200, app.changes(since))
            if method == "POST" and url.path == "/api/edit":
                return self._reply(200, app.edit(role, self._user(), self._body()))
            if method == "POST" and url.path == "/api/import":
                return self._reply(200, app.import_networks(role, self._user(), self._body().get("plans", [])))
            raise RequestError(404, "No such request.")
        except RequestError as error:
            self._reply(error.status, {"error": str(error)})
        except ConflictError as error:
            self._reply(409, {"error": str(error), "conflict": True})
        except IpamError as error:
            self._reply(422, {"error": str(error)})
        except (KeyError, TypeError, ValueError) as error:
            self._reply(400, {"error": f"The request is missing something or malformed ({error})."})
        except Exception:  # Report it to the client rather than dropping the connection
            log.exception("Request failed: %s %s", method, self.path)
            self._reply(500, {"error": "The server had a problem with that request; details are in its log."})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def log_to_file(directory=None):
    """Send the server's log to server.log in its folder (a few rotated files)."""
    directory = Path(directory or server_dir())
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(directory / "server.log", maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def run_in_foreground(directory=None, port=None):
    """Run the server in this console until Ctrl+C (NOMAD.exe --ipam-server), for trying it out or troubleshooting."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    log_to_file(directory)
    server = IpamServer(directory, port=port)
    if sys.stdout is not None:  # The packaged exe has no console
        print(f"NOMAD IPAM server on port {server.port}; press Ctrl+C to stop.")
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.stop()
        thread.join(10)
