"""Choosing a session folder to move things into, with a way to make a new one on the spot."""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QInputDialog, QLabel, QPushButton, QTreeWidget, \
    QTreeWidgetItem, QVBoxLayout

from ..terminal.sessions import normalize_folder

PATH_ROLE = Qt.UserRole
TOP_LEVEL = "(Top level)"


class FolderPickerDialog(QDialog):
    """Pick a folder. excluded folders (and everything inside them) can't be chosen: a folder can't move into itself.
    New folders made here only exist once something is moved into them."""

    def __init__(self, parent, folders, what, current="", excluded=()):
        super().__init__(parent)
        self.setWindowTitle("Move To")
        self.setMinimumSize(360, 420)
        self.excluded = [normalize_folder(folder) for folder in excluded]
        layout = QVBoxLayout(self)
        label = QLabel(f"Move {what} to:")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        layout.addWidget(self.tree, 1)
        new_button = QPushButton("New Folder...")
        new_button.setToolTip("Make a folder inside the selected one")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.addButton(new_button, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Move")

        self.root = QTreeWidgetItem([TOP_LEVEL])
        self.root.setData(0, PATH_ROLE, "")
        self.tree.addTopLevelItem(self.root)
        self.items = {"": self.root}
        for folder in sorted(folders, key=str.lower):
            self.add_folder(folder)
        self.tree.expandAll()
        self.tree.setCurrentItem(self.items.get(normalize_folder(current), self.root))

        self.tree.currentItemChanged.connect(self.update_ok)
        self.tree.itemDoubleClicked.connect(lambda: self.ok_button.isEnabled() and self.accept())
        new_button.clicked.connect(self.new_folder)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.update_ok()

    def is_excluded(self, path):
        return any(path == folder or path.startswith(folder + "/") for folder in self.excluded)

    def add_folder(self, path):
        if path in self.items:
            return self.items[path]
        parent_path, _, name = path.rpartition("/")
        parent = self.add_folder(parent_path)
        item = QTreeWidgetItem([name])
        item.setData(0, PATH_ROLE, path)
        if self.is_excluded(path):
            item.setDisabled(True)
            item.setToolTip(0, "A folder can't be moved into itself.")
        parent.addChild(item)
        self.items[path] = item
        return item

    @property
    def folder(self):
        item = self.tree.currentItem()
        return item.data(0, PATH_ROLE) if item is not None else ""

    def update_ok(self):
        item = self.tree.currentItem()
        self.ok_button.setEnabled(item is not None and not self.is_excluded(self.folder))

    def new_folder(self):
        parent = self.folder if not self.is_excluded(self.folder) else ""
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:" + (f" (inside {parent})" if parent else ""))
        name = normalize_folder(name)
        if not ok or not name:
            return
        item = self.add_folder(f"{parent}/{name}" if parent else name)
        font = QFont(item.font(0))
        font.setItalic(True)  # New: made when something moves into it
        item.setFont(0, font)
        self.tree.expandAll()
        self.tree.setCurrentItem(item)
