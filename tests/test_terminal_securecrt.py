import pytest

from nomad.terminal.securecrt import IMPORT_FOLDER, SecureCrtError, WrongPassphrase, decrypt_password, \
    encrypt_password, import_securecrt, read_securecrt_export
from nomad.terminal.sessions import AUTH_KEY, RAW, SERIAL, SSH, TELNET, Session, SessionStore


def session_key(name, protocol, extra=""):
    return f"""<key name="{name}">
<string name="Protocol Name">{protocol}</string>
<dword name="Is Session">1</dword>
{extra}
</key>"""


def export_xml(sessions):
    return f"""\ufeff<?xml version="1.0" encoding="UTF-8"?>
<VanDyke version="3.0">
<key name="Global"><dword name="Auto Reconnect">0</dword></key>
<key name="Sessions">
{sessions}
</key>
</VanDyke>"""


def write_export(tmp_path, passphrase=""):
    core = session_key("001 - core - 10.0.0.1", "SSH2", f"""
<string name="Hostname">10.0.0.1</string><dword name="[SSH2] Port">22</dword><string name="Username">admin</string>
<string name="Password V2">{encrypt_password("S3cret!", passphrase)}</string><dword name="Session Password Saved">1</dword>
<array name="Description"><string>Core switch</string><string>Rack 4</string></array>""")
    edge = session_key("edge", "SSH2", f"""
<string name="Hostname">10.0.0.2</string><dword name="[SSH2] Port">2222</dword><string name="Username">ops</string>
<string name="Password V2">{encrypt_password("other", passphrase, salt=bytes(range(16)) if passphrase else None)}</string>
<dword name="Use Global Public Key">0</dword><string name="Identity Filename V2">C:\\keys\\ops</string>""")
    sessions = f"""
<key name="Site A"><key name="Switches">{core}</key>{edge}</key>
{session_key("old telnet", "Telnet", '<string name="Hostname">10.0.0.3</string><dword name="Port">2323</dword>')}
{session_key("printer", "Raw", '<string name="Hostname">10.0.0.9</string><dword name="Port">9100</dword>')}
{session_key("console", "Serial", '<string name="Com Port">com4</string><dword name="Baud Rate">115200</dword>'
                                  '<dword name="Parity">2</dword><dword name="Stop Bits">2</dword>'
                                  '<dword name="Data Bits">7</dword><dword name="CTS Flow">1</dword>')}
{session_key("ancient", "RLogin", '<string name="Hostname">10.0.0.4</string>')}
{session_key("Default", "SSH2", '<string name="Hostname"/>')}"""
    path = tmp_path / "export.xml"
    path.write_text(export_xml(sessions), encoding="utf-8")
    return str(path)


def test_password_formats():
    for salt in (None, b"0123456789abcdef"):
        stored = encrypt_password("pässword", "phrase", salt)
        assert stored.startswith("02:" if salt is None else "03:")
        assert decrypt_password(stored, "phrase") == "pässword"
        with pytest.raises(WrongPassphrase):
            decrypt_password(stored, "wrong")
    assert decrypt_password(encrypt_password("", "")) == ""
    with pytest.raises(WrongPassphrase):
        decrypt_password("03:" + "00" * 64, "")
    for bad in ("02:zz", "02:", "02:" + "00" * 16, "09:" + "00" * 32):
        with pytest.raises(WrongPassphrase):
            decrypt_password(bad, "")


def test_read_export(tmp_path):
    export = read_securecrt_export(write_export(tmp_path))
    by_name = {item.session.name: item.session for item in export.sessions}
    assert set(by_name) == {"001 - core - 10.0.0.1", "edge", "old telnet", "printer", "console"}
    assert export.skipped == ["ancient"]  # "Default" is SecureCRT's template, not a session
    core = by_name["001 - core - 10.0.0.1"]
    assert (core.protocol, core.host, core.port, core.username, core.folder, core.notes) == \
        (SSH, "10.0.0.1", 22, "admin", f"{IMPORT_FOLDER}/Site A/Switches", "Core switch\nRack 4")
    edge = by_name["edge"]
    assert (edge.port, edge.auth, edge.key_file, edge.folder) == (2222, AUTH_KEY, r"C:\keys\ops", f"{IMPORT_FOLDER}/Site A")
    assert (by_name["old telnet"].protocol, by_name["old telnet"].port) == (TELNET, 2323)
    assert (by_name["printer"].protocol, by_name["printer"].port, by_name["printer"].line_ending) == (RAW, 9100, "CR+LF")
    console = by_name["console"]
    assert (console.protocol, console.serial_port, console.baud_rate, console.data_bits, console.parity,
            console.stop_bits, console.flow_control) == (SERIAL, "COM4", 115200, 7, "Even", 2, "RTS/CTS")
    assert export.encrypted_count == 2


