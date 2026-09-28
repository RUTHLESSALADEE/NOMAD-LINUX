import socket
import threading

import paramiko
import pytest

from nomad.terminal.credentials import unprotect
from nomad.terminal.hostkeys import MATCH, KnownHosts
from nomad.terminal.legacy_ssh import LegacyRSAKey
from nomad.terminal.sessions import AUTH_KEY, RAW, SERIAL, SSH, TELNET, Session
from nomad.terminal.telnet import DO, IAC, NAWS, SB, SE, WILL
from nomad.terminal.transports import Cancelled, ConnectionFailed, Prompter, RawTransport, SerialTransport, \
    SshTransport, TelnetTransport, make_transport


@pytest.fixture(scope="module")
def host_key():
    return paramiko.RSAKey.generate(2048)


@pytest.fixture(scope="module")
def user_key():
    return paramiko.RSAKey.generate(2048)


class FakeServer(paramiko.ServerInterface):
    def __init__(self, password="secret", authorized_key=None):
        self.password, self.authorized_key = password, authorized_key
        self.pty = None
        self.window_changes = []
        self.shell = threading.Event()

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if username == "admin" and password == self.password else \
            paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        ok = self.authorized_key is not None and key.asbytes() == self.authorized_key.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.pty = (term, width, height)
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        self.window_changes.append((width, height))
        return True


