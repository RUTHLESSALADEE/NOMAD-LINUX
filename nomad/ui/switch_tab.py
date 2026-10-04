"""Switch Port tab: which switch, port and VLAN this computer is plugged into, from LLDP and CDP announcements."""
import logging

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QApplication, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton, \
    QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..lldp import discover
from ..pktmon import PktmonBusy, find_pktmon
from ..system import CommandError
from .common import StoppableThread, set_hint
from .theme import accent_button

log = logging.getLogger(__name__)

DEFAULT_SECONDS = 90  # Long enough for CDP's default 60 second interval


class DiscoveryThread(StoppableThread):
    found = pyqtSignal(object)  # lldp.Neighbor
    progress = pyqtSignal(float, float)
    finished_discovery = pyqtSignal(str, str)  # (message, kind)

    def __init__(self, seconds, parent=None):
        super().__init__(parent)
        self.seconds = seconds

    def run(self):
        try:
            neighbors = discover(self.seconds, should_stop=lambda: self.stopping, found=self.found.emit,
                                 progress=self.progress.emit)
        except (CommandError, OSError, PktmonBusy) as error:
            self.finished_discovery.emit(f"Couldn't listen for switches: {error}", "error")
            return
        if neighbors:
            count = len(neighbors)
            self.finished_discovery.emit(f"Found {count} switch announcement{'' if count == 1 else 's'}.", "success")
        elif self.stopping:
            self.finished_discovery.emit("Stopped before any switch announced itself.", "warning")
        else:
            self.finished_discovery.emit(
                "No switch announced itself. The switch may have LLDP and CDP turned off, the adapter may be "
                "plugged into an unmanaged switch, or it may be Wi-Fi (access points rarely announce).", "warning")


class SwitchTab(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.worker = None
        self.neighbors = []
        self.init_ui()
        self.update_buttons()

    def init_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel("Managed switches announce their name, the port you're plugged into and its VLAN every 30 "
                       "seconds (LLDP) or 60 seconds (CDP, on Cisco switches). This listens on every wired adapter "
                       "until one is heard. It uses Windows' built-in packet monitor (pktmon), so it needs "
                       "administrator rights, and it clears any packet filters you've set in pktmon yourself.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.seconds_input = QSpinBox()
        self.seconds_input.setRange(10, 600)
        self.seconds_input.setValue(DEFAULT_SECONDS)
        self.seconds_input.setButtonSymbols(QSpinBox.NoButtons)
        self.seconds_input.setToolTip("Give up after this long. Stops sooner once a switch is heard.")
        form = QFormLayout()
        form.addRow("Listen for up to (s):", self.seconds_input)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.start_button = accent_button("Find Switch Port")
        self.stop_button = QPushButton("Stop")
        self.copy_button = QPushButton("Copy Results")
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        buttons.addWidget(self.copy_button)
        layout.addLayout(buttons)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v of %m seconds")
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Switch", "Details"])
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, 1)

        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.copy_button.clicked.connect(self.copy_results)

    # ----------------------------------------------------------------- Tab interface

    def save_settings(self, settings):
        settings.setValue("switch/seconds", self.seconds_input.value())

    def restore_settings(self, settings):
        self.seconds_input.setValue(settings.value("switch/seconds", DEFAULT_SECONDS, int))

    def shutdown(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(15000)  # Long enough to stop pktmon and remove its filters

    # ----------------------------------------------------------------- Discovery

    def start(self):
        if self.worker is not None:
            return
        if not find_pktmon():
            set_hint(self.status_label, "This version of Windows doesn't have pktmon (it arrived in Windows 10 "
                                        "version 1809), so switch discovery isn't available.", "error")
            return
        if not self.window.require_admin("Listening for switch announcements"):
            return
        self.tree.clear()
        self.neighbors = []
        seconds = self.seconds_input.value()
        self.progress_bar.setRange(0, seconds)
        self.progress_bar.setValue(0)
        set_hint(self.status_label, "Listening for switch announcements...", "info")
        log.info("Listening for LLDP/CDP for up to %s seconds", seconds)
        self.worker = DiscoveryThread(seconds, self)
        self.worker.found.connect(self.add_neighbor)
        self.worker.progress.connect(self.show_progress)
        self.worker.finished_discovery.connect(self.on_finished)
        self.worker.finished.connect(self.on_thread_finished)
        self.worker.start()
        self.window.set_busy("switch", "Listening for switches")
        self.update_buttons()

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.stop_button.setEnabled(False)
            set_hint(self.status_label, "Stopping (checking what was heard so far)...", "info")

    def show_progress(self, elapsed, total):
        self.progress_bar.setValue(int(elapsed))

    def on_finished(self, message, kind):
        if kind == "success":
            self.progress_bar.setValue(self.progress_bar.maximum())
        set_hint(self.status_label, message, kind)
        log.info(message)

    def on_thread_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.window.clear_busy("switch")
        self.update_buttons()

    @staticmethod
    def rows(neighbor):
        rows = [("Switch name", neighbor.system_name), ("Device ID", neighbor.device_id if neighbor.system_name
                                                        and neighbor.device_id != neighbor.system_name else ""),
                ("Port", neighbor.port_id), ("Port description", neighbor.port_description),
                ("VLAN (untagged)", neighbor.native_vlan), ("Voice VLAN", neighbor.voice_vlan),
                ("VLANs", ", ".join(neighbor.vlan_names)),
                ("Management address", ", ".join(neighbor.management_addresses)),
                ("Model", neighbor.platform), ("Capabilities", ", ".join(neighbor.capabilities)),
                ("Duplex", neighbor.duplex), ("Software", neighbor.description),
                ("Switch MAC address", neighbor.source_mac), ("Heard on", neighbor.interface),
                ("Protocol", neighbor.protocol)]
        return [(label, value) for label, value in rows if value]

    def add_neighbor(self, neighbor):
        self.neighbors.append(neighbor)
        port = f", port {neighbor.port_id}" if neighbor.port_id else ""
        vlan = f", VLAN {neighbor.native_vlan}" if neighbor.native_vlan else ""
        top = QTreeWidgetItem([neighbor.name or neighbor.source_mac, f"{neighbor.protocol}{port}{vlan}"])
        for label, value in self.rows(neighbor):
            child = QTreeWidgetItem([label, value.splitlines()[0] if value else value])
            child.setToolTip(1, value)
            top.addChild(child)
        self.tree.addTopLevelItem(top)
        top.setExpanded(True)
        self.update_buttons()

    def copy_results(self):
        lines = []
        for neighbor in self.neighbors:
            lines.append(f"{neighbor.name} ({neighbor.protocol})")
            lines += [f"    {label}: {value}" for label, value in self.rows(neighbor)]
        QApplication.clipboard().setText("\n".join(lines) + "\n")
        self.window.show_status("Copied the switch details to the clipboard.", "info")

    def update_buttons(self):
        running = self.worker is not None
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running and not self.worker.stopping)
        self.copy_button.setEnabled(bool(self.neighbors))
