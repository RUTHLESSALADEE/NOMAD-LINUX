"""Things to do with a host from a right-click menu (Sweep results, Network Map devices): open a session, browse to
it, ping, trace, scan, read it over SNMP or capture its traffic, each on its own page."""
import subprocess
import webbrowser

from PyQt5.QtWidgets import QMessageBox

from ..sweep import find_putty
from ..terminal.sessions import SSH, TELNET


class HostActions:
    def __init__(self, window, parent):
        self.window, self.parent = window, parent

    def add_to(self, menu, host):
        """Add the actions for host to menu. Returns {QAction: callable} for running the chosen one."""
        return {
            menu.addAction("Open SSH Session"): lambda: self.open_terminal(host, SSH),
            menu.addAction("Open SCP Session"): lambda: self.window.scp_tab.open_address(host),
            menu.addAction("Open Telnet Session"): lambda: self.open_terminal(host, TELNET),
            menu.addAction("SSH with PuTTY"): lambda: self.open_ssh(host),
            menu.addAction(f"Open https://{host}"): lambda: self.open_web(host),
            menu.addAction(f"Open http://{host}"): lambda: self.open_web(host, "http"),
            menu.addAction("Ping"): lambda: self.ping(host),
            menu.addAction("Traceroute"): lambda: self.trace(host),
            menu.addAction("Monitor Latency"): lambda: self.monitor_latency(host),
            menu.addAction("Scan Ports"): lambda: self.scan_ports(host),
            menu.addAction("SNMP Details"): lambda: self.snmp(host),
            menu.addAction("Capture Traffic..."): lambda: self.capture(host),
        }

    def open_terminal(self, host, protocol):
        """Open a session to host on the Terminal page."""
        if host:
            self.window.terminal_tab.open_address(host, protocol)

    def open_ssh(self, host):
        if not host:
            return
        putty = find_putty()
        if not putty:
            QMessageBox.warning(self.parent, "PuTTY Not Found",
                                "PuTTY wasn't found on the PATH or in its usual install folders. "
                                "Install PuTTY from https://www.putty.org, then try again.")
            return
        try:
            subprocess.Popen([putty, "-ssh", host])
        except OSError as error:
            QMessageBox.critical(self.parent, "SSH", f"Couldn't start PuTTY:\n\n{error}")
            return
        self.window.show_status(f"Opened an SSH session to {host}.", "info")

    def open_web(self, host, scheme="https"):
        if host:
            url = f"{scheme}://{host}"
            webbrowser.open_new_tab(url)
            self.window.show_status(f"Opened {url}.", "info")

    def ping(self, host):
        if host:
            self.window.navigator.setCurrentWidget(self.window.ping_tab)
            self.window.ping_tab.ping_host(host)

    def trace(self, host):
        if host:
            self.window.navigator.setCurrentWidget(self.window.traceroute_tab)
            self.window.traceroute_tab.trace_host(host)

    def monitor_latency(self, host):
        if host:
            self.window.navigator.setCurrentWidget(self.window.latency_tab)
            self.window.latency_tab.add_target(host, host)

    def scan_ports(self, host):
        if host:
            self.window.navigator.setCurrentWidget(self.window.ports_tab)
            self.window.ports_tab.scan_host(host)

    def snmp(self, host):
        if host:
            self.window.navigator.setCurrentWidget(self.window.snmp_tab)
            self.window.snmp_tab.query_host(host)

    def capture(self, host):
        """Fill in the host on the Packet Capture page; capturing needs a deliberate Start (and admin rights)."""
        if host:
            self.window.navigator.setCurrentWidget(self.window.capture_tab)
            self.window.capture_tab.capture_host(host)
