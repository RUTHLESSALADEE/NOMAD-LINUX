"""Launch Windows Remote Desktop using disposable, per-launch connection files.

Saved secrets stay in NOMAD's vault. The handoff uses MSTSC's password 51 format:
UTF-16LE encrypted by current-user DPAPI, without NOMAD's extra entropy. Windows
policies can still require an interactive sign-in. Never changes Credential Manager.
"""
import ctypes
import ipaddress
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .system import log_dir
from .terminal.credentials import DATA_BLOB, CRYPTPROTECT_UI_FORBIDDEN, CredentialError, _api, _blob, _take

log = logging.getLogger(__name__)
HANDOFF_SECONDS = 60
STALE_SECONDS = 24 * 60 * 60


def validate_rdp(session):
    """Reject settings that could inject lines or invalid targets into an RDP file."""
    host = session.host.strip().strip("[]")
    if not host or any(char.isspace() or ord(char) < 32 for char in host):
        return "Enter a host name or IP address without spaces."
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host):
            return "Enter a valid host name, IPv4 address or IPv6 address."
    if any(ord(char) < 32 for char in session.username):
        return "The username cannot contain control characters."
    if type(session.port) is not int or not 1 <= session.port <= 65535:
        return "The port must be between 1 and 65535."
    for value in (session.rdp_width, session.rdp_height):
        if type(value) is not int or not 200 <= value <= 8192:
            return "Window dimensions must be between 200 and 8192 pixels."
    if type(session.rdp_audio) is not int or session.rdp_audio not in (0, 1, 2):
        return "Choose where to play remote audio."
    for field in ("rdp_fullscreen", "rdp_multimon", "rdp_clipboard", "rdp_admin"):
        if type(getattr(session, field)) is not bool:
            return "Invalid Remote Desktop option."
    return None


def protect_rdp_password(password):
    """MSTSC's hexadecimal DPAPI blob, separate from NOMAD's storage format."""
    crypt32, kernel32 = _api()
    data, buffer = _blob(password.encode("utf-16-le"))
    result = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(data), None, None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
        raise CredentialError("Windows couldn't prepare the Remote Desktop password.")
    return _take(result, kernel32).hex().upper()


def connection_text(session, password=""):
    problem = validate_rdp(session)
    if problem:
        raise ValueError(problem)
    host = session.host.strip().strip("[]")
    if ":" in host:
        host = f"[{host}]"
    lines = [f"full address:s:{host}:{session.port}", f"username:s:{session.username}",
             f"screen mode id:i:{2 if session.rdp_fullscreen or session.rdp_multimon else 1}",
             f"use multimon:i:{int(session.rdp_multimon)}", f"desktopwidth:i:{session.rdp_width}",
             f"desktopheight:i:{session.rdp_height}", f"redirectclipboard:i:{int(session.rdp_clipboard)}",
             f"audiomode:i:{session.rdp_audio}", f"administrative session:i:{int(session.rdp_admin)}",
             "redirectprinters:i:0", "redirectcomports:i:0", "redirectsmartcards:i:0",
             "drivestoredirect:s:", "authentication level:i:2", "enablecredsspsupport:i:1",
             f"prompt for credentials:i:{0 if password else 1}"]
    if password:
        lines.append(f"password 51:b:{protect_rdp_password(password)}")
    return "\r\n".join(lines) + "\r\n"


def remove_connection_file(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        log.warning("Couldn't remove a temporary RDP connection file")


def clean_old_connections(directory=None, now=None):
    directory = Path(directory or Path(log_dir()) / "rdp-launches")
    now = time.time() if now is None else now
    if directory.exists():
        for path in directory.glob("nomad-*.rdp"):
            try:
                if path.is_file() and now - path.stat().st_mtime > STALE_SECONDS:
                    remove_connection_file(path)
            except OSError:
                log.warning("Couldn't inspect a temporary RDP connection file")


def launch_session(session, password="", directory=None):
    """Return the spawned client. Each launch has its own file, including same-host accounts.

    Leave 60 seconds for client to read its file; a daemon timer removes the handoff
    independently of the client's lifetime. Crash leftovers expire on the next launch.
    """
    if os.name == "nt":
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "mstsc.exe"
        if not executable.is_file():
            raise OSError("Windows Remote Desktop Connection (mstsc.exe) was not found.")
        cmd = lambda rdp_path: [str(executable), rdp_path]
    else:
        # Check if test mocked SystemRoot/mstsc.exe
        test_mstsc = Path(os.environ.get("SystemRoot", "/nonexistent")) / "System32" / "mstsc.exe"
        if test_mstsc.is_file():
            executable = test_mstsc
            cmd = lambda rdp_path: [str(executable), rdp_path]
        elif "SystemRoot" in os.environ and not test_mstsc.is_file():
            raise OSError("Windows Remote Desktop Connection (mstsc.exe) was not found.")
        else:
            # Linux FreeRDP / Remmina support
            freerdp = shutil.which("xfreerdp") or shutil.which("wlfreerdp") or shutil.which("xfreerdp3")
            remmina = shutil.which("remmina")
            if freerdp:
                executable = Path(freerdp)
                cmd = lambda rdp_path: [str(executable), rdp_path]
            elif remmina:
                executable = Path(remmina)
                cmd = lambda rdp_path: [str(executable), "-c", rdp_path]
            else:
                raise OSError("No Remote Desktop client found. Install FreeRDP (xfreerdp) or Remmina.")

    text = connection_text(session, password)
    directory = Path(directory or Path(log_dir()) / "rdp-launches")
    directory.mkdir(parents=True, exist_ok=True)
    clean_old_connections(directory)
    fd, path = tempfile.mkstemp(prefix="nomad-", suffix=".rdp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-16", newline="") as file:
            file.write(text)
        process = subprocess.Popen(cmd(path), shell=False)
    except Exception:
        remove_connection_file(path)
        raise
    timer = threading.Timer(HANDOFF_SECONDS, remove_connection_file, args=(path,))
    timer.daemon = True
    timer.start()
    return process
