"""Saved terminal sessions (SSH, Telnet, serial, raw TCP), organised in folders, plus importing PuTTY's sessions."""
import dataclasses
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote

from ..system import app_data_dir
from .vault import Vault

log = logging.getLogger(__name__)

SSH, TELNET, SERIAL, RAW = "SSH", "Telnet", "Serial", "Raw TCP"
PROTOCOLS = [SSH, TELNET, SERIAL, RAW]
DEFAULT_PORTS = {SSH: 22, TELNET: 23, RAW: 23}
AUTH_PASSWORD, AUTH_KEY, AUTH_AGENT = "password", "key", "agent"
FILE_PROTOCOLS = ["Auto", "SFTP", "SCP"]
PARITIES = ["None", "Even", "Odd", "Mark", "Space"]
FLOW_CONTROLS = ["None", "XON/XOFF", "RTS/CTS", "DSR/DTR"]
BAUD_RATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
LINE_ENDINGS = {"CR": "\r", "LF": "\n", "CR+LF": "\r\n"}
ENCODINGS = ["utf-8", "cp437", "latin-1", "cp1252"]
FILE_NAME = "sessions.json"
FORMAT_VERSION = 1
RECENT_LIMIT = 10


@dataclass
class Session:
    name: str
    protocol: str = SSH
    host: str = ""
    port: int = 22
    folder: str = ""  # "Site A/Core"; "" for the top level
    # SSH
    username: str = ""
    auth: str = AUTH_PASSWORD
    saved_password: str = ""  # Encrypted with credentials.protect(); "" to ask each time
    key_file: str = ""
    saved_passphrase: str = ""  # For an encrypted key file, also encrypted
    keepalive: int = 30  # Seconds; 0 turns it off
    file_protocol: str = "Auto"  # The SCP page: "Auto" (SFTP if the server has it, else SCP), "SFTP" or "SCP"
    scp_sudo: bool = False  # The SCP page works as root, through sudo
    # Serial
    serial_port: str = "COM1"
    baud_rate: int = 9600
    data_bits: int = 8
    parity: str = "None"
    stop_bits: float = 1
    flow_control: str = "None"
    # Terminal
    terminal_type: str = "xterm-256color"
    encoding: str = "utf-8"
    backspace_sends_delete: bool = True  # DEL (^?) as most systems expect; off sends ^H
    local_echo: bool = False  # For raw TCP and serial devices that don't echo what you type
    line_ending: str = "CR"  # What Enter sends on raw TCP and serial
    scrollback: int = 10000
    log_to_file: bool = False
    log_folder: str = ""
    notes: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def path(self):
        return f"{self.folder}/{self.name}" if self.folder else self.name

    def target(self):
        """Where it connects, for display: "admin@10.0.0.1", "COM3 9600 8N1", "10.0.0.5:9100"."""
        if self.protocol == SERIAL:
            parity = self.parity[0] if self.parity else "N"
            stop = int(self.stop_bits) if float(self.stop_bits).is_integer() else self.stop_bits
            return f"{self.serial_port} {self.baud_rate} {self.data_bits}{parity}{stop}"
        default = DEFAULT_PORTS.get(self.protocol)
        where = self.host if self.port == default else f"{self.host}:{self.port}"
        return f"{self.username}@{where}" if self.protocol == SSH and self.username else where

    def copy(self, **changes):
        """A copy with a new id (so it's a separate session), unless an id is given."""
        changes.setdefault("id", uuid.uuid4().hex)
        return dataclasses.replace(self, **changes)


def validate_session(session):
    """Returns an error message, or None."""
    if not session.name.strip():
        return "Give the session a name."
    if "/" in session.name:
        return "Session names can't contain \"/\" (it separates folders)."
    if session.protocol not in PROTOCOLS:
        return f"Unknown protocol {session.protocol}."
    if session.protocol == SERIAL:
        if not session.serial_port.strip():
            return "Choose a serial port, such as COM3."
        if session.baud_rate <= 0:
            return "Enter a baud rate, such as 9600."
        return None
    if not session.host.strip():
        return "Enter the host name or IP address to connect to."
    if not 1 <= int(session.port) <= 65535:
        return "The port must be between 1 and 65535."
    if session.protocol == SSH and session.auth == AUTH_KEY and not session.key_file.strip():
        return "Choose the private key file, or use password authentication."
    return None


def normalize_folder(folder):
    return "/".join(part.strip() for part in folder.replace("\\", "/").split("/") if part.strip())


