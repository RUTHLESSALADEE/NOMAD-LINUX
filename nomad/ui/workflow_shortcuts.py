"""Page-wide run/stop shortcuts use the same enabled buttons as mouse actions."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAction, QApplication


DIAGNOSTIC_BUTTONS = {
    "PingTab": ("start_button", "stop_button"),
    "TracerouteTab": ("start_button", "stop_button"),
    "SweepTab": ("start_button", "stop_button"),
    "PortsTab": ("start_button", "stop_button"),
    "MtuTab": ("run_button", "stop_button"),
    "LatencyTab": ("start_button", "stop_button"),
    "IperfTab": ("start_button", "stop_button"),
    "SnmpTab": ("walk_button", "stop_button"),
    "DnsServersTab": ("dns_start_button", "dns_stop_button"),
    "LookupTab": ("lookup_button", None),
    "WebCheckTab": ("web_button", None),
    "CaptureTab": ("start_button", "stop_button"),
    "DhcpTab": ("start_button", "stop_button"),
    "SwitchTab": ("start_button", "stop_button"),
}


def perform_page_action(page, stop=False):
    names = DIAGNOSTIC_BUTTONS.get(type(page).__name__)
    if names is None:
        return False
    name = names[1 if stop else 0]
    if type(page).__name__ == "LatencyTab" and not stop and QApplication.focusWidget() in (
            page.name_input, page.host_input):
        name = "add_button"
    elif type(page).__name__ == "IperfTab" and page.server_radio.isChecked():
        name = "stop_server_button" if stop else "start_server_button"
    if name is None:
        return False
    button = getattr(page, name)
    if not button.isEnabled():
        return False
    button.click()
    return True


def install_workflow_shortcuts(window):
    tool_actions = []
    for title, keys, callback in (
        ("Run Current Tool", ["Shift+Return", "Shift+Enter"],
         lambda: perform_page_action(window.navigator.currentWidget())),
        ("Stop Current Tool", ["Shift+Esc"],
         lambda: perform_page_action(window.navigator.currentWidget(), stop=True)),
        ("Choose Adapter", ["Alt+A"], lambda: focus_adapter(window)),
        ("Keyboard Guide", ["F1"], window.show_shortcuts),
    ):
        action = QAction(title, window)
        action.setShortcuts(keys)
        action.setShortcutContext(Qt.WindowShortcut)
        action.setAutoRepeat(False)
        action.triggered.connect(callback)
        window.addAction(action)
        if title in ("Run Current Tool", "Stop Current Tool"):
            tool_actions.append(action)

    def update_tool_scope():
        page = window.navigator.currentWidget()
        buttons = DIAGNOSTIC_BUTTONS.get(type(page).__name__)
        tool_actions[0].setEnabled(buttons is not None)
        tool_actions[1].setEnabled(buttons is not None and buttons[1] is not None)

    window.navigator.currentChanged.connect(lambda _: update_tool_scope())
    update_tool_scope()


def focus_adapter(window):
    if window.focus_mode:
        window.set_focus_mode(False)
    window.adapter_combo.setFocus()
