"""The two sides of an SCP tab: this computer's files and the server's, as sortable lists with a path bar.

The panes only show files and say what the user asked for (open, drop, a key command); the SCP view does the work.
Files dragged from one pane to the other, or from Windows Explorer, arrive through `dropped`.
"""
import ctypes
import json
import os
import string
import time

from PyQt5.QtCore import QEvent, QMimeData, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QDrag, QFont, QKeySequence
from PyQt5.QtWidgets import QAbstractItemView, QFileIconProvider, QHBoxLayout, QHeaderView, QLabel, QLineEdit, \
    QPushButton, QShortcut, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..terminal.files import Entry, parent as remote_parent
from .common import format_size, run_in_background
from .theme import COLORS

MIME_TYPE = "application/x-nomad-files"
NAME, SIZE, MODIFIED, PERMISSIONS, OWNER = range(5)
ENTRY_ROLE = Qt.UserRole
# Keys the panes handle themselves while they have focus, even where the window uses them (F5 refreshes network
# settings, Ctrl+R runs a report): the same keys as WinSCP's commander
COMMANDS = {
    (Qt.Key_F5, Qt.NoModifier): "copy", (Qt.Key_F2, Qt.NoModifier): "rename", (Qt.Key_F4, Qt.NoModifier): "edit",
    (Qt.Key_F7, Qt.NoModifier): "mkdir", (Qt.Key_F8, Qt.NoModifier): "delete",
    (Qt.Key_Delete, Qt.NoModifier): "delete", (Qt.Key_R, Qt.ControlModifier): "refresh",
    (Qt.Key_Return, Qt.AltModifier): "properties", (Qt.Key_Enter, Qt.AltModifier): "properties",
    (Qt.Key_Backspace, Qt.NoModifier): "up", (Qt.Key_Up, Qt.AltModifier): "back",
    (Qt.Key_L, Qt.ControlModifier): "path",
    (Qt.Key_H, Qt.ControlModifier | Qt.AltModifier): "hidden",
    (Qt.Key_F, Qt.ControlModifier | Qt.ShiftModifier): "filter",
}


def format_time(mtime):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""


class FileItem(QTreeWidgetItem):
    """One row. Folders sort before files whichever way a column is sorted, and ".." stays on top."""

    def __init__(self, entry, icon=None, up=False):
        super().__init__()
        self.entry, self.up = entry, up
        self.setText(NAME, ".." if up else entry.name)
        if icon is not None:
            self.setIcon(NAME, icon)
        self.setData(NAME, ENTRY_ROLE, entry)
        if up:
            return
        self.setText(SIZE, "" if entry.is_dir else format_size(entry.size))
        self.setTextAlignment(SIZE, Qt.AlignRight | Qt.AlignVCenter)
        self.setText(MODIFIED, format_time(entry.mtime))
        self.setText(PERMISSIONS, entry.permissions if entry.mode or entry.owner else "")
        self.setText(OWNER, f"{entry.owner}:{entry.group}" if entry.owner else "")
        tip = entry.path + (f"\n→ {entry.link_target}" if entry.link_target else "")
        self.setToolTip(NAME, tip)
        if entry.name.startswith("."):
            self.setForeground(NAME, QColor(COLORS["muted"]))
        if entry.is_link:
            font = QFont(self.font(NAME))
            font.setItalic(True)
            self.setFont(NAME, font)

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else NAME
        ascending = tree is None or tree.header().sortIndicatorOrder() == Qt.AscendingOrder
        # Keep ".." and folders first in both directions
        mine = (not self.up, not self.entry.is_dir)
        theirs = (not other.up, not other.entry.is_dir)
        if mine != theirs:
            return mine < theirs if ascending else mine > theirs
        if column == SIZE:
            return (self.entry.size, self.entry.name.lower()) < (other.entry.size, other.entry.name.lower())
        if column == MODIFIED:
            return self.entry.mtime < other.entry.mtime
        if column in (PERMISSIONS, OWNER):
            return self.text(column) < other.text(column)
        return self.entry.name.lower() < other.entry.name.lower()


