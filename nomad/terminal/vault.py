"""Saved-password protection: Windows' DPAPI, plus an optional NOMAD master password on top.

Without a master password, secrets are encrypted with DPAPI for the current Windows account (format "v1": plain
DPAPI text from credentials.protect). With one, each secret is first encrypted with AES-256-GCM under a key derived
from the master password with scrypt, then that is wrapped with DPAPI too (format "v2:..."). Reading a v2 secret needs
both this Windows account and the master password, so other programs running as the same user can't read it.

There's no recovery for a forgotten master password: the only way out is to forget the saved passwords.
"""
import base64
import hashlib
import os
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import credentials
from .credentials import CredentialError

V2_PREFIX = "v2:"
KDF_N, KDF_R, KDF_P = 2 ** 16, 8, 1  # About 0.3 s and 64 MB per try: cheap to unlock, costly to guess at scale
KDF_MAX_MEMORY = 256 * 1024 * 1024
KEY_BYTES = 32
NONCE_BYTES = 12
ASSOCIATED_DATA = b"NOMAD saved credential v2"
CHECK_TEXT = b"NOMAD master password check"
MINIMUM_LENGTH = 8
LOCK_CHOICES = [(0, "Until NOMAD closes"), (15 * 60, "After 15 minutes unused"), (60 * 60, "After 1 hour unused"),
                (4 * 60 * 60, "After 4 hours unused")]


class VaultLocked(Exception):
    """A saved secret needs the master password, which hasn't been entered (or the vault locked itself)."""


class VaultError(Exception):
    """Something the user should be told, such as a wrong master password."""


def derive_key(password, salt, n=KDF_N, r=KDF_R, p=KDF_P):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES,
                          maxmem=KDF_MAX_MEMORY)


def _seal(key, plaintext):
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, ASSOCIATED_DATA)


def _open(key, sealed):
    return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], ASSOCIATED_DATA)


def password_problem(password):
    """Why a new master password isn't acceptable, or None."""
    if len(password) < MINIMUM_LENGTH:
        return f"Use at least {MINIMUM_LENGTH} characters."
    if password.strip() != password:
        return "The password can't start or end with a space."
    return None


