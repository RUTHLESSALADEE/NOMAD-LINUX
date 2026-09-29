"""Tabs of open sessions and the pop-out windows they can move to. Shared by the Terminal and SCP pages.

A view (a tab's widget) has: session, title, state (CONNECTING / CONNECTED / DISCONNECTED), a state_changed(view)
signal, connect_session(), reconnect(), disconnect_session(), shutdown(), confirm_close() (False keeps the tab open),
focus_target() (the widget to focus), and add_tab_actions(menu, actions) for its own entries in the tab's menu.
The page has: tabs, store, closing, open_session(session, saved), pop_out(view), move_to_main(view, source) and
save_quick_session(view), plus a window_title ("NOMAD Terminal").
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence
from PyQt5.QtWidgets import QMainWindow, QMenu, QMessageBox, QShortcut, QTabWidget

from .terminal_view import CONNECTED, CONNECTING, DISCONNECTED
from .theme import COLORS

STATE_COLORS = {CONNECTED: COLORS["success"], CONNECTING: COLORS["warning"], DISCONNECTED: COLORS["muted"]}


class SessionTabs(QTabWidget):
    """Tabs of open sessions. Used on a page and in pop-out windows."""
    emptied = pyqtSignal()

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self.show_tab_menu)
        self.tabCloseRequested.connect(lambda index: self.close_view(self.widget(index)))
        self.currentChanged.connect(self.focus_current)

    def views(self):
        return [self.widget(index) for index in range(self.count())]

    def add_view(self, view, select=True):
        index = self.addTab(view, view.title)
        view.state_changed.connect(self.update_tab)
        self.update_tab(view)
        if select:
            self.setCurrentIndex(index)
            view.focus_target().setFocus()
        return index

    def take_view(self, view):
        """Remove a view from these tabs without closing it (to move it elsewhere)."""
        index = self.indexOf(view)
        if index < 0:
            return
        view.state_changed.disconnect(self.update_tab)
        self.removeTab(index)
        if not self.count():
            self.emptied.emit()

    def close_view(self, view, ask=True):
        """Close a tab. ask=False skips the view's own question (such as transfers still running), for shutdown."""
        if self.indexOf(view) < 0:
            return
        if ask and not view.confirm_close():
            return
        self.take_view(view)
        view.shutdown()
        view.deleteLater()

    def update_tab(self, view):
        index = self.indexOf(view)
        if index < 0:
            return
        self.setTabText(index, view.title)
        self.setTabToolTip(index, f"{view.session.target()} ({view.session.protocol}): {view.state}")
        self.tabBar().setTabTextColor(index, QColor(STATE_COLORS[view.state]))

    def focus_current(self, index):
        view = self.widget(index)
        if view is not None:
            view.focus_target().setFocus()

    def show_tab_menu(self, position):
        index = self.tabBar().tabAt(position)
        if index < 0:
            return
        view = self.widget(index)
        menu = QMenu(self)
        actions = {}
        if view.state == DISCONNECTED:
            actions[menu.addAction("Connect")] = view.connect_session
        else:
            actions[menu.addAction("Reconnect")] = view.reconnect
            actions[menu.addAction("Disconnect")] = view.disconnect_session
        actions[menu.addAction("Duplicate Session")] = lambda: self.page.open_session(view.session, saved=False)
        if self.page.tabs is self:
            actions[menu.addAction("Pop Out to a Window")] = lambda: self.page.pop_out(view)
        else:
            actions[menu.addAction("Move to Main Window")] = lambda: self.page.move_to_main(view, self)
        menu.addSeparator()
        view.add_tab_actions(menu, actions)
        if self.page.store.get(view.session.id) is None:
            actions[menu.addAction("Save as Session...")] = lambda: self.page.save_quick_session(view)
        menu.addSeparator()
        actions[menu.addAction("Close")] = lambda: self.close_view(view)
        if self.count() > 1:
            actions[menu.addAction("Close Other Tabs")] = lambda: [self.close_view(other) for other in self.views()
                                                                   if other is not view]
        chosen = menu.exec_(self.tabBar().mapToGlobal(position))
        if chosen in actions:
            actions[chosen]()


class SessionWindow(QMainWindow):
    """A pop-out window of session tabs."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.tabs = SessionTabs(page, self)
        self.tabs.emptied.connect(self.close)
        self.tabs.currentChanged.connect(self.update_title)
        self.setCentralWidget(self.tabs)
        self.resize(960, 620)
        next_tab = QShortcut(QKeySequence("Ctrl+Tab"), self)
        next_tab.activated.connect(lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) %
                                                                     max(1, self.tabs.count())))
        full_screen = QShortcut(QKeySequence("F11"), self)
        full_screen.activated.connect(self.toggle_full_screen)

    def toggle_full_screen(self):
        """F11 in a pop-out window: the whole screen for its sessions."""
        self.setWindowState(self.windowState() ^ Qt.WindowFullScreen)

    def update_title(self):
        view = self.tabs.currentWidget()
        title = self.page.window_title
        self.setWindowTitle(f"{view.title} - {title}" if view is not None else title)

    def closeEvent(self, event):
        connected = [view for view in self.tabs.views() if view.state != DISCONNECTED]
        if connected and not self.page.closing:
            reply = QMessageBox.question(self, "Close Window",
                                         f"Close this window and disconnect {len(connected)} session"
                                         f"{'' if len(connected) == 1 else 's'}?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        for view in self.tabs.views():
            self.tabs.take_view(view)
            view.shutdown()
            view.deleteLater()
        if self in self.page.windows:
            self.page.windows.remove(self)
        super().closeEvent(event)
