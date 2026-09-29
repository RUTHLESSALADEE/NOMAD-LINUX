"""Older SSH algorithms that paramiko no longer ships, for switches and routers that support nothing newer.

Many devices still in service (Cisco IOS 12.x and early 15.x, older HP/Aruba and Dell switches) only offer SHA-1
key exchange and "ssh-rsa" host keys. They're offered last, so any device that supports something newer uses
it: the client's preference order decides, and the key exchange hash covers both sides' lists, so an attacker
can't strip the modern ones out.
"""
import threading
import time
from hashlib import sha1

import paramiko
from cryptography.hazmat.primitives import hashes
from paramiko.auth_handler import AuthOnlyHandler
from paramiko.common import cMSG_SERVICE_REQUEST, cMSG_USERAUTH_REQUEST
from paramiko.kex_gex import KexGexSHA256
from paramiko.kex_group14 import KexGroup14SHA256
from paramiko.message import Message
from paramiko.rsakey import RSAKey
from paramiko.ssh_exception import SSHException
from paramiko.transport import ServiceRequestingTransport


class KexGroup14SHA1(KexGroup14SHA256):
    name = "diffie-hellman-group14-sha1"
    hash_algo = sha1


class KexGroup1SHA1(KexGroup14SHA256):
    """1024-bit Oakley group 2 (RFC 2409)."""
    name = "diffie-hellman-group1-sha1"
    hash_algo = sha1
    P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3"
            "CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5"
            "AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF", 16)


class KexGexSHA1(KexGexSHA256):
    name = "diffie-hellman-group-exchange-sha1"
    hash_algo = sha1


class LegacyRSAKey(RSAKey):
    """RSA host keys that sign with SHA-1 ("ssh-rsa"), as well as the SHA-2 forms."""
    HASHES = {**RSAKey.HASHES, "ssh-rsa": hashes.SHA1}


LEGACY_KEX = (KexGexSHA1, KexGroup14SHA1, KexGroup1SHA1)
SERVICE_TIMEOUT = 30  # Seconds to wait for the device to agree to a login
LEGACY_NAMES = {kex.name for kex in LEGACY_KEX} | {"ssh-rsa"}


class _AuthHandler(AuthOnlyHandler):
    def send_auth_request(self, username, method, finish_message=None):
        """paramiko's version, except the event that hears the answer exists before the request goes out. paramiko
        sends first, so a server that answers quickly signals the old event, and a right password then waits out the
        30 s auth timeout and counts as wrong."""
        self.auth_method = method
        self.username = username
        message = Message()
        message.add_byte(cMSG_USERAUTH_REQUEST)
        message.add_string(username)
        message.add_string("ssh-connection")
        message.add_string(method)
        if finish_message is not None:  # paramiko 5's auth_none passes none (a "none" request has no more fields)
            finish_message(message)
        self.auth_event = threading.Event()
        with self.transport.lock:
            self.transport._send_message(message)
        return self.wait_for_response(self.auth_event)


class CompatibleTransport(ServiceRequestingTransport):
    """paramiko's transport, plus the older algorithms at the end of each preference list.

    Based on ServiceRequestingTransport, which asks for the "ssh-userauth" service once per connection (as OpenSSH
    does). The classic Transport asks again before every login attempt, and Cisco IOS XE treats that repeat as a
    protocol error and disconnects, so a password typed after the first attempt never reached the device.
    """
    _preferred_kex = paramiko.Transport._preferred_kex + tuple(kex.name for kex in LEGACY_KEX)
    _preferred_keys = paramiko.Transport._preferred_keys + ("ssh-rsa",)
    _kex_info = {**paramiko.Transport._kex_info, **{kex.name: kex for kex in LEGACY_KEX}}
    _key_info = {**paramiko.Transport._key_info, "ssh-rsa": LegacyRSAKey}
    kex_name = ""  # The key exchange agreed on (paramiko forgets it once the exchange is done)

    def get_auth_handler(self):
        return _AuthHandler(self)

    def ensure_session(self):
        """Ask for the login service once. paramiko's version waits forever for the answer, even if the device
        hangs up; this one gives up when the connection closes or after SERVICE_TIMEOUT seconds."""
        if not self.active or not self.initial_kex_done:
            raise SSHException("No existing session")
        if self._service_userauth_accepted:
            return
        message = Message()
        message.add_byte(cMSG_SERVICE_REQUEST)
        message.add_string("ssh-userauth")
        self._send_message(message)
        deadline = time.monotonic() + SERVICE_TIMEOUT
        while not self._service_userauth_accepted:
            if not self.active:
                raise SSHException("The device closed the connection before login could start.")
            if time.monotonic() > deadline:
                raise SSHException("The device didn't answer the request to log in.")
            time.sleep(0.05)
        self.auth_handler = self.get_auth_handler()

    def _parse_kex_init(self, m):
        super()._parse_kex_init(m)
        if self.kex_engine is not None:  # Not every kex class has a name, so look it up by class
            self.kex_name = next((name for name in self.preferred_kex
                                  if self._kex_info.get(name) is type(self.kex_engine)), "")


def uses_legacy(transport):
    """Whether a connected transport ended up on one of the older algorithms."""
    return getattr(transport, "kex_name", "") in LEGACY_NAMES or getattr(transport, "host_key_type", "") == "ssh-rsa"


def describe_algorithms(transport):
    """Like "curve25519-sha256@libssh.org, ssh-ed25519, aes128-ctr", for showing what a connection uses."""
    parts = [getattr(transport, "kex_name", ""), transport.host_key_type or "", transport.local_cipher or ""]
    return ", ".join(part for part in parts if part)
