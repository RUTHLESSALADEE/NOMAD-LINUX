"""RDP configuration, credential handoff and launcher isolation."""
import ctypes
import os

import pytest

from nomad import rdp
from nomad.terminal.credentials import DATA_BLOB, _api, _blob, _take
from nomad.terminal.sessions import RDP, SSH, TERMINAL_PROTOCOLS, Session, SessionFolderStore, SessionStore, \
    import_putty, parse_quick_connect, validate_session


def session(**changes):
    return Session("Windows server", protocol=RDP, host="server.example", port=3389, **changes)


def test_saved_settings_and_recent_do_not_leak_credentials(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    item = session(username=r"DOMAIN\user", saved_password="encrypted", rdp_multimon=True,
                   rdp_audio=2, rdp_width=1920, rdp_admin=True)
    store.put(item)
    store.remember(item, when=123)
    loaded = SessionStore(store.path)
    assert loaded.get(item.id) == item
    assert loaded.recent[0].session.saved_password == ""
    assert loaded.recent_session(loaded.recent[0]).saved_password == "encrypted"


@pytest.mark.parametrize("address,host,port,user", [
    ("rdp://server", "server", 3389, ""), ("server:3390", "server", 3390, ""),
    ("[2001:db8::1]:3390", "2001:db8::1", 3390, ""),
    (r"DOMAIN\user@server", "server", 3389, r"DOMAIN\user"),
])
def test_quick_connect(address, host, port, user):
    item = parse_quick_connect(address, RDP)
    assert (item.protocol, item.host, item.port, item.username) == (RDP, host, port, user)
    assert validate_session(item) is None


@pytest.mark.parametrize("field,value", [("host", "server\r\nredirectprinters:i:1"),
    ("host", "server with spaces"), ("host", "-bad"), ("username", "user\nsetting:i:1"),
    ("port", 0), ("port", "bad"), ("rdp_width", 0), ("rdp_height", "bad"),
    ("rdp_audio", 3), ("rdp_multimon", "yes")])
def test_reject_invalid_settings(field, value):
    item = session()
    setattr(item, field, value)
    assert validate_session(item)
    with pytest.raises(ValueError):
        rdp.connection_text(item)


def test_configuration_password_and_no_password(monkeypatch):
    monkeypatch.setattr(rdp, "protect_rdp_password", lambda password: "ABCDEF")
    item = session(username="user@domain", rdp_fullscreen=False, rdp_multimon=True,
                   rdp_clipboard=False, rdp_audio=2, rdp_admin=True)
    text = rdp.connection_text(item, "secret")
    assert "password 51:b:ABCDEF\r\n" in text and "secret" not in text
    assert "screen mode id:i:2\r\n" in text
    assert "use multimon:i:1\r\n" in text
    assert "redirectclipboard:i:0\r\n" in text
    assert "administrative session:i:1\r\n" in text
    assert "authentication level:i:2\r\n" in text
    assert "enablecredsspsupport:i:1\r\n" in text
    assert "prompt for credentials:i:0\r\n" in text
    plain = rdp.connection_text(item)
    assert "password 51" not in plain
    assert "prompt for credentials:i:1\r\n" in plain
    item.host = "2001:db8::1"
    assert "full address:s:[2001:db8::1]:3389" in rdp.connection_text(item)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_native_password_blob_round_trip():
    password = "RDP secret é 中"
    encrypted = rdp.protect_rdp_password(password)
    crypt32, kernel32 = _api()
    data, buffer = _blob(bytes.fromhex(encrypted))
    result = DATA_BLOB()
    assert crypt32.CryptUnprotectData(ctypes.byref(data), None, None, None, None, 1, ctypes.byref(result))
    assert _take(result, kernel32).decode("utf-16-le") == password


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    executable = tmp_path / "System32" / "mstsc.exe"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    calls, timers = [], []
    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()
    monkeypatch.setattr(rdp.subprocess, "Popen", popen)
    class Timer:
        def __init__(self, delay, function, args):
            self.delay, self.function, self.args = delay, function, args
            timers.append(self)
        def start(self):
            pass
    monkeypatch.setattr(rdp.threading, "Timer", Timer)
    return tmp_path / "handoff", calls, timers


def test_launcher_uses_unique_files_and_argument_list(launcher, monkeypatch):
    directory, calls, timers = launcher
    monkeypatch.setattr(rdp, "protect_rdp_password", lambda secret: "ABCDEF")
    rdp.launch_session(session(username="first"), "secret", directory)
    rdp.launch_session(session(username="second"), "other", directory)
    files = list(directory.glob("*.rdp"))
    assert len(files) == 2
    assert {"username:s:first" in file.read_text(encoding="utf-16") for file in files} == {True, False}
    for args, kwargs in calls:
        assert len(args) == 2 and kwargs == {"shell": False}
        assert "secret" not in str(args)
    assert all(timer.delay == 60 and timer.daemon for timer in timers)
    for timer in timers:
        timer.function(*timer.args)
    assert not list(directory.glob("*.rdp"))


def test_failed_launch_removes_file(launcher, monkeypatch):
    directory, calls, timers = launcher
    def fail(*args, **kwargs):
        raise OSError("Cannot launch")
    monkeypatch.setattr(rdp.subprocess, "Popen", fail)
    with pytest.raises(OSError):
        rdp.launch_session(session(), directory=directory)
    assert not list(directory.glob("*.rdp")) and not timers


def test_missing_client_does_not_write_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    with pytest.raises(OSError, match="not found"):
        rdp.launch_session(session(), directory=tmp_path / "handoff")
    assert not (tmp_path / "handoff").exists()


def test_stale_cleanup_only_removes_old_nomad_files(tmp_path):
    for name in ("nomad-old.rdp", "nomad-fresh.rdp", "other.rdp"):
        (tmp_path / name).touch()
    os.utime(tmp_path / "nomad-old.rdp", (0, 0))
    os.utime(tmp_path / "other.rdp", (0, 0))
    rdp.clean_old_connections(tmp_path)
    assert {file.name for file in tmp_path.iterdir()} == {"nomad-fresh.rdp", "other.rdp"}


def test_rdp_backup_restores_credentials_and_preferences(tmp_path):
    from PyQt5.QtCore import QSettings
    from nomad.terminal.backup import write_backup, read_backup, restore_backup
    from nomad.terminal.commands import CommandStore
    from nomad.terminal.highlight import HighlightStore
    store = SessionStore(str(tmp_path / "source" / "sessions.json"))
    store.vault.reveal = lambda secret: secret.removeprefix("source:")
    item = session(saved_password="source:rdp-secret", rdp_admin=True, rdp_multimon=True)
    store.put(item)
    terminal = SessionFolderStore(store, TERMINAL_PROTOCOLS)
    desktops = SessionFolderStore(store, {RDP}, "rdp_folders")
    terminal.add_folder("Switches/Empty")
    desktops.add_folder("Desktops/Empty")
    commands = CommandStore(str(tmp_path / "commands.json"))
    highlights = HighlightStore(str(tmp_path / "highlights.json"))
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    settings.setValue("rdp/quick", "server:3390")
    path = tmp_path / "backup.nomad"
    write_backup(path, "backup-password", terminal, commands, highlights, settings)
    assert b"rdp-secret" not in path.read_bytes()
    data = read_backup(path, "backup-password")
    destination = SessionStore(str(tmp_path / "destination" / "sessions.json"))
    destination.vault.protect = lambda secret: "destination:" + secret
    restore_backup(data, SessionFolderStore(destination, TERMINAL_PROTOCOLS), commands, highlights, settings)
    restored = destination.get(item.id)
    assert restored.saved_password == "destination:rdp-secret"
    assert restored.rdp_admin and restored.rdp_multimon
    assert settings.value("rdp/quick") == "server:3390"
    assert "Desktops/Empty" in destination.rdp_folders
    assert "Desktops/Empty" not in destination.folders
    assert "Switches/Empty" in destination.folders
    assert "Switches/Empty" not in destination.rdp_folders
    assert destination.vault.forget_everything(destination.sessions) == 1
    assert restored.saved_password == ""


def test_independent_folder_namespaces_persist_and_mutate_safely(tmp_path):
    source = SessionStore(str(tmp_path / "sessions.json"))
    terminal = SessionFolderStore(source, TERMINAL_PROTOCOLS)
    desktops = SessionFolderStore(source, {RDP}, "rdp_folders")
    terminal.add_folder("Switches/Empty")
    desktops.add_folder("Desktops/Empty")
    ssh = Session("Same name", host="switch", folder="Shared/Nested")
    desktop = session(folder="Shared/Nested")
    terminal.put(ssh)
    desktops.put(desktop)
    assert "Switches" not in desktops.all_folders()
    assert "Desktops" not in terminal.all_folders()
    terminal.rename_folder("Shared", "Network")
    assert desktop.folder == "Shared/Nested" and ssh.folder == "Network/Nested"
    desktops.move_folder("Shared", "Windows")
    assert desktop.folder == "Windows/Shared/Nested" and ssh.folder == "Network/Nested"
    desktops.delete_folder("Windows")
    assert source.get(ssh.id) is not None and source.get(desktop.id) is None
    desktops.put(session(folder="Network/Nested"))
    terminal.delete_folder("Network")
    assert len(desktops.sessions) == 1 and not terminal.sessions
    loaded = SessionStore(source.path)
    assert "Switches/Empty" in SessionFolderStore(loaded, TERMINAL_PROTOCOLS).all_folders()
    assert "Switches/Empty" not in SessionFolderStore(loaded, {RDP}, "rdp_folders").all_folders()
    assert "Desktops/Empty" in SessionFolderStore(loaded, {RDP}, "rdp_folders").all_folders()


def test_scoped_updates_imports_and_vault_include_all_protocols(tmp_path):
    from nomad.ui.vault_dialog import vault_sessions
    from nomad.terminal.securecrt import import_securecrt
    from types import SimpleNamespace
    source = SessionStore(str(tmp_path / "sessions.json"))
    terminal = SessionFolderStore(source, TERMINAL_PROTOCOLS)
    desktops = SessionFolderStore(source, {RDP}, "rdp_folders")
    desktop = session()
    desktops.put(desktop)
    desktops.put(desktop.copy(id=desktop.id, name="Updated"))
    assert desktops.sessions[0].name == "Updated" and len(desktops.sessions) == 1
    assert import_putty(terminal, [("PuTTY", {"HostName": "switch", "Protocol": "ssh"})]) == 1
    export = SimpleNamespace(sessions=[SimpleNamespace(session=Session("SecureCRT", host="another"), password="")])
    assert import_securecrt(terminal, export) == (1, 0)
    assert len(terminal.sessions) == 2 and len(desktops.sessions) == 1
    assert len(vault_sessions(desktops)) == 3 and len(vault_sessions(terminal)) == 3
    assert desktops.vault is terminal.vault is source.vault
