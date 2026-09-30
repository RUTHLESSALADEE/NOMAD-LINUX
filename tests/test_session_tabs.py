"""Tiled session tabs: sharing sessions among the panes of a layout, and moving them between panes."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtCore import pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget  # noqa: E402

from nomad.ui.session_tabs import SessionTabs, arrange  # noqa: E402
from nomad.ui.terminal_view import CONNECTED  # noqa: E402


def names(result):
    return [(views, current) for views, current in result]


def test_tabs_are_dealt_out_in_order():
    assert names(arrange([(["a", "b", "c", "d", "e"], "e")], 4)) == \
        [(["a", "b"], None), (["c"], None), (["d"], None), (["e"], "e")]


def test_fewer_sessions_than_panes_leaves_panes_empty():
    assert arrange([(["a", "b"], "a")], 4) == [(["a"], "a"), (["b"], None), ([], None), ([], None)]


def test_panes_that_go_away_join_the_last_one_keeping_the_focused_session():
    groups = [(["a"], "a"), (["b"], "b"), (["c", "d"], "d"), (["e"], "e")]
    assert arrange(groups, 2, focused="d") == [(["a"], "a"), (["b", "c", "d", "e"], "d")]


def test_empty_panes_take_a_session_from_the_fullest():
    groups = [(["a", "b", "c"], "a"), (["d"], "d")]
    assert arrange(groups, 3, focused="a") == [(["a", "c"], "a"), (["d"], "d"), (["b"], "b")]


class Session:
    protocol = "SSH"
    id = "session"

    def __init__(self, name):
        self.name = name

    def target(self):
        return self.name


class View(QWidget):
    state_changed = pyqtSignal(object)

    def __init__(self, name):
        super().__init__()
        self.session, self.title, self.state = Session(name), name, CONNECTED
        self.field = QLineEdit(self)
        QVBoxLayout(self).addWidget(self.field)

    def focus_target(self):
        return self.field

    def confirm_close(self):
        return True

    def shutdown(self):
        pass


class Page:
    tiling = True


@pytest.fixture
def tabs():
    app = QApplication.instance() or QApplication([])
    page = Page()
    page.tabs = SessionTabs(page)
    yield page.tabs
    page.tabs.deleteLater()
    app.processEvents()


def layout_of(tabs):
    return [[view.title for view in pane.views] for pane in tabs.panes]


def test_each_pane_has_its_own_tabs_and_new_sessions_open_in_empty_panes(tabs):
    for name in "abc":
        tabs.add_view(View(name))
    tabs.set_layout("grid4")
    assert layout_of(tabs) == [["a"], ["b"], ["c"], []]
    tabs.add_view(View("d"))
    assert layout_of(tabs) == [["a"], ["b"], ["c"], ["d"]]
    assert tabs.currentWidget().title == "d"


def test_moving_a_session_to_another_pane(tabs):
    views = [View(name) for name in "abc"]
    for view in views:
        tabs.add_view(view)
    tabs.set_layout("side2")
    tabs.move_view(views[0], tabs.panes[1])
    assert layout_of(tabs) == [["b"], ["c", "a"]]
    assert tabs.currentWidget() is views[0]
    tabs.set_layout("tabs")
    assert layout_of(tabs) == [["b", "c", "a"]]
    assert tabs.currentWidget() is views[0]


def test_closing_the_last_session_empties_the_tabs(tabs):
    emptied = []
    tabs.emptied.connect(lambda: emptied.append(True))
    view = View("a")
    tabs.add_view(view)
    tabs.set_layout("stack2")
    tabs.close_view(view)
    assert tabs.count() == 0 and emptied


def test_a_session_opens_in_the_active_pane_when_it_is_empty(tabs):
    for name in "ab":
        tabs.add_view(View(name))
    tabs.set_layout("grid4")
    tabs.activate(tabs.panes[3], focus=False)
    tabs.add_view(View("c"))
    assert layout_of(tabs) == [["a"], ["b"], [], ["c"]]


def test_moving_a_session_to_another_windows_pane(tabs):
    other_page = Page()
    other_page.tabs = tabs  # Both windows belong to the same page
    other = SessionTabs(other_page)
    view = View("a")
    tabs.add_view(view)
    tabs.add_view(View("b"))
    other.set_layout("side2")
    other.move_view(view, other.panes[1], tabs)
    assert layout_of(tabs) == [["b"]]
    assert layout_of(other) == [[], ["a"]]
    assert other.currentWidget() is view
    other.deleteLater()
