"""Diagnostics report dialog: run the standard checks on the selected adapter and save the results as HTML."""
import logging
import os
import time
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, \
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from ..report import INFO, OK, PROBLEM, STATUS_LABELS, WARNING, render_html, render_text, run_diagnostics
from .common import ColumnFitter, StoppableThread, set_hint
from .theme import COLORS, accent_button, monospace_font

log = logging.getLogger(__name__)

STATUS_COLORS = {OK: COLORS["success"], INFO: COLORS["link"], WARNING: COLORS["warning"], PROBLEM: COLORS["error"]}


class DiagnosticsThread(StoppableThread):
    progress = pyqtSignal(int, int, str)
    finding = pyqtSignal(object)
    finished_report = pyqtSignal(object)

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter

    def run(self):
        report = run_diagnostics(self.adapter, should_stop=lambda: self.stopping, progress=self.progress.emit,
                                 finding=self.finding.emit)
        self.finished_report.emit(report)


class ReportDialog(QDialog):
    def __init__(self, window, adapter):
        super().__init__(window)
        self.window = window
        self.adapter = adapter
        self.report = None
        self.worker = None
        self.setWindowTitle(f"Diagnostics Report: {adapter.name}")
        self.resize(820, 620)

        layout = QVBoxLayout(self)
        intro = QLabel(f"Checking {adapter.name}: its settings, the gateway, DNS, internet access, the route to the "
                       "internet, the path MTU and the ARP table. It takes about half a minute and works without "
                       "internet access too.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Check", "Result"])
        self.tree.setWordWrap(True)
        self.tree.header().setStretchLastSection(True)
        ColumnFitter(self.tree)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.save_button = accent_button("Save Report...")
        self.save_button.setToolTip("Save as a web page (HTML) that opens in any browser, to email or attach to "
                                    "a ticket.")
        self.copy_button = QPushButton("Copy as Text")
        self.run_button = QPushButton("Run Again")
        self.stop_button = QPushButton("Stop")
        self.close_button = QPushButton("Close")
        for button in (self.save_button, self.copy_button, self.run_button, self.stop_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.save_button.clicked.connect(self.save)
        self.copy_button.clicked.connect(self.copy)
        self.run_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.close_button.clicked.connect(self.close)
        self.start()

    def start(self):
        if self.worker is not None:
            return
        self.report = None
        self.tree.clear()
        self.progress_bar.setRange(0, 7)
        self.progress_bar.setValue(0)
        self.worker = DiagnosticsThread(self.adapter, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.finding.connect(self.add_finding)
        self.worker.finished_report.connect(self.on_report)
        self.worker.finished.connect(self.on_thread_finished)
        self.worker.start()
        self.window.set_busy("report", "Running diagnostics")
        self.update_buttons()

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.stop_button.setEnabled(False)

    def on_progress(self, step, total, text):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(step)
        if step < total:
            set_hint(self.status_label, f"{text}...", "info")

    def add_finding(self, finding):
        top = QTreeWidgetItem([finding.section, f"{STATUS_LABELS[finding.status]}: {finding.summary}"])
        top.setForeground(1, QColor(STATUS_COLORS[finding.status]))
        top.setToolTip(1, finding.summary)
        for line in finding.details:
            child = QTreeWidgetItem(["", line])
            child.setToolTip(1, line)
            top.addChild(child)
        if finding.preformatted:
            for line in finding.preformatted.rstrip().splitlines():
                child = QTreeWidgetItem(["", line])
                child.setFont(1, monospace_font())
                top.addChild(child)
        self.tree.addTopLevelItem(top)
        top.setExpanded(finding.status in (WARNING, PROBLEM))

    def on_report(self, report):
        self.report = report
        counts = report.counts()
        if self.worker is not None and self.worker.stopping:
            set_hint(self.status_label, "Stopped. The report has the checks that finished.", "warning")
            return
        summary = ", ".join(f"{counts[status]} {STATUS_LABELS[status].lower()}"
                            for status in (PROBLEM, WARNING, OK, INFO) if counts.get(status))
        kind = {PROBLEM: "error", WARNING: "warning"}.get(report.status, "success")
        set_hint(self.status_label, f"Done: {summary}.", kind)
        log.info("Diagnostics report for %s: %s", self.adapter.name, summary)

    def on_thread_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.window.clear_busy("report")
        self.update_buttons()

    def update_buttons(self):
        running = self.worker is not None
        has_report = self.report is not None and bool(self.report.findings)
        self.save_button.setEnabled(has_report and not running)
        self.copy_button.setEnabled(has_report and not running)
        self.run_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def default_path(self):
        documents = Path.home() / "Documents"
        folder = documents if documents.is_dir() else Path.home()
        return str(folder / f"Network report {self.report.computer} {time.strftime('%Y-%m-%d %H%M')}.html")

    def save(self):
        if self.report is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Report", self.default_path(),
                                              "Web page (*.html);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(render_html(self.report))
        except OSError as error:
            QMessageBox.critical(self, "Save Failed", f"Couldn't save {path}:\n\n{error}")
            return
        self.window.show_status(f"Saved the report to {path}.")
        reply = QMessageBox.question(self, "Report Saved", f"Saved the report to:\n{path}\n\nOpen it now?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.Yes:
            try:
                from ..system import open_path
                open_path(path)
            except OSError as error:
                QMessageBox.warning(self, "Open Report", f"Couldn't open the report:\n\n{error}")

    def copy(self):
        if self.report is not None:
            QApplication.clipboard().setText(render_text(self.report))
            self.window.show_status("Copied the report to the clipboard.", "info")

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(15000)
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
