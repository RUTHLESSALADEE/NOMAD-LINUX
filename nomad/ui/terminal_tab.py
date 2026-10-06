"""Terminal page: saved sessions in folders, quick connect, and SSH / Telnet / serial / raw TCP sessions in tabs that
can be popped out into their own windows."""
from ..terminal.commands import CommandStore
from ..terminal.highlight import HighlightStore
from ..terminal.sessions import SSH, TERMINAL_PROTOCOLS
from .session_page import SessionPage
from .terminal_view import SessionView


class TerminalTab(SessionPage):
    protocols = set(TERMINAL_PROTOCOLS)
    tiling = True
    placeholder_text = ("Double-click a saved session to open it, or type an address in Quick connect.\n\n"
                        "Select text to copy it; right-click to paste. Ctrl+Shift+F finds text, Shift+PgUp "
                        "scrolls back, and Ctrl+mouse wheel changes the text size.\n\n"
                        "For more room: « hides the session list, and F11 (Focus) hides everything but "
                        "the sessions. Layout shows several sessions at once: side by side, stacked or in a "
                        "grid.")

    def __init__(self, window, store, highlights=None, commands=None):
        # Before the page is built: the tabs' command bar needs the buttons
        self.highlights = highlights if highlights is not None else HighlightStore()
        self.commands = commands if commands is not None else CommandStore()
        super().__init__(window, store)
        self.highlights.listeners.append(self.apply_highlighting)

    def make_view(self, session):
        view = SessionView(session, self.store)
        view.mirror = self.mirror_typed
        view.view.highlighter = self.highlighter()
        view.view.hotkey = lambda number, view=view: self.press_button(view, number)
        return view

    def press_button(self, view, number):
        """Ctrl+1 to Ctrl+9 in a session: the command button with that number, whether or not the Buttons bar is
        showing. True if sent; with no button of that number, the key goes to the device."""
        tabs = view.parentWidget()
        while tabs is not None and getattr(tabs, "command_bar", None) is None:
            tabs = tabs.parentWidget()
        bar = tabs.command_bar if tabs is not None else None
        if bar is None or number >= len(self.commands.buttons):
            return False
        bar.send(self.commands.buttons[number].id, target=view)
        return True

    def highlighter(self):
        return self.highlights.highlighter if self.highlights.enabled else None

    def apply_highlighting(self):
        """After the rules change, or highlighting is switched on or off."""
        for view in self.all_views():
            view.view.highlighter = self.highlighter()
            view.view.update()

    def companion_actions(self, session):
        if session.protocol != SSH:
            return []
        return [("Open in SCP", lambda: self.window.scp_tab.open_session(session))]
