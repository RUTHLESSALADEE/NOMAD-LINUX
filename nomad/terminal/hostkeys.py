"""Known SSH host keys: remembering each device's key, and noticing when it changes.

Kept in NOMAD's own file (OpenSSH known_hosts format) in %APPDATA%\\NOMAD, so it doesn't interfere with OpenSSH's.
"""
import base64
import hashlib
import os
import threading

import paramiko

from ..system import app_data_dir

FILE_NAME = "known_hosts"

NEW, MATCH, CHANGED = "new", "match", "changed"


def host_entry(host, port):
    """How known_hosts names a host: plain for port 22, [host]:port otherwise."""
    return host if int(port) == 22 else f"[{host}]:{port}"


def fingerprint(key):
    """SHA256:... as OpenSSH shows it."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class KnownHosts:
    def __init__(self, path=None):
        self.path = path or os.path.join(app_data_dir(), FILE_NAME)
        self.lock = threading.Lock()
        self.keys = paramiko.HostKeys()
        if os.path.exists(self.path):
            try:
                self.keys.load(self.path)
            except (OSError, paramiko.SSHException, ValueError):
                pass  # A damaged file: start again rather than refuse to connect

    def check(self, host, port, key):
        """NEW, MATCH or CHANGED, with the key we knew (or None)."""
        with self.lock:
            known = self.keys.lookup(host_entry(host, port))
        if not known:
            return NEW, None
        stored = known.get(key.get_name())
        if stored is None:
            # We know a different type of key for this host; treat as changed so the user decides
            return CHANGED, next(iter(known.values()))
        return (MATCH, stored) if stored.asbytes() == key.asbytes() else (CHANGED, stored)

    def remember(self, host, port, key):
        """Trust key for host from now on, replacing any key we had for it."""
        entry = host_entry(host, port)
        with self.lock:
            if entry in self.keys:
                del self.keys[entry]
            self.keys.add(entry, key.get_name(), key)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.keys.save(self.path)
