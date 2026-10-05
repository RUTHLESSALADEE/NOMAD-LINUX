"""The transfer queue under an SCP tab's panes: each file's progress and speed, pause, cancel and retry."""
import os
import posixpath
import time

from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, \
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..terminal.files import ASK, CANCELLED, DONE, FAILED, NEWER, OVERWRITE, PAUSED, QUEUED, RENAME, RUNNING, \
    SKIP, SKIPPED, UPLOAD
from .common import ColumnFitter, format_size
from .theme import COLORS

DIRECTION, NAME, TARGET, SIZE, PROGRESS, SPEED, STATUS = range(7)
STATE_COLORS = {DONE: "success", FAILED: "error", SKIPPED: "muted", CANCELLED: "muted", PAUSED: "warning",
                RUNNING: "accent", QUEUED: "text"}
POLICIES = [("Ask", ASK), ("Overwrite", OVERWRITE), ("Overwrite if newer", NEWER), ("Skip", SKIP),
            ("Rename the new file", RENAME)]
FINISHED = (DONE, FAILED, SKIPPED, CANCELLED)
STALL_SECONDS = 10  # A running transfer with no data for this long shows as stalled


def format_speed(rate):
    return f"{format_size(rate)}/s" if rate else ""


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60}:{seconds % 60:02d}"


