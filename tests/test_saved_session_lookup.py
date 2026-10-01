"""Opening a session from another page (Network Map, IPAM, Sweep) uses the host's saved session when there is one."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMenu, QWidget  # noqa: E402

from nomad.terminal.commands import CommandStore  # noqa: E402
from nomad.terminal.highlight import HighlightStore  # noqa: E402
from nomad.terminal.sessions import SSH, TELNET, Session, SessionStore, same_host  # noqa: E402
from nomad.ui import session_page  # noqa: E402
from nomad.ui.host_menu import HostActions  # noqa: E402
from nomad.ui.terminal_tab import TerminalTab  # noqa: E402


def test_same_host():
    assert same_host("10.0.0.1", "10.0.0.1")
    assert same_host("fe80::1", "[FE80:0::1]")
    assert same_host("Core-SW1", "core-sw1.corp.example")
    assert same_host("core-sw1.corp.example.", "CORE-SW1.corp.example")
    assert not same_host("core-sw1.corp.example", "core-sw1.lab.example")
    assert not same_host("10.0.0.1", "10.0.0.10")
    assert not same_host("10", "10.0.0.1")
    assert not same_host("", "")


def test_matching_by_any_alias_protocol_and_user(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    admin = Session("Core-SW1", SSH, "core-sw1.corp.example", username="admin", folder="HQ")
    readonly = Session("Core-SW1 RO", SSH, "10.255.0.1", username="readonly", folder="HQ")
    telnet = Session("Core-SW1 Telnet", TELNET, "10.0.0.1", port=23)
    for session in (readonly, admin, telnet):
        store.put(session)
    assert store.matching(["10.0.0.1"], SSH) == []
    assert store.matching(["10.0.0.1"], TELNET) == [telnet]
    assert store.matching(["10.0.0.1", "core-sw1", "10.255.0.1"], SSH) == [admin, readonly]
    assert store.matching(["10.0.0.1", "core-sw1", "10.255.0.1"], SSH, "ADMIN") == [admin]


class Navigator:
    def setCurrentWidget(self, widget):
        pass


class Window(QWidget):
    focus_mode = False
    navigator = Navigator()


@pytest.fixture
def page(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window()
    page = TerminalTab(window, SessionStore(str(tmp_path / "sessions.json")),
                       HighlightStore(str(tmp_path / "highlights.json")), CommandStore(str(tmp_path / "commands.json")))
    page.opened = []
    monkeypatch.setattr(page, "open_session", lambda session, saved=True: page.opened.append(session) or session)
    yield page
    page.shutdown()
    window.deleteLater()
    app.processEvents()


def test_one_saved_session_is_opened_with_its_credentials(page):
    saved = Session("Core-SW1", SSH, "core-sw1", username="admin", saved_password="secret")
    page.store.put(saved)
    page.open_address("10.0.0.1", aliases=["core-sw1.corp.example"])
    assert page.opened == [saved]


def test_without_a_saved_session_it_quick_connects_with_the_name_and_folder(page):
    page.store.put(Session("Other", SSH, "10.0.0.2"))
    session = page.open_address("10.0.0.1", name="core-sw1", folder="HQ / Core")
    assert page.opened == [session]
    assert (session.host, session.name, session.folder, session.username) == ("10.0.0.1", "core-sw1", "HQ/Core", "")
    assert page.store.get(session.id) is None


def test_several_saved_sessions_ask_which(page, monkeypatch):
    admin = Session("Admin", SSH, "10.0.0.1", username="admin")
    readonly = Session("Read Only", SSH, "10.0.0.1", username="readonly")
    page.store.put(admin)
    page.store.put(readonly)
    asked = []
    monkeypatch.setattr(session_page, "choose_saved_session",
                        lambda parent, session, matches: asked.append(matches) or matches[1])
    page.open_address("10.0.0.1")
    assert asked == [[admin, readonly]] and page.opened == [readonly]

    monkeypatch.setattr(session_page, "choose_saved_session", lambda parent, session, matches: None)
    page.open_address("10.0.0.1")
    assert page.opened == [readonly]  # Cancelled

    monkeypatch.setattr(session_page, "choose_saved_session", lambda parent, session, matches: session)
    page.open_address("10.0.0.1")
    assert page.opened[-1].id not in (admin.id, readonly.id)  # New connection


def test_a_new_session_can_be_asked_for_instead(page):
    page.store.put(Session("Core-SW1", SSH, "10.0.0.1", username="admin"))
    session = page.open_address("10.0.0.1", use_saved=False)
    assert session.username == "" and page.store.get(session.id) is None


def test_the_host_menu_names_the_saved_session(page):
    page.store.put(Session("Core-SW1", SSH, "core-sw1", username="admin"))
    window = Window()
    window.terminal_tab = window.scp_tab = page
    menu = QMenu()
    actions = HostActions(window, window).add_to(menu, "10.0.0.1", aliases=["core-sw1"])
    labels = [action.text() for action in actions]
    assert labels[:5] == ["Open SSH Session (Core-SW1)", "Open New SSH Session", "Open SCP Session (Core-SW1)",
                          "Open New SCP Session", "Open Telnet Session"]
    actions[next(action for action in actions if action.text() == "Open New SSH Session")]()
    assert page.opened[-1].username == ""
