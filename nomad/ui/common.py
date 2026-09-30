"""Widgets and helpers shared by the tabs."""
import logging
import threading

from PyQt5.QtCore import QObject, QRunnable, QThread, QThreadPool, pyqtSignal
from PyQt5.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

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


_running_signals = set()  # Keeps signal objects alive until their task finishes


def run_in_background(function, on_success=None, on_error=None):
    """Run function() on a worker thread and call on_success(result) / on_error(exception) on the UI thread."""
    signals = _TaskSignals()
    _running_signals.add(signals)

    def finished():
        _running_signals.discard(signals)

    if on_success:
        signals.succeeded.connect(on_success)
    if on_error:
        signals.failed.connect(on_error)
    signals.succeeded.connect(finished)
    signals.failed.connect(finished)
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