class Vault:
    """Protects and reveals saved secrets. settings is a dict persisted by its owner (the session store)."""

    def __init__(self, settings=None, save=lambda: None, clock=time.monotonic):
        self.settings = settings if settings is not None else {}
        self.save_settings = save
        self.clock = clock
        self.lock_object = threading.RLock()
        self.key = None
        self.last_used = 0.0

    # ----------------------------------------------------------------- State

    @property
    def enabled(self):
        return bool(self.settings.get("salt"))

    @property
    def lock_after(self):
        return int(self.settings.get("lock_after", 0))

    def set_lock_after(self, seconds):
        self.settings["lock_after"] = int(seconds)
        self.save_settings()

    @property
    def unlocked(self):
        with self.lock_object:
            if self.key is not None and self.lock_after and self.clock() - self.last_used > self.lock_after:
                self.key = None  # Idle too long: lock again
            return self.key is not None

    def lock(self):
        with self.lock_object:
            self.key = None

    def _key_for(self, password):
        salt = base64.b64decode(self.settings["salt"])
        return derive_key(password, salt, self.settings.get("n", KDF_N), self.settings.get("r", KDF_R),
                          self.settings.get("p", KDF_P))

    def check_password(self, password):
        """The key for password if it's the master password, else None."""
        if not self.enabled:
            return None
        key = self._key_for(password)
        try:
            _open(key, base64.b64decode(self.settings["check"]))
        except (InvalidTag, ValueError):
            return None
        return key

    def unlock(self, password):
        """True if password is right (the vault stays unlocked until locked or idle for lock_after)."""
        key = self.check_password(password)
        if key is None:
            return False
        with self.lock_object:
            self.key = key
            self.last_used = self.clock()
        return True

    # ----------------------------------------------------------------- Secrets

    @staticmethod
    def needs_master_password(stored):
        return bool(stored) and stored.startswith(V2_PREFIX)

    def protect(self, secret):
        """Encrypt a secret for storing. Raises VaultLocked if the master password is needed first."""
        if not self.enabled:
            return credentials.protect(secret)
        with self.lock_object:
            if not self.unlocked:
                raise VaultLocked()
            self.last_used = self.clock()
            sealed = _seal(self.key, secret.encode("utf-8"))
        return V2_PREFIX + credentials.protect(base64.b64encode(sealed).decode("ascii"))

    def reveal(self, stored):
        """Decrypt a stored secret. Raises VaultLocked (needs the master password) or CredentialError (unreadable)."""
        if not self.needs_master_password(stored):
            return credentials.unprotect(stored)
        wrapped = credentials.unprotect(stored[len(V2_PREFIX):])
        with self.lock_object:
            if not self.unlocked:
                raise VaultLocked()
            self.last_used = self.clock()
            key = self.key
        try:
            return _open(key, base64.b64decode(wrapped)).decode("utf-8")
        except (InvalidTag, ValueError):
            raise CredentialError("This saved password was protected with a different master password. Enter it "
                                  "again.") from None

    # ----------------------------------------------------------------- Setting, changing and removing

    def _reencrypt(self, sessions, reveal_with, protect_with):
        """Re-encrypt every session's saved secrets. Ones that can't be read (another account's) are cleared.
        Returns how many were cleared."""
        cleared = 0
        for session in sessions:
            for field in ("saved_password", "saved_passphrase"):
                stored = getattr(session, field)
                if not stored:
                    continue
                try:
                    setattr(session, field, protect_with(reveal_with(stored)))
                except (CredentialError, VaultLocked):
                    setattr(session, field, "")
                    cleared += 1
        return cleared

    def set_password(self, sessions, new_password, current_password=None):
        """Turn the master password on, or change it. Re-encrypts every saved secret. Returns how many unreadable
        secrets were cleared. Raises VaultError for a wrong current password or an unacceptable new one."""
        problem = password_problem(new_password)
        if problem:
            raise VaultError(problem)
        with self.lock_object:
            old_key = None
            if self.enabled:
                old_key = self.check_password(current_password or "")
                if old_key is None:
                    raise VaultError("The current master password isn't right.")
            salt = os.urandom(16)
            new_key = derive_key(new_password, salt)
            new_settings = {**self.settings, "kdf": "scrypt", "salt": base64.b64encode(salt).decode("ascii"),
                            "n": KDF_N, "r": KDF_R, "p": KDF_P,
                            "check": base64.b64encode(_seal(new_key, CHECK_TEXT)).decode("ascii")}

            def reveal_old(stored):
                if not self.needs_master_password(stored):
                    return credentials.unprotect(stored)
                if old_key is None:
                    raise CredentialError("No master password to read it with.")
                sealed = base64.b64decode(credentials.unprotect(stored[len(V2_PREFIX):]))
                try:
                    return _open(old_key, sealed).decode("utf-8")
                except (InvalidTag, ValueError):
                    raise CredentialError("Protected with a different master password.") from None

            def protect_new(secret):
                sealed = _seal(new_key, secret.encode("utf-8"))
                return V2_PREFIX + credentials.protect(base64.b64encode(sealed).decode("ascii"))

            cleared = self._reencrypt(sessions, reveal_old, protect_new)
            self.settings.clear()
            self.settings.update(new_settings)
            self.key = new_key
            self.last_used = self.clock()
        self.save_settings()
        return cleared

    def remove_password(self, sessions, current_password):
        """Turn the master password off (secrets go back to Windows-account protection only)."""
        with self.lock_object:
            key = self.check_password(current_password)
            if key is None:
                raise VaultError("The master password isn't right.")
            self.key, self.last_used = key, self.clock()
            cleared = self._reencrypt(sessions, self.reveal, credentials.protect)
            for name in ("kdf", "salt", "n", "r", "p", "check"):
                self.settings.pop(name, None)
            self.key = None
        self.save_settings()
        return cleared

    def forget_everything(self, sessions):
        """For a forgotten master password: clear every saved secret and turn the master password off."""
        with self.lock_object:
            count = 0
            for session in sessions:
                for field in ("saved_password", "saved_passphrase"):
                    if getattr(session, field):
                        setattr(session, field, "")
                        count += 1
            for name in ("kdf", "salt", "n", "r", "p", "check"):
                self.settings.pop(name, None)
            self.key = None
        self.save_settings()
        return count
