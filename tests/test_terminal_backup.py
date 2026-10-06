import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QByteArray, QSettings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nomad.terminal.backup import MAGIC, BackupError, read_backup, restore_backup, write_backup
from nomad.terminal.commands import CommandButton, CommandStore
from nomad.terminal.highlight import HighlightRule, HighlightStore
from nomad.terminal.securecrt import IMPORT_FOLDER, read_securecrt_export, write_securecrt_export
from nomad.terminal.sessions import AUTH_KEY, SERIAL, Session, SessionStore
from nomad.terminal.vault import derive_key


def stores(path):
    return (SessionStore(str(path / 'sessions.json')), CommandStore(str(path / 'commands.json')),
            HighlightStore(str(path / 'highlights.json')), QSettings(str(path / 'settings.ini'), QSettings.IniFormat))


def test_portable_backup_round_trip(tmp_path):
    store, commands, highlights, settings = stores(tmp_path / 'source')
    # Two different local encryption contexts: exported secrets must be reprotected, never copied as opaque blobs.
    store.vault.reveal = lambda value: value.removeprefix('source:')
    store.sessions = [Session('edge', host='10.0.0.1', folder='Site', saved_password='source:secret',
                              saved_passphrase='source:key-secret', notes='Ünicode', key_file='C:/keys/key'),
                      Session('console', protocol=SERIAL, serial_port='COM5', baud_rate=115200)]
    store.folders = {'Empty/Nested'}
    store.remember(store.sessions[0])
    store.vault_settings['lock_after'] = 900
    commands.buttons = [CommandButton('show', 'show version\nshow clock')]
    highlights.rules = [HighlightRule('down', 'Amber')]
    highlights.enabled = False
    settings.setValue('terminal/splitter', QByteArray(b'\x00\x01\xff'))
    settings.setValue('terminal/manager', False)
    settings.setValue('scp/quick', 'admin@router')
    settings.setValue('view/text_scale', 1.25)
    settings.setValue('ipam/server', 'not-exported')
    path = tmp_path / 'backup.nomad'
    write_backup(path, 'backup-password', store, commands, highlights, settings)
    assert b'secret' not in path.read_bytes() and b'10.0.0.1' not in path.read_bytes()
    data = read_backup(path, 'backup-password')
    destination = stores(tmp_path / 'destination')
    destination[0].sessions = [Session('old', host='old')]
    destination[0].vault.protect = lambda secret: 'destination:' + secret
    destination[3].setValue('terminal/old', 'removed')
    destination[3].setValue('ipam/server', 'keep-me')
    restore_backup(data, *destination)
    restored = SessionStore(destination[0].path)
    assert [s.id for s in restored.sessions] == [s.id for s in store.sessions]
    assert restored.sessions[0].saved_password == 'destination:secret'
    assert restored.sessions[0].saved_passphrase == 'destination:key-secret'
    assert restored.sessions[1].baud_rate == 115200
    assert restored.folders == store.all_folders() and restored.vault.lock_after == 900
    assert restored.recent[0].saved_id == restored.sessions[0].id
    assert restored.recent[0].session.saved_password == ''
    assert destination[1].buttons == commands.buttons
    assert destination[2].rules == highlights.rules and not destination[2].enabled
    assert destination[3].value('terminal/splitter') == QByteArray(b'\x00\x01\xff')
    assert not destination[3].contains('terminal/old')
    assert destination[3].value('ipam/server') == 'keep-me'
    assert 'ipam/server' not in data['settings']


def test_wrong_password_and_damaged_backup(tmp_path):
    source = stores(tmp_path)
    path = tmp_path / 'backup.nomad'
    write_backup(path, 'backup-password', *source)
    with pytest.raises(BackupError, match='incorrect'):
        read_backup(path, 'wrong')
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(BackupError, match='damaged'):
        read_backup(path, 'backup-password')
    path.write_bytes(b'not a backup')
    with pytest.raises(BackupError, match='supported'):
        read_backup(path, 'backup-password')


@pytest.mark.parametrize('change', [
    lambda data: data['sessions'][0].update(port='bad'),
    lambda data: data['sessions'][0].update(host=[]),
    lambda data: data['settings'].update({'ipam/server': 'unexpected'}),
    lambda data: data.update(commands={}),
    lambda data: data['settings'].update({'view/text_scale': 'invalid'}),
])
def test_malformed_payload_rejected(tmp_path, change):
    source = stores(tmp_path)
    source[0].sessions = [Session('edge', host='router')]
    path = tmp_path / 'backup.nomad'
    write_backup(path, 'backup-password', *source)
    raw = path.read_bytes()
    offset = len(MAGIC)
    salt, nonce = raw[offset:offset + 16], raw[offset + 16:offset + 28]
    key = derive_key('backup-password', salt)
    data = json.loads(AESGCM(key).decrypt(nonce, raw[offset + 28:], MAGIC))
    change(data)
    path.write_bytes(MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, json.dumps(data).encode(), MAGIC))
    with pytest.raises(BackupError):
        read_backup(path, 'backup-password')