class FileList(QTreeWidget):
    command = pyqtSignal(str)
    dropped = pyqtSignal(object, object)  # Payload ({"source", "view", "paths"}), target folder Entry or None

    def __init__(self, side, view_id, parent=None):
        super().__init__(parent)
        self.side, self.view_id = side, view_id
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setHeaderLabels(["Name", "Size", "Modified", "Permissions", "Owner"])
        header = self.header()
        header.setStretchLastSection(True)  # Spare room goes to the last column; too little, and the list scrolls
        header.setMinimumSectionSize(40)
        width = self.fontMetrics().horizontalAdvance
        header.setSectionResizeMode(NAME, QHeaderView.Interactive)
        header.resizeSection(NAME, max(240, width("M") * 18))
        for column, sample in ((SIZE, "000.0 MB"), (MODIFIED, "0000-00-00 00:00"), (PERMISSIONS, "drwxr-xr-x"),
                               (OWNER, "root:root")):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, width(sample) + 24)
        self.sortByColumn(NAME, Qt.AscendingOrder)

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride:
            key = (event.key(), int(event.modifiers()) & ~int(Qt.KeypadModifier))
            if (key[0], Qt.KeyboardModifiers(key[1])) in COMMANDS:
                event.accept()  # Ours while a pane has focus
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        name = COMMANDS.get((event.key(), Qt.KeyboardModifiers(int(event.modifiers()) & ~int(Qt.KeypadModifier))))
        if name is not None:
            self.command.emit(name)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.currentItem() is not None:
            self.itemActivated.emit(self.currentItem(), 0)
            return
        super().keyPressEvent(event)

    def selected_entries(self):
        return [item.entry for item in self.selectedItems() if isinstance(item, FileItem) and not item.up]

    # ----------------------------------------------------------------- Dragging out

    def startDrag(self, _actions):
        entries = self.selected_entries()
        if not entries:
            return
        data = QMimeData()
        payload = {"source": self.side, "view": self.view_id, "paths": [entry.path for entry in entries],
                   "dirs": [entry.path for entry in entries if entry.is_dir]}
        data.setData(MIME_TYPE, json.dumps(payload).encode())
        if self.side == "local":  # So they can be dropped into Explorer or another program too
            data.setUrls([QUrl.fromLocalFile(entry.path) for entry in entries])
        drag = QDrag(self)
        drag.setMimeData(data)
        drag.exec_(Qt.CopyAction)

    # ----------------------------------------------------------------- Dropping in

    def payload(self, mime):
        if mime.hasFormat(MIME_TYPE):
            try:
                return json.loads(bytes(mime.data(MIME_TYPE)).decode())
            except ValueError:
                return None
        if self.side == "remote" and mime.hasUrls():  # From Windows Explorer
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            if paths:
                return {"source": "local", "view": None, "paths": paths,
                        "dirs": [path for path in paths if os.path.isdir(path)]}
        return None

    def accepts(self, payload, target):
        if payload is None:
            return False
        if payload["source"] != self.side:
            return payload["view"] in (None, self.view_id)  # Only between the panes of one tab
        # Within the remote pane: moving onto a folder
        return self.side == "remote" and target is not None and target.path not in payload["paths"]

    def target_at(self, position):
        item = self.itemAt(position)
        if isinstance(item, FileItem) and item.entry.is_dir and not item.up:
            return item.entry
        return None

    def dragEnterEvent(self, event):
        if self.payload(event.mimeData()) is not None:
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        payload = self.payload(event.mimeData())
        target = self.target_at(event.pos())
        if self.accepts(payload, target):
            event.setDropAction(Qt.MoveAction if payload["source"] == self.side else Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        payload = self.payload(event.mimeData())
        target = self.target_at(event.pos())
        if not self.accepts(payload, target):
            event.ignore()
            return
        event.setDropAction(Qt.CopyAction)  # Qt mustn't remove anything itself
        event.accept()
        self.dropped.emit(payload, target)


class FilePane(QWidget):
    """A path bar, the list, and a status line. Subclasses list folders."""
    navigated = pyqtSignal(str)  # The folder now shown
    copy_text, copy_tip = "", ""  # The button that copies the selection to the other side
    command_requested = pyqtSignal(str)  # copy, rename, edit, mkdir, delete, properties: for the view
    title = ""

    def __init__(self, side, view_id, parent=None):
        super().__init__(parent)
        self.side = side
        self.path = ""
        self.history = []
        self.show_hidden = True
        self.entries = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        bar = QHBoxLayout()
        bar.setSpacing(2)
        label = self.title_label = QLabel(self.title)
        label.setStyleSheet(f"color: {COLORS['muted']}; font-weight: bold;")
        self.path_input = QLineEdit()
        self.path_input.setToolTip("The folder shown. Type a path and press Enter to go there.")
        self.up_button = self.tool_button("↑", "Up a folder (Backspace)")
        self.back_button = self.tool_button("←", "Back (Alt+Up)")
        self.home_button = self.tool_button("⌂", "Home folder")
        self.refresh_button = self.tool_button("⟳", "Refresh (Ctrl+R)")
        bar.addWidget(label)
        bar.addWidget(self.path_input, 1)
        for button in (self.back_button, self.up_button, self.home_button, self.refresh_button):
            bar.addWidget(button)
        layout.addLayout(bar)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter names (Ctrl+Shift+F)")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.setVisible(False)
        layout.addWidget(self.filter_input)
        self.list = FileList(side, view_id)
        layout.addWidget(self.list, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {COLORS['muted']};")
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        self.copy_button = QPushButton(self.copy_text)
        self.copy_button.setToolTip(self.copy_tip)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(lambda: self.command_requested.emit("copy"))
        status_row.addWidget(self.copy_button)
        layout.addLayout(status_row)
        self.icons = QFileIconProvider()
        self.request = 0  # Numbers each listing, so a slow one that finishes late doesn't replace a newer one

        self.path_input.returnPressed.connect(lambda: self.go(self.normalize(self.path_input.text())))
        self.up_button.clicked.connect(self.go_up)
        self.back_button.clicked.connect(self.go_back)
        self.home_button.clicked.connect(self.go_home)
        self.refresh_button.clicked.connect(self.refresh)
        self.list.itemActivated.connect(self.on_activated)
        self.list.itemSelectionChanged.connect(self.update_status)
        self.list.command.connect(self.on_command)
        path_shortcut = QShortcut(QKeySequence("Ctrl+L"), self, context=Qt.WidgetWithChildrenShortcut)
        path_shortcut.activated.connect(self.focus_path)
        self.filter_input.textChanged.connect(self.apply_filter)

    @staticmethod
    def tool_button(text, tip):
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        return button

    # ----------------------------------------------------------------- Subclasses

    def normalize(self, path):
        return path

    def parent_of(self, path):
        raise NotImplementedError

    def home(self):
        raise NotImplementedError

    def list_folder(self, path, done):
        """List a folder and call done(path, entries) or done(path, error message)."""
        raise NotImplementedError

    def open_file(self, entry):
        raise NotImplementedError

    # ----------------------------------------------------------------- Navigating

    def go(self, path, remember=True, select=None):
        if path is None:
            return
        self.request += 1
        request = self.request

        def done(listed, result):
            if request != self.request:
                return  # The user went somewhere else while this was listing
            if isinstance(result, str):
                self.status.setText(result)
                self.status.setStyleSheet(f"color: {COLORS['error']};")
                self.path_input.setText(self.path)
                return
            if remember and self.path and listed != self.path:
                self.history.append(self.path)
                del self.history[:-50]
            self.path = listed
            self.show_entries(result, select)
            self.navigated.emit(listed)

        self.list_folder(path, done)

    def refresh(self, select=None):
        if select is None:
            select = [entry.name for entry in self.list.selected_entries()]
        self.go(self.path, remember=False, select=select)

    def go_up(self):
        current = self.path
        parent = self.parent_of(current)
        if parent != current:
            self.go(parent, select=[self.name_in_parent(current)])

    def name_in_parent(self, path):
        return path.rstrip("/").rpartition("/")[2]

    def go_back(self):
        if self.history:
            self.go(self.history.pop(), remember=False)

    def go_home(self):
        self.go(self.home())

    def on_activated(self, item, _column):
        if not isinstance(item, FileItem):
            return
        if item.up:
            self.go_up()
        elif item.entry.is_dir:
            self.go(item.entry.path)
        else:
            self.open_file(item.entry)

    def on_command(self, name):
        if name == "up":
            self.go_up()
        elif name == "back":
            self.go_back()
        elif name == "refresh":
            self.refresh()
        elif name == "hidden":
            self.show_hidden = not self.show_hidden
            self.refresh()
        elif name == "filter":
            self.filter_input.setVisible(True)
            self.filter_input.setFocus()
        elif name == "path":
            self.focus_path()
        else:
            self.command_requested.emit(name)

    def focus_path(self):
        self.path_input.setFocus()
        self.path_input.selectAll()

    # ----------------------------------------------------------------- Showing a folder

    def show_entries(self, entries, select=None):
        self.entries = entries
        select = set(select or ())
        self.path_input.setText(self.path or "This PC")
        self.list.setSortingEnabled(False)
        self.list.clear()
        if self.parent_of(self.path) != self.path:
            self.list.addTopLevelItem(FileItem(Entry("..", self.parent_of(self.path), is_dir=True), up=True))
        current = None
        for entry in entries:
            if not self.show_hidden and entry.name.startswith("."):
                continue
            item = FileItem(entry, self.icon_for(entry))
            self.list.addTopLevelItem(item)
            if entry.name in select:
                item.setSelected(True)
                current = current or item
        self.list.setSortingEnabled(True)
        if current is not None:
            self.list.setCurrentItem(current, 0, self.list.selectionModel().NoUpdate)
            self.list.scrollToItem(current)
        elif self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0), 0, self.list.selectionModel().NoUpdate)
        self.apply_filter()
        self.status.setStyleSheet(f"color: {COLORS['muted']};")
        self.update_status()

    def icon_for(self, entry):
        return self.icons.icon(QFileIconProvider.Folder if entry.is_dir else QFileIconProvider.File)

    def apply_filter(self):
        words = self.filter_input.text().lower().split()
        for index in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(index)
            name = item.text(NAME).lower()
            item.setHidden(bool(words) and not item.up and not all(word in name for word in words))
        if not words and self.filter_input.isVisible() and not self.filter_input.hasFocus():
            self.filter_input.setVisible(False)

    def update_status(self):
        selected = self.list.selected_entries()
        files = [entry for entry in self.entries if not entry.is_dir]
        folders = len(self.entries) - len(files)
        text = f"{folders} folder{'' if folders == 1 else 's'}, {len(files)} file{'' if len(files) == 1 else 's'} " \
               f"({format_size(sum(entry.size for entry in files))})"
        if selected:
            size = sum(entry.size for entry in selected if not entry.is_dir)
            text += f"  ·  {len(selected)} selected ({format_size(size)})"
        self.status.setText(text)
        self.copy_button.setEnabled(bool(selected) and self.list.isEnabled())

    def selected_entries(self):
        return self.list.selected_entries()


