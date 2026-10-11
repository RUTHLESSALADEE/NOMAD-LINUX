"""Sending configuration to Cisco IOS / IOS-XE switches over SSH with no one watching (Port Mirroring's Set Up and
Take Away): log in (the switch's saved SSH session, else the login chosen), go to the enable (#) prompt, type the
lines one at a time, and stop at the first one the switch refuses.

Nothing is ever saved (no write memory): a switch that reloads comes back without what was sent.

Qt-free: the Port Mirroring page runs it on a worker thread.
"""
import logging
import re
import threading
from dataclasses import dataclass, field

import paramiko

from ..terminal.transports import Cancelled, ConnectionFailed
from . import macssh, mirror

log = logging.getLogger(__name__)

LINE_TIMEOUT = 30  # Seconds for one configuration line (creating a VLAN can take a while on a Catalyst 9000)
ENABLE_TIMEOUT = 15
PASSWORD_PROMPT = re.compile(r"(?:password|secret)\s*:\s*$", re.IGNORECASE)
CONFIRM_PROMPT = re.compile(r"\[(?:confirm|yes/no|y/n)\]\s*:?\s*$", re.IGNORECASE)


class ConfigShell(macssh.SshShell):
    """A switch's command line that can go to enable and configure."""

    def __init__(self, session, vault=None, known_hosts=None, trust_new=False, password=None,
                 should_stop=lambda: False, enable_secret=""):
        super().__init__(session, vault, known_hosts, trust_new, password, should_stop)
        self.enable_secret = enable_secret

    def open(self):
        super().open()
        if self.prompt.endswith(">"):
            self.enable()

    def enable(self):
        """From the > prompt to the # one, with the enable password given (if it asks for one)."""
        self.channel.send("enable\n")
        text = self.read(ENABLE_TIMEOUT, done=lambda tail: bool(PASSWORD_PROMPT.search(tail) or
                                                                 macssh.PROMPT.match(tail)))
        tail = last_line(text)
        if PASSWORD_PROMPT.search(tail):
            if not self.enable_secret:
                self.channel.send("\n")  # Give up on it (IOS asks three times, then says it's bad)
                self.read(ENABLE_TIMEOUT, done=lambda tail: bool(PASSWORD_PROMPT.search(tail) or
                                                                  macssh.PROMPT.match(tail)))
                raise ConnectionFailed("it logs in at the > prompt and asks for an enable password: enter it in "
                                       "Enable password.")
            self.channel.send(self.enable_secret + "\n")
            text = self.read(ENABLE_TIMEOUT, done=lambda tail: bool(PASSWORD_PROMPT.search(tail) or
                                                                     macssh.PROMPT.match(tail)))
            tail = last_line(text)
            if PASSWORD_PROMPT.search(tail):
                self.channel.send("\x03")
                raise ConnectionFailed("it refused the enable password.")
        if not tail.endswith("#"):
            raise ConnectionFailed(f"enable didn't give the # prompt ({tail or 'nothing'}).")
        self.prompt = tail

    def line(self, line, timeout=LINE_TIMEOUT):
        """Type one configuration line: what the switch answered (without the line echoed and the prompt after)."""
        self.channel.send(line + "\n")
        text = self.read(timeout, done=lambda tail: bool(macssh.PROMPT.match(tail) or CONFIRM_PROMPT.search(tail)))
        lines = text.rstrip().splitlines()
        if lines and CONFIRM_PROMPT.search(lines[-1]):
            self.channel.send("\n")  # Take the default ([confirm] is yes)
            lines += self.read(timeout).rstrip().splitlines()
        wanted = line.strip()
        echoed = next((index for index, item in enumerate(lines) if wanted and wanted in item), None)
        if echoed is not None:
            lines = lines[echoed + 1:]
        if lines and macssh.PROMPT.match(lines[-1].strip()):
            lines = lines[:-1]
        return "\n".join(lines)

    def configure(self, text, stop_on_error=True):
        """Type each line of text. Returns (transcript, [(line, [error lines])] of the lines refused). Stops at the
        first refused when stop_on_error (and leaves configuration mode)."""
        transcript, refused = [], []
        in_config = False
        for line in text.splitlines():
            if not line.strip():
                continue
            answer = self.line(line)
            transcript.append(line.rstrip())
            if answer.strip():
                transcript.append(answer.rstrip())
            word = line.strip()
            if word.startswith("conf"):
                in_config = True
            elif word == "end":
                in_config = False
            errors = mirror.errors_in(answer)
            if errors:
                refused.append((word, errors))
                if stop_on_error:
                    if in_config:
                        self.line("end")
                    break
        return "\n".join(transcript), refused


def last_line(text):
    lines = (text or "").rstrip().splitlines()
    return lines[-1].strip() if lines else ""


@dataclass
class Outcome:
    """How sending to one switch went."""
    ok: bool = False
    transcript: str = ""
    refused: list = field(default_factory=list)  # [(line, [error lines])]
    applet_ok: object = None  # True, False (refused), None (none sent)
    problem: str = ""  # Couldn't log in, or what was refused, in a sentence

    @property
    def first_error(self):
        if self.refused:
            line, errors = self.refused[0]
            return f"{line}: {errors[0]}"
        return self.problem