def run_ssh_server(host_key, server, legacy_only=False):
    """Accept one SSH connection on a free port; the shell echoes input in capitals until "exit"."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        transport = paramiko.Transport(connection)
        if legacy_only:  # Like an old switch: SHA-1 key exchange and ssh-rsa only
            transport._preferred_kex = ("diffie-hellman-group1-sha1",)
            transport._kex_info = {**transport._kex_info, **__import__("nomad.terminal.legacy_ssh", fromlist=["x"])
                                   .CompatibleTransport._kex_info}
            transport._preferred_keys = ("ssh-rsa",)
            transport._key_info = {**transport._key_info, "ssh-rsa": LegacyRSAKey}
            transport.add_server_key(LegacyRSAKey(key=host_key.key))
        else:
            transport.add_server_key(host_key)
        try:
            transport.start_server(server=server)
            channel = transport.accept(60)  # Generous: a busy machine can stall for a while
            if channel is None:
                return
            server.shell.wait(5)
            channel.sendall(b"Welcome\r\n")
            while True:
                data = channel.recv(1024)
                if not data or b"exit" in data:
                    channel.send_exit_status(0)
                    break
                channel.sendall(data.upper())
            channel.close()
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()
            listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


class ScriptedPrompter(Prompter):
    def __init__(self, host_key="trust", secrets=(), text=None):
        self.host_key_answer, self.secrets, self.text_answer = host_key, list(secrets), text
        self.asked, self.saved = [], []

    def host_key(self, host, port, key_type, fingerprint, changed, old_fingerprint):
        self.asked.append(("host_key", changed, fingerprint, old_fingerprint))
        return self.host_key_answer

    def secret(self, title, prompt, can_save):
        self.asked.append(("secret", prompt))
        return self.secrets.pop(0) if self.secrets else None

    def text(self, title, prompt):
        self.asked.append(("text", prompt))
        return self.text_answer

    def save_secret(self, kind, value):
        self.saved.append((kind, value))


def read_until(transport, marker, limit=50):
    data = b""
    for _ in range(limit):
        chunk = transport.read()
        if not chunk:
            break
        data += chunk
        if marker in data:
            break
    return data


def test_ssh_password_login_host_keys_and_resize(tmp_path, host_key):
    known = KnownHosts(str(tmp_path / "known_hosts"))
    server = FakeServer()
    port = run_ssh_server(host_key, server)
    prompter = ScriptedPrompter(secrets=[("wrong", False), ("secret", True)])
    session = Session("test", SSH, "127.0.0.1", port, username="admin")
    transport = SshTransport(session, prompter, (100, 30), known_hosts=known)
    transport.connect()
    assert prompter.asked[0][:2] == ("host_key", False) and prompter.asked[0][2].startswith("SHA256:")
    assert [item[1] for item in prompter.asked[1:]] == ["Password for admin@127.0.0.1:",
                                                        "That didn't work. Try the password again:"]
    assert prompter.saved == [("password", "secret")]  # The user ticked "save"
    assert server.pty == (b"xterm-256color", 100, 30) and "SSH to admin@127.0.0.1" in transport.description
    assert b"Welcome" in read_until(transport, b"Welcome")
    transport.send(b"hello")
    assert b"HELLO" in read_until(transport, b"HELLO")
    transport.resize(120, 40)
    transport.send(b"exit")
    while transport.read():
        pass
    assert transport.close_reason == "Logged out."
    assert server.window_changes == [(120, 40)]
    transport.close()
    # "Trust" remembered the key, on disk too
    assert known.check("127.0.0.1", port, host_key)[0] == MATCH
    assert KnownHosts(str(tmp_path / "known_hosts")).check("127.0.0.1", port, host_key)[0] == MATCH

    # A host whose key is known connects without a question
    port = run_ssh_server(host_key, FakeServer())
    known.remember("127.0.0.1", port, host_key)
    prompter = ScriptedPrompter(host_key="cancel", secrets=[("secret", False)])
    again = SshTransport(Session("test", SSH, "127.0.0.1", port, username="admin"), prompter, known_hosts=known)
    again.connect()
    assert not [item for item in prompter.asked if item[0] == "host_key"]
    again.close()


def test_ssh_changed_host_key_is_flagged_and_can_be_refused(tmp_path, host_key):
    known = KnownHosts(str(tmp_path / "known_hosts"))
    other = paramiko.RSAKey.generate(1024)
    port = run_ssh_server(other, FakeServer())
    known.remember("127.0.0.1", port, host_key)  # We knew a different key for this address
    prompter = ScriptedPrompter(host_key="cancel")
    transport = SshTransport(Session("t", SSH, "127.0.0.1", port, username="admin"), prompter, known_hosts=known)
    with pytest.raises(Cancelled):
        transport.connect()
    kind, changed, new, old = prompter.asked[0]
    assert changed and new != old and old.startswith("SHA256:")


def test_ssh_legacy_only_device(tmp_path, host_key):
    port = run_ssh_server(host_key, FakeServer(), legacy_only=True)
    transport = SshTransport(Session("old", SSH, "127.0.0.1", port, username="admin"),
                             ScriptedPrompter(secrets=[("secret", False)]),
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    transport.connect()
    assert "diffie-hellman-group1-sha1" in transport.description and "ssh-rsa" in transport.description
    assert "older SHA-1" in transport.notice
    transport.close()


def test_ssh_key_file_and_saved_password(tmp_path, host_key, user_key):
    key_file = tmp_path / "id_rsa"
    user_key.write_private_key_file(str(key_file), password="phrase")
    port = run_ssh_server(host_key, FakeServer(authorized_key=user_key))
    prompter = ScriptedPrompter(secrets=[("phrase", True)])
    session = Session("k", SSH, "127.0.0.1", port, username="admin", auth=AUTH_KEY, key_file=str(key_file))
    transport = SshTransport(session, prompter, known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    transport.connect()
    assert prompter.saved == [("passphrase", "phrase")]
    transport.close()

    from nomad.terminal.credentials import protect
    port = run_ssh_server(host_key, FakeServer())
    prompter = ScriptedPrompter()
    session = Session("p", SSH, "127.0.0.1", port, username="admin", saved_password=protect("secret"))
    transport = SshTransport(session, prompter, known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    transport.connect()
    assert [item for item in prompter.asked if item[0] == "secret"] == []  # Used the saved password
    assert unprotect(session.saved_password) == "secret"
    transport.close()


def test_ssh_ppk_key_gets_a_helpful_message(tmp_path, host_key):
    ppk = tmp_path / "key.ppk"
    ppk.write_text("PuTTY-User-Key-File-3: ssh-rsa\n")
    port = run_ssh_server(host_key, FakeServer())
    transport = SshTransport(Session("k", SSH, "127.0.0.1", port, username="admin", auth=AUTH_KEY,
                                     key_file=str(ppk)), ScriptedPrompter(),
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    with pytest.raises(ConnectionFailed, match="PuTTYgen"):
        transport.connect()


def listening_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_ssh_to_something_that_isnt_ssh(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=lambda: listener.accept()[0].sendall(b"User Access Verification\r\n") or None,
                     daemon=True).start()
    transport = SshTransport(Session("x", SSH, "127.0.0.1", port, username="a"), ScriptedPrompter(),
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    with pytest.raises(ConnectionFailed, match="isn't answering as an SSH server|closed the connection"):
        transport.connect()
    listener.close()
    with pytest.raises(ConnectionFailed, match="refused"):
        SshTransport(Session("x", SSH, "127.0.0.1", listening_port()), ScriptedPrompter()).connect()


def test_raw_tcp(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        with connection:
            connection.sendall(b"ready\r\n")
            connection.sendall(connection.recv(100)[::-1])

    threading.Thread(target=serve, daemon=True).start()
    session = Session("r", RAW, "127.0.0.1", port, line_ending="CR+LF", local_echo=True)
    transport = make_transport(session)
    assert isinstance(transport, RawTransport) and transport.enter == "\r\n" and transport.local_echo
    transport.connect()
    assert read_until(transport, b"ready") == b"ready\r\n"
    transport.send(b"abc")
    assert read_until(transport, b"cba") == b"cba"
    assert transport.read() == b"" and "closed" in transport.close_reason
    transport.close()
    listener.close()


def test_telnet_negotiates_and_hides_it(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received = []

    def serve():
        connection, _ = listener.accept()
        with connection:
            connection.sendall(bytes([IAC, DO, NAWS, IAC, WILL, 1]) + b"Username: ")
            for _ in range(3):
                data = connection.recv(100)
                received.append(data)
                if b"admin" in data:
                    break
            connection.sendall(b"\xff\xffOK\r\n")

    threading.Thread(target=serve, daemon=True).start()
    transport = TelnetTransport(Session("t", TELNET, "127.0.0.1", port), size=(90, 30))
    transport.connect()
    assert read_until(transport, b"Username") == b"Username: "  # The negotiation isn't shown
    transport.resize(100, 50)
    transport.send(b"admin\r\n")
    assert b"OK" in read_until(transport, b"OK")
    everything = b"".join(received)
    assert bytes([IAC, WILL, NAWS, IAC, SB, NAWS, 0, 90, 0, 30, IAC, SE]) in everything
    assert bytes([IAC, SB, NAWS, 0, 100, 0, 50, IAC, SE]) in everything  # The resize was passed on
    transport.close()
    listener.close()


def test_serial_loopback():
    session = Session("s", SERIAL, serial_port="loop://", baud_rate=115200, line_ending="CR")
    transport = SerialTransport(session)
    transport.connect()
    transport.send(b"show version\r")
    assert read_until(transport, b"\r") == b"show version\r"
    transport.send_break(0.01)
    transport.close()
    assert transport.read() == b""


def test_serial_missing_port():
    with pytest.raises(ConnectionFailed, match="doesn't exist|in use"):
        SerialTransport(Session("s", SERIAL, serial_port="COM250")).connect()


@pytest.fixture
def cisco_like_service_requests(monkeypatch):
    """Make the test server behave like Cisco IOS XE: a second "ssh-userauth" service request on the same connection
    is a protocol error ("expected packet type 50, got 5") and it disconnects."""
    from paramiko.auth_handler import AuthHandler
    original = AuthHandler._parse_service_request

    def strict(self, message):
        self.transport.service_requests = getattr(self.transport, "service_requests", 0) + 1
        if self.transport.service_requests > 1:
            self._disconnect_service_not_available()
            return None
        return original(self, message)

    monkeypatch.setattr(AuthHandler, "_parse_service_request", strict)


def test_ssh_to_cisco_like_device_retries_on_one_connection(tmp_path, host_key, cisco_like_service_requests):
    port = run_ssh_server(host_key, FakeServer())
    prompter = ScriptedPrompter(secrets=[("wrong", False), ("secret", False)])
    transport = SshTransport(Session("ios-xe", SSH, "127.0.0.1", port, username="admin"), prompter,
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    transport.connect()  # "none", then a wrong password, then the right one, without disconnecting
    assert "SSH to admin@127.0.0.1" in transport.description
    transport.close()


class KeyboardInteractiveServer(FakeServer):
    """Like a device logging in through RADIUS or TACACS: only keyboard-interactive, with a Password: prompt."""

    def get_allowed_auths(self, username):
        return "keyboard-interactive"

    def check_auth_interactive(self, username, submethods):
        query = paramiko.server.InteractiveQuery("", "RADIUS")
        query.add_prompt("Password: ", False)
        return query

    def check_auth_interactive_response(self, responses):
        return paramiko.AUTH_SUCCESSFUL if list(responses) == [self.password] else paramiko.AUTH_FAILED


def test_ssh_keyboard_interactive_only(tmp_path, host_key, cisco_like_service_requests):
    port = run_ssh_server(host_key, KeyboardInteractiveServer())
    prompter = ScriptedPrompter(secrets=[("wrong", False), ("secret", False)])
    transport = SshTransport(Session("radius", SSH, "127.0.0.1", port, username="admin"), prompter,
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    transport.connect()
    assert transport.transport.is_authenticated()
    transport.close()


def test_ssh_disconnect_during_login_is_not_called_a_wrong_password(tmp_path, host_key, monkeypatch):
    from paramiko.auth_handler import AuthHandler
    original = AuthHandler._parse_userauth_request

    methods = []

    def hang_up(self, message):
        start = message.packet.tell()
        message.get_text(), message.get_text()  # User name and service
        method = message.get_text()
        message.packet.seek(start)  # Leave the message as it was for the real handler
        methods.append(method)
        if method != "none":
            self._disconnect_no_more_auth()  # Like a device that has had too many login attempts
            return None
        return original(self, message)

    monkeypatch.setattr(AuthHandler, "_parse_userauth_request", hang_up)
    port = run_ssh_server(host_key, FakeServer())
    transport = SshTransport(Session("x", SSH, "127.0.0.1", port, username="admin"),
                             ScriptedPrompter(secrets=[("secret", False)]),
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    with pytest.raises(ConnectionFailed, match="closed the connection during login"):
        transport.connect()
    assert methods == ["none", "password"]  # It hung up at the password, which was never judged wrong


def test_ssh_device_hanging_up_before_login_does_not_hang(tmp_path, host_key, monkeypatch):
    from paramiko.auth_handler import AuthHandler
    import time

    def hang_up(self, message):
        self.transport.close()  # Gone before agreeing to a login

    monkeypatch.setattr(AuthHandler, "_parse_service_request", hang_up)
    port = run_ssh_server(host_key, FakeServer())
    transport = SshTransport(Session("x", SSH, "127.0.0.1", port, username="admin"),
                             ScriptedPrompter(secrets=[("secret", False)]),
                             known_hosts=KnownHosts(str(tmp_path / "known_hosts")))
    started = time.monotonic()
    with pytest.raises(ConnectionFailed, match="closed the connection"):
        transport.connect()
    assert time.monotonic() - started < 10  # paramiko's own version would have waited forever
