"""Terminal page: saved sessions in folders, quick connect, and SSH / Telnet / serial / raw TCP sessions in tabs that
can be popped out into their own windows."""
from ..terminal.sessions import SSH
from .session_page import SessionPage
from .terminal_view import SessionView


class TerminalTab(SessionPage):
    placeholder_text = ("Double-click a saved session to open it, or type an address in Quick connect.\n\n"
                        "Select text to copy it; right-click to paste. Ctrl+Shift+F finds text, Shift+PgUp "
                        "scrolls back, and Ctrl+mouse wheel changes the text size.\n\n"
                        "For more room: « hides the session list, and F11 (Focus) hides everything but "
                        "the sessions.")

    def make_view(self, session):
        return SessionView(session, self.store)

    def companion_actions(self, session):
        if session.protocol != SSH:
            return []
        return [("Open in SCP", lambda: self.window.scp_tab.open_session(session))]