def session_from_dict(data):
    known = {item.name for item in dataclasses.fields(Session)}
    values = {key: value for key, value in data.items() if key in known}
    session = Session(**{"name": "Session", **values})
    session.folder = normalize_folder(session.folder)
    return session


def target_key(session):
    """What makes two connections "the same place", for the recent list."""
    if session.protocol == SERIAL:
        return SERIAL, session.serial_port.upper(), int(session.baud_rate)
    return session.protocol, session.host.lower(), int(session.port), session.username.lower()


@dataclass
class RecentEntry:
    """A connection made recently. session is a copy without saved secrets; saved_id is the saved session it came
    from (or was later saved as), which is used instead while it still exists."""
    session: Session
    saved_id: str = ""
    last_used: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self):
        return {"id": self.id, "saved_id": self.saved_id, "last_used": self.last_used,
                "session": dataclasses.asdict(self.session)}

    @classmethod
    def from_dict(cls, data):
        return cls(session_from_dict(data.get("session") or {}), str(data.get("saved_id") or ""),
                   float(data.get("last_used") or 0), str(data.get("id") or uuid.uuid4().hex))


class SessionStore:
    """Sessions and folders, saved as JSON in the roaming app data folder."""

    def __init__(self, path=None):
        self.path = path or os.path.join(app_data_dir(), FILE_NAME)
        self.sessions = []
        self.folders = set()  # Includes empty folders, which have no session to imply them
        self.recent = []  # RecentEntry, newest first
        self.vault_settings = {}  # Master password salt and check value (no secrets), kept by the Vault
        self.vault = Vault(self.vault_settings, self.save)
        self.listeners = []  # Called after every save, so each page showing the sessions can refresh
        self.load()

    def load(self):
        self.sessions, self.folders, self.recent = [], set(), []
        self.vault_settings.clear()
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError) as error:
            log.error("Couldn't read %s: %s", self.path, error)
            return
        self.sessions = [session_from_dict(item) for item in data.get("sessions", []) if isinstance(item, dict)]
        self.folders = {normalize_folder(folder) for folder in data.get("folders", []) if isinstance(folder, str)}
        self.folders.discard("")
        self.recent = [RecentEntry.from_dict(item) for item in data.get("recent", [])
                       if isinstance(item, dict)][:RECENT_LIMIT]
        if isinstance(data.get("vault"), dict):
            self.vault_settings.update(data["vault"])

    def save(self):
        data = {"version": FORMAT_VERSION, "folders": sorted(self.all_folders()),
                "sessions": [dataclasses.asdict(session) for session in self.sessions],
                "recent": [entry.to_dict() for entry in self.recent]}
        if self.vault_settings:
            data["vault"] = dict(self.vault_settings)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
        os.replace(temporary, self.path)  # Never leave a half-written file behind
        for listener in list(self.listeners):
            listener()

    def all_folders(self):
        """Every folder, including the parents of nested ones."""
        folders = set()
        for folder in self.folders | {session.folder for session in self.sessions}:
            parts = folder.split("/") if folder else []
            for depth in range(1, len(parts) + 1):
                folders.add("/".join(parts[:depth]))
        return folders

    def get(self, session_id):
        return next((session for session in self.sessions if session.id == session_id), None)

    def put(self, session):
        """Add a session, or replace the one with the same id."""
        session.folder = normalize_folder(session.folder)
        for index, existing in enumerate(self.sessions):
            if existing.id == session.id:
                self.sessions[index] = session
                break
        else:
            self.sessions.append(session)
        self.save()

    def delete(self, session_id):
        self.sessions = [session for session in self.sessions if session.id != session_id]
        self.save()

    def add_folder(self, folder):
        folder = normalize_folder(folder)
        if folder:
            self.folders.add(folder)
            self.save()
        return folder

    def rename_folder(self, old, new):
        """Rename a folder, or give it a new path to move it (with everything under it). If the new path is already
        a folder, the two merge; a session whose name is taken there gets " (2)" added. The folder it came out of
        stays, even if that leaves it empty."""
        old, new = normalize_folder(old), normalize_folder(new)
        if not old or not new or old == new:
            return
        if new.startswith(old + "/"):
            raise ValueError("A folder can't be moved into itself.")

        def moved(folder):
            if folder == old:
                return new
            if folder.startswith(old + "/"):
                return new + folder[len(old):]
            return folder

        self.folders = self.all_folders()  # Keep folders that only existed because of the sessions moving out
        for session in self.sessions:
            folder = moved(session.folder)
            if folder != session.folder:
                session.folder = folder
                session.name = self.unique_name(session.name, folder, ignore_id=session.id)
        self.folders = {moved(folder) for folder in self.folders}
        self.folders.discard("")
        self.save()

    def move_folder(self, folder, parent):
        """Move a folder (and everything in it) into another folder, or to the top level with parent "". Returns
        its new path."""
        folder, parent = normalize_folder(folder), normalize_folder(parent)
        name = folder.rpartition("/")[2]
        new = f"{parent}/{name}" if parent else name
        if parent == folder or parent.startswith(folder + "/"):
            raise ValueError("A folder can't be moved into itself.")
        self.rename_folder(folder, new)
        return new

    def move_sessions(self, session_ids, folder):
        """Move sessions into a folder ("" for the top level). Names already taken there get " (2)" added.
        Returns how many moved."""
        folder = normalize_folder(folder)
        self.folders = self.all_folders()  # Folders emptied by the move stay
        if folder:
            self.folders.add(folder)
        moved = 0
        for session in self.sessions:
            if session.id in session_ids and session.folder != folder:
                session.folder = folder
                session.name = self.unique_name(session.name, folder, ignore_id=session.id)
                moved += 1
        self.save()
        return moved

    def delete_many(self, session_ids):
        self.sessions = [session for session in self.sessions if session.id not in session_ids]
        self.save()

    def delete_folder(self, folder):
        """Delete a folder and every session and folder in it."""
        folder = normalize_folder(folder)

        def inside(path):
            return path == folder or path.startswith(folder + "/")

        self.sessions = [session for session in self.sessions if not inside(session.folder)]
        self.folders = {path for path in self.folders if not inside(path)}
        self.save()

    # ----------------------------------------------------------------- Recent connections

    def remember(self, session, when=None):
        """Put a connection at the top of the recent list (moving it up if it's already there)."""
        saved_id = session.id if self.get(session.id) is not None else ""
        key = target_key(session)

        def same(entry):
            if saved_id:
                return entry.saved_id == saved_id
            return not self.get(entry.saved_id) and target_key(entry.session) == key

        previous = next((entry for entry in self.recent if same(entry)), None)
        entry = RecentEntry(session.copy(saved_password="", saved_passphrase=""), saved_id,
                            time.time() if when is None else when, previous.id if previous else uuid.uuid4().hex)
        self.recent = [entry] + [other for other in self.recent if other is not previous][:RECENT_LIMIT - 1]
        self.save()
        return entry

    def recent_entry(self, entry_id):
        return next((entry for entry in self.recent if entry.id == entry_id), None)

    def recent_session(self, entry):
        """What to open for a recent entry: the saved session if it still exists, otherwise a fresh copy."""
        saved = self.get(entry.saved_id) if entry.saved_id else None
        return saved if saved is not None else entry.session.copy()

    def link_recent(self, session):
        """A session was just saved: recent entries for the same place (not already tied to a saved session) now
        open it, with its name and saved password."""
        key = target_key(session)
        changed = False
        for entry in self.recent:
            if not self.get(entry.saved_id) and target_key(entry.session) == key:
                entry.saved_id = session.id
                entry.session = session.copy(saved_password="", saved_passphrase="")
                changed = True
        if changed:
            self.save()

    def forget_recent(self, entry_id=None):
        """Remove one recent entry, or all of them."""
        self.recent = [entry for entry in self.recent if entry_id is not None and entry.id != entry_id]
        self.save()

    def unique_name(self, name, folder, ignore_id=None):
        taken = {session.name.lower() for session in self.sessions
                 if session.folder == folder and session.id != ignore_id}
        if name.lower() not in taken:
            return name
        number = 2
        while f"{name} ({number})".lower() in taken:
            number += 1
        return f"{name} ({number})"