def read_folder(path):
    """Entries for a local folder (raises OSError if it can't be read)."""
    entries = []
    with os.scandir(path) as items:
        for item in items:
            try:
                info = item.stat()
                is_dir = item.is_dir()
            except OSError:
                continue
            entries.append(Entry(item.name, item.path, is_dir=is_dir, size=0 if is_dir else info.st_size,
                                 mtime=info.st_mtime))
    return entries


class LocalPane(FilePane):
    """This computer. "" is the list of drives."""
    title = "Local"
    copy_text, copy_tip = "Upload ▶", "Upload the selected files and folders to the remote folder (F5)"

    def __init__(self, view_id, parent=None):
        super().__init__("local", view_id, parent)
        self.list.setColumnHidden(PERMISSIONS, True)
        self.list.setColumnHidden(OWNER, True)

    def normalize(self, path):
        path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
        if path.lower() in ("", "this pc"):
            return ""
        if len(path) == 2 and path[1] == ":":
            path += "\\"
        return os.path.normpath(path)

    def parent_of(self, path):
        stripped = path.rstrip("\\/")
        drive, rest = os.path.splitdrive(stripped)
        if not rest:
            return ""  # From a drive's (or share's) root: the list of drives
        parent = os.path.dirname(stripped)
        return drive + "\\" if parent == drive else parent

    def name_in_parent(self, path):
        stripped = path.rstrip("\\/")
        return os.path.basename(stripped) or stripped

    def home(self):
        return os.path.expanduser("~")

    def list_folder(self, path, done):
        if path == "":
            # Windows' list of drive letters: asking each drive if it exists can hang on a disconnected network drive
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            drives = [f"{letter}:\\" for index, letter in enumerate(string.ascii_uppercase) if mask >> index & 1]
            done("", [Entry(drive[:2], drive, is_dir=True) for drive in drives])
            return
        self.status.setText("Listing...")
        # Off the UI thread: a slow network share or a drive that's gone mustn't freeze the window
        run_in_background(lambda: read_folder(path), lambda entries: done(path, entries),
                          lambda error: done(path, f"Can't open {path}: {getattr(error, 'strerror', None) or error}"))

    def icon_for(self, entry):
        if not self.path:
            return self.icons.icon(QFileIconProvider.Drive)
        return super().icon_for(entry)

    def open_file(self, entry):
        try:
            from ..system import open_path
            open_path(entry.path)
        except OSError as error:
            self.status.setText(f"Couldn't open {entry.name}: {error.strerror or error}")


