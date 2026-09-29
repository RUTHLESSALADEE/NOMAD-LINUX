import pytest

from nomad.terminal.credentials import CredentialError, protect, unprotect
from nomad.terminal.sessions import AUTH_KEY, RAW, SERIAL, SSH, TELNET, Session, SessionStore, import_putty, \
    parse_quick_connect, session_from_putty, validate_session


def test_store_round_trip_and_folders(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(str(path))
    core = Session("core-sw1", SSH, "10.0.0.1", folder="Site A/Core", username="admin")
    store.put(core)
    store.put(Session("console", SERIAL, serial_port="COM3", baud_rate=115200, folder="Site A"))
    store.add_folder(" Site B / Empty ")
    loaded = SessionStore(str(path))
    assert [session.name for session in loaded.sessions] == ["core-sw1", "console"]
    assert loaded.get(core.id).username == "admin"
    assert loaded.all_folders() == {"Site A", "Site A/Core", "Site B", "Site B/Empty"}

    loaded.rename_folder("Site A", "HQ")
    assert {session.folder for session in loaded.sessions} == {"HQ/Core", "HQ"}
    loaded.delete_folder("HQ/Core")
    assert [session.name for session in loaded.sessions] == ["console"]
    assert loaded.unique_name("console", "HQ") == "console (2)"
    assert not list(tmp_path.glob("*.tmp"))  # Saved atomically


def test_damaged_or_old_files(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json")
    assert SessionStore(str(path)).sessions == []
    path.write_text('{"sessions": [{"name": "x", "host": "h", "unknown_field": 1, "folder": "/a//b/"}], "folders": [5]}')
    store = SessionStore(str(path))
    assert store.sessions[0].folder == "a/b" and store.sessions[0].protocol == SSH


def test_targets_and_validation():
    assert Session("s", SSH, "10.0.0.1", username="admin").target() == "admin@10.0.0.1"
    assert Session("s", SSH, "10.0.0.1", 2222).target() == "10.0.0.1:2222"
    assert Session("s", SERIAL, serial_port="COM3", baud_rate=9600).target() == "COM3 9600 8N1"
    assert validate_session(Session("", SSH, "h")) == "Give the session a name."
    assert "host" in validate_session(Session("s", TELNET, ""))
    assert "key file" in validate_session(Session("s", SSH, "h", auth=AUTH_KEY))
    assert validate_session(Session("a/b", SSH, "h")) is not None
    assert validate_session(Session("s", SERIAL, serial_port="COM1")) is None


@pytest.mark.parametrize("text, expected", [
    ("admin@10.0.0.1", (SSH, "10.0.0.1", 22, "admin")), ("10.0.0.1:2222", (SSH, "10.0.0.1", 2222, "")),
    ("telnet 10.0.0.5", (TELNET, "10.0.0.5", 23, "")), ("telnet://sw1:2323", (TELNET, "sw1", 2323, "")),
    ("raw 10.0.0.9:9100", (RAW, "10.0.0.9", 9100, "")), ("ssh root@[fe80::1]:22", (SSH, "fe80::1", 22, "root")),
])
def test_quick_connect(text, expected):
    session = parse_quick_connect(text)
    assert (session.protocol, session.host, session.port, session.username) == expected


def test_quick_connect_serial_and_errors():
    session = parse_quick_connect("com3:115200")
    assert (session.protocol, session.serial_port, session.baud_rate) == (SERIAL, "COM3", 115200)
    assert parse_quick_connect("COM7").baud_rate == 9600
    for bad in ("", "two words here", "host:70000"):
        with pytest.raises(ValueError):
            parse_quick_connect(bad)


def test_putty_import(tmp_path):
    putty = [
        ("core%20switch", {"HostName": "admin@10.0.0.1", "PortNumber": 22, "Protocol": "ssh", "PingIntervalSecs": 60}),
        ("printer", {"HostName": "10.0.0.9", "PortNumber": 9100, "Protocol": "raw"}),
        ("console", {"Protocol": "serial", "SerialLine": "COM4", "SerialSpeed": 115200, "SerialDataBits": 8,
                     "SerialStopHalfbits": 2, "SerialParity": 2, "SerialFlowControl": 1}),
        ("keyed", {"HostName": "10.0.0.2", "Protocol": "ssh", "UserName": "ops", "PublicKeyFile": r"C:\k\ops.ppk"}),
        ("broken", {"HostName": "", "Protocol": "ssh"}),
        ("rlogin", {"HostName": "old", "Protocol": "rlogin"}),
    ]
    core = session_from_putty(*putty[0])
    assert (core.name, core.host, core.username, core.keepalive) == ("core switch", "10.0.0.1", "admin", 60)
    console = session_from_putty(*putty[2])
    assert (console.serial_port, console.baud_rate, console.parity, console.stop_bits, console.flow_control) == \
        ("COM4", 115200, "Even", 1, "XON/XOFF")
    assert session_from_putty(*putty[3]).auth == AUTH_KEY
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert import_putty(store, putty) == 4
    assert import_putty(store, putty) == 0  # Already there
    assert {session.folder for session in store.sessions} == {"Imported from PuTTY"}


def test_credentials_round_trip():
    stored = protect("S3cret pässword")
    assert stored != "S3cret pässword" and unprotect(stored) == "S3cret pässword"
    with pytest.raises(CredentialError):
        unprotect("not base64!")
    with pytest.raises(CredentialError):
        unprotect(protect("x")[:-8] + "AAAAAAA=")


def test_move_sessions_and_folders(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = SessionStore(path)
    core = Session("core", SSH, "10.0.0.1", folder="Imported/Site A/Switches")
    edge = Session("edge", SSH, "10.0.0.2", folder="Imported/Site A")
    other_core = Session("core", SSH, "10.9.0.1", folder="Site A/Switches")
    loose = Session("loose", SSH, "10.0.0.3")
    for session in (core, edge, other_core, loose):
        store.sessions.append(session)
    store.save()

    # Moving a folder where one of the same name exists merges them; a clashing session name gets " (2)"
    assert store.move_folder("Imported/Site A", "") == "Site A"
    assert (store.get(core.id).folder, store.get(core.id).name) == ("Site A/Switches", "core (2)")
    assert store.get(other_core.id).name == "core"  # The one already there keeps its name
    assert store.get(edge.id).folder == "Site A"
    assert "Imported" in store.all_folders()  # The folder it came out of stays, now empty

    # Moving up a level and into another folder; a folder can't go inside itself
    assert store.move_folder("Site A/Switches", "Imported") == "Imported/Switches"
    with pytest.raises(ValueError):
        store.move_folder("Imported", "Imported/Switches")
    with pytest.raises(ValueError):
        store.rename_folder("Imported", "Imported/Inner")
    assert store.move_folder("Imported", "") == "Imported"  # Already there: nothing changes

    # Sessions: several at once, to a folder or the top level, names made unique
    assert store.move_sessions({core.id, other_core.id}, "") == 2
    assert sorted(store.get(item).name for item in (core.id, other_core.id)) == ["core", "core (2)"]
    assert "Imported/Switches" in store.all_folders()  # Emptied by the move, still there
    assert store.move_sessions({loose.id}, "New/Deep") == 1 and "New" in store.all_folders()
    assert store.move_sessions({loose.id}, "New/Deep") == 0

    store.delete_many({core.id, edge.id})
    reloaded = SessionStore(path)
    assert {session.name for session in reloaded.sessions} == {"core", "loose"}  # other_core and loose
    assert {"Imported", "Imported/Switches", "Site A", "New/Deep"} <= reloaded.all_folders()
