"""A page of session tabs beside the saved-session sidebar, with pop-out windows: the base of the Terminal and SCP
pages."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QMenu, QMessageBox, QSplitter, QStackedWidget, QToolButton,     QVBoxLayout, QWidget

from ..terminal.sessions import SSH, parse_quick_connect
from .session_dialog import SessionDialog
from .session_manager import SessionManager
from .session_tabs import SessionTabs, SessionWindow
from .terminal_view import DISCONNECTED
from .theme import COLORS


class SessionPage(QWidget):
    """A page of session tabs beside the session sidebar: the Terminal page, and the SCP page. Subclasses set the
    protocols the sidebar shows, the placeholder text, and make_view(session)."""
    protocols = None
    settings_prefix = "terminal"
    window_title = "NOMAD Terminal"
    placeholder_text = ""
    kind = "terminal session"  # For "3 terminal sessions are still connected"

    def __init__(self, window, store):
        super().__init__(window)
        self.window = window
        self.store = store
        self.windows = []
        self.closing = False
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # Every pixel goes to the sessions
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        self.splitter = splitter
        self.manager = SessionManager(self, self.store, self.protocols, self.settings_prefix)
        splitter.addWidget(self.manager)

        # Open sessions
        self.stack = QStackedWidget()
        placeholder = QLabel(self.placeholder_text)
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setWordWrap(True)
        placeholder.setStyleSheet(f"color: {COLORS['muted']};")
        self.tabs = SessionTabs(self)
        self.tabs.emptied.connect(self.on_tabs_emptied)
        self.stack.addWidget(placeholder)
        self.stack.addWidget(self.tabs)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 990])
        layout.addWidget(splitter)

        # Beside the tabs: hide or show the session list, a menu of sessions while it's hidden, and focus mode
        corner_left = QWidget()
        left_layout = QHBoxLayout(corner_left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(0)
        self.manager_toggle = QToolButton()
        self.manager_toggle.setAutoRaise(True)
        self.sessions_button = QToolButton()
        self.sessions_button.setText("Sessions")
        self.sessions_button.setAutoRaise(True)
        self.sessions_button.setPopupMode(QToolButton.InstantPopup)
        self.sessions_menu = QMenu(self.sessions_button)
        self.sessions_menu.aboutToShow.connect(self.fill_sessions_menu)
        self.sessions_button.setMenu(self.sessions_menu)
        left_layout.addWidget(self.manager_toggle)
        left_layout.addWidget(self.sessions_button)
        self.tabs.setCornerWidget(corner_left, Qt.TopLeftCorner)
        self.focus_button = QToolButton()
        self.focus_button.setText("Focus")
        self.focus_button.setAutoRaise(True)
        self.focus_button.setCheckable(True)
        self.focus_button.setToolTip("Give the sessions the whole window: hides NOMAD's sidebar, the adapter bar and "
                                     "the session list (F11)")
        self.tabs.setCornerWidget(self.focus_button, Qt.TopRightCorner)

        self.manager_toggle.clicked.connect(lambda: self.set_manager_visible(not self.manager_shown))
        self.focus_button.clicked.connect(lambda checked: self.window.set_focus_mode(checked))
        self.splitter.splitterMoved.connect(self.on_splitter_moved)
        self.set_manager_visible(True)

    def make_view(self, session):
        raise NotImplementedError

    # ----------------------------------------------------------------- Making room

    manager_shown = True

    def set_manager_visible(self, visible, remember=True):
        """Show or hide the session list. remember=False is for focus mode, which puts it back afterwards."""
        if remember:
            self.manager_shown = visible
        self.manager.setVisible(visible)
        if visible and self.splitter.sizes()[0] == 0:
            self.splitter.setSizes([210, max(400, self.splitter.width() - 210)])
        self.manager_toggle.setText("«" if visible else "»")
        self.manager_toggle.setToolTip("Hide the session list" if visible else "Show the session list")
        self.sessions_button.setVisible(not visible)

    def on_splitter_moved(self, position, _index):
        if position == 0:  # Dragged all the way closed: same as hiding it
            self.set_manager_visible(False)

    def show_page(self, index):
        """Switch between the placeholder (0) and the open sessions (1). With nothing open, the buttons that bring
        the session list back aren't on screen, so it's always shown then; the choice to hide it applies again once
        a session opens."""
        self.stack.setCurrentIndex(index)
        if not self.window.focus_mode:
            self.set_manager_visible(self.manager_shown or index == 0, remember=False)

    def on_tabs_emptied(self):
        self.show_page(0)

    def fill_sessions_menu(self):
        self.manager.fill_menu(self.sessions_menu)
        self.sessions_menu.addAction("Show the Session List", lambda: self.set_manager_visible(True))

    # ----------------------------------------------------------------- Page interface

    def save_settings(self, settings):
        prefix = self.settings_prefix
        settings.setValue(f"{prefix}/splitter", self.splitter.saveState())
        settings.setValue(f"{prefix}/manager", self.manager_shown)
        self.manager.save_settings(settings)

    def restore_settings(self, settings):
        prefix = self.settings_prefix
        state = settings.value(f"{prefix}/splitter")
        if state is not None:
            self.splitter.restoreState(state)
        self.manager_shown = settings.value(f"{prefix}/manager", True, bool)
        self.show_page(self.stack.currentIndex())
        self.manager.restore_settings(settings)

    def shutdown(self):
        self.closing = True
        for window in list(self.windows):
            window.close()
        for view in self.tabs.views():
            self.tabs.close_view(view, ask=False)

    def all_views(self):
        views = self.tabs.views()
        for window in self.windows:
            views += window.tabs.views()
        return views

    def confirm_close(self):
        """Before the app closes: ask if sessions are still connected. Returns False to keep the app open."""
        connected = [view for view in self.all_views() if view.state != DISCONNECTED]
        if not connected:
            return True
        count = len(connected)
        reply = QMessageBox.question(self.window, "Sessions Open",
                                     f"{count} {self.kind}{'' if count == 1 else 's'} "
                                     f"{'is' if count == 1 else 'are'} still connected. Close NOMAD and "
                                     "disconnect?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return reply == QMessageBox.Yes

    # ----------------------------------------------------------------- Opening sessions

    def open_session(self, session, saved=True, window=False):
        """Open a tab for a session and connect. saved=False opens a copy that isn't tied to the saved one."""
        if not saved:
            session = session.copy()
        view = self.make_view(session)
        self.store.remember(session)
        if window:
            new_window = self.new_window()
            new_window.tabs.add_view(view)
            new_window.update_title()
            new_window.show()
        else:
            self.show_page(1)
            self.tabs.add_view(view)
            self.window.navigator.setCurrentWidget(self)
        view.connect_session()
        return view

    def open_address(self, text, protocol=SSH):
        """Quick connect from another page, such as Sweep's "Open SSH Session"."""
        try:
            session = parse_quick_connect(text, protocol)
        except ValueError as error:
            QMessageBox.warning(self, "Connect", str(error))
            return None
        return self.open_session(session, saved=True)

    def new_window(self):
        new_window = SessionWindow(self)
        self.windows.append(new_window)
        return new_window

    def pop_out(self, view):
        self.tabs.take_view(view)
        new_window = self.new_window()
        new_window.tabs.add_view(view)
        new_window.update_title()
        new_window.show()
        new_window.activateWindow()

    def move_to_main(self, view, source):
        source.take_view(view)
        self.show_page(1)
        self.tabs.add_view(view)
        self.window.navigator.setCurrentWidget(self)
        self.window.activateWindow()

    def save_quick_session(self, view):
        session = view.session
        dialog = SessionDialog(self, session.copy(id=session.id), self.store.all_folders(), "Save Session", self.store)
        if dialog.exec_():
            self.store.put(dialog.session)
            self.store.link_recent(dialog.session)
            view.session = dialog.session
            self.manager.fill_tree(select=dialog.session.id)