# ----------------------------------------------------------------- Quick connect

QUICK_PATTERN = re.compile(r"^(?:(?P<scheme>ssh|telnet|raw|serial)(?:://|\s+))?(?:(?P<user>[^@\s]+)@)?"
                           r"(?P<host>\[[^\]]+\]|[^\s:]+)(?::(?P<port>\d+))?$", re.IGNORECASE)
SCHEMES = {"ssh": SSH, "telnet": TELNET, "raw": RAW, "serial": SERIAL}


def parse_quick_connect(text, default_protocol=SSH):
    """Turn "admin@10.0.0.1", "telnet 10.0.0.5", "10.0.0.9:2222", "raw 10.0.0.5:9100" or "COM3:115200" into a
    Session (not saved). Raises ValueError with a message suitable for showing to the user."""
    text = text.strip()
    if not text:
        raise ValueError("Type a host to connect to, such as admin@10.0.0.1 or COM3.")
    serial = re.match(r"^(?:serial(?:://|\s+))?(COM\d+)(?:[:\s]+(\d+))?$", text, re.IGNORECASE)
    if serial:
        port = serial.group(1).upper()
        baud = int(serial.group(2)) if serial.group(2) else 9600
        return Session(name=port, protocol=SERIAL, serial_port=port, baud_rate=baud)
    match = QUICK_PATTERN.match(text)
    if not match:
        raise ValueError(f"'{text}' isn't something to connect to. Try admin@10.0.0.1, telnet 10.0.0.5 or COM3.")
    protocol = SCHEMES.get((match.group("scheme") or "").lower(), default_protocol)
    host = match.group("host").strip("[]")
    port = int(match.group("port")) if match.group("port") else DEFAULT_PORTS.get(protocol, 22)
    if not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535.")
    user = match.group("user") or ""
    name = f"{user}@{host}" if user else host
    return Session(name=name, protocol=protocol, host=host, port=port, username=user,
                   line_ending="CR+LF" if protocol == RAW else "CR")


