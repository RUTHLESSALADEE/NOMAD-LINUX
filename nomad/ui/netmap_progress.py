"""How a Network Map crawl is going: a progress bar with counts and the time left, what each device being read is
doing now, and a log of what was found, skipped and why."""
import math
import time

from PyQt5.QtCore import QObject, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, \
    QProgressBar, QPushButton, QSplitter, QVBoxLayout, QWidget

from .common import SortableTableItem, read_only_table
from .theme import monospace_font

READING_COLUMNS = ["Device", "Doing now", "Time"]
TICK_MS = 500
LOG_LINES = 50000
ESTIMATE_AFTER = 5  # Devices read before guessing the time left


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{seconds:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def estimate(counts, elapsed):
    """A rough time left, from how fast devices have been read so far, or "" before there's enough to go on."""
    done = counts["read"] + counts["no_snmp"] + counts["unreachable"]
    left = counts["reading"] + counts["queued"]
    if done < ESTIMATE_AFTER or elapsed < 5 or not left:
        return ""
    seconds = left * elapsed / done
    if seconds < 60:
        return "under a minute left"
    return f"about {math.ceil(seconds / 60)} min left so far"


def counts_text(counts, elapsed):
    parts = [f"Read {counts['read']}", f"reading {counts['reading']}", f"queued {counts['queued']}",
             f"{counts['found']} found"]
    if counts["no_snmp"]:
        parts.append(f"{counts['no_snmp']} ping but no SNMP")
    if counts["unreachable"]:
        parts.append(f"{counts['unreachable']} unreachable")
    parts.append(duration(elapsed))
    left = estimate(counts, elapsed)
    if left:
        parts.append(left)
    return "  ·  ".join(parts)


class CrawlProgress(QObject):
    """Owns the progress row (shown while crawling) and the Crawl tab (what's being read now, and the log)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started_at = None
        self.counts = {"read": 0, "reading": 0, "queued": 0, "found": 0, "no_snmp": 0, "unreachable": 0}
        self.reading = {}  # Address -> [label, step, started]
        self.phase = ""
        self.lines = []
        self.labels = {}  # Address -> device label, when known

        self.row = QWidget()
        row_layout = QHBoxLayout(self.row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setMaximumWidth(220)
        self.bar.setMaximumHeight(14)
        self.counts_label = QLabel()
        row_layout.addWidget(self.bar)
        row_layout.addWidget(self.counts_label, 1)
        self.row.setVisible(False)

        self.tab = QWidget()
        tab_layout = QVBoxLayout(self.tab)
        splitter = QSplitter(Qt.Vertical)
        reading_box = QWidget()
        reading_layout = QVBoxLayout(reading_box)
        reading_layout.setContentsMargins(0, 0, 0, 0)
        reading_layout.addWidget(QLabel("Reading now:"))
        self.reading_table = read_only_table(READING_COLUMNS)
        reading_layout.addWidget(self.reading_table)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Log:"))
        log_header.addStretch()
        self.copy_button = QPushButton("Copy Log")
        self.save_button = QPushButton("Save Log...")
        log_header.addWidget(self.copy_button)
        log_header.addWidget(self.save_button)
        log_layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)
        self.log_view.setFont(monospace_font())
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(reading_box)
        splitter.addWidget(log_box)
        splitter.setSizes([160, 400])
        tab_layout.addWidget(splitter)
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(self.lines)))
        self.save_button.clicked.connect(self.save_log)

        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.refresh)

    # ----------------------------------------------------------------- Crawl life cycle

    def start(self):
        self.started_at = time.monotonic()
        self.counts = dict.fromkeys(self.counts, 0)
        self.reading, self.labels, self.phase = {}, {}, ""
        self.lines = []
        self.log_view.clear()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.row.setVisible(True)
        self.timer.start()
        self.refresh()

    def finish(self):
        self.timer.stop()
        self.reading = {}
        self.refresh()
        self.row.setVisible(False)
        if self.started_at is not None:
            self.add_log(f"Finished in {duration(time.monotonic() - self.started_at)}")

    def handle(self, kind, details):
        """A Crawler event (see Crawler's events), delivered on the UI thread."""
        if kind == "started":
            address, key = details
            self.reading[address] = [self.labels.get(address, key or ""), "", time.monotonic()]
        elif kind == "step":
            address, text = details
            if address in self.reading:
                self.reading[address][1] = text
        elif kind == "finished":
            self.reading.pop(details[0], None)
        elif kind == "log":
            self.add_log(details[0])
        elif kind == "counts":
            self.counts = details[0]
        elif kind == "phase":
            self.phase = details[0]
            self.add_log(details[0])
        elif kind == "map":
            for device in details[0].devices.values():
                if device.mgmt_ip:
                    self.labels[device.mgmt_ip] = device.label

    def add_log(self, text):
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self.lines.append(line)
        self.log_view.appendPlainText(line)

    # ----------------------------------------------------------------- Showing it

    def refresh(self):
        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        counts = self.counts
        if self.phase:
            self.bar.setRange(0, 0)  # Busy: hosts and traceroutes don't have a count up front
            self.counts_label.setText(f"{self.phase}...  ·  {duration(elapsed)}")
        else:
            done = counts["read"] + counts["no_snmp"] + counts["unreachable"]
            self.bar.setRange(0, max(1, done + counts["reading"] + counts["queued"]))
            self.bar.setValue(done)
            self.counts_label.setText(counts_text(counts, elapsed))
        self.fill_reading()

    def fill_reading(self):
        now = time.monotonic()
        rows = sorted(self.reading.items(), key=lambda item: item[1][2])
        table = self.reading_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row, (address, (label, step, started)) in enumerate(rows):
            label = self.labels.get(address) or label
            name = f"{label} ({address})" if label and not label.startswith("ip:") and label != address else address
            for column, (text, key) in enumerate(((name, None), (step, None), (duration(now - started),
                                                                                  now - started))):
                table.setItem(row, column, SortableTableItem(text, key))

    def find(self, text):
        """Select the next place text is in the log, going round to the top. Returns False if it isn't there."""
        if self.log_view.find(text):
            return True
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.Start)
        self.log_view.setTextCursor(cursor)
        return self.log_view.find(text)

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self.tab, "Save Crawl Log", "Network map crawl.txt",
                                              "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write("\n".join(self.lines) + "\n")
        except OSError as error:
            QMessageBox.critical(self.tab, "Save Crawl Log", f"Couldn't save the log:\n\n{error}")
