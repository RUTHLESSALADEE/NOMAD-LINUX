"""Widgets and helpers shared by the tabs."""
import logging
import threading

from PyQt5 import sip
from PyQt5.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, pyqtSignal
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QSizePolicy, QTableWidget, QTableWidgetItem

from .theme import COLORS

log = logging.getLogger(__name__)

INVALID_INPUT_STYLE = f"border: 1px solid {COLORS['error']};"
HINT_COLORS = {kind: COLORS[name] for kind, name in
               {"error": "error", "warning": "warning", "info": "muted", "success": "success"}.items()}


def set_invalid(line_edit, invalid):
    """Outline a field in red while it holds an invalid value."""
    line_edit.setStyleSheet(INVALID_INPUT_STYLE if invalid else "")


def set_hint(label, text, kind="info"):
    label.setStyleSheet(f"color: {HINT_COLORS[kind]};")
    label.setText(text)


def format_size(size):
    """Bytes for display, such as "12.3 MB"."""
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024


def format_ms(value):
    """Milliseconds for display: "" when missing, "<1 ms" when under a millisecond."""
    return "" if value is None else "<1 ms" if value < 1 else f"{value:.0f} ms"


class SortableTableItem(QTableWidgetItem):
    """Table item that sorts by its sort_key, so IP addresses and numbers sort numerically."""

    def __init__(self, text, sort_key=None, data=None):
        super().__init__(text)
        self.sort_key = sort_key
        self.data_object = data

    def __lt__(self, other):
        other_key = getattr(other, "sort_key", None)
        if self.sort_key is not None and other_key is not None:
            return self.sort_key < other_key
        return super().__lt__(other)


def read_only_table(columns):
    """A table of results: rows are selected whole, one at a time, and can't be edited."""
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class _TaskSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)


class _Task(QRunnable):
    def __init__(self, function, signals):
        super().__init__()
        self.function = function
        self.signals = signals

    def run(self):
        try:
            result = self.function()
        except Exception as error:  # Reported to the caller's error handler
            log.exception("Background task failed")
            self.signals.failed.emit(error)
        else:
            self.signals.succeeded.emit(result)


_running_signals = set()  # Keeps signal objects alive until their task finishes and Qt frees them


def forget_deleted(objects):
    """Drop from a set the Qt objects Qt has freed."""
    for item in [item for item in objects if sip.isdeleted(item)]:
        objects.discard(item)


def run_in_background(function, on_success=None, on_error=None):
    """Run function() on a worker thread and call on_success(result) / on_error(exception) on the UI thread."""
    forget_deleted(_running_signals)
    signals = _TaskSignals()
    _running_signals.add(signals)
    if on_success:
        signals.succeeded.connect(on_success)
    if on_error:
        signals.failed.connect(on_error)
    # Freed by Qt once it has said how it went (its own slot, after on_success or on_error): freeing it from a lambda
    # (dropping the last reference to it) while its signal is being delivered isn't safe
    signals.succeeded.connect(signals.deleteLater)
    signals.failed.connect(signals.deleteLater)
    QThreadPool.globalInstance().start(_Task(function, signals))


class StoppableThread(QThread):
    """Base for long-running workers (ping, traceroute, MTU test) that report progress through signals."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    @property
    def stopping(self):
        return self.stop_event.is_set()


_orphaned_threads = set()  # Threads let go of while still running, kept alive until they finish


def release_thread(thread, wait_ms=3000):
    """Wait for a stopped thread to finish; if it's still stuck (connecting, say), let it finish on its own. Its
    signals are cut and it no longer belongs to its widget, so closing the widget can't destroy a running thread
    (which takes the whole program down)."""
    if thread.wait(wait_ms):
        return
    try:
        thread.disconnect()  # Every signal: nothing it says now should reach the widget
    except TypeError:
        pass
    thread.setParent(None)
    forget_deleted(_orphaned_threads)
    _orphaned_threads.add(thread)
    thread.finished.connect(thread.deleteLater)  # Freed by Qt once it has finished (its own slot, not a lambda)
    if thread.isFinished():  # Finished just now, before that was connected
        thread.deleteLater()


class OneLineLabel(QLabel):
    """A label that can be kept to one line (set_one_line): what doesn't fit is cut short with "...", the whole of
    it in the tooltip. text() is always the whole text. wraps: whether it wraps when it isn't kept to one line."""

    def __init__(self, text="", parent=None, wraps=False):
        super().__init__(text, parent)
        self.wraps, self.one_line = wraps, False
        self.setWordWrap(wraps)

    def set_one_line(self, on, width=None):
        """width: keep to that many pixels, taking the same room whatever it says (rather than whatever's left)."""
        self.one_line = on
        self.setWordWrap(self.wraps and not on)
        if on and width:
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            self.setFixedWidth(width)
        else:
            self.setSizePolicy(QSizePolicy.Ignored if on else QSizePolicy.Preferred, QSizePolicy.Preferred)
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
        self.setToolTip(self.text() if on else "")
        self.updateGeometry()
        self.update()

    def setText(self, text):
        super().setText(text)
        if self.one_line:
            self.setToolTip(text)

    def paintEvent(self, event):
        if not self.one_line:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, rect.width())
        self.style().drawItemText(painter, rect, int(self.alignment()), self.palette(), self.isEnabled(), text,
                                  self.foregroundRole())
