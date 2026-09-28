"""Saved passwords, encrypted with Windows' Data Protection API (DPAPI) for the current Windows account.

Only the same Windows user on the same computer can decrypt them: copying sessions.json to another account or
machine leaves the passwords unreadable (those sessions then ask for the password again).
"""
import base64
import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1
ENTROPY = b"NOMAD saved session credential"  # Extra input, so other programs' DPAPI data can't be swapped in
DESCRIPTION = "NOMAD saved session password"


class CredentialError(Exception):
    """A saved password couldn't be read (for example, the sessions file came from another Windows account)."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _api():
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


def protect(secret):
    """Encrypt a password for this Windows account. Returns text safe to store in a JSON file."""
    crypt32, kernel32 = _api()
    data, _data_buffer = _blob(secret.encode("utf-8"))
    entropy, _entropy_buffer = _blob(ENTROPY)
    result = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(data), DESCRIPTION, ctypes.byref(entropy), None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
        raise CredentialError(f"Windows couldn't encrypt the password ({ctypes.WinError(ctypes.get_last_error())}).")
    return base64.b64encode(_take(result, kernel32)).decode("ascii")


def unprotect(stored):
    """Decrypt a password saved by protect(). Raises CredentialError if it can't be read on this account."""
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
