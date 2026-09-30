"""Keyword highlighting rules, command buttons and escaped text (anti-idle), without the user interface."""
from nomad.terminal.commands import CommandButton, CommandStore
from nomad.terminal.highlight import DEFAULT_RULES, Highlighter, HighlightRule, HighlightStore
from nomad.terminal.sessions import Session, decode_escapes


def colored(text, rules=DEFAULT_RULES):
    return [(text[start:end], color) for start, end, color in Highlighter(rules).spans(text)]


def test_administratively_down_wins_over_down():
    assert colored("Gi1/0/2 is administratively down, line protocol is down") == \
        [("administratively down", "Amber"), ("down", "Red")]


def test_whole_words_only():
    assert colored("backup uptime") == []
    assert colored("Vlan10 is up, line protocol is up") == [("up", "Green"), ("up", "Green")]


def test_cisco_errors():
    assert colored("% Invalid input detected at '^' marker.") == [("% Invalid", "Red")]
    assert colored("Gi1/0/3   err-disabled") == [("err-disabled", "Red")]


def test_addresses():
    assert colored("10.1.2.3/24 via 192.168.0.1") == [("10.1.2.3/24", "Blue"), ("192.168.0.1", "Blue")]
    assert colored("0011.2233.4455 and aa:bb:cc:dd:ee:ff") == [("0011.2233.4455", "Purple"),
                                                               ("aa:bb:cc:dd:ee:ff", "Purple")]
    assert colored("version 999.1.1.1") == []  # Not an address


def test_case_and_bad_rules():
    rules = [HighlightRule("DOWN", "Red", match_case=True), HighlightRule("([", "Blue", regex=True)]
    assert colored("down DOWN", rules) == [("DOWN", "Red")]  # The bad pattern is skipped


def test_highlight_rules_are_saved(tmp_path):
    store = HighlightStore(str(tmp_path / "highlights.json"))
    heard = []
    store.listeners.append(lambda: heard.append(True))
    store.set_rules([HighlightRule("flapping", "Amber")])
    store.set_enabled(False)
    again = HighlightStore(str(tmp_path / "highlights.json"))
    assert [rule.pattern for rule in again.rules] == ["flapping"] and not again.enabled and heard


def test_command_buttons_are_saved_in_order(tmp_path):
    store = CommandStore(str(tmp_path / "commands.json"))
    first, second = CommandButton("One", "show clock"), CommandButton("Two", "show ver", press_enter=False)
    store.put(first)
    store.put(second)
    store.move(second.id, -1)
    store.put(CommandButton("Uno", "show clock", id=first.id))  # Edited in place
    again = CommandStore(str(tmp_path / "commands.json"))
    assert [(button.name, button.press_enter) for button in again.buttons] == [("Two", False), ("Uno", True)]
    again.delete(second.id)
    assert [button.name for button in CommandStore(str(tmp_path / "commands.json")).buttons] == ["Uno"]


def test_escapes():
    assert decode_escapes(Session("x").anti_idle_text) == " \b"
    assert decode_escapes(r"a\r\n\t\e\x03\\") == "a\r\n\t\x1b\x03\\"
    assert decode_escapes(r"\q \x9") == r"\q \x9"  # Unknown escapes stay as typed