class Pusher:
    """Logs in to each switch once (kept until close) and sends to it."""

    def __init__(self, logins, vault=None, known_hosts=None, trust_new=False, enable_secret="",
                 should_stop=lambda: False, shell_factory=ConfigShell):
        self.logins, self.vault, self.known_hosts = logins, vault, known_hosts
        self.trust_new, self.enable_secret, self.should_stop = trust_new, enable_secret, should_stop
        self.shell_factory = shell_factory
        self.shells = {}
        self.problems = {}
        self.login_labels = {}
        self.lock = threading.Lock()

    def shell(self, address, names=()):
        """The switch's command line at the # prompt, or None (and why in problems[address])."""
        if address in self.shells:
            return self.shells[address]
        session, self.login_labels[address] = self.logins.login_for(address, names)
        if session is None:
            self.problems[address] = "no saved SSH session or login to log in with."
            self.shells[address] = None
            return None
        shell = self.shell_factory(session, self.vault, self.known_hosts, self.trust_new, self.logins.password,
                                   self.should_stop, enable_secret=self.enable_secret)
        try:
            shell.open()
        except Cancelled:
            shell.close()
            shell = None
            self.problems[address] = "stopped."
        except ConnectionFailed as error:
            shell.close()
            shell = None
            self.problems[address] = str(error)
        except (paramiko.SSHException, OSError, EOFError) as error:
            shell.close()
            shell = None
            self.problems[address] = f"SSH failed: {error}"
        self.shells[address] = shell
        return shell

    def forget(self, address):
        shell = self.shells.pop(address, None)
        if shell is not None:
            shell.close()

    def read_state(self, address, names=()):
        """mirror.SwitchState: the switch's sessions and NOMAD's applets."""
        shell = self.shell(address, names)
        if shell is None:
            return mirror.SwitchState(problem=self.problems.get(address, ""))
        try:
            text = ""
            for command in ("show monitor session all", "show monitor"):
                text = shell.run(command)
                if not macssh.INVALID.search(text):
                    break
            else:
                return mirror.SwitchState(problem="it doesn't know show monitor (no SPAN on it?).")
            applets = shell.run("show running-config | include event manager applet")
        except Cancelled:
            return mirror.SwitchState(problem="stopped.")
        except (ConnectionFailed, paramiko.SSHException, OSError, EOFError) as error:
            self.forget(address)
            return mirror.SwitchState(problem=str(error))
        return mirror.SwitchState(read=True, sessions=mirror.parse_sessions(text), applets=mirror.parse_applets(applets))

    def set_up(self, address, setup, applet="", names=()):
        """Send a step's lines (setup, then the applet, if any). ok when every setup line was taken; an applet
        refused leaves applet_ok False (and is cleaned away)."""
        outcome = Outcome()
        shell = self.shell(address, names)
        if shell is None:
            outcome.problem = f"Couldn't log in: {self.problems.get(address, 'no reason given')}"
            return outcome
        try:
            outcome.transcript, outcome.refused = shell.configure(setup)
            outcome.ok = not outcome.refused
            if outcome.ok and applet:
                transcript, refused = shell.configure(applet)
                outcome.transcript += "\n" + transcript
                outcome.applet_ok = not refused
                if refused:
                    name = re.search(r"event manager applet (\S+)", applet)
                    if name:
                        shell.configure(f"configure terminal\nno event manager applet {name.group(1)}\nend",
                                        stop_on_error=False)
        except Cancelled:
            outcome.problem = "Stopped."
        except (ConnectionFailed, paramiko.SSHException, OSError, EOFError) as error:
            self.forget(address)
            outcome.ok = False
            outcome.problem = f"The connection failed partway: {error}"
        if outcome.refused and not outcome.problem:
            outcome.problem = f"It refused {outcome.first_error}"
        return outcome

    def take_away(self, address, undo, names=()):
        """Send a step's undo, carrying on past lines refused. ok when the session (and VLAN, trunks) went: a missing
        applet (it already ran) or something already gone isn't a failure."""
        outcome = Outcome()
        shell = self.shell(address, names)
        if shell is None:
            outcome.problem = f"Couldn't log in: {self.problems.get(address, 'no reason given')}"
            return outcome
        try:
            outcome.transcript, refused = shell.configure(undo, stop_on_error=False)
        except Cancelled:
            outcome.problem = "Stopped."
            return outcome
        except (ConnectionFailed, paramiko.SSHException, OSError, EOFError) as error:
            self.forget(address)
            outcome.problem = f"The connection failed partway: {error}"
            return outcome
        outcome.refused = [(line, errors) for line, errors in refused if not harmless_undo(line, errors)]
        outcome.ok = not outcome.refused
        if outcome.refused:
            outcome.problem = f"It refused {outcome.first_error}"
        return outcome

    def close(self):
        shells, self.shells = list(self.shells.values()), {}
        for shell in shells:
            if shell is not None:
                shell.close()


def harmless_undo(line, errors):
    """A line of an undo refused only because what it takes away is gone already."""
    text = " ".join(errors).lower()
    if line.startswith("no event manager applet"):
        return True
    return any(word in text for word in ("not found", "does not exist", "not exist", "no such", "not configured",
                                         "not present"))
