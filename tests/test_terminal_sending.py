"""Sending on the Terminal page: Send to All and Type in All, command buttons, the line delay, reconnecting
automatically, anti-idle and keyword highlighting."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from nomad.terminal.commands import CommandButton, CommandStore  # noqa: E402
from nomad.terminal.highlight import HighlightStore  # noqa: E402
from nomad.terminal.sessions import TELNET, Session, SessionStore  # noqa: E402
from nomad.ui.terminal_tab import TerminalTab  # noqa: E402
from nomad.ui.terminal_view import CONNECTED, DISCONNECTED  # noqa: E402


class FakeTransport:
    local_echo = False

    def __init__(self, enter):
        self.enter = enter
        self.sent = b""

    def send(self, data):
        self.sent += data

    def close(self):
        pass

    def resize(self, columns, rows):  # The terminal shrinks when the Send to All bar opens
        pass


class Navigator:
    def setCurrentWidget(self, widget):
        pass


class Window(QWidget):
    focus_mode = False
    navigator = Navigator()


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window()
    page = TerminalTab(window, SessionStore(str(tmp_path / "sessions.json")),
                       HighlightStore(str(tmp_path / "highlights.json")), CommandStore(str(tmp_path / "commands.json")))
    window.resize(1000, 700)
    page.setParent(window)
    page.resize(1000, 700)
    window.show()
    yield page
    page.shutdown()
    window.deleteLater()
    app.processEvents()


def connect(page, name, enter="\r"):
    """A session tab that's 'connected' to a fake transport."""
    view = page.make_view(Session(name=name, protocol=TELNET, host=name))
    page.show_page(1)
    page.tabs.add_view(view)
    view.transport = FakeTransport(enter)
    view.view.enter = enter
    view.state = CONNECTED
    return view


def test_a_command_goes_to_every_connected_session_with_its_own_enter(page):
    one, two = connect(page, "one"), connect(page, "two", enter="\r\n")
    connect(page, "three").state = "Disconnected"
    assert page.send_to_all("show clock", "all") == 2
    assert one.transport.sent == b"show clock\r"
    assert two.transport.sent == b"show clock\r\n"


def test_sessions_left_out_get_nothing(page):
    one, two = connect(page, "one"), connect(page, "two")
    two.toggle_left_out()
    assert page.send_to_all("wr mem", "all") == 1
    assert two.transport.sent == b""
    assert "⊘" in page.tabs.panes[0].bar.tabText(1)


def test_on_screen_means_the_session_showing_in_each_pane(page):
    one, two, three = connect(page, "one"), connect(page, "two"), connect(page, "three")
    page.tabs.set_layout("side2")  # [one, two] and [three]
    page.tabs.show_view(one)
    assert {view.session.name for view in page.broadcast_targets("screen")} == {"one", "three"}


def test_type_in_all_mirrors_typing_only_while_on(page):
    one, two = connect(page, "one"), connect(page, "two", enter="\r\n")
    one.type_text("x")
    assert two.transport.sent == b""
    page.set_broadcast(mirror=True)
    one.type_text("y")
    one.type_text("\r")
    assert one.transport.sent == b"xy\r"
    assert two.transport.sent == b"y\r\n"  # Enter translated to the other session's own


def test_closing_the_bar_stops_type_in_all(page):
    connect(page, "one")
    page.tabs.show_send_bar(True)
    page.set_broadcast(mirror=True)
    assert page.tabs.send_bar.mirror_box.isChecked()
    page.tabs.show_send_bar(False)
    assert not page.mirror_typing


def test_the_bar_sends_and_remembers_the_command(page):
    one = connect(page, "one")
    page.tabs.show_send_bar(True)
    bar = page.tabs.send_bar
    bar.command_input.setText("terminal length 0")
    bar.send()
    assert one.transport.sent == b"terminal length 0\r"
    assert bar.command_input.history == ["terminal length 0"]
    assert "Sent to 1 session" in bar.status_label.text()


# ----------------------------------------------------------------- Control keys in the Send to All box

def test_ctrl_c_in_the_command_box_interrupts_every_session(page):
    one, two = connect(page, "one"), connect(page, "two")
    page.tabs.show_send_bar(True)
    QTest.keyClick(page.tabs.send_bar.command_input, Qt.Key_C, Qt.ControlModifier)
    assert one.transport.sent == two.transport.sent == b"\x03"
    assert "Sent Ctrl+C to 2 sessions" in page.tabs.send_bar.status_label.text()


def test_ctrl_c_still_copies_selected_text_in_the_box(page):
    one = connect(page, "one")
    page.tabs.show_send_bar(True)
    box = page.tabs.send_bar.command_input
    box.setText("show run")
    box.selectAll()
    QTest.keyClick(box, Qt.Key_C, Qt.ControlModifier)
    assert one.transport.sent == b""
    assert QApplication.clipboard().text() == "show run"


