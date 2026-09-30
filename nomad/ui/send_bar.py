"""Send to All: a bar under the terminal sessions that sends a command to many sessions at once, and can mirror
typing to them (like SecureCRT's Send Commands to All Sessions and MobaXterm's MultiExec)."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, \
    QToolButton

from .theme import COLORS

SCOPES = [("all", "All connected sessions"), ("screen", "Sessions on screen"), ("window", "This window's sessions")]
HISTORY_LIMIT = 100
# The Keys menu: keys that can't be typed into the command box as themselves
KEYS = [("Ctrl+C (interrupt)", "\x03"), ("Ctrl+Z (end configuration mode)", "\x1a"),
        ("Ctrl+Shift+6 (Cisco abort: stops ping and traceroute)", "\x1e"), ("Ctrl+D", "\x04"),
        ("Tab (complete)", "\t"), ("Space (next page at --More--)", " "), ("q (quit a pager)", "q"),
        ("Esc", "\x1b"), ("Enter", "\r")]


class CommandInput(QLineEdit):
    """The command box, with the commands sent before on Up and Down. Ctrl+C (unless text is selected, which it
    copies), Ctrl+Z (when the box is empty) and Ctrl+Shift+6 go to the sessions as keys."""
    keys = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.history = []
        self.position = 0

    def remember(self, text):
        if text and (not self.history or self.history[-1] != text):
            self.history = (self.history + [text])[-HISTORY_LIMIT:]
        self.position = len(self.history)

    def event(self, event):
        # Ctrl+C and Ctrl+Z are also shortcuts (copy, undo); take them first when they're meant for the sessions
        if event.type() == event.ShortcutOverride and self.session_key(event):
            event.accept()
            return True
        return super().event(event)

    def session_key(self, event):
        """The control character a key press sends to the sessions, or "" if it's for the box."""
        modifiers = event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier)
        if modifiers == Qt.ControlModifier and event.key() == Qt.Key_C and not self.hasSelectedText():
            return "\x03"
        if modifiers == Qt.ControlModifier and event.key() == Qt.Key_Z and not self.text():
            return "\x1a"
        if modifiers & Qt.ControlModifier and event.key() in (Qt.Key_6, Qt.Key_AsciiCircum):
            return "\x1e"
        return ""

    def keyPressEvent(self, event):
        key = self.session_key(event)
        if key:
            self.keys.emit(key)
            return
        if event.key() in (Qt.Key_Up, Qt.Key_Down) and self.history:
            step = -1 if event.key() == Qt.Key_Up else 1
            self.position = max(0, min(len(self.history), self.position + step))
            self.setText(self.history[self.position] if self.position < len(self.history) else "")
            return
        super().keyPressEvent(event)


class SendBar(QFrame):
    """Under a window's sessions. Where to send is shared by every window's bar (it's the page's), as is Type in
    All, so the bars always agree."""

    def __init__(self, page, tabs):
        super().__init__()
        self.page = page
        self.tabs = tabs
        self.setObjectName("sendBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(QLabel("Send to:"))
        self.scope_combo = QComboBox()
        for key, label in SCOPES:
            self.scope_combo.addItem(label, key)
        self.scope_combo.setToolTip("Which sessions get the command (sessions left out from their tab's menu and "
                                    "disconnected ones never do)")
        layout.addWidget(self.scope_combo)
        self.command_input = CommandInput()
        self.command_input.setPlaceholderText("Command to send (Enter sends it, with each session's Enter; "
                                              "Up/Down for earlier ones; Ctrl+C interrupts all)")
        layout.addWidget(self.command_input, 1)
        self.send_button = QPushButton("Send")
        layout.addWidget(self.send_button)
        keys_button = QToolButton()
        keys_button.setText("Keys")
        keys_button.setPopupMode(QToolButton.InstantPopup)
        keys_button.setToolTip("Send a key to all of them, such as Ctrl+C or Space at --More--")
        keys_menu = QMenu(keys_button)
        for label, text in KEYS:
            keys_menu.addAction(label, lambda text=text, label=label: self.send_keys(text, label.split(" (")[0]))
        keys_button.setMenu(keys_menu)
        layout.addWidget(keys_button)
        self.mirror_box = QCheckBox("Type in all")
        self.mirror_box.setToolTip("While ticked, what you type in any of these sessions goes to all of them")
        layout.addWidget(self.mirror_box)
        self.status_label = QLabel()
        layout.addWidget(self.status_label)
        close_button = QToolButton()
        close_button.setText("×")
        close_button.setAutoRaise(True)
        close_button.setToolTip("Close Send to All (also stops Type in All)")
        layout.addWidget(close_button)

        self.command_input.returnPressed.connect(self.send)
        self.command_input.keys.connect(lambda text: self.send_keys(text, {"\x03": "Ctrl+C", "\x1a": "Ctrl+Z"}
                                                                    .get(text, "Ctrl+Shift+6")))
        self.send_button.clicked.connect(self.send)
        self.scope_combo.activated.connect(lambda _: self.page.set_broadcast(scope=self.scope_combo.currentData()))
        self.mirror_box.clicked.connect(lambda checked: self.page.set_broadcast(mirror=checked))
        close_button.clicked.connect(lambda: self.tabs.show_send_bar(False))
        self.setVisible(False)  # Filled in by refresh() when shown

    def targets(self):
        return self.page.broadcast_targets(self.page.broadcast_scope, self.tabs)

    def send(self):
        text = self.command_input.text()
        count = self.page.send_to_all(text, self.page.broadcast_scope, self.tabs)
        self.command_input.remember(text)
        self.command_input.clear()
        self.refresh(f"Sent to {count} session{'' if count == 1 else 's'}." if count else
                     "No connected sessions to send to.")

    def send_keys(self, text, label):
        count = self.page.send_keys_to_all(text, self.page.broadcast_scope, self.tabs)
        self.refresh(f"Sent {label} to {count} session{'' if count == 1 else 's'}." if count else
                     "No connected sessions to send to.")
        self.command_input.setFocus()

    def refresh(self, message=""):
        """Match the page's settings, and say how many sessions a command would reach."""
        index = self.scope_combo.findData(self.page.broadcast_scope)
        self.scope_combo.setCurrentIndex(max(0, index))
        self.mirror_box.setChecked(self.page.mirror_typing)
        count = len(self.targets())
        if not message:
            message = f"{count} session{'' if count == 1 else 's'}"
            if self.page.mirror_typing:
                message = f"Typing goes to {message}"
        self.status_label.setText(message)
        warning = self.page.mirror_typing
        self.setStyleSheet(f"#sendBar {{ background: {COLORS['warning_background']}; "
                           f"border-top: 2px solid {COLORS['warning']}; }}" if warning else
                           f"#sendBar {{ border-top: 1px solid {COLORS['border']}; }}")
        self.status_label.setStyleSheet(f"color: {COLORS['warning'] if warning else COLORS['muted']};")

    def focus(self):
        self.refresh()
        self.command_input.setFocus()