class RemotePane(FilePane):
    """The server, listed through the view's browsing worker."""
    title = "Remote"
    copy_text, copy_tip = "◀ Download", "Download the selected files and folders to the local folder (F5)"

    def __init__(self, view_id, parent=None):
        super().__init__("remote", view_id, parent)
        self.worker = None
        self.home_path = "/"
        self.open_requested = None  # Set by the view: open_requested(entry)

    def normalize(self, path):
        path = path.strip()
        if path.startswith("~"):
            path = self.home_path.rstrip("/") + path[1:]
        if not path.startswith("/"):
            path = (self.path.rstrip("/") + "/" + path) if self.path else "/" + path
        parts = []
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(part)
        return "/" + "/".join(parts)

    def parent_of(self, path):
        return remote_parent(path) if path else "/"

    def home(self):
        return self.home_path

    def list_folder(self, path, done):
        if self.worker is None:
            done(path, "Not connected.")
            return
        self.status.setText("Listing...")
        self.worker.submit(lambda fs: fs.listdir(path), lambda entries: done(path, entries),
                           lambda message: done(path, message))

    def open_file(self, entry):
        if self.open_requested is not None:
            self.open_requested(entry)

    def set_title(self, text, warning=False):
        self.title_label.setText(text)
        self.title_label.setStyleSheet(f"color: {COLORS['warning' if warning else 'muted']}; font-weight: bold;")

    def set_connected(self, connected):
        for widget in (self.list, self.path_input, self.up_button, self.back_button, self.home_button,
                       self.refresh_button):
            widget.setEnabled(connected)
        if not connected:
            self.status.setText("Not connected.")
            self.copy_button.setEnabled(False)
