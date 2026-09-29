"""The terminal widget with a mouse: selecting text while the device keeps printing."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from nomad.terminal.sessions import SSH, Session  # noqa: E402
from nomad.ui.terminal_view import SessionView  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def make_view(app, scrollback):
    view = SessionView(Session("t", SSH, "h", scrollback=scrollback))
    view.resize(800, 500)
    view.show()
    app.processEvents()
    return view


def print_lines(view, first, count):
    # Numbered first, so a selection that slips onto another line copies different text
    view.on_data("".join(f"{number:04d} line\r\n" for number in range(first, first + count)).encode())


def cell(view, row, column):
    terminal = view.view
    return QPoint(int((column + 0.5) * terminal.cell_width), int((row + 0.5) * terminal.cell_height))


def start_selecting(view):
    """Press on the top row and drag along it, leaving the button down. Returns the text selected."""
    terminal = view.view
    QTest.mousePress(terminal, Qt.LeftButton, Qt.NoModifier, cell(view, 0, 0))
    # A move with the button held (QTest.mouseMove sends one with no buttons)
    QApplication.sendEvent(terminal, QMouseEvent(QEvent.MouseMove, QPointF(cell(view, 0, 5)), Qt.NoButton,
                                                 Qt.LeftButton, Qt.NoModifier))
    return terminal.selected_text()


def release(view):
    QTest.mouseRelease(view.view, Qt.LeftButton, Qt.NoModifier, cell(view, 0, 6))


def test_output_while_selecting_keeps_the_selection(app):
    view = make_view(app, 1000)
    print_lines(view, 0, 40)
    selected = start_selecting(view)
    assert selected[:4].isdigit()
    print_lines(view, 40, 5)  # The device prints while the mouse button is still down (this used to crash)
    release(view)
    assert view.view.selection is not None
    assert QApplication.clipboard().text() == selected  # The same text, though it has scrolled up
    view.shutdown()


def test_selection_follows_its_text_when_the_scrollback_is_full(app):
    view = make_view(app, 10)
    print_lines(view, 0, 60)  # The 10-line scrollback is full: every new line drops the oldest
    selected = start_selecting(view)
    print_lines(view, 60, 3)
    release(view)
    assert QApplication.clipboard().text() == selected
    view.shutdown()


def test_selected_text_scrolling_away_entirely(app):
    view = make_view(app, 10)
    print_lines(view, 0, 60)
    QApplication.clipboard().setText("before")
    start_selecting(view)
    print_lines(view, 60, 50)  # Far more than the scrollback holds: the selected text has gone
    release(view)
    assert view.view.selection is None
    assert QApplication.clipboard().text() == "before"  # Nothing wrong copied
    view.shutdown()


def test_clear_scrollback_drops_a_selection_there(app):
    view = make_view(app, 1000)
    print_lines(view, 0, 60)
    view.view.set_offset(20)  # Scrolled back, selecting old text
    start_selecting(view)
    release(view)
    view.clear_scrollback()
    assert view.view.selection is None
    view.shutdown()
