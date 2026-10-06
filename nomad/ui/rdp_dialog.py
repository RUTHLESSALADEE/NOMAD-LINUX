"""Saved Remote Desktop connection editor."""
from PyQt5.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, \
    QLineEdit, QMessageBox, QSpinBox, QTextEdit, QVBoxLayout

from ..terminal.credentials import CredentialError
from ..terminal.sessions import RDP, normalize_folder, validate_session
from .common import set_hint
from .vault_dialog import password_field, protect_secret


class RdpDialog(QDialog):
    def __init__(self, parent, session, folders, title="RDP Session", store=None):
        super().__init__(parent)
        self.session, self.store = session, store
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_input = QLineEdit(session.name)
        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.addItems(["", *sorted(folders, key=str.lower)])
        self.folder_combo.setCurrentText(session.folder)
        self.host_input = QLineEdit(session.host)
        self.host_input.setPlaceholderText("Computer name or IP address")
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(session.port)
        self.username_input = QLineEdit(session.username)
        self.username_input.setPlaceholderText(r"DOMAIN\user or user@domain")
        self.password_input = password_field("Saved (encrypted). Type to replace." if session.saved_password else
                                             "Leave blank to sign in through Windows")
        self.save_password_check = QCheckBox("Remember password in NOMAD's vault")
        self.save_password_check.setChecked(bool(session.saved_password))
        self.save_password_check.setToolTip("Untick to forget the saved password. Windows may still ask you to sign in.")
        for label, widget in (("Name:", self.name_input), ("Folder:", self.folder_combo),
                              ("Address:", self.host_input), ("Port:", self.port_input),
                              ("Username:", self.username_input), ("Password:", self.password_input),
                              ("", self.save_password_check)):
            form.addRow(label, widget)
        self.fullscreen_check = QCheckBox("Full screen")
        self.fullscreen_check.setChecked(session.rdp_fullscreen)
        self.multimon_check = QCheckBox("Use all monitors")
        self.multimon_check.setChecked(session.rdp_multimon)
        self.width_input, self.height_input = QSpinBox(), QSpinBox()
        for widget, value in ((self.width_input, session.rdp_width), (self.height_input, session.rdp_height)):
            widget.setRange(200, 8192)
            widget.setValue(value)
        self.clipboard_check = QCheckBox("Share clipboard")
        self.clipboard_check.setChecked(session.rdp_clipboard)
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["Play on this computer", "Play on remote computer", "Do not play"])
        self.audio_combo.setCurrentIndex(session.rdp_audio)
        self.admin_check = QCheckBox("Administrative session")
        self.admin_check.setChecked(session.rdp_admin)
        self.notes_input = QTextEdit(session.notes)
        self.notes_input.setAcceptRichText(False)
        self.notes_input.setMaximumHeight(85)
        for label, widget in (("", self.fullscreen_check), ("", self.multimon_check),
                              ("Window width:", self.width_input), ("Window height:", self.height_input),
                              ("", self.clipboard_check), ("Audio:", self.audio_combo),
                              ("", self.admin_check), ("Notes:", self.notes_input)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fullscreen_check.toggled.connect(self.update_dimensions)
        self.multimon_check.toggled.connect(self.update_dimensions)
        self.update_dimensions()

    def update_dimensions(self):
        enabled = not (self.fullscreen_check.isChecked() or self.multimon_check.isChecked())
        self.width_input.setEnabled(enabled)
        self.height_input.setEnabled(enabled)

    def save(self):
        session = self.session.copy(id=self.session.id)
        session.protocol = RDP
        session.name = self.name_input.text().strip()
        session.folder = normalize_folder(self.folder_combo.currentText())
        session.host = self.host_input.text().strip().strip("[]")
        session.port = self.port_input.value()
        session.username = self.username_input.text().strip()
        session.notes = self.notes_input.toPlainText().strip()
        session.rdp_fullscreen = self.fullscreen_check.isChecked()
        session.rdp_multimon = self.multimon_check.isChecked()
        session.rdp_width, session.rdp_height = self.width_input.value(), self.height_input.value()
        session.rdp_clipboard = self.clipboard_check.isChecked()
        session.rdp_audio = self.audio_combo.currentIndex()
        session.rdp_admin = self.admin_check.isChecked()
        problem = validate_session(session)
        if problem:
            set_hint(self.error_label, problem, "error")
            return
        try:
            if not self.save_password_check.isChecked():
                session.saved_password = ""
            elif self.password_input.text():
                encrypted = protect_secret(self, self.store, self.password_input.text())
                if encrypted is None:
                    return
                session.saved_password = encrypted
        except CredentialError as error:
            QMessageBox.warning(self, "Save RDP Password", str(error))
            return
        self.session = session
        self.password_input.clear()
        self.accept()