def test_credential_failure_does_not_replace_existing_configuration(tmp_path):
    source = stores(tmp_path)
    source[0].sessions = [Session('original', host='router')]
    source[0].save()
    before = (tmp_path / 'sessions.json').read_bytes()
    def fail(secret):
        raise ValueError('unavailable')
    source[0].vault.protect = fail
    data = {'sessions': [Session('new', host='new', saved_password='secret')]}
    with pytest.raises(ValueError):
        restore_backup(data, *source)
    assert source[0].sessions[0].name == 'original'
    assert (tmp_path / 'sessions.json').read_bytes() == before


def test_securecrt_export_preserves_hierarchy_and_escapes(tmp_path):
    sessions = [Session('Site', host='root'), Session('edge & <one>', host='router', folder='Site/Core', port=2222,
                username='admin', auth=AUTH_KEY, key_file='C:/key', notes='line 1\nline 2', saved_password='secret'),
                Session('edge & <one>', host='other', folder='Site/Core'), Session('Default', host='template-name'),
                Session('console', protocol=SERIAL)]
    path = tmp_path / 'securecrt.xml'
    assert write_securecrt_export(path, sessions) == 4
    assert 'secret' not in path.read_text() and 'Password V2' not in path.read_text()
    imported = read_securecrt_export(path)
    assert len(imported.sessions) == 4
    assert {x.session.name for x in imported.sessions} == {'Site (2)', 'edge & <one>', 'edge & <one> (2)', 'Default (2)'}
    edge = next(x.session for x in imported.sessions if x.session.host == 'router')
    assert edge.folder == IMPORT_FOLDER + '/Site/Core'
    assert (edge.port, edge.username, edge.auth, edge.key_file, edge.notes) == (2222, 'admin', AUTH_KEY, 'C:/key', 'line 1\nline 2')


def test_securecrt_password_export_round_trip(tmp_path):
    from nomad.terminal.securecrt import WrongPassphrase
    sessions = [Session('edge', host='router', saved_password='local:pässword<&>'),
                Session('core', host='core', saved_password='local:pässword<&>'),
                Session('unsaved', host='other'), Session('console', protocol=SERIAL, saved_password='local:unused')]
    path = tmp_path / 'securecrt.xml'
    assert write_securecrt_export(path, sessions, lambda stored: stored.removeprefix('local:'), 'securecrt-phrase') == 3
    text = path.read_text(encoding='utf-8')
    assert 'pässword' not in text and 'securecrt-phrase' not in text and 'local:' not in text
    assert 'Passphrase V2' not in text
    export = read_securecrt_export(path)
    encrypted = [item.encrypted_password for item in export.sessions if item.encrypted_password]
    assert len(encrypted) == 2 and all(value.startswith('03:') for value in encrypted)
    assert encrypted[0] != encrypted[1]  # A fresh salt per saved password.
    with pytest.raises(WrongPassphrase):
        export.decrypt('wrong-phrase')
    assert export.decrypt('securecrt-phrase') == 2
    assert [item.password for item in export.sessions] == ['pässword<&>', 'pässword<&>', '']


def test_securecrt_unreadable_password_preserves_previous_export(tmp_path):
    from nomad.terminal.credentials import CredentialError
    path = tmp_path / 'securecrt.xml'
    path.write_text('previous export')
    def reveal(stored):
        if stored == 'unreadable':
            raise CredentialError('Cannot decrypt saved password')
        return 'secret'
    with pytest.raises(CredentialError):
        write_securecrt_export(path, [Session('first', host='router', saved_password='readable'),
                                     Session('second', host='other', saved_password='unreadable')], reveal, 'phrase')
    assert path.read_text() == 'previous export'


