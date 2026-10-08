import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QDialog, QWidget

from nomad.terminal.backup import read_backup, restore_backup, write_backup
from nomad.terminal.commands import CommandStore
from nomad.terminal.highlight import HighlightStore
from nomad.terminal.sessions import AUTH_KEY, RDP, TELNET, TERMINAL_PROTOCOLS, Credential, Session, \
    SessionFolderStore, SessionStore, validate_credential
from nomad.ui import prompts
from nomad.ui.credential_dialogs import OWN_LOGIN, CredentialDialog
from nomad.ui.rdp_dialog import RdpDialog
from nomad.ui.session_dialog import SessionDialog


@pytest.fixture
def store(tmp_path):
    return SessionStore(str(tmp_path / "sessions.json"))


@pytest.fixture
def parent():
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    yield widget
    for dialog in widget.findChildren(QDialog):
        dialog.close()
    widget.close()
    widget.deleteLater()
    app.processEvents()


def tacacs(store, default=False, **values):
    values.setdefault("saved_password", store.vault.protect("pw1"))
    credential = Credential("TACACS", username="jsmith", **values)
    store.credentials.put(credential, default=default)
    return credential


def test_sessions_follow_their_credential(store, tmp_path):
    credential = tacacs(store)
    switch = Session("sw1", host="10.0.0.1", credential_id=credential.id)
    store.put(switch)
    assert switch.username == "jsmith" and store.vault.reveal(switch.saved_password) == "pw1"

    changed = Credential("TACACS", username="jsmith2", saved_password=store.vault.protect("pw2"), id=credential.id)
    store.credentials.put(changed)
    assert switch.username == "jsmith2" and store.vault.reveal(switch.saved_password) == "pw2"

    reloaded = SessionStore(str(tmp_path / "sessions.json"))
    assert reloaded.credentials.get(credential.id).username == "jsmith2"
    assert reloaded.get(switch.id).credential_id == credential.id
    assert reloaded.credentials.users(credential.id) == [reloaded.get(switch.id)]


def test_deleting_a_credential_leaves_sessions_their_login(store):
    credential = tacacs(store, default=True)
    switch = Session("sw1", host="10.0.0.1", credential_id=credential.id)
    store.put(switch)
    store.credentials.delete(credential.id)
    assert store.credentials.default is None
    assert switch.credential_id == "" and switch.username == "jsmith"
    assert store.vault.reveal(switch.saved_password) == "pw1"


def test_rdp_uses_only_password_credentials_and_assign_skips_others(store):
    key = Credential("Key", username="ops", auth=AUTH_KEY, key_file="C:/k")
    store.credentials.put(key)
    password = tacacs(store)
    assert [item.name for item in store.credentials.sorted(RDP)] == ["TACACS"]
    rdp = Session("pc", protocol=RDP, host="pc1", port=3389)
    telnet = Session("old", protocol=TELNET, host="10.0.0.9", port=23)
    ssh = Session("sw", host="10.0.0.1")
    store.sessions = [rdp, telnet, ssh]
    assert store.credentials.assign([rdp, telnet, ssh], key.id) == 1
    assert ssh.auth == AUTH_KEY and ssh.key_file == "C:/k" and rdp.credential_id == "" and telnet.credential_id == ""
    assert store.credentials.assign([rdp, ssh], password.id) == 2
    assert rdp.username == "jsmith" and store.vault.reveal(rdp.saved_password) == "pw1"
    assert store.credentials.assign([rdp], "") == 1
    assert rdp.credential_id == "" and rdp.username == "jsmith"  # Kept as its own


def test_folder_view_sees_every_session_and_credential(store):
    credential = tacacs(store)
    rdp_view = SessionFolderStore(store, {RDP}, "rdp_folders")
    rdp_view.put(Session("pc", protocol=RDP, host="pc1", port=3389, credential_id=credential.id))
    terminal_view = SessionFolderStore(store, TERMINAL_PROTOCOLS)
    terminal_view.put(Session("sw", host="10.0.0.1", credential_id=credential.id))
    assert len(terminal_view.credentials.users(credential.id)) == 2
    assert credential in terminal_view.credential_sessions


def test_recent_unsaved_connection_keeps_its_credential(store):
    credential = tacacs(store)
    quick = Session("10.0.0.5", host="10.0.0.5", credential_id=credential.id)
    store.credentials.apply(quick)
    entry = store.remember(quick)
    assert entry.session.saved_password == ""
    reopened = store.recent_session(entry)
    assert store.vault.reveal(reopened.saved_password) == "pw1"


def test_master_password_reencrypts_credentials(store):
    credential = tacacs(store)
    switch = Session("sw1", host="10.0.0.1", credential_id=credential.id)
    store.put(switch)
    store.vault.set_password(store.credential_sessions, "correct horse")
    assert store.vault.needs_master_password(credential.saved_password)
    assert store.vault.reveal(credential.saved_password) == "pw1"
    assert store.vault.reveal(switch.saved_password) == "pw1"


