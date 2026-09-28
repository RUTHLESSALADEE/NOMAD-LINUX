"""Terminal page: saved sessions in folders, quick connect, and SSH / Telnet / serial / raw TCP sessions in tabs that
can be popped out into their own windows."""
import logging

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QKeySequence
from PyQt5.QtWidgets import QAbstractItemView, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, \
    QMainWindow, QMenu, QMessageBox, QShortcut, QSplitter, QStackedWidget, QTabWidget, QToolButton, QTreeWidget, \
    QTreeWidgetItem, QVBoxLayout, QWidget

from ..terminal.sessions import PROTOCOLS, SERIAL, SSH, Session, SessionStore, import_putty, parse_quick_connect, \
    normalize_folder
from .common import set_hint
from .session_dialog import SessionDialog
from .vault_dialog import SecurityDialog
from .terminal_view import CONNECTED, CONNECTING, DISCONNECTED, SessionView
from .theme import COLORS

log = logging.getLogger(__name__)

SESSION_ROLE = Qt.UserRole
FOLDER_ROLE = Qt.UserRole + 1
STATE_COLORS = {CONNECTED: COLORS["success"], CONNECTING: COLORS["warning"], DISCONNECTED: COLORS["muted"]}


class SessionTabs(QTabWidget):
    """Tabs of open sessions. Used on the Terminal page and in pop-out windows."""
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
            view.view.setFocus()
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

    def close_view(self, view):
        if self.indexOf(view) < 0:
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
            view.view.setFocus()

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
        if view.session.protocol == SERIAL:
            break_action = menu.addAction("Send Break")
            break_action.setToolTip("What Cisco devices watch for at boot to enter ROMMON (password recovery).")
            break_action.setEnabled(view.state == CONNECTED)
            actions[break_action] = view.send_break
        actions[menu.addAction("Stop Logging" if view.log_file is not None else "Log to File")] = view.toggle_logging
        actions[menu.addAction("Find...")] = view.show_find
        actions[menu.addAction("Clear Scrollback")] = view.clear_scrollback
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


class TerminalWindow(QMainWindow):
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
        self.setWindowTitle(f"{view.title} - NOMAD Terminal" if view is not None else "NOMAD Terminal")

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
            self.tabs.close_view(view)
        if self in self.page.windows:
            self.page.windows.remove(self)
        super().closeEvent(event)


class TerminalTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = SessionStore()
        self.windows = []
        self.closing = False
        self.collapsed = set()
        self.init_ui()
        self.fill_tree()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # Every pixel goes to the sessions
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        self.splitter = splitter

        # Session manager: quick connect, a filter, the saved sessions and two small menus
        self.manager = QWidget()
        self.manager.setMinimumWidth(170)
        manager_layout = QVBoxLayout(self.manager)
        manager_layout.setContentsMargins(4, 4, 2, 4)
        manager_layout.setSpacing(4)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(2)
        self.quick_protocol = QComboBox()
        self.quick_protocol.addItems(PROTOCOLS)
        self.quick_protocol.setToolTip("Protocol for quick connect, unless the text says otherwise "
                                       "(\"telnet 10.0.0.5\", \"raw host:9100\", \"COM3:115200\").")
        self.quick_input = QLineEdit()
        self.quick_input.setPlaceholderText("Quick connect")
        self.quick_input.setToolTip("admin@10.0.0.1, telnet 10.0.0.5, raw 10.0.0.9:9100 or COM3:115200, then Enter")
        quick_row.addWidget(self.quick_protocol)
        quick_row.addWidget(self.quick_input, 1)
        manager_layout.addLayout(quick_row)
        self.quick_status = QLabel()
        self.quick_status.setWordWrap(True)
        self.quick_status.setVisible(False)
        manager_layout.addWidget(self.quick_status)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter sessions")
        self.filter_input.setClearButtonEnabled(True)
        manager_layout.addWidget(self.filter_input)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)  # One column: where a session connects to is in its tooltip
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.setIndentation(14)
        manager_layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(2)
        self.new_button = QToolButton()
        self.new_button.setText("New")
        self.new_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.new_button.setToolTip("New session (the arrow also offers a new folder)")
        new_menu = QMenu(self.new_button)
        new_menu.addAction("New Session...", lambda: self.new_session(self.selected_folder()))
        new_menu.addAction("New Folder...", lambda: self.new_folder(self.selected_folder()))
        self.new_button.setMenu(new_menu)
        self.more_button = QToolButton()
        self.more_button.setText("⋯")
        self.more_button.setToolTip("Import from PuTTY, and saved password protection")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.more_button)
        more_menu.addAction("Import from PuTTY", self.import_from_putty)
        more_menu.addSeparator()
        more_menu.addAction("Master Password...", self.show_protection)
        self.lock_action = more_menu.addAction("Lock Saved Passwords Now", self.lock_now)
        self.more_button.setMenu(more_menu)
        self.protection_label = QLabel()
        self.protection_label.setStyleSheet(f"color: {COLORS['muted']};")
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.more_button)
        buttons.addWidget(self.protection_label, 1)
        manager_layout.addLayout(buttons)
        self.protection_timer = QTimer(self)
        self.protection_timer.timeout.connect(self.update_protection)
        self.protection_timer.start(5000)  # Shows when the master password locks itself after being idle
        splitter.addWidget(self.manager)

        # Open sessions
        self.stack = QStackedWidget()
        placeholder = QLabel("Double-click a saved session to open it, or type an address in Quick connect.\n\n"
                             "Select text to copy it; right-click to paste. Ctrl+Shift+F finds text, Shift+PgUp "
                             "scrolls back, and Ctrl+mouse wheel changes the text size.\n\n"
                             "For more room: « hides the session list, and F11 (Focus) hides everything but "
                             "the sessions.")
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

        self.quick_input.returnPressed.connect(self.quick_connect)
        self.quick_input.textChanged.connect(lambda: self.quick_status.setVisible(False))
        self.filter_input.textChanged.connect(self.fill_tree)
        self.tree.itemActivated.connect(self.on_item_activated)
        self.tree.customContextMenuRequested.connect(self.show_tree_menu)
        self.tree.itemExpanded.connect(lambda item: self.collapsed.discard(item.data(0, FOLDER_ROLE)))
        self.tree.itemCollapsed.connect(lambda item: self.collapsed.add(item.data(0, FOLDER_ROLE)))
        self.new_button.clicked.connect(lambda: self.new_session(self.selected_folder()))
        self.manager_toggle.clicked.connect(lambda: self.set_manager_visible(not self.manager_shown))
        self.focus_button.clicked.connect(lambda checked: self.window.set_focus_mode(checked))
        self.splitter.splitterMoved.connect(self.on_splitter_moved)
        self.update_protection()
        self.set_manager_visible(True)
        delete_shortcut = QShortcut(QKeySequence.Delete, self.tree)
        delete_shortcut.setContext(Qt.WidgetShortcut)
        delete_shortcut.activated.connect(self.delete_selected)

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
        """The session list as a menu, for when it's hidden: folders become submenus."""
        menu = self.sessions_menu
        menu.clear()
        menu.addAction("Quick Connect...", self.quick_connect_dialog)
        menu.addAction("New Session...", lambda: self.new_session(""))
        menu.addSeparator()
        submenus = {"": menu}

        def folder_menu(path):
            if path in submenus:
                return submenus[path]
            parent_path, _, name = path.rpartition("/")
            submenus[path] = folder_menu(parent_path).addMenu(name)
            return submenus[path]

        for path in sorted(self.store.all_folders(), key=str.lower):
            folder_menu(path)
        for session in sorted(self.store.sessions, key=lambda item: item.name.lower()):
            action = folder_menu(session.folder).addAction(session.name, lambda session=session:
                                                           self.open_session(session))
            action.setToolTip(f"{session.target()} ({session.protocol})")
        if not self.store.sessions:
            menu.addAction("No saved sessions").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Show the Session List", lambda: self.set_manager_visible(True))

    def quick_connect_dialog(self):
        text, ok = QInputDialog.getText(self, "Quick Connect", "Connect to (admin@10.0.0.1, telnet 10.0.0.5, "
                                        "COM3:115200):", text=self.quick_input.text())
        if ok and text.strip():
            self.quick_input.setText(text.strip())
            self.quick_connect()

    def lock_now(self):
        self.store.vault.lock()
        self.update_protection()

    # ----------------------------------------------------------------- Page interface

    def save_settings(self, settings):
        settings.setValue("terminal/splitter", self.splitter.saveState())
        settings.setValue("terminal/manager", self.manager_shown)
        settings.setValue("terminal/quick", self.quick_input.text())
        settings.setValue("terminal/quick_protocol", self.quick_protocol.currentText())
        settings.setValue("terminal/collapsed", "\n".join(sorted(folder for folder in self.collapsed if folder)))

    def restore_settings(self, settings):
        state = settings.value("terminal/splitter")
        if state is not None:
            self.splitter.restoreState(state)
        self.manager_shown = settings.value("terminal/manager", True, bool)
        self.show_page(self.stack.currentIndex())
        self.quick_input.setText(settings.value("terminal/quick", "", str))
        self.quick_protocol.setCurrentText(settings.value("terminal/quick_protocol", SSH, str))
        self.collapsed = {folder for folder in settings.value("terminal/collapsed", "", str).split("\n") if folder}
        self.fill_tree()

    def update_protection(self):
        vault = self.store.vault
        if vault.enabled:
            text = "Master password " + ("unlocked" if vault.unlocked else "locked")
            tip = "Saved passwords need your Windows account and the master password."
        else:
            text, tip = "", "Saved passwords are protected by your Windows account."
        self.protection_label.setText(text)
        self.protection_label.setToolTip(tip)
        self.lock_action.setEnabled(vault.enabled and vault.unlocked)

    def show_protection(self):
        SecurityDialog(self, self.store).exec_()
        self.update_protection()

    def shutdown(self):
        self.closing = True
        for window in list(self.windows):
            window.close()
        for view in self.tabs.views():
            self.tabs.close_view(view)

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
        reply = QMessageBox.question(self.window, "Sessions Open",
                                     f"{len(connected)} terminal session{'' if len(connected) == 1 else 's'} "
                                     f"{'is' if len(connected) == 1 else 'are'} still connected. Close NOMAD and "
                                     "disconnect?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return reply == QMessageBox.Yes

    # ----------------------------------------------------------------- Opening sessions

    def open_session(self, session, saved=True, window=False):
        """Open a tab for a session and connect. saved=False opens a copy that isn't tied to the saved one."""
        if not saved:
            session = session.copy()
        view = SessionView(session, self.store)
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

    def quick_connect(self):
        try:
            session = parse_quick_connect(self.quick_input.text(), self.quick_protocol.currentText())
        except ValueError as error:
            set_hint(self.quick_status, str(error), "error")
            self.quick_status.setVisible(True)
            return
        self.open_session(session, saved=True)

    def new_window(self):
        new_window = TerminalWindow(self)
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
            view.session = dialog.session
            self.fill_tree(select=dialog.session.id)

    # ----------------------------------------------------------------- The session tree

    def fill_tree(self, select=None):
        selected = select or self.selected_id()
        words = self.filter_input.text().lower().split()
        self.tree.clear()
        folder_items = {}

        def folder_item(path):
            if not path:
                return self.tree.invisibleRootItem()
            if path in folder_items:
                return folder_items[path]
            parent_path, _, name = path.rpartition("/")
            parent = folder_item(parent_path)
            item = QTreeWidgetItem([name])
            font = QFont(item.font(0))
            font.setBold(True)
            item.setFont(0, font)
            item.setData(0, FOLDER_ROLE, path)
            parent.addChild(item)
            folder_items[path] = item
            return item

        if not words:
            for path in sorted(self.store.all_folders(), key=str.lower):
                folder_item(path)
        for session in sorted(self.store.sessions, key=lambda item: (item.folder.lower(), item.name.lower())):
            text = " ".join((session.path, session.target(), session.protocol, session.notes)).lower()
            if words and not all(word in text for word in words):
                continue
            item = QTreeWidgetItem([session.name])
            item.setData(0, SESSION_ROLE, session.id)
            lines = [f"{session.target()}  ({session.protocol})"]
            if session.folder:
                lines.append(f"Folder: {session.folder}")
            if session.notes:
                lines.append(session.notes)
            item.setToolTip(0, "\n".join(lines))
            folder_item(session.folder).addChild(item)
            if session.id == selected:
                self.tree.setCurrentItem(item)
        for path, item in folder_items.items():
            item.setExpanded(bool(words) or path not in self.collapsed)
        if not self.store.sessions and not words:
            hint = QTreeWidgetItem(["No saved sessions yet"])
            hint.setToolTip(0, "New creates one; the ⋯ menu imports your PuTTY sessions.")
            hint.setFlags(Qt.NoItemFlags)
            self.tree.addTopLevelItem(hint)

    def selected_id(self):
        item = self.tree.currentItem()
        return item.data(0, SESSION_ROLE) if item is not None else None

    def selected_folder(self):
        """The folder of the selected item (or the selected folder itself), for creating things in."""
        item = self.tree.currentItem()
        if item is None:
            return ""
        if item.data(0, FOLDER_ROLE):
            return item.data(0, FOLDER_ROLE)
        session = self.store.get(item.data(0, SESSION_ROLE))
        return session.folder if session else ""

    def on_item_activated(self, item, _column):
        session = self.store.get(item.data(0, SESSION_ROLE))
        if session is not None:
            self.open_session(session)

    def show_tree_menu(self, position):
        item = self.tree.itemAt(position)
        if item is not None:
            self.tree.setCurrentItem(item)
        session = self.store.get(item.data(0, SESSION_ROLE)) if item is not None else None
        folder = item.data(0, FOLDER_ROLE) if item is not None else None
        menu = QMenu(self)
        actions = {}
        if session is not None:
            actions[menu.addAction("Connect")] = lambda: self.open_session(session)
            actions[menu.addAction("Connect in New Window")] = lambda: self.open_session(session, window=True)
            menu.addSeparator()
            actions[menu.addAction("Edit...")] = lambda: self.edit_session(session)
            actions[menu.addAction("Duplicate")] = lambda: self.duplicate_session(session)
            actions[menu.addAction("Delete")] = lambda: self.delete_session(session)
            menu.addSeparator()
        actions[menu.addAction("New Session...")] = lambda: self.new_session(self.selected_folder())
        actions[menu.addAction("New Folder...")] = lambda: self.new_folder(self.selected_folder())
        if folder:
            actions[menu.addAction("Rename Folder...")] = lambda: self.rename_folder(folder)
            actions[menu.addAction("Delete Folder")] = lambda: self.delete_folder(folder)
        menu.addSeparator()
        actions[menu.addAction("Import from PuTTY")] = self.import_from_putty
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(position))
        if chosen in actions:
            actions[chosen]()

    def new_session(self, folder=""):
        session = Session(name="", folder=folder)
        dialog = SessionDialog(self, session, self.store.all_folders(), "New Session", self.store)
        if dialog.exec_():
            self.store.put(dialog.session)
            self.fill_tree(select=dialog.session.id)

    def edit_session(self, session):
        dialog = SessionDialog(self, session.copy(id=session.id), self.store.all_folders(), "Edit Session", self.store)
        if dialog.exec_():
            self.store.put(dialog.session)
            for view in self.all_views():
                if view.session.id == session.id:
                    view.session = dialog.session  # Used from the next connect on
            self.fill_tree(select=dialog.session.id)

    def duplicate_session(self, session):
        copy = session.copy(name=self.store.unique_name(session.name, session.folder))
        self.store.put(copy)
        self.fill_tree(select=copy.id)

    def delete_session(self, session):
        reply = QMessageBox.question(self, "Delete Session", f"Delete the saved session {session.name}?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.store.delete(session.id)
            self.fill_tree()

    def delete_selected(self):
        item = self.tree.currentItem()
        if item is None:
            return
        if item.data(0, FOLDER_ROLE):
            self.delete_folder(item.data(0, FOLDER_ROLE))
        elif self.store.get(item.data(0, SESSION_ROLE)):
            self.delete_session(self.store.get(item.data(0, SESSION_ROLE)))

    def new_folder(self, parent=""):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:" + (f" (inside {parent})" if parent else ""))
        if ok and normalize_folder(name):
            path = self.store.add_folder(f"{parent}/{name}" if parent else name)
            self.collapsed.discard(path)
            self.fill_tree()

    def rename_folder(self, folder):
        name, ok = QInputDialog.getText(self, "Rename Folder", "New name:", text=folder.rpartition("/")[2])
        if ok and normalize_folder(name) and "/" not in name.strip():
            parent = folder.rpartition("/")[0]
            self.store.rename_folder(folder, f"{parent}/{name}" if parent else name)
            self.fill_tree()

    def delete_folder(self, folder):
        count = sum(1 for session in self.store.sessions
                    if session.folder == folder or session.folder.startswith(folder + "/"))
        message = f"Delete the folder {folder}" + (f" and the {count} session{'' if count == 1 else 's'} in it"
                                                   if count else "") + "?"
        reply = QMessageBox.question(self, "Delete Folder", message, QMessageBox.Yes | QMessageBox.No,
                                     QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.store.delete_folder(folder)
            self.fill_tree()

    def import_from_putty(self):
        try:
            added = import_putty(self.store)
        except OSError as error:
            QMessageBox.critical(self, "Import from PuTTY", f"Couldn't read PuTTY's sessions:\n\n{error}")
            return
        self.fill_tree()
        message = f"Imported {added} session{'' if added == 1 else 's'} from PuTTY into the folder " \
                  "\"Imported from PuTTY\"." if added else \
            "No new PuTTY sessions to import (none saved, or all imported already)."
        QMessageBox.information(self, "Import from PuTTY", message)
