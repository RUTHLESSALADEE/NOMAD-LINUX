import pytest

from nomad.terminal.model import LogCleaner, Position, TerminalModel, encode_key, encode_paste
from nomad.terminal.telnet import DO, DONT, ECHO, IAC, NAWS, SB, SE, SGA, TTYPE, WILL, WONT, TelnetProtocol, escape


def lines(model):
    return [model.line_text(index) for index in range(model.line_count)]


def test_scrollback_keeps_lines_that_scroll_off():
    model = TerminalModel(20, 3, scrollback=100)
    model.feed(b"one\r\ntwo\r\nthree\r\nfour\r\nfive")
    assert model.history_length == 2 and lines(model) == ["one", "two", "three", "four", "five"]
    model.clear_scrollback()
    assert lines(model) == ["three", "four", "five"]


def test_alternate_screen_restores_the_main_screen():
    model = TerminalModel(20, 3, scrollback=100)
    model.feed(b"prompt> ls\r\nfile1\r\nprompt> ")
    model.feed(b"\x1b[?1049h\x1b[Hvim\r\nlines\r\nthat\r\nscroll\r\naway")
    assert model.screen.alternate and model.history_length == 0  # Full-screen output stays out of the history
    model.feed(b"\x1b[?1049l")
    assert not model.screen.alternate
    assert lines(model) == ["prompt> ls", "file1", "prompt>"] and model.cursor_position() == Position(2, 8)


def test_replies_to_device_queries():
    replies = []
    model = TerminalModel(20, 5, respond=replies.append)
    model.feed(b"ab\x1b[6n")
    assert replies == ["\x1b[1;3R"]  # Cursor position report


def test_modes_and_split_characters():
    model = TerminalModel(20, 3)
    assert not model.application_cursor
    model.feed(b"\x1b[?1h\x1b[?2004h")
    assert model.application_cursor and model.bracketed_paste
    model.feed("é".encode()[:1])
    model.feed("é".encode()[1:])  # A character split across two reads
    assert model.line(0)[0].data == "é"


def test_selection_words_and_find():
    model = TerminalModel(30, 3)
    model.feed(b"interface Gi1/0/24\r\n description uplink\r\nend")
    assert model.text_between(Position(0, 10), Position(1, 11)) == "Gi1/0/24\n description"
    assert model.word_at(Position(0, 12)) == (Position(0, 10), Position(0, 17))
    assert model.find("UPLINK") == Position(1, 13)
    assert model.find("i", before=Position(1, 13)) == Position(1, 9)
    assert model.find("interface", before=Position(0, 0)) is None and model.find("missing") is None


@pytest.mark.parametrize("arguments, expected", [
    (dict(key="Up"), "\x1b[A"), (dict(key="Up", application_cursor=True), "\x1bOA"),
    (dict(key="Left", ctrl=True), "\x1b[1;5D"), (dict(key="Home"), "\x1b[H"), (dict(key="F5"), "\x1b[15~"),
    (dict(key="Backspace"), "\x7f"), (dict(key="Backspace", backspace_delete=False), "\x08"),
    (dict(key="Enter"), "\r"), (dict(key="Enter", enter="\r\n"), "\r\n"), (dict(key="Delete"), "\x1b[3~"),
    (dict(key="", text="c", ctrl=True), "\x03"), (dict(key="", text="Z", ctrl=True), "\x1a"),
    (dict(key="", text="[", ctrl=True), "\x1b"), (dict(key="", text=" ", ctrl=True), "\x00"),
    (dict(key="", text="b", alt=True), "\x1bb"), (dict(key="", text="@", ctrl=True, alt=True), "@"),
    (dict(key="", text="x"), "x"), (dict(key="Shift"), ""),
])
def test_encode_key(arguments, expected):
    assert encode_key(**arguments) == expected


def test_encode_paste():
    assert encode_paste("conf t\r\nint gi1\nexit") == "conf t\rint gi1\rexit"
    assert encode_paste("a\nb", bracketed=True) == "\x1b[200~a\rb\x1b[201~"


def test_log_cleaner():
    cleaner = LogCleaner()
    assert cleaner.clean("\x1b[1;31mError\x1b[0m: down\r\nnext\x1b[") == "Error: down\nnext"
    assert cleaner.clean("0mline\x07\r\n") == "line\n"  # The escape split across chunks is still removed
    assert cleaner.clean("\x1b]0;window title\x07after\r\nprogress\rdone\r\n") == "after\nprogressdone\n"


def test_telnet_negotiation():
    protocol = TelnetProtocol("xterm", (120, 40))
    shown, replies = protocol.feed(bytes([IAC, DO, NAWS, IAC, DO, TTYPE, IAC, WILL, ECHO, IAC, WILL, SGA,
                                          IAC, DO, 39]) + b"Username: ")
    assert shown == b"Username: " and protocol.server_echoes
    assert bytes([IAC, WILL, NAWS, IAC, SB, NAWS, 0, 120, 0, 40, IAC, SE]) in replies
    assert bytes([IAC, WILL, TTYPE]) in replies and bytes([IAC, DO, ECHO]) in replies
    assert bytes([IAC, WONT, 39]) in replies  # Refuses what it doesn't support
    shown, replies = protocol.feed(bytes([IAC, SB, TTYPE, 1, IAC, SE]))
    assert replies == bytes([IAC, SB, TTYPE, 0]) + b"XTERM" + bytes([IAC, SE])
    assert protocol.feed(bytes([IAC, DO, NAWS]))[1] == b""  # Doesn't repeat itself
    assert protocol.resize(80, 255) == bytes([IAC, SB, NAWS, 0, 80, 0, IAC, IAC, IAC, SE])  # 255 is doubled
    assert protocol.feed(bytes([IAC, IAC]) + b"x")[0] == bytes([255]) + b"x"
    protocol.feed(bytes([IAC, DONT, NAWS]))
    assert protocol.resize(100, 30) == b""
    assert protocol.feed(bytes([IAC, WONT, ECHO]))[0] == b"" and not protocol.server_echoes


def test_telnet_handles_commands_split_across_reads():
    protocol = TelnetProtocol()
    shown1, replies1 = protocol.feed(bytes([ord("a"), IAC]))
    shown2, replies2 = protocol.feed(bytes([DO, TTYPE, ord("b")]))
    assert shown1 + shown2 == b"ab" and replies2 == bytes([IAC, WILL, TTYPE])
    assert escape(b"\xffdata") == b"\xff\xffdata"


def test_telnet_option_can_be_switched_back_on():
    protocol = TelnetProtocol(size=(80, 24))
    assert protocol.feed(bytes([IAC, DO, NAWS]))[1].startswith(bytes([IAC, WILL, NAWS]))
    assert protocol.feed(bytes([IAC, DONT, NAWS]))[1] == bytes([IAC, WONT, NAWS])
    again = protocol.feed(bytes([IAC, DO, NAWS]))[1]
    assert again.startswith(bytes([IAC, WILL, NAWS])) and protocol.naws_agreed
