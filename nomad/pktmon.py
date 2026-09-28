"""Windows' built-in packet monitor (pktmon, Windows 10 1809 and later), shared by Switch Port and Packet Capture.

pktmon is one system-wide capture, so only one NOMAD feature can use it at a time: take `lock` first.
Everything here needs administrator rights.
"""
import shutil
import threading

from .system import CommandError, run_command

lock = threading.Lock()


class PktmonBusy(Exception):
    """Another part of NOMAD is already capturing."""


def find_pktmon():
    return shutil.which("pktmon.exe")


def pktmon(*arguments, timeout=120):
    return run_command(["pktmon", *arguments], timeout=timeout)


def quietly(*arguments):
    """Run a pktmon command whose failure doesn't matter (stopping a capture that isn't running, for example)."""
    try:
        pktmon(*arguments)
    except CommandError:
        pass


def reset():
    """Stop any capture left running and remove all packet filters, so a new capture starts clean."""
    quietly("stop")
    quietly("filter", "remove")


def add_filter(name, *conditions):
    """Add a packet filter, like add_filter("Web", "-t", "TCP", "-p", "443")."""
    pktmon("filter", "add", name, *conditions)


def start(etl_path, packet_size=0, file_size_mb=None):
    """Capture on every network adapter into etl_path. packet_size 0 keeps whole packets."""
    arguments = ["start", "--capture", "--comp", "nics", "--pkt-size", str(packet_size), "--file-name", etl_path]
    if file_size_mb:
        arguments += ["--file-size", str(int(file_size_mb))]
    pktmon(*arguments)


def stop():
    pktmon("stop")


def to_pcapng(etl_path, pcapng_path):
    """Convert a capture to pcapng, which Wireshark opens. Big captures take a while."""
    pktmon("etl2pcap", etl_path, "--out", pcapng_path, timeout=1800)


def acquire(what):
    """Take the pktmon lock without waiting. Raises PktmonBusy if another feature is using it."""
    if not lock.acquire(blocking=False):
        raise PktmonBusy(f"Windows' packet monitor is busy with another capture in NOMAD. Stop that first, then "
                         f"try {what} again.")