@pytest.mark.parametrize('choice', ['export', 'cancel', 'locked', 'unreadable', 'no-passwords'])
def test_securecrt_export_dialog(tmp_path, monkeypatch, choice):
    from PyQt5.QtWidgets import QMessageBox
    from nomad.terminal.credentials import CredentialError
    from nomad.ui import terminal_transfer
    store = SessionStore(str(tmp_path / 'sessions.json'))
    store.sessions = [Session('edge', host='router', saved_password='' if choice == 'no-passwords' else 'stored')]
    window = SimpleNamespace(terminal_tab=SimpleNamespace(store=store))
    path = tmp_path / 'securecrt.xml'
    prompts, unlocks, errors, messages = [], [], [], []
    def passphrase(*args):
        prompts.append(True)
        return None if choice == 'cancel' else 'securecrt-phrase'
    def unlock(*args):
        unlocks.append(True)
        return choice != 'locked'
    def reveal(stored):
        if choice == 'unreadable':
            raise CredentialError('Cannot decrypt saved password')
        return 'saved-secret'
    store.vault.reveal = reveal
    monkeypatch.setattr(terminal_transfer.QFileDialog, 'getSaveFileName', lambda *args: (str(path), ''))
    monkeypatch.setattr(terminal_transfer, 'securecrt_passphrase', passphrase)
    monkeypatch.setattr(terminal_transfer, 'ensure_unlocked', unlock)
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: errors.append(args))
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: messages.append(args))
    terminal_transfer.export_securecrt(window)
    assert bool(prompts) == (choice != 'no-passwords')
    assert bool(unlocks) == (choice not in ('no-passwords', 'cancel'))
    assert bool(errors) == (choice == 'unreadable')
    assert path.exists() == (choice in ('export', 'no-passwords'))
    assert bool(messages) == path.exists()
    if choice == 'export':
        export = read_securecrt_export(path)
        assert export.decrypt('securecrt-phrase') == 1
        assert export.sessions[0].password == 'saved-secret'


def test_failed_restore_rolls_back_files_and_memory(tmp_path, monkeypatch):
    source = stores(tmp_path)
    source[0].sessions = [Session('original', host='router')]
    source[0].save()
    source[1].save()
    source[2].save()
    source[3].setValue('terminal/quick', 'original')
    path = tmp_path / 'backup.nomad'
    write_backup(path, 'backup-password', *source)
    data = read_backup(path, 'backup-password')
    data['sessions'][0].name = 'replacement'
    before = {owner.path: Path(owner.path).read_bytes() for owner in source[:3]}
    def fail():
        raise OSError('disk full')
    monkeypatch.setattr(source[1], 'save', fail)
    with pytest.raises(OSError, match='disk full'):
        restore_backup(data, *source)
    assert source[0].sessions[0].name == 'original'
    for owner in source[:3]:
        with open(owner.path, 'rb') as file:
            assert file.read() == before[owner.path]
    assert source[3].value('terminal/quick') == 'original'


@pytest.mark.parametrize('choice', ['cancel', 'wrong-password', 'restore'])
def test_import_dialog_only_restores_after_validation_and_confirmation(tmp_path, monkeypatch, choice):
    from PyQt5.QtWidgets import QMessageBox
    from nomad.ui import terminal_transfer
    source = stores(tmp_path / 'source')
    source[0].sessions = [Session('imported', host='router')]
    path = tmp_path / 'backup.nomad'
    write_backup(path, 'backup-password', *source)
    destination = stores(tmp_path / 'destination')
    destination[0].sessions = [Session('original', host='old')]
    refreshed = []
    tab = SimpleNamespace(store=destination[0], commands=destination[1], highlights=destination[2],
                          restore_settings=lambda settings: refreshed.append('terminal'))
    window = SimpleNamespace(terminal_tab=tab, settings=destination[3],
                             scp_tab=SimpleNamespace(restore_settings=lambda settings: refreshed.append('scp')),
                             set_text_scale=lambda scale: refreshed.append(scale))
    monkeypatch.setattr(terminal_transfer.QFileDialog, 'getOpenFileName', lambda *a: (str(path), ''))
    monkeypatch.setattr(terminal_transfer, 'backup_password', lambda *a: 'wrong' if choice == 'wrong-password' else 'backup-password')
    questions, errors = [], []
    def question(*args):
        questions.append(args)
        return QMessageBox.No if choice == 'cancel' else QMessageBox.Yes
    monkeypatch.setattr(QMessageBox, 'question', question)
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: errors.append(args))
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: None)
    monkeypatch.setattr(terminal_transfer, 'ensure_unlocked', lambda *args: True)
    terminal_transfer.import_terminal(window)
    assert destination[0].sessions[0].name == ('imported' if choice == 'restore' else 'original')
    assert bool(refreshed) == (choice == 'restore')
    assert bool(errors) == (choice == 'wrong-password')
    assert bool(questions) == (choice != 'wrong-password')
