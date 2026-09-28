"""Tools > IPAM Server: turn this computer into the tribe's IPAM server (the NOMAD IPAM Server Windows service),
and hand out the tribe key file laptops connect with."""
import logging
import os
import subprocess

from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, \
    QPushButton, QSpinBox, QVBoxLayout

from ..ipam import service
from ..ipam.server import DEFAULT_PORT, KEY_FILE_SUFFIX, change_team_secret, fingerprint_of_file, load_config, \
    server_dir, write_team_key
from .common import run_in_background, set_hint
from .theme import accent_button

log = logging.getLogger(__name__)


class IpamServerDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("IPAM Server")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        intro = QLabel("Make this computer the tribe's IPAM server: it keeps the shared copy of the tribe's networks "
                       "and sends changes to NOMAD on every laptop. It runs as a Windows service, so it keeps "
                       "working when nobody is signed in. Laptops connect with the tribe key file.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.status_label = QLabel()
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(service.configured_port() or DEFAULT_PORT)
        self.port_input.setToolTip("The TCP port laptops connect to. Installing opens it in Windows Firewall.")
        self.folder_label = QLabel(str(server_dir()))
        self.fingerprint_label = QLabel()
        self.fingerprint_label.setWordWrap(True)
        form.addRow("Service:", self.status_label)
        form.addRow("Port:", self.port_input)
        form.addRow("Data folder:", self.folder_label)
        form.addRow("Certificate:", self.fingerprint_label)
        layout.addLayout(form)

        service_row = QHBoxLayout()
        self.install_button = accent_button("Install Service")
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.uninstall_button = QPushButton("Uninstall...")
        for button in (self.install_button, self.start_button, self.stop_button, self.uninstall_button):
            service_row.addWidget(button)
        service_row.addStretch()
        layout.addLayout(service_row)
        key_row = QHBoxLayout()
        self.save_key_button = QPushButton("Save Tribe Key File...")
        self.save_key_button.setToolTip("The file laptops connect with (Tribe > Connect with Tribe Key File on the IP "
                                        "Addresses page). Anyone with it can change the tribe's IPAM.")
        self.change_key_button = QPushButton("Change Tribe Key...")
        self.change_key_button.setToolTip("Lock out every copy of the current tribe key file, for example after a "
                                          "laptop is lost. Laptops then need the new file.")
        self.open_folder_button = QPushButton("Open Data Folder")
        for button in (self.save_key_button, self.change_key_button, self.open_folder_button):
            key_row.addWidget(button)
        key_row.addStretch()
        layout.addLayout(key_row)
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.install_button.clicked.connect(self.install)
        self.start_button.clicked.connect(lambda: self.run("Starting the service...", service.start, "Started."))
        self.stop_button.clicked.connect(lambda: self.run("Stopping the service...", service.stop, "Stopped."))
        self.uninstall_button.clicked.connect(self.uninstall)
        self.save_key_button.clicked.connect(self.save_key)
        self.change_key_button.clicked.connect(self.change_key)
        self.open_folder_button.clicked.connect(lambda: os.startfile(server_dir()))
        self.busy = False
        self.refresh()

    def refresh(self):
        admin = getattr(self.window, "admin", False)
        state = service.status()
        configured = (server_dir() / "config.json").exists()
        self.status_label.setText(state)
        try:
            self.fingerprint_label.setText(f"Self-signed; fingerprint {fingerprint_of_file(server_dir() / 'cert.pem')}"
                                           if configured and admin else "Created when the service is installed")
        except OSError:
            self.fingerprint_label.setText("Created when the service is installed")
        idle = admin and not self.busy
        self.install_button.setText("Update Service" if state != service.NOT_INSTALLED else "Install Service")
        self.install_button.setToolTip("Install it again from this copy of NOMAD (after updating NOMAD, or to change "
                                       "the port). The data and tribe key are kept." if state != service.NOT_INSTALLED
                                       else "Set up the server's data folder, certificate and tribe key, install the "
                                            "service, open the port in Windows Firewall and start it.")
        self.install_button.setEnabled(idle)
        self.port_input.setEnabled(idle)
        self.start_button.setEnabled(idle and state == service.STOPPED)
        self.stop_button.setEnabled(idle and state == service.RUNNING)
        self.uninstall_button.setEnabled(idle and state != service.NOT_INSTALLED)
        for button in (self.save_key_button, self.change_key_button, self.open_folder_button):
            button.setEnabled(idle and configured)
        if not admin:
            set_hint(self.message_label, "Managing the IPAM server needs administrator rights: use File > Restart as "
                                         "Administrator, then open this again.", "warning")

    def run(self, message, action, done, on_done=None):
        """Run a service action on a worker thread (they can take several seconds)."""
        self.busy = True
        set_hint(self.message_label, message, "info")
        self.refresh()

        def succeeded(result):
            self.busy = False
            set_hint(self.message_label, done, "success")
            self.refresh()
            if on_done:
                on_done(result)

        def failed(error):
            self.busy = False
            log.warning("IPAM server: %s failed: %s", message, error)
            set_hint(self.message_label, str(error), "error")
            self.refresh()

        run_in_background(action, succeeded, failed)

    def install(self):
        port = self.port_input.value()
        self.run("Installing the service (this takes a few seconds)...", lambda: service.install(port),
                 f"The IPAM server is running on port {port}. Next: Save Tribe Key File and give it to the tribe; "
                 "then import your spreadsheet on the IP Addresses page (it goes to the server).",
                 lambda _: self.window.ipam_tab.connect_team())

    def uninstall(self):
        if QMessageBox.question(self, "Uninstall the IPAM Server",
                                "Stop and remove the service and its firewall rule? Laptops can no longer sync or "
                                f"make changes. The data stays in {server_dir()}, so installing again carries on "
                                "where it left off.") != QMessageBox.Yes:
            return
        self.run("Removing the service...", service.uninstall, "Removed. The data is kept.",
                 lambda _: self.window.ipam_tab.connect_team())

    def save_key(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Tribe Key File", f"NOMAD tribe{KEY_FILE_SUFFIX}",
                                              f"NOMAD tribe key (*{KEY_FILE_SUFFIX})")
        if not path:
            return
        try:
            config = load_config()
        except OSError as error:
            log.warning("Couldn't read the IPAM server's settings: %s", error)
            set_hint(self.message_label, f"Couldn't read the server's settings in {server_dir()} "
                                         f"({error.strerror or error}). Update Service repairs the folder's "
                                         "permissions.", "error")
            return
        try:
            write_team_key(path, config)
        except OSError as error:
            log.warning("Couldn't save the tribe key file: %s", error)
            set_hint(self.message_label, f"Couldn't save {path}: {error.strerror or error}", "error")
            return
        set_hint(self.message_label, f"Saved {path}. Give it to the tribe (a USB stick or a file share only the "
                                     "tribe can read): anyone with it can change the tribe's IPAM.", "success")

    def change_key(self):
        if QMessageBox.question(self, "Change the Tribe Key",
                                "Make a new tribe key? Every laptop stops syncing until it's given the new tribe key "
                                "file.") != QMessageBox.Yes:
            return
        try:
            change_team_secret()
        except OSError as error:
            set_hint(self.message_label, f"Couldn't change it: {error.strerror or error}", "error")
            return
        if service.status() == service.RUNNING:
            self.run("Restarting the service with the new key...", lambda: (service.stop(), service.start()),
                     "Tribe key changed. Save the new tribe key file and give it to the tribe.")
        else:
            set_hint(self.message_label, "Tribe key changed. Save the new tribe key file and give it to the tribe.",
                     "success")