class QueuePanel(QWidget):
    pause_toggled = pyqtSignal(bool)
    cancel_requested = pyqtSignal(list)  # Transfers
    retry_requested = pyqtSignal(list)
    policy_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = {}  # Transfer id: QTreeWidgetItem
        self.transfers = {}  # Transfer id: Transfer
        self.samples = {}  # Transfer id: (time, bytes done) at the last speed update
        self.speeds = {}
        self.progress_seen = {}  # Transfer id: (bytes done, when that changed), to notice a stalled transfer
        self.stall_timer = QTimer(self)
        self.stall_timer.timeout.connect(self.check_stalls)
        self.stall_timer.start(1000)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        title = QLabel("Transfers")
        title.setStyleSheet(f"color: {COLORS['muted']}; font-weight: bold;")
        self.summary = QLabel()
        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("Pause the queue. A paused SFTP transfer continues where it stopped.")
        self.retry_button = QPushButton("Retry")
        self.retry_button.setToolTip("Try the selected failed or cancelled transfers again (all of them if none are "
                                     "selected). SFTP transfers resume where they stopped.")
        self.clear_button = QPushButton("Clear Finished")
        self.policy = QComboBox()
        for label, value in POLICIES:
            self.policy.addItem(label, value)
        self.policy.setToolTip("What to do when a file being copied already exists")
        self.verify = QCheckBox("Verify")
        self.verify.setToolTip("After each file, compare its SHA-256 on both sides (worked out on the server with "
                               "sha256sum where it can)")
        bar.addWidget(title)
        bar.addWidget(self.summary, 1)
        bar.addWidget(QLabel("If it exists:"))
        bar.addWidget(self.policy)
        bar.addWidget(self.verify)
        bar.addWidget(self.pause_button)
        bar.addWidget(self.retry_button)
        bar.addWidget(self.clear_button)
        layout.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.setHeaderLabels(["", "File", "To", "Size", "Progress", "Speed", "Status"])
        header = self.tree.header()
        header.setStretchLastSection(True)
        ColumnFitter(self.tree, only=(DIRECTION, SIZE, PROGRESS, SPEED))
        header.resizeSection(NAME, 220)
        header.resizeSection(TARGET, 260)
        layout.addWidget(self.tree, 1)

        self.pause_button.toggled.connect(self.on_pause)
        self.retry_button.clicked.connect(self.retry)
        self.clear_button.clicked.connect(self.clear_finished)
        self.policy.currentIndexChanged.connect(lambda _: self.policy_changed.emit(self.policy.currentData()))
        self.tree.customContextMenuRequested.connect(self.show_menu)
        self.update_summary()

    @property
    def verify_algorithm(self):
        return "SHA-256" if self.verify.isChecked() else ""

    def on_pause(self, paused):
        self.pause_button.setText("Resume" if paused else "Pause")
        self.pause_toggled.emit(paused)

    # ----------------------------------------------------------------- Rows

    def add(self, transfers, after=None):
        """Add rows for transfers (after another transfer's row, for the files a folder expands into)."""
        index = self.tree.indexOfTopLevelItem(self.items[after.id]) + 1 if after is not None and \
            after.id in self.items else self.tree.topLevelItemCount()
        for offset, transfer in enumerate(transfers):
            item = QTreeWidgetItem()
            item.setData(0, Qt.UserRole, transfer.id)
            self.items[transfer.id] = item
            self.transfers[transfer.id] = transfer
            self.tree.insertTopLevelItem(index + offset, item)
            self.update(transfer)
        self.update_summary()

    def update(self, transfer):
        item = self.items.get(transfer.id)
        if item is None:
            return
        upload = transfer.direction == UPLOAD
        item.setText(DIRECTION, "↑" if upload else "↓")
        item.setToolTip(DIRECTION, "Upload" if upload else "Download")
        item.setText(NAME, transfer.name + ("/" if transfer.is_dir else ""))
        item.setToolTip(NAME, transfer.source)
        folder = posixpath.dirname(transfer.remote) if upload else os.path.dirname(transfer.local)
        item.setText(TARGET, folder)
        item.setToolTip(TARGET, transfer.target)
        item.setText(SIZE, format_size(transfer.size) if transfer.size or not transfer.is_dir else "")
        if transfer.state == RUNNING and transfer.size:
            item.setText(PROGRESS, f"{100 * transfer.done // transfer.size}%")
        elif transfer.state == DONE:
            item.setText(PROGRESS, "100%")
        elif transfer.state in (PAUSED, FAILED, CANCELLED) and transfer.size and transfer.done:
            item.setText(PROGRESS, f"{100 * transfer.done // transfer.size}%")
        else:
            item.setText(PROGRESS, "")
        item.setText(SPEED, self.speed_text(transfer))
        status = transfer.state
        if transfer.message and transfer.message != transfer.state:
            # While running, the message says what's happening ("Checking SHA-256..."); otherwise it explains
            status = transfer.message if transfer.state == RUNNING else f"{transfer.state}: {transfer.message}"
        if transfer.state == RUNNING and transfer.resumed_from:
            status += f" (resumed at {format_size(transfer.resumed_from)})"
        stalled = self.stalled_for(transfer)
        if stalled:
            status = (f"Stalled: no data for {stalled} s. The network or server may be down; it carries on by itself "
                      "if the connection comes back, or Cancel it.")
        item.setText(STATUS, status)
        item.setToolTip(STATUS, status)
        color = QColor(COLORS["warning" if stalled else STATE_COLORS.get(transfer.state, "text")])
        for column in (PROGRESS, STATUS):
            item.setForeground(column, color)
        self.update_summary()

    def stalled_for(self, transfer):
        """Seconds a running transfer has had no data for, once that's STALL_SECONDS or more (otherwise 0)."""
        if transfer.state != RUNNING or transfer.message:  # A message means checking or listing, not copying
            self.progress_seen.pop(transfer.id, None)
            return 0
        now = time.monotonic()
        seen = self.progress_seen.get(transfer.id)
        if seen is None or seen[0] != transfer.done:
            self.progress_seen[transfer.id] = (transfer.done, now)
            return 0
        quiet = int(now - seen[1])
        return quiet if quiet >= STALL_SECONDS else 0

    def check_stalls(self):
        """Once a second: a stalled transfer sends no updates, so look at the running ones here."""
        for transfer_id in list(self.progress_seen):
            transfer = self.transfers.get(transfer_id)
            if transfer is not None and transfer.state == RUNNING:
                self.update(transfer)

    def speed_text(self, transfer):
        if transfer.state != RUNNING:
            self.samples.pop(transfer.id, None)
            self.speeds.pop(transfer.id, None)
            return ""
        now = time.monotonic()
        last = self.samples.get(transfer.id)
        if last is None or transfer.done < last[1]:
            self.samples[transfer.id] = (now, transfer.done)
            return ""
        elapsed = now - last[0]
        if elapsed >= 1.0:
            rate = (transfer.done - last[1]) / elapsed
            previous = self.speeds.get(transfer.id)
            self.speeds[transfer.id] = rate if previous is None else previous * 0.5 + rate * 0.5
            self.samples[transfer.id] = (now, transfer.done)
        rate = self.speeds.get(transfer.id)
        if not rate:
            return ""
        remaining = (transfer.size - transfer.done) / rate if transfer.size else None
        return f"{format_speed(rate)}  {format_eta(remaining)}"

    def update_summary(self):
        transfers = [transfer for transfer in self.transfers.values() if not transfer.is_dir]
        waiting = [transfer for transfer in transfers if transfer.state in (QUEUED, RUNNING, PAUSED)]
        failed = sum(1 for transfer in transfers if transfer.state == FAILED)
        if not transfers:
            self.summary.setText("Nothing queued. Drag files between the panes, or select them and press F5.")
        elif not waiting:
            self.summary.setText(f"All done ({len(transfers)} file{'' if len(transfers) == 1 else 's'}"
                                 + (f", {failed} failed" if failed else "") + ").")
        else:
            left = sum(max(0, transfer.size - transfer.done) for transfer in waiting)
            rate = sum(self.speeds.values())
            text = f"{len(waiting)} file{'' if len(waiting) == 1 else 's'} to go, {format_size(left)}"
            if rate:
                text += f", {format_speed(rate)}, about {format_eta(left / rate)} left"
            if failed:
                text += f"  ·  {failed} failed"
            self.summary.setText(text)
        self.retry_button.setEnabled(any(transfer.state in (FAILED, CANCELLED) for transfer in transfers))
        self.clear_button.setEnabled(any(transfer.state in FINISHED for transfer in self.transfers.values()))

    def active(self):
        return [transfer for transfer in self.transfers.values() if transfer.state in (QUEUED, RUNNING, PAUSED)]

    def selected(self):
        return [self.transfers[item.data(0, Qt.UserRole)] for item in self.tree.selectedItems()]

    def retry(self, transfers=None):
        transfers = transfers or [transfer for transfer in self.selected() if transfer.state in (FAILED, CANCELLED)] \
            or [transfer for transfer in self.transfers.values() if transfer.state in (FAILED, CANCELLED)]
        self.retry_requested.emit([transfer for transfer in transfers if transfer.state in (FAILED, CANCELLED)])

    def clear_finished(self):
        for transfer_id, transfer in list(self.transfers.items()):
            if transfer.state in FINISHED:
                item = self.items.pop(transfer_id)
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
                del self.transfers[transfer_id]
        self.update_summary()

    def remove_finished_transfers(self, transfers):
        """Drop finished transfers from a shared list (the worker's), keeping the rest in order."""
        transfers[:] = [transfer for transfer in transfers if transfer.id in self.transfers or
                        transfer.state not in FINISHED]

    def show_menu(self, position):
        selected = self.selected()
        if not selected:
            return
        menu = QMenu(self)
        cancel = menu.addAction("Cancel")
        cancel.setEnabled(any(transfer.state in (QUEUED, RUNNING, PAUSED) for transfer in selected))
        retry = menu.addAction("Retry")
        retry.setEnabled(any(transfer.state in (FAILED, CANCELLED) for transfer in selected))
        menu.addSeparator()
        clear = menu.addAction("Clear Finished")
        chosen = menu.exec_(self.tree.viewport().mapToGlobal(position))
        if chosen is cancel:
            self.cancel_requested.emit([transfer for transfer in selected
                                        if transfer.state in (QUEUED, RUNNING, PAUSED)])
        elif chosen is retry:
            self.retry(selected)
        elif chosen is clear:
            self.clear_finished()