def test_ctrl_z_goes_to_the_sessions_only_when_the_box_is_empty(page):
    one = connect(page, "one")
    page.tabs.show_send_bar(True)
    box = page.tabs.send_bar.command_input
    box.setText("conf t")
    QTest.keyClick(box, Qt.Key_Z, Qt.ControlModifier)  # Undo, in the box
    assert one.transport.sent == b""
    box.clear()
    QTest.keyClick(box, Qt.Key_Z, Qt.ControlModifier)
    assert one.transport.sent == b"\x1a"


# ----------------------------------------------------------------- Command buttons

def test_a_command_button_sends_to_the_current_session(page):
    one, two = connect(page, "one"), connect(page, "two")
    button = CommandButton("Brief", "terminal length 0\nshow ip int brief")
    page.commands.put(button)
    page.tabs.show_command_bar(True)
    page.tabs.show_view(one)
    page.tabs.command_bar.send(button.id)
    assert one.transport.sent == b"terminal length 0\rshow ip int brief\r"
    assert two.transport.sent == b""


def test_a_command_button_goes_to_all_while_typing_in_all(page):
    one, two = connect(page, "one"), connect(page, "two")
    button = CommandButton("Ping", "ping ", press_enter=False)
    page.commands.put(button)
    page.set_broadcast(mirror=True)
    page.tabs.command_bar.send(button.id)
    assert one.transport.sent == two.transport.sent == b"ping "


def test_ctrl_number_presses_that_button_even_with_the_bar_hidden(page):
    one, two = connect(page, "one"), connect(page, "two")
    page.commands.put(CommandButton("Clock", "show clock"))
    page.commands.put(CommandButton("Brief", "show ip int brief"))
    assert not page.tabs.command_bar.isVisible()
    QTest.keyClick(two.view, Qt.Key_2, Qt.ControlModifier)  # In the session the key was pressed in
    assert two.transport.sent == b"show ip int brief\r" and one.transport.sent == b""
    page.tabs.show_command_bar(True)
    QTest.keyClick(two.view, Qt.Key_1, Qt.ControlModifier)
    assert two.transport.sent.endswith(b"show clock\r")


def test_ctrl_number_goes_to_the_device_without_a_button(page):
    one = connect(page, "one")
    page.commands.put(CommandButton("Clock", "show clock"))
    QTest.keyClick(one.view, Qt.Key_6, Qt.ControlModifier)  # No sixth button: Ctrl+^ as before
    assert one.transport.sent == b"\x1e"


def test_reordering_buttons_moves_their_hotkeys_too(page):
    one = connect(page, "one")
    for name in ("Clock", "Brief", "Run"):
        page.commands.put(CommandButton(name, f"show {name.lower()}"))
    run = page.commands.buttons[2]
    page.commands.move_to(run.id, 0)
    assert [button.name for button in page.commands.buttons] == ["Run", "Clock", "Brief"]
    QTest.keyClick(one.view, Qt.Key_1, Qt.ControlModifier)
    assert one.transport.sent == b"show run\r"
    page.commands.move_to(run.id, 99)  # Past the end: last
    assert [button.name for button in page.commands.buttons] == ["Clock", "Brief", "Run"]


def test_dropping_a_button_puts_it_where_it_lands(page):
    for name in ("Clock", "Brief", "Run"):
        page.commands.put(CommandButton(name, f"show {name.lower()}"))
    connect(page, "one")  # So the sessions, and the bar under them, are showing
    page.tabs.show_command_bar(True)
    QApplication.processEvents()
    row = page.tabs.command_bar.row
    clock, brief, run = sorted(row.buttons(), key=lambda widget: widget.x())
    assert row.drop_position(0, run.button_id)[0] == 0  # Before Clock
    assert row.drop_position(brief.geometry().center().x() + 1, clock.button_id)[0] == 1  # After Brief
    assert row.drop_position(row.width(), clock.button_id)[0] == 2  # At the end

    from PyQt5.QtCore import QMimeData, QPoint
    from PyQt5.QtGui import QDropEvent
    from nomad.ui.command_bar import BUTTON_MIME
    mime = QMimeData()
    mime.setData(BUTTON_MIME, run.button_id.encode())
    row.dropEvent(QDropEvent(QPoint(1, 5), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier))
    QApplication.processEvents()
    assert [button.name for button in page.commands.buttons] == ["Run", "Clock", "Brief"]
    assert [widget.text() for widget in row.buttons()] == ["Run", "Clock", "Brief"]  # The bar shows it


