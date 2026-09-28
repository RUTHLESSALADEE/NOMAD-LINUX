"""Telnet option negotiation (RFC 854 and friends), kept separate from the socket so it can be tested.

Python no longer ships telnetlib, and a terminal only needs the client side: agree to send the window size (NAWS)
and terminal type, let the server echo and suppress go-ahead, and politely refuse everything else.
"""
import struct

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
NOP, GA = 241, 249
ECHO, SGA, TTYPE, NAWS = 1, 3, 24, 31
TTYPE_IS, TTYPE_SEND = 0, 1
WE_WILL = {TTYPE, NAWS}  # Options we agree to do when the server asks (DO)
THEY_WILL = {ECHO, SGA}  # Options we're happy for the server to do (WILL)
MAX_SUBNEGOTIATION = 4096


class TelnetProtocol:
    """Feed it bytes from the server; it returns the data to show and any replies to send back."""

    def __init__(self, terminal_type="xterm-256color", size=(80, 24)):
        self.terminal_type = terminal_type
        self.size = size
        self.state = "data"
        self.command = None
        self.subnegotiation = bytearray()
        self.naws_agreed = False
        self.server_echoes = False
        self.answered = set()  # (command, option) we've already replied to, to avoid negotiation loops

    def feed(self, data):
        """Returns (data for the terminal, bytes to send back)."""
        shown, replies = bytearray(), bytearray()
        for byte in data:
            if self.state == "data":
                if byte == IAC:
                    self.state = "iac"
                else:
                    shown.append(byte)
            elif self.state == "iac":
                if byte == IAC:
                    shown.append(IAC)  # An escaped 255 is data
                    self.state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self.command, self.state = byte, "option"
                elif byte == SB:
                    self.subnegotiation.clear()
                    self.state = "sb"
                else:
                    self.state = "data"  # NOP, GA and the like need no answer
            elif self.state == "option":
                replies += self.negotiate(self.command, byte)
                self.state = "data"
            elif self.state == "sb":
                if byte == IAC:
                    self.state = "sb_iac"
                elif len(self.subnegotiation) < MAX_SUBNEGOTIATION:
                    self.subnegotiation.append(byte)
            elif self.state == "sb_iac":
                if byte == SE:
                    replies += self.handle_subnegotiation(bytes(self.subnegotiation))
                    self.state = "data"
                else:
                    if byte == IAC:
                        self.subnegotiation.append(IAC)
                    self.state = "sb"
        return bytes(shown), bytes(replies)

    def negotiate(self, command, option):
        # Forget the opposite answer, so an option switched off can be agreed again later
        opposite = {DO: WONT, DONT: WILL, WILL: DONT, WONT: DO}[command]
        self.answered.discard((opposite, option))
        if command == DO:
            if option in WE_WILL:
                reply = self.once(WILL, option)
                if option == NAWS and reply:
                    self.naws_agreed = True
                    reply += self.window_size()
                return reply
            return self.once(WONT, option)
        if command == DONT:
            if option == NAWS:
                self.naws_agreed = False
            return self.once(WONT, option)
        if command == WILL:
            if option == ECHO:
                self.server_echoes = True
            return self.once(DO if option in THEY_WILL else DONT, option)
        if command == WONT:
            if option == ECHO:
                self.server_echoes = False
            return self.once(DONT, option)
        return b""

    def once(self, command, option):
        """Reply to a request only the first time, so two sides can't bounce the same request back and forth."""
        if (command, option) in self.answered:
            return b""
        self.answered.add((command, option))
        return bytes([IAC, command, option])

    def handle_subnegotiation(self, payload):
        if len(payload) >= 2 and payload[0] == TTYPE and payload[1] == TTYPE_SEND:
            return bytes([IAC, SB, TTYPE, TTYPE_IS]) + self.terminal_type.upper().encode("ascii") + bytes([IAC, SE])
        return b""

    def window_size(self):
        columns, rows = self.size
        body = struct.pack(">HH", max(0, min(columns, 65535)), max(0, min(rows, 65535)))
        body = body.replace(bytes([IAC]), bytes([IAC, IAC]))  # A 255 in the size must be doubled
        return bytes([IAC, SB, NAWS]) + body + bytes([IAC, SE])

    def resize(self, columns, rows):
        """Record a new window size. Returns the bytes to tell the server, if it asked to know."""
        self.size = (columns, rows)
        return self.window_size() if self.naws_agreed else b""


def escape(data):
    """Data to send: a literal 255 byte has to be doubled."""
    return data.replace(bytes([IAC]), bytes([IAC, IAC]))