# ----------------------------------------------------------------- Importing from PuTTY

PUTTY_KEY = r"Software\SimonTatham\PuTTY\Sessions"
PUTTY_PROTOCOLS = {"ssh": SSH, "telnet": TELNET, "raw": RAW, "serial": SERIAL}
PUTTY_PARITY = {0: "None", 1: "Odd", 2: "Even", 3: "Mark", 4: "Space"}
PUTTY_FLOW = {0: "None", 1: "XON/XOFF", 2: "RTS/CTS", 3: "DSR/DTR"}


def session_from_putty(name, values, folder="Imported from PuTTY"):
    """Build a Session from one PuTTY session's registry values, or None if it can't be used."""
    protocol = PUTTY_PROTOCOLS.get(str(values.get("Protocol", "ssh")).lower())
    if protocol is None:
        return None
    session = Session(name=unquote(name).replace("/", "-"), protocol=protocol, folder=folder)
    session.host = str(values.get("HostName", "")).strip()
    if "@" in session.host:  # PuTTY lets people type user@host into the host box
        session.username, session.host = session.host.split("@", 1)
    session.port = int(values.get("PortNumber", DEFAULT_PORTS.get(protocol, 22)) or DEFAULT_PORTS.get(protocol, 22))
    session.username = str(values.get("UserName", "")) or session.username
    key_file = str(values.get("PublicKeyFile", "")).strip()
    if key_file:
        session.auth, session.key_file = AUTH_KEY, key_file
    ping = int(values.get("PingIntervalSecs", 0) or 0) or int(values.get("PingInterval", 0) or 0) * 60
    session.keepalive = ping
    if protocol == SERIAL:
        session.serial_port = str(values.get("SerialLine", "COM1"))
        session.baud_rate = int(values.get("SerialSpeed", 9600) or 9600)
        session.data_bits = int(values.get("SerialDataBits", 8) or 8)
        session.stop_bits = int(values.get("SerialStopHalfbits", 2) or 2) / 2
        session.parity = PUTTY_PARITY.get(int(values.get("SerialParity", 0) or 0), "None")
        session.flow_control = PUTTY_FLOW.get(int(values.get("SerialFlowControl", 0) or 0), "None")
    elif not session.host:
        return None
    return session


def read_putty_sessions():
    """[(name, {value: data})] for every session PuTTY has saved for this user (not its Default Settings)."""
    import winreg
    sessions = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PUTTY_KEY)
    except OSError:
        return sessions
    with root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if unquote(name) == "Default Settings":
                continue
            values = {}
            with winreg.OpenKey(root, name) as key:
                value_index = 0
                while True:
                    try:
                        value_name, data, _ = winreg.EnumValue(key, value_index)
                    except OSError:
                        break
                    values[value_name] = data
                    value_index += 1
            sessions.append((name, values))
    return sessions


def import_putty(store, putty_sessions=None):
    """Add PuTTY's sessions to the store, skipping ones already there. Returns how many were added."""
    putty_sessions = read_putty_sessions() if putty_sessions is None else putty_sessions
    existing = {(session.name.lower(), session.host.lower(), session.protocol) for session in store.sessions}
    added = 0
    for name, values in putty_sessions:
        session = session_from_putty(name, values)
        if session is None or (session.name.lower(), session.host.lower(), session.protocol) in existing:
            continue
        store.sessions.append(session)
        existing.add((session.name.lower(), session.host.lower(), session.protocol))
        added += 1
    if added:
        store.save()
    return added
