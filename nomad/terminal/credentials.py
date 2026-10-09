"""Saved passwords, encrypted with Windows' Data Protection API (DPAPI) for the current Windows account,
or POSIX fallback using PBKDF2/AES-GCM for Linux/Unix.

Only the same Windows user on the same computer can decrypt them: copying sessions.json to another account or
machine leaves the passwords unreadable (those sessions then ask for the password again).
"""
import base64
import ctypes
import os
import sys
from pathlib import Path

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4  # Any account on this computer can decrypt it (for services)
ENTROPY = b"NOMAD saved session credential"  # Extra input, so other programs' DPAPI data can't be swapped in
DESCRIPTION = "NOMAD saved session password"

# WinTypes fallback for non-Windows platforms
if sys.platform == "win32":
    from ctypes import wintypes
else:
    class _WinTypes:
        DWORD = ctypes.c_uint32
        LPCWSTR = ctypes.c_wchar_p
        HANDLE = ctypes.c_void_p
    wintypes = _WinTypes()


class CredentialError(Exception):
    """A saved password couldn't be read (for example, the sessions file came from another Windows account)."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _api():
    if sys.platform != "win32":
        raise NotImplementedError("Windows DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
                                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(DATA_BLOB)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return crypt32, kernel32


def _take(blob, kernel32):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


# --------------------------------------------------------------------- Linux / POSIX fallback
def _linux_key(machine=False):
    """Derive an AES-256 key bound to this user (or this machine if machine=True)."""
    import hashlib
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    machine_id = b""
    for mid_path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        if mid_path.is_file():
            try:
                machine_id = mid_path.read_bytes().strip()
                break
            except OSError:
                pass
    if not machine_id:
        machine_id = b"nomad-default-machine-id"

    if machine:
        salt = hashlib.sha256(b"nomad-machine:" + machine_id).digest()
        material = machine_id + ENTROPY
    else:
        # User-bound: use ~/.nomad/key or ~/.config/nomad/key, plus machine_id and UID
        uid = str(os.getuid()).encode("ascii") if hasattr(os, "getuid") else b"0"
        key_dir = Path.home() / ".nomad"
        key_file = key_dir / "credential.key"
        if not key_file.is_file():
            try:
                key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                key_file.write_bytes(os.urandom(32))
                key_file.chmod(0o600)
            except OSError:
                pass
        user_key = b""
        if key_file.is_file():
            try:
                user_key = key_file.read_bytes()
            except OSError:
                pass
        salt = hashlib.sha256(b"nomad-user:" + machine_id + b":" + uid).digest()
        material = user_key + machine_id + uid + ENTROPY

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return kdf.derive(material)


def _posix_protect(secret: str, machine: bool = False) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _linux_key(machine=machine)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    data = secret.encode("utf-8")
    ct = aesgcm.encrypt(nonce, data, ENTROPY)
    payload = b"\x01" + (b"\x01" if machine else b"\x00") + nonce + ct
    return base64.b64encode(payload).decode("ascii")


def _posix_unprotect(stored: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.b64decode(stored, validate=True)
    except (ValueError, TypeError):
        raise CredentialError("The saved password is damaged.") from None
    if len(raw) < 1 + 1 + 12 + 16 or raw[0] != 1:
        raise CredentialError("The saved password is damaged or from an unknown version.")
    machine = (raw[1] == 1)
    nonce = raw[2:14]
    ct = raw[14:]
    key = _linux_key(machine=machine)
    aesgcm = AESGCM(key)
    try:
        data = aesgcm.decrypt(nonce, ct, ENTROPY)
    except Exception:
        raise CredentialError("The saved password can't be read with this user account. Enter it again.")
    return data.decode("utf-8")


def protect(secret, machine=False):
    """Encrypt a password for this Windows account (or, with machine, for any account on this computer: what a
    service and the GUI on the same machine both need). Returns text safe to store in a JSON file."""
    if sys.platform != "win32":
        return _posix_protect(secret, machine=machine)

    crypt32, kernel32 = _api()
    data, _data_buffer = _blob(secret.encode("utf-8"))
    entropy, _entropy_buffer = _blob(ENTROPY)
    result = DATA_BLOB()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if machine else 0)
    if not crypt32.CryptProtectData(ctypes.byref(data), DESCRIPTION, ctypes.byref(entropy), None, None, flags,
                                    ctypes.byref(result)):
        raise CredentialError(f"Windows couldn't encrypt the password ({ctypes.WinError(ctypes.get_last_error())}).")
    return base64.b64encode(_take(result, kernel32)).decode("ascii")


def unprotect(stored):
    """Decrypt a password saved by protect(). Raises CredentialError if it can't be read on this account."""
    if sys.platform != "win32":
        return _posix_unprotect(stored)

    crypt32, kernel32 = _api()
    try:
        raw = base64.b64decode(stored, validate=True)
    except (ValueError, TypeError):
        raise CredentialError("The saved password is damaged.") from None
    data, _data_buffer = _blob(raw)
    entropy, _entropy_buffer = _blob(ENTROPY)
    result = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(data), None, ctypes.byref(entropy), None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
        raise CredentialError("The saved password can't be read with this Windows account. Enter it again.")
    return _take(result, kernel32).decode("utf-8")
