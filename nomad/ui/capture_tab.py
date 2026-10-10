"""Packet Capture page: capture traffic with Windows' pktmon and save a pcapng file for Wireshark."""
import logging
import os
import subprocess
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, \
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget

from ..capture import DEFAULT_FILE_SIZE_MB, PROTOCOLS, Capture, default_output_path, find_wireshark, validate_filter
from ..pktmon import PktmonBusy, find_pktmon
from ..system import CommandError
from .common import format_size, run_in_background, set_hint, set_invalid
from .theme import accent_button

log = logging.getLogger(__name__)

UPDATE_MILLISECONDS = 500


def format_duration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


class CaptureTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.capture = None
        self.busy = False  # Starting, or stopping and saving
        self.saved_path = None
        self.init_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_progress)
        self.update_buttons()

    def init_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel("Capture network traffic on every adapter with Windows' built-in packet monitor (pktmon) and "
                       "save it as a pcapng file, which Wireshark opens. Needs administrator rights. A filter keeps "
                       "the capture small; leave the fields blank to capture everything. Starting a capture clears "
                       "any packet filters you've set in pktmon yourself.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("Optional: an IP address or subnet, such as 10.0.0.5 or 10.0.0.0/24")
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("Optional, such as 443")
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(PROTOCOLS)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self.address_input, 3)
        filter_row.addWidget(QLabel("Port:"))
        filter_row.addWidget(self.port_input, 1)
        filter_row.addWidget(QLabel("Protocol:"))
        filter_row.addWidget(self.protocol_combo)

        self.whole_check = QCheckBox("Capture whole packets")
        self.whole_check.setChecked(True)
        self.whole_check.setToolTip("Untick to keep only the first 128 bytes of each packet (the headers), which "
                                    "keeps long captures small.")
        self.size_input = QSpinBox()
        self.size_input.setRange(10, 100000)
        self.size_input.setValue(DEFAULT_FILE_SIZE_MB)
        self.size_input.setSuffix(" MB")
        self.size_input.setToolTip("Once the capture reaches this size, the oldest packets are dropped to make room.")
        self.minutes_input = QSpinBox()
        self.minutes_input.setRange(0, 24 * 60)
        self.minutes_input.setSpecialValueText("Until stopped")
        self.minutes_input.setSuffix(" min")
        self.minutes_input.setToolTip("Stop and save automatically after this long.")
        for spin_box in (self.size_input, self.minutes_input):
            spin_box.setButtonSymbols(QSpinBox.NoButtons)
        options_row = QHBoxLayout()
        options_row.addWidget(self.whole_check)
        options_row.addSpacing(12)
        options_row.addWidget(QLabel("Keep up to:"))
        options_row.addWidget(self.size_input)
        options_row.addSpacing(12)
        options_row.addWidget(QLabel("Stop after:"))
        options_row.addWidget(self.minutes_input)
        options_row.addStretch()

        self.output_input = QLineEdit()
        self.browse_button = QPushButton("Browse...")
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_input, 1)
        output_row.addWidget(self.browse_button)

        form = QFormLayout()
        form.addRow("Only traffic to or from:", filter_row)
        form.addRow("Options:", options_row)
        form.addRow("Save to:", output_row)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.start_button = accent_button("Start Capture")
        self.stop_button = QPushButton("Stop and Save")
        self.discard_button = QPushButton("Discard")
        self.discard_button.setToolTip("Stop without saving.")
        self.wireshark_button = QPushButton("Open in Wireshark")
        self.folder_button = QPushButton("Open Folder")
        for button in (self.start_button, self.stop_button, self.discard_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.wireshark_button)
        buttons.addWidget(self.folder_button)
        layout.addLayout(buttons)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        self.browse_button.clicked.connect(self.browse)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(lambda: self.stop(save=True))
        self.discard_button.clicked.connect(lambda: self.stop(save=False))
        self.wireshark_button.clicked.connect(self.open_in_wireshark)
        self.folder_button.clicked.connect(self.open_folder)
        for line_edit in (self.address_input, self.port_input, self.output_input):
            line_edit.textChanged.connect(lambda _, line_edit=line_edit: set_invalid(line_edit, False))

    # ----------------------------------------------------------------- Page interface

    def focus_find(self):
        self.address_input.setFocus()
        self.address_input.selectAll()

    def save_settings(self, settings):
        settings.setValue("capture/address", self.address_input.text())
        settings.setValue("capture/port", self.port_input.text())
        settings.setValue("capture/protocol", self.protocol_combo.currentText())
        settings.setValue("capture/whole", self.whole_check.isChecked())
        settings.setValue("capture/size", self.size_input.value())
        settings.setValue("capture/minutes", self.minutes_input.value())
        settings.setValue("capture/folder", os.path.dirname(self.output_input.text()))

    def restore_settings(self, settings):
        self.address_input.setText(settings.value("capture/address", "", str))
        self.port_input.setText(settings.value("capture/port", "", str))
        self.protocol_combo.setCurrentText(settings.value("capture/protocol", "Any", str))
        self.whole_check.setChecked(settings.value("capture/whole", True, bool))
        self.size_input.setValue(settings.value("capture/size", DEFAULT_FILE_SIZE_MB, int))
        self.minutes_input.setValue(settings.value("capture/minutes", 0, int))
        self.output_input.setText(default_output_path(settings.value("capture/folder", "", str) or None))

    def shutdown(self):
        """Closing the app mid-capture saves what was captured rather than losing it."""
        capture = self.capture
        if capture is None or self.busy:  # Already being saved or discarded in the background
            return
        try:
            if capture.running:
                capture.stop()
                capture.save(self.output_input.text())
                log.info("Saved the capture to %s while closing", self.output_input.text())
        except (CommandError, OSError) as error:
            log.error("Couldn't save the capture while closing: %s", error)
        finally:
            capture.cleanup()
            self.capture = None

    def capture_host(self, address):
        """Fill in a host to capture (from other pages)."""
        self.address_input.setText(address)
        self.port_input.clear()
        self.protocol_combo.setCurrentText("Any")

    # ----------------------------------------------------------------- Capture

    def browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Capture As", self.output_input.text(),
                                              "Wireshark captures (*.pcapng);;All files (*)")
        if path:
            self.output_input.setText(path if path.lower().endswith(".pcapng") else path + ".pcapng")

    def start(self):
        if self.capture is not None or self.busy:
            return
        try:
            capture_filter = validate_filter(self.address_input.text(), self.port_input.text(),
                                             self.protocol_combo.currentText())
        except ValueError as error:
            target = self.port_input if "port" in str(error).lower() else self.address_input
            set_invalid(target, True)
            set_hint(self.status_label, str(error), "error")
            return
        output = self.output_input.text().strip()
        if not output:
            self.output_input.setText(default_output_path())
            output = self.output_input.text()
        if os.path.exists(output):
            self.output_input.setText(default_output_path(os.path.dirname(output)))
        if not find_pktmon():
            set_hint(self.status_label, "This version of Windows doesn't have pktmon (it arrived in Windows 10 "
                                        "version 1809), so packet capture isn't available.", "error")
            return
        if not self.window.require_admin("Capturing packets"):
            return
        capture = Capture(capture_filter, self.whole_check.isChecked(), self.size_input.value())
        self.busy = True
        self.saved_path = None
        set_hint(self.status_label, "Starting the capture...", "info")
        self.update_buttons()

        def started(_):
            self.busy = False
            self.capture = capture
            self.timer.start(UPDATE_MILLISECONDS)
            self.window.set_busy("capture", "Capturing packets")
            log.info("Capturing %s", capture_filter.describe())
            self.update_progress()
            self.update_buttons()

        def failed(error):
            self.busy = False
            message = str(error) if isinstance(error, PktmonBusy) else f"Couldn't start the capture: {error}"
            set_hint(self.status_label, message, "error")
            self.update_buttons()

        run_in_background(capture.start, started, failed)

    def update_progress(self):
        capture = self.capture
        if capture is None or not capture.running:
            return
        elapsed = capture.elapsed()
        limit = self.minutes_input.value() * 60
        what = capture.filter.describe()
        remaining = f"  ·  stops in {format_duration(limit - elapsed)}" if limit else ""
        set_hint(self.status_label, f"Capturing {what}: {format_duration(elapsed)}, {format_size(capture.size())}"
                                    f"{remaining}", "info")
        if limit and elapsed >= limit:
            self.stop(save=True)

    def stop(self, save=True):
        capture = self.capture
        if capture is None or self.busy:
            return
        self.busy = True
        self.timer.stop()
        output = self.output_input.text().strip()
        set_hint(self.status_label, "Stopping and converting to pcapng (large captures take a moment)..." if save
                 else "Stopping...", "info")
        self.window.set_busy("capture", "Saving the capture" if save else "Stopping the capture")
        self.update_buttons()

        def finish():
            try:
                capture.stop()
                return capture.save(output) if save else None
            finally:
                capture.cleanup()

        def saved(size):
            self.finish_capture()
            if save:
                self.saved_path = output
                set_hint(self.status_label, f"Saved {format_size(size)} to {output}.", "success")
                log.info("Saved a capture to %s", output)
                self.output_input.setText(default_output_path(os.path.dirname(output)))
            else:
                set_hint(self.status_label, "Capture discarded.", "info")
            self.update_buttons()

        def failed(error):
            self.finish_capture()
            set_hint(self.status_label, f"Couldn't save the capture: {error}", "error")
            self.update_buttons()

        run_in_background(finish, saved, failed)

    def finish_capture(self):
        self.busy = False
        self.capture = None
        self.window.clear_busy("capture")

    def open_in_wireshark(self):
        wireshark = find_wireshark()
        if not wireshark or not self.saved_path:
            return
        try:
            subprocess.Popen([wireshark, self.saved_path])
        except OSError as error:
            QMessageBox.critical(self, "Wireshark", f"Couldn't start Wireshark:\n\n{error}")

    def open_folder(self):
        path = self.saved_path or self.output_input.text()
        folder = os.path.dirname(path)
        try:
            if self.saved_path and os.path.isfile(self.saved_path):
                if sys.platform == "win32":
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(self.saved_path)])
                else:
                    from ..system import open_path
                    open_path(folder)
            else:
                from ..system import open_path
                open_path(folder)
        except OSError as error:
            QMessageBox.critical(self, "Open Folder", f"Couldn't open {folder}:\n\n{error}")

    def update_buttons(self):
        capturing = self.capture is not None
        self.start_button.setEnabled(not capturing and not self.busy)
        self.stop_button.setEnabled(capturing and not self.busy)
        self.discard_button.setEnabled(capturing and not self.busy)
        for widget in (self.address_input, self.port_input, self.protocol_combo, self.whole_check, self.size_input,
                       self.output_input, self.browse_button):
            widget.setEnabled(not capturing and not self.busy)
        has_file = bool(self.saved_path) and os.path.isfile(self.saved_path)
        self.wireshark_button.setEnabled(has_file and find_wireshark() is not None)
        self.wireshark_button.setToolTip("" if find_wireshark() else "Wireshark isn't installed.")
        self.folder_button.setEnabled(bool(self.output_input.text()))
