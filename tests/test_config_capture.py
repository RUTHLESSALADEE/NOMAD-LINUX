import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from nomad.terminal.config_capture import ConfigCapture, write_config
from nomad.terminal.sessions import Session
from nomad.ui import terminal_view


def test_split_prompt_and_escape_sequences():
    capture = ConfigCapture("edge#", "show running-config")
    for chunk in ["show running-config\r\n\x1b[3", "2mhostname edge\x1b[0m\r\n", "end\r\ned", "ge#"]:
        capture.feed(chunk)
    assert capture.complete
    assert capture.output() == "hostname edge\nend\n"


def test_incomplete_and_rejected_commands():
    capture = ConfigCapture("edge#", "show running-config")
    capture.feed("show running-config\r\nhostname edge\r\n--More--")
    with pytest.raises(ValueError, match="incomplete"):
        capture.output()
    capture.feed("\r\n% Invalid input detected\r\nedge#")
    with pytest.raises(ValueError, match="rejected"):
        capture.output()


def test_failed_file_replace_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "config.cfg"
    path.write_text("original")
    def fail(*args):
        raise OSError("access denied")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_config(path, "new config")
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


@pytest.fixture
def view(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    view = terminal_view.SessionView(Session("edge", host="router", scrollback=10, anti_idle=30))
    sent = []
    view.transport = SimpleNamespace(send=sent.append, local_echo=False, close=lambda: None)
    view.set_state(terminal_view.CONNECTED)
    view.on_data(b"edge#")
    path = tmp_path / "edge.cfg"
    monkeypatch.setattr(terminal_view.QInputDialog, "getItem", lambda *args: (args[3][0], True))
    monkeypatch.setattr(terminal_view.QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    yield view, sent, path
    view.shutdown()
    view.deleteLater()
    app.processEvents()


def test_full_capture_exceeds_scrollback_and_does_not_mirror(view):
    widget, sent, path = view
    widget.mirror = lambda *args: pytest.fail("Capture must stay in this session")
    widget.save_running_config()
    assert sent == [b"terminal length 0\r"]
    assert not widget.idle_timer.isActive()
    widget.on_data(b"terminal length 0\r\nedge#")
    widget.advance_config_capture()
    assert sent[-1] == b"show running-config\r"
    widget.type_text("x")
    widget.session.line_delay = 100
    assert not widget.send_block("show clock\nshow version")
    assert not widget.outbox
    assert sent[-1] == b"show running-config\r"
    config = "hostname edge\n" + "".join(f"interface Ethernet{i}\n description port {i}\n" for i in range(2000)) + "end\n"
    widget.on_data(("show running-config\r\n" + config.replace("\n", "\r\n") + "edge#").encode())
    assert widget.config_timer.isActive()
    widget.advance_config_capture()
    assert path.read_text() == config
    assert widget.config_capture is None
    assert widget.idle_timer.isActive()


@pytest.mark.parametrize("reason", ["cancel", "disconnect", "timeout", "rejected"])
def test_failed_capture_preserves_destination(view, reason):
    widget, sent, path = view
    path.write_text("original")
    widget.save_running_config()
    if reason == "cancel":
        widget.cancel_config_capture()
        assert sent[-1] == b"\x03"
    elif reason == "disconnect":
        widget.disconnect_session()
    elif reason == "timeout":
        widget.config_timeout.timeout.emit()
    else:
        widget.on_data(b"terminal length 0\r\n% Invalid input detected\r\nedge#")
        widget.advance_config_capture()
        assert b"show running-config\r" not in sent
    assert path.read_text() == "original"
    assert widget.config_capture is None


def test_cancelled_save_dialog_sends_nothing(view, monkeypatch):
    widget, sent, path = view
    monkeypatch.setattr(terminal_view.QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    widget.save_running_config()
    assert not sent and not path.exists()


@pytest.mark.parametrize("profile", ["Juniper Junos", "Custom command (disable paging first)"])
def test_single_command_profiles(view, monkeypatch, profile):
    widget, sent, path = view
    monkeypatch.setattr(terminal_view.QInputDialog, "getItem", lambda *args: (profile, True))
    monkeypatch.setattr(terminal_view.QInputDialog, "getText", lambda *args, **kwargs: ("display current-configuration", True))
    widget.save_running_config()
    command = "show configuration | no-more" if profile == "Juniper Junos" else "display current-configuration"
    assert sent == [(command + "\r").encode()]
    widget.on_data((command + "\r\nsystem { host-name edge; }\r\nedge#").encode())
    widget.advance_config_capture()
    assert path.read_text() == "system { host-name edge; }\n"


def press(widget, keys):
    """Type keys (a QKeySequence string) into the session's terminal, as the user would."""
    from PyQt5.QtGui import QKeySequence
    from PyQt5.QtTest import QTest
    sequence = QKeySequence(keys)[0]
    QTest.keyClick(widget.view, sequence & ~0xFE000000 & 0x01FFFFFF, terminal_view.Qt.KeyboardModifiers(
        sequence & 0xFE000000))


def test_ctrl_s_logs_and_ctrl_shift_s_saves_config(view, tmp_path, monkeypatch):
    widget, sent, path = view
    log_path = tmp_path / "edge.log"
    monkeypatch.setattr(terminal_view.QFileDialog, "getSaveFileName", lambda *args: (
        str(log_path) if args[1] == "Log Session" else str(path), ""))
    widget.transport.resize = lambda columns, rows: None  # Shown, the terminal is sized
    widget.show()
    widget.activateWindow()
    widget.view.setFocus()
    QApplication.processEvents()
    press(widget, "Ctrl+S")
    assert widget.log_file is not None and widget.logging_button.text() == "Stop Logging"
    assert b"\x13" not in b"".join(sent)  # Not sent to the device as XOFF
    press(widget, "Ctrl+S")
    assert widget.log_file is None and log_path.exists()
    press(widget, "Ctrl+Shift+S")
    assert sent == [b"terminal length 0\r"] and widget.config_button.text() == "Cancel Save"
    press(widget, "Ctrl+Shift+S")  # Again while saving: cancels, as the button does
    assert widget.config_capture is None and sent[-1] == b"\x03"
    widget.hide()


def test_ctrl_shift_s_needs_a_connection(view):
    widget, sent, path = view
    widget.set_state(terminal_view.DISCONNECTED)
    widget.config_shortcut()
    assert not sent and widget.config_capture is None
