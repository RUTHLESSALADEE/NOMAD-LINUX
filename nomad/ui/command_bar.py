"""Command buttons under the terminal sessions: saved commands (or blocks of configuration) sent with one click."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, \
    QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ..terminal.commands import CommandButton
from .common import set_hint
from .theme import COLORS, monospace_font


class CommandDialog(QDialog):
    """Add or edit a command button."""

    def __init__(self, parent, button, title="Command Button"):
        super().__init__(parent)
        self.button = button
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_input = QLineEdit(button.name)
        self.name_input.setPlaceholderText("The button's label, such as Interfaces")
        self.text_input = QPlainTextEdit(button.text)
        self.text_input.setFont(monospace_font())
        self.text_input.setPlaceholderText("show ip interface brief\n\nOr a block of configuration, one command per "
                                           "line:\nconfigure terminal\ninterface Gi1/0/5\n description Printer\nend")
        self.enter_check = QCheckBox("Press Enter after the last line")
        self.enter_check.setChecked(button.press_enter)
        self.enter_check.setToolTip("Untick for a command you finish typing yourself, such as \"ping \"")
        form.addRow("Name:", self.name_input)
        form.addRow("Commands:", self.text_input)
        form.addRow("", self.enter_check)
        layout.addLayout(form)
        hint = QLabel("Each line is sent with the session's Enter. Sessions with a line delay (in their settings) send "
                      "the lines one at a time. Click the button to send to the session you're in, or right-click it "
                      "> Send to All.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(hint)
        self.error_label = QLabel()
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        name = self.name_input.text().strip()
        text = self.text_input.toPlainText().rstrip("\r\n")
        if not name:
            set_hint(self.error_label, "Give the button a name.", "error")
            return
        if not text.strip():
            set_hint(self.error_label, "Enter the command (or commands) to send.", "error")
            return
        self.button = CommandButton(name, text, self.enter_check.isChecked(), self.button.id)
        self.accept()


class CommandBar(QFrame):
    """A row of command buttons. Clicking sends to the session you're in, or to every session Send to All reaches
    while Type in All is on (so a click goes where your typing goes)."""

    def __init__(self, page, tabs):
        super().__init__()
        self.page = page
        self.tabs = tabs
        self.store = page.commands
        self.setObjectName("commandBar")
        self.setStyleSheet(f"#commandBar {{ border-top: 1px solid {COLORS['border']}; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.row = QWidget()
        self.row_layout = QHBoxLayout(self.row)
        self.row_layout.setContentsMargins(0, 0, 0, 0)
        self.row_layout.setSpacing(4)
        self.scroll.setWidget(self.row)
        layout.addWidget(self.scroll, 1)
        self.status_label = QLabel()
        self.status_label.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(self.status_label)
        add_button = QToolButton()
        add_button.setText("+ Add")
        add_button.setAutoRaise(True)
        add_button.setToolTip("Add a command button")
        add_button.clicked.connect(self.add)
        layout.addWidget(add_button)
        self.store.listeners.append(self.fill)
        self.destroyed.connect(lambda: self.store.listeners.remove(self.fill) if self.fill in self.store.listeners
                               else None)
        self.fill()
        self.setVisible(False)
        self.setMaximumHeight(self.sizeHint().height() + 12)

    def fill(self):
        while self.row_layout.count():
            item = self.row_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not self.store.buttons:
            hint = QLabel("No command buttons yet: + Add makes one (a command, or a block of configuration).")
            hint.setStyleSheet(f"color: {COLORS['muted']};")
            self.row_layout.addWidget(hint)
        for button in self.store.buttons:
            widget = QToolButton()
            widget.setText(button.name)
            widget.setToolTip(button.text + ("" if button.press_enter else "\n(without pressing Enter)"))
            widget.setContextMenuPolicy(Qt.CustomContextMenu)
            widget.clicked.connect(lambda _, button_id=button.id: self.send(button_id))
            widget.customContextMenuRequested.connect(
                lambda position, widget=widget, button_id=button.id: self.show_menu(button_id,
                                                                                    widget.mapToGlobal(position)))
            self.row_layout.addWidget(widget)
        self.row_layout.addStretch()

    # ----------------------------------------------------------------- Sending

    def send(self, button_id, to_all=None):
        """Send a button's commands: to the session you're in, or to all (to_all None: all while Type in All)."""
        button = self.store.get(button_id)
        if button is None:
            return
        if to_all is None:
            to_all = self.page.mirror_typing
        if to_all:
            targets = self.page.broadcast_targets(self.page.broadcast_scope, self.tabs)
        else:
            current = self.tabs.currentWidget()
            targets = [current] if current is not None and hasattr(current, "send_block") else []
        count = sum(bool(view.send_block(button.text, button.press_enter)) for view in targets)
        if not count:
            set_hint(self.status_label, "Not connected." if not to_all else "No connected sessions to send to.",
                     "warning")
        elif to_all:
            set_hint(self.status_label, f"Sent {button.name} to {count} session{'' if count == 1 else 's'}.", "info")
        else:
            self.status_label.clear()
        if not to_all and targets:
            targets[0].focus_target().setFocus()  # Carry on typing in the session

    # ----------------------------------------------------------------- Editing

    def show_menu(self, button_id, global_position):
        button = self.store.get(button_id)
        if button is None:
            return
        menu = QMenu(self)
        menu.addAction("Send to This Session", lambda: self.send(button_id, to_all=False))
        menu.addAction("Send to All", lambda: self.send(button_id, to_all=True))
        menu.addSeparator()
        menu.addAction("Edit...", lambda: self.edit(button))
        menu.addAction("Duplicate", lambda: self.store.put(CommandButton(f"{button.name} (copy)", button.text,
                                                                         button.press_enter)))
        menu.addAction("Move Left", lambda: self.store.move(button_id, -1))
        menu.addAction("Move Right", lambda: self.store.move(button_id, 1))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.delete(button))
        menu.exec_(global_position)

    def add(self):
        dialog = CommandDialog(self, CommandButton(""), "Add Command Button")
        if dialog.exec_():
            self.store.put(dialog.button)

    def edit(self, button):
        dialog = CommandDialog(self, button, "Edit Command Button")
        if dialog.exec_():
            self.store.put(dialog.button)

    def delete(self, button):
        reply = QMessageBox.question(self, "Delete Button", f"Delete the {button.name} button?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.store.delete(button.id)
