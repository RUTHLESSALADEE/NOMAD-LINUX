import pytest

from nomad.terminal import credentials
from nomad.terminal.credentials import CredentialError
from nomad.terminal.sessions import SSH, Session, SessionStore
from nomad.terminal.transports import Prompter, SshTransport
from nomad.terminal.vault import V2_PREFIX, Vault, VaultError, VaultLocked


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.put(Session("a", SSH, "10.0.0.1", saved_password=credentials.protect("alpha")))
    store.put(Session("b", SSH, "10.0.0.2", saved_passphrase=credentials.protect("bravo")))
    store.put(Session("c", SSH, "10.0.0.3"))
    return store


def secrets(store):
    return [(session.saved_password, session.saved_passphrase) for session in store.sessions]


def test_without_a_master_password_it_is_plain_dpapi(store):
    vault = store.vault
    assert not vault.enabled
    stored = vault.protect("secret")
    assert not stored.startswith(V2_PREFIX) and credentials.unprotect(stored) == "secret"
    assert vault.reveal(store.sessions[0].saved_password) == "alpha"


def test_setting_a_master_password_reencrypts_everything(store, tmp_path):
    vault = store.vault
    assert vault.set_password(store.sessions, "correct horse") == 0
    store.save()
    for password, passphrase in secrets(store):
        for value in (password, passphrase):
            assert not value or value.startswith(V2_PREFIX)
    # Undoing the Windows (DPAPI) layer alone gives only the master-password-encrypted form, not the password
    stored = store.sessions[0].saved_password
    inner = credentials.unprotect(stored[len(V2_PREFIX):])
    assert inner != "alpha" and "alpha" not in inner
    assert vault.reveal(store.sessions[0].saved_password) == "alpha"  # Still unlocked after setting it

    reopened = SessionStore(str(tmp_path / "sessions.json"))
    assert reopened.vault.enabled and not reopened.vault.unlocked
    assert "salt" in reopened.vault.settings and "correct horse" not in (tmp_path / "sessions.json").read_text()
    with pytest.raises(VaultLocked):
        reopened.vault.reveal(reopened.sessions[1].saved_passphrase)
    with pytest.raises(VaultLocked):
        reopened.vault.protect("new")
    assert not reopened.vault.unlock("wrong password")
    assert reopened.vault.unlock("correct horse")
    assert reopened.vault.reveal(reopened.sessions[1].saved_passphrase) == "bravo"
    assert reopened.vault.reveal(reopened.vault.protect("new")) == "new"


def test_change_and_remove(store):
    vault = store.vault
    vault.set_password(store.sessions, "first password")
    with pytest.raises(VaultError, match="current master password"):
        vault.set_password(store.sessions, "second password", "not it")
    vault.set_password(store.sessions, "second password", "first password")
    vault.lock()
    assert not vault.unlock("first password") and vault.unlock("second password")
    assert vault.reveal(store.sessions[0].saved_password) == "alpha"
    with pytest.raises(VaultError):
        vault.remove_password(store.sessions, "first password")
    vault.remove_password(store.sessions, "second password")
    assert not vault.enabled
    assert credentials.unprotect(store.sessions[0].saved_password) == "alpha"


def test_weak_passwords_are_refused(store):
    for weak in ("short", " padded password "):
        with pytest.raises(VaultError):
            store.vault.set_password(store.sessions, weak)


def test_forgetting_everything(store):
    store.vault.set_password(store.sessions, "correct horse")
    assert store.vault.forget_everything(store.sessions) == 2
    assert not store.vault.enabled and secrets(store) == [("", ""), ("", ""), ("", "")]


def test_idle_lock():
    clock = Clock()
    vault = Vault(clock=clock)
    vault.set_password([], "correct horse")
    vault.set_lock_after(60)
    stored = vault.protect("x")
    clock.now += 50
    assert vault.reveal(stored) == "x"  # Using it keeps it unlocked
    clock.now += 50
    assert vault.unlocked
    clock.now += 61
    assert not vault.unlocked
    with pytest.raises(VaultLocked):
        vault.reveal(stored)


def test_secret_from_another_master_password_is_reported():
    first, second = Vault(), Vault()
    first.set_password([], "correct horse")
    second.set_password([], "battery staple")
    with pytest.raises(CredentialError, match="different master password"):
        second.reveal(first.protect("x"))


class UnlockingPrompter(Prompter):
    def __init__(self, vault, password):
        self.vault, self.password, self.asked = vault, password, 0

    def unlock_vault(self):
        self.asked += 1
        return self.password is not None and self.vault.unlock(self.password)


def test_transport_asks_to_unlock_and_falls_back(store):
    store.vault.set_password(store.sessions, "correct horse")
    store.vault.lock()
    session = store.sessions[0]
    prompter = UnlockingPrompter(store.vault, "correct horse")
    transport = SshTransport(session, prompter)
    transport.vault = store.vault
    assert transport.reveal(session.saved_password) == "alpha" and prompter.asked == 1
    store.vault.lock()
    declined = UnlockingPrompter(store.vault, None)
    transport = SshTransport(session, declined)
    transport.vault = store.vault
    assert transport.reveal(session.saved_password) is None and declined.asked == 1  # Then it asks for the password
    assert transport.reveal("") is None