def test_validate_credential(store):
    tacacs(store)
    assert "name" in validate_credential(Credential(""))
    assert "already" in validate_credential(Credential("tacacs", username="x"), store.credentials)
    assert "user name" in validate_credential(Credential("Lab"))
    assert validate_credential(Credential("Lab", username="x")) is None


def test_backup_round_trip_keeps_credentials(tmp_path, store):
    credential = tacacs(store, default=True)
    store.put(Session("sw1", host="10.0.0.1", credential_id=credential.id))
    commands, highlights = CommandStore(str(tmp_path / "c.json")), HighlightStore(str(tmp_path / "h.json"))
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    write_backup(str(tmp_path / "b.nomad"), "backup-pass", store, commands, highlights, settings)

    target = SessionStore(str(tmp_path / "other" / "sessions.json"))
    data = read_backup(str(tmp_path / "b.nomad"), "backup-pass")
    restore_backup(data, target, CommandStore(str(tmp_path / "other" / "c.json")),
                   HighlightStore(str(tmp_path / "other" / "h.json")),
                   QSettings(str(tmp_path / "other" / "s.ini"), QSettings.IniFormat))
    assert target.credentials.default.name == "TACACS"
    assert target.vault.reveal(target.credentials.default.saved_password) == "pw1"
    assert target.sessions[0].credential_id == credential.id


def test_new_session_dialog_starts_with_the_default_credential(parent, store):
    credential = tacacs(store, default=True)
    dialog = SessionDialog(parent, Session(name=""), [], "New Session", store)
    assert dialog.credential_picker.credential_id() == credential.id
    assert dialog.username_input.text() == "jsmith" and not dialog.username_input.isEnabled()
    dialog.name_input.setText("sw1")
    dialog.host_input.setText("10.0.0.1")
    dialog.save()
    assert dialog.session.credential_id == credential.id and dialog.session.username == "jsmith"
    assert store.vault.reveal(dialog.session.saved_password) == "pw1"

    # Typing a login for this session instead brings back what was there before
    own = Session(name="", username="")
    dialog = SessionDialog(parent, own, [], "New Session", store)
    dialog.credential_picker.combo.setCurrentIndex(dialog.credential_picker.combo.findText(OWN_LOGIN))
    assert dialog.username_input.isEnabled() and dialog.username_input.text() == ""
    dialog.name_input.setText("sw2")
    dialog.host_input.setText("10.0.0.2")
    dialog.username_input.setText("admin")
    dialog.save()
    assert dialog.session.credential_id == "" and dialog.session.username == "admin"

    # A session that already has its own user name isn't given the default
    named = Session(name="x", host="h", username="root")
    assert SessionDialog(parent, named, [], "New Session", store).credential_picker.credential_id() == ""


def test_rdp_dialog_uses_credential(parent, store):
    credential = tacacs(store, default=True)
    dialog = RdpDialog(parent, Session(name="pc", protocol=RDP, host="pc1", port=3389), [], store=store)
    assert not dialog.password_input.isEnabled()
    dialog.save()
    assert dialog.session.credential_id == credential.id and dialog.session.username == "jsmith"
    assert store.vault.reveal(dialog.session.saved_password) == "pw1"


def test_first_credential_becomes_default(parent, store):
    dialog = CredentialDialog(parent, store, title="New Credential")
    assert dialog.default_check.isChecked()
    dialog.name_input.setText("TACACS")
    dialog.username_input.setText("jsmith")
    dialog.password_input.setText("pw")
    dialog.save()
    assert store.credentials.default is dialog.credential
    assert store.vault.reveal(dialog.credential.saved_password) == "pw"
    assert not CredentialDialog(parent, store).default_check.isChecked()


def test_password_saved_at_login_goes_to_the_credential(store, monkeypatch):
    credential = tacacs(store, saved_password="")
    switch = Session("sw1", host="10.0.0.1", credential_id=credential.id)
    other = Session("sw2", host="10.0.0.2", credential_id=credential.id)
    store.put(switch)
    store.put(other)
    reports = []
    monkeypatch.setattr(prompts, "protect_secret", lambda parent, store, value: store.vault.protect(value))
    owner = SimpleNamespace(store=store, session=switch.copy(id=switch.id),
                            report=lambda message, warning: reports.append(message))
    prompts.PromptAnswers.save_secret(owner, "password", "new-pw")
    assert store.vault.reveal(credential.saved_password) == "new-pw"
    assert store.vault.reveal(other.saved_password) == "new-pw"
    assert store.vault.reveal(owner.session.saved_password) == "new-pw"
    assert "TACACS" in reports[0]