def test_the_bar_shows_a_new_button(page):
    page.commands.put(CommandButton("Clock", "show clock"))
    layout = page.tabs.command_bar.row_layout
    names = [layout.itemAt(index).widget().text() for index in range(layout.count())
             if layout.itemAt(index).widget() is not None]
    assert names == ["Clock"]


# ----------------------------------------------------------------- Line delay

def test_with_a_line_delay_lines_go_one_at_a_time(page):
    one = connect(page, "one")
    one.session.line_delay = 30
    one.send_block("a\nb\nc", final_enter=False)
    assert one.transport.sent == b"a\r"
    assert "Sending line 2 of 3" in one.status_label.text()
    QTest.qWait(300)
    assert one.transport.sent == b"a\rb\rc"
    assert "Sending" not in one.status_label.text()


def test_a_block_can_ask_for_a_delay_the_session_does_not_have(page):
    one = connect(page, "one")
    assert one.session.line_delay == 0
    one.send_block("configure terminal\ncdp run\nend", min_delay=40)
    assert one.transport.sent == b"configure terminal\r"
    QTest.qWait(400)
    assert one.transport.sent == b"configure terminal\rcdp run\rend\r"
    one.send_block("a\nb")  # Later blocks go at the session's own pace again
    assert one.transport.sent.endswith(b"a\rb\r")


def test_stop_sending_drops_the_rest(page):
    one = connect(page, "one")
    one.session.line_delay = 1000
    one.send_block("a\nb\nc")
    one.stop_sending()
    QTest.qWait(50)
    assert one.transport.sent == b"a\r"


def test_a_paste_uses_the_line_delay(page):
    one = connect(page, "one")
    one.session.line_delay = 10
    QApplication.clipboard().setText("interface Gi1/0/1\n shutdown")
    one.paste()
    QTest.qWait(200)
    assert one.transport.sent == b"interface Gi1/0/1\r shutdown"  # No Enter after the last line, as pasted


# ----------------------------------------------------------------- Reconnecting automatically

def test_a_drop_starts_reconnecting_when_it_is_on(page):
    one = connect(page, "one")
    one.auto_reconnect = True
    one.on_closed("Connection closed by the device.")
    assert one.reconnect_timer.isActive() and one.reconnect_attempts == 1
    one.stop_reconnecting()
    assert not one.reconnect_timer.isActive()


def test_no_reconnecting_after_typing_exit(page):
    one = connect(page, "one")
    one.auto_reconnect = True
    one.type_text("exit\r")
    one.on_closed("Connection closed.")
    assert not one.reconnect_timer.isActive()


def test_no_reconnecting_when_it_is_off(page):
    one = connect(page, "one")
    one.on_closed("Connection closed.")
    assert not one.reconnect_timer.isActive()


def test_failing_while_reconnecting_tries_again_until_disconnected(page):
    one = connect(page, "one")
    one.auto_reconnect = True
    one.on_closed("Connection closed.")
    one.reconnect_timer.stop()
    one.on_failed("Connection refused.")  # The device is still starting
    assert one.reconnect_timer.isActive() and one.reconnect_attempts == 2
    one.disconnect_session()  # On purpose: stop
    assert not one.reconnect_timer.isActive() and one.state == DISCONNECTED


# ----------------------------------------------------------------- Anti-idle

def test_anti_idle_sends_after_the_idle_time(page):
    one = connect(page, "one")
    one.session.anti_idle = 1
    one.set_state(CONNECTED, "Connected.")
    QTest.qWait(1300)
    assert one.transport.sent == b" \b"


def test_typing_puts_anti_idle_off(page):
    one = connect(page, "one")
    one.session.anti_idle = 1
    one.set_state(CONNECTED, "Connected.")
    QTest.qWait(600)
    one.type_text("x")
    QTest.qWait(600)
    assert one.transport.sent == b"x"


# ----------------------------------------------------------------- Keyword highlighting

def test_keywords_are_coloured_in_the_terminal(page):
    one = connect(page, "one")
    one.model.feed(b"Gi2 is administratively down, Gi3 down\r\n")  # Fits the narrow test terminal
    index = one.model.history_length
    colors = one.view.keyword_colors(one.model.line(index), one.model.columns)
    text = one.model.line_text(index)
    assert colors[text.index("administratively")] == "Amber"
    assert colors[text.rindex("down")] == "Red"
    assert colors[0] is None


def test_the_devices_own_colours_are_left_alone(page):
    one = connect(page, "one")
    one.model.feed(b"\x1b[32mdown\x1b[0m\r\n")
    assert one.view.keyword_colors(one.model.line(one.model.history_length), one.model.columns) is None


def test_switching_highlighting_off(page):
    one = connect(page, "one")
    page.highlights.set_enabled(False)
    assert one.view.highlighter is None
    page.highlights.set_enabled(True)
    assert one.view.highlighter is not None