def test_decrypt_with_passphrase(tmp_path):
    export = read_securecrt_export(write_export(tmp_path, passphrase="crt pass"))
    with pytest.raises(WrongPassphrase):
        export.decrypt("")
    assert export.decrypt("crt pass") == 2
    assert {item.session.name: item.password for item in export.sessions if item.password} == \
        {"001 - core - 10.0.0.1": "S3cret!", "edge": "other"}


def test_import_into_store(tmp_path):
    export = read_securecrt_export(write_export(tmp_path))
    export.decrypt("")
    store = SessionStore(str(tmp_path / "sessions.json"))
    added, saved = import_securecrt(store, export, protect=lambda password: "enc:" + password)
    assert (added, saved) == (5, 2)
    core = next(session for session in store.sessions if session.host == "10.0.0.1")
    assert core.saved_password == "enc:S3cret!"
    assert import_securecrt(store, read_securecrt_export(write_export(tmp_path))) == (0, 0)  # Already there
    reloaded = SessionStore(str(tmp_path / "sessions.json"))
    assert len(reloaded.sessions) == 5 and f"{IMPORT_FOLDER}/Site A/Switches" in reloaded.all_folders()


def test_import_without_passwords(tmp_path):
    export = read_securecrt_export(write_export(tmp_path, passphrase="unknown"))
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert import_securecrt(store, export, protect=lambda password: "enc:" + password) == (5, 0)
    assert all(not session.saved_password for session in store.sessions)


def test_not_an_export(tmp_path):
    for content in ("<not xml", "<VanDyke><key name='Global'/></VanDyke>", "<Other><key name='Sessions'/></Other>"):
        path = tmp_path / "bad.xml"
        path.write_text(content)
        with pytest.raises(SecureCrtError):
            read_securecrt_export(str(path))
    with pytest.raises(SecureCrtError):
        read_securecrt_export(str(tmp_path / "missing.xml"))


def test_recent_connections(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = SessionStore(path)
    saved = Session("core", SSH, "10.0.0.1", username="admin", saved_password="secret-blob")
    store.put(saved)
    store.remember(saved, when=100)
    quick = Session("admin@10.0.0.2", SSH, "10.0.0.2", username="admin")
    store.remember(quick, when=200)
    store.remember(quick.copy(), when=300)  # Same place again: moves up, no duplicate
    assert [entry.session.host for entry in store.recent] == ["10.0.0.2", "10.0.0.1"]
    assert store.recent[0].last_used == 300
    assert store.recent[1].saved_id == saved.id and store.recent[1].session.saved_password == ""  # No secrets kept
    assert store.recent_session(store.recent[1]) is store.get(saved.id)

    # Saving a quick connection ties its entry to the new saved session
    kept = Session("edge", SSH, "10.0.0.2", username="ADMIN", folder="Site")
    store.put(kept)
    store.link_recent(kept)
    assert store.recent[0].saved_id == kept.id and store.recent_session(store.recent[0]).name == "edge"

    # A deleted saved session still reconnects from its copy, as an unsaved session
    store.delete(saved.id)
    fallback = store.recent_session(store.recent[1])
    assert fallback.host == "10.0.0.1" and store.get(fallback.id) is None

    for number in range(12):
        store.remember(Session(f"h{number}", SSH, f"10.1.0.{number}"), when=400 + number)
    assert len(store.recent) == 10 and store.recent[0].session.name == "h11"

    reloaded = SessionStore(path)
    assert [entry.to_dict() for entry in reloaded.recent] == [entry.to_dict() for entry in store.recent]
    reloaded.forget_recent(reloaded.recent[0].id)
    assert len(reloaded.recent) == 9
    reloaded.forget_recent()
    assert SessionStore(path).recent == []
