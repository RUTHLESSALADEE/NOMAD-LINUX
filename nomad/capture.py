"""Packet capture with Windows' built-in pktmon, saved as a pcapng file that Wireshark opens."""
import glob
import ipaddress
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import pktmon
from .etw import EtwError
from .packets.reader import Record
from .pktmon_live import LiveFeed
from .promisc import Promiscuous, PromiscuousError

log = logging.getLogger(__name__)

PROTOCOLS = ["Any", "TCP", "UDP", "ICMP", "ICMPv6", "ARP"]
FILTER_NAME = "NOMAD-Capture"
DEFAULT_FILE_SIZE_MB = 512
HEADERS_ONLY_BYTES = 128  # pktmon's default: enough for the headers of most packets


@dataclass
class CaptureFilter:
    address: str = ""  # An IP address or a subnet in CIDR form
    port: str = ""
    protocol: str = "Any"

    @property
    def empty(self):
        return not self.address and not self.port and self.protocol == "Any"

    def describe(self):
        parts = []
        if self.protocol != "Any":
            parts.append(self.protocol)
        if self.address:
            parts.append(f"to or from {self.address}")
        if self.port:
            parts.append(f"port {self.port}")
        return " ".join(parts) if parts else "all traffic"


def validate_filter(address, port, protocol):
    """Returns a CaptureFilter. Raises ValueError with a message suitable for showing to the user."""
    address, port = address.strip(), port.strip()
    if address:
        try:
            address = str(ipaddress.ip_network(address, strict=False)) if "/" in address else \
                str(ipaddress.ip_address(address))
        except ValueError:
            raise ValueError(f"'{address}' is not an IP address or subnet (such as 10.0.0.5 or 10.0.0.0/24).") \
                from None
    if port:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(f"'{port}' is not a port number from 1 to 65535.")
        port = str(int(port))
    if protocol not in PROTOCOLS:
        raise ValueError(f"Unknown protocol {protocol}.")
    if port and protocol in ("ICMP", "ICMPv6", "ARP"):
        raise ValueError(f"{protocol} doesn't use ports. Clear the port, or choose TCP, UDP or Any.")
    return CaptureFilter(address, port, protocol)


def filter_arguments(capture_filter):
    """pktmon filter add arguments for a CaptureFilter, or None to capture everything."""
    if capture_filter.empty:
        return None
    arguments = []
    if capture_filter.protocol == "ARP":
        arguments += ["-d", "ARP"]
    elif capture_filter.protocol != "Any":
        arguments += ["-t", capture_filter.protocol]
    if capture_filter.address:
        arguments += ["-i", capture_filter.address]
    if capture_filter.port:
        arguments += ["-p", capture_filter.port]
    return arguments


def bpf_filter_expression(capture_filter):
    """Convert a CaptureFilter into a standard tcpdump / pcap BPF filter expression."""
    if capture_filter.empty:
        return ""
    terms = []
    if capture_filter.protocol and capture_filter.protocol != "Any":
        proto = capture_filter.protocol.lower()
        if proto == "icmpv6":
            proto = "icmp6"
        terms.append(proto)
    if capture_filter.address:
        addr = capture_filter.address
        if "/" in addr:
            terms.append(f"net {addr}")
        else:
            terms.append(f"host {addr}")
    if capture_filter.port:
        terms.append(f"port {capture_filter.port}")
    return " and ".join(terms)


def default_output_path(folder=None):
    folder = folder or os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(folder):
        folder = os.path.expanduser("~")
    ext = "pcapng" if os.name == "nt" else "pcap"
    return os.path.join(folder, f"NOMAD capture {time.strftime('%Y-%m-%d %H%M%S')}.{ext}")


def find_wireshark():
    candidates = [
        shutil.which("wireshark"), shutil.which("Wireshark"),
        shutil.which("Wireshark.exe"), r"C:\Program Files\Wireshark\Wireshark.exe",
        r"C:\Program Files (x86)\Wireshark\Wireshark.exe"
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


class PcapFileLiveFeed:
    """Follows a pcap file written by tcpdump in real time, yielding Record objects for LiveCapture."""

    def __init__(self, pcap_path):
        self.path = pcap_path
        self.queue = deque()
        self.lock = threading.Lock()
        self.thread = None
        self.running = False
        self.order = "<"
        self.units = 1e6
        self.link_type = 1

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._tail, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def _tail(self):
        # Wait for file creation
        for _ in range(50):
            if os.path.isfile(self.path) and os.path.getsize(self.path) >= 24:
                break
            time.sleep(0.05)
            if not self.running:
                return

        try:
            with open(self.path, "rb") as f:
                hdr = f.read(24)
                if len(hdr) < 24:
                    return
                magic = hdr[:4]
                self.order = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
                self.units = 1e6 if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4") else 1e9
                self.link_type = struct.unpack(self.order + "I", hdr[20:24])[0] & 0xFFFF

                while self.running:
                    pkthdr = f.read(16)
                    if len(pkthdr) < 16:
                        time.sleep(0.05)
                        continue
                    sec, usec, caplen, origlen = struct.unpack(self.order + "IIII", pkthdr)
                    data = b""
                    while len(data) < caplen and self.running:
                        chunk = f.read(caplen - len(data))
                        if not chunk:
                            time.sleep(0.02)
                            continue
                        data += chunk
                    if len(data) == caplen:
                        rec = Record(self.link_type, sec + usec / self.units, data, max(caplen, origlen))
                        with self.lock:
                            self.queue.append(rec)
        except Exception as e:
            log.warning("PcapFileLiveFeed error: %s", e)

    def take(self, now=None, everything=False):
        with self.lock:
            res = list(self.queue)
            self.queue.clear()
            return res

    @property
    def waiting(self):
        with self.lock:
            return len(self.queue)

    def missed(self):
        return 0, False


class Capture:
    """One capture: start() it, then stop() and save() it. Holds the pktmon lock in between."""

    def __init__(self, capture_filter, whole_packets=True, file_size_mb=DEFAULT_FILE_SIZE_MB, promiscuous_address="",
                 live=False):
        self.filter, self.whole_packets, self.file_size_mb = capture_filter, whole_packets, file_size_mb
        self.want_live = live
        self.feed = None
        self.live_problem = ""
        # An adapter on a SPAN port: held in promiscuous mode while capturing, so the copies (addressed to other
        # machines) get through to pktmon
        self.promiscuous = Promiscuous(promiscuous_address) if promiscuous_address else None
        self.promiscuous_problem = ""
        self.folder = None
        self.etl = None
        self.pcap = None
        self.process = None
        self.started = None
        self.running = False
        self.holds_lock = False

    def start(self):
        """Raises pktmon.PktmonBusy, CommandError (e.g. without administrator rights) or OSError."""
        if not pktmon.find_pktmon():
            tool = "pktmon" if os.name == "nt" else "tcpdump"
            raise OSError(f"{tool} isn't available for packet capture.")
        pktmon.acquire("capturing")
        self.holds_lock = True
        try:
            is_nt = os.name == "nt" or "pktmon" in (pktmon.find_pktmon() or "")
            if is_nt:
                self.folder = tempfile.mkdtemp(prefix="nomad-capture-")
                self.etl = os.path.join(self.folder, "capture.etl")
                pktmon.reset()
                arguments = filter_arguments(self.filter)
                if arguments:
                    pktmon.add_filter(FILTER_NAME, *arguments)
                if self.want_live:
                    self.feed = LiveFeed()
                    self.start_feed()
                pktmon.start(self.etl, 0 if self.whole_packets else HEADERS_ONLY_BYTES, self.file_size_mb)
            else:
                self.folder = tempfile.mkdtemp(prefix="nomad-capture-", dir="/tmp")
                os.chmod(self.folder, 0o777)
                self.pcap = os.path.join(self.folder, "capture.pcap")
                snaplen = "0" if self.whole_packets else str(HEADERS_ONLY_BYTES)
                cmd = ["tcpdump", "-i", "any", "-U", "-s", snaplen, "-w", self.pcap]
                bpf = bpf_filter_expression(self.filter)
                if bpf:
                    cmd.extend(["--", bpf])
                env = os.environ.copy()
                env.pop("LD_LIBRARY_PATH", None)
                self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
                # Wait briefly to confirm tcpdump started and created the pcap file
                for _ in range(20):
                    time.sleep(0.05)
                    if os.path.exists(self.pcap) and os.path.getsize(self.pcap) >= 24:
                        break
                    if self.process.poll() is not None:
                        err = self.process.stderr.read().decode("utf-8", "replace") if self.process.stderr else ""
                        raise OSError(f"tcpdump failed to start: {err.strip()}")
                if self.want_live:
                    self.feed = PcapFileLiveFeed(self.pcap)
                    self.feed.start()
        except BaseException:
            self.cleanup()
            raise
        if self.promiscuous is not None:
            try:
                self.promiscuous.start()
            except PromiscuousError as error:  # Capture anyway: this PC's own traffic still comes through
                self.promiscuous_problem = str(error)
        self.started = time.monotonic()
        self.running = True

    def start_feed(self):
        """Start listening for the live view on Windows (before pktmon starts, so it sees the first packets)."""
        if self.feed is None:
            return
        try:
            self.feed.start()
        except (EtwError, OSError) as error:  # Capture anyway: the packets open after Stop and Save
            self.live_problem = str(error)
            self.feed = None

    @property
    def live(self):
        """Whether the live view gets the packets."""
        return self.feed is not None

    def size(self):
        """Bytes captured so far (pktmon writes the file as it goes)."""
        if self.pcap and os.path.exists(self.pcap):
            try:
                return os.path.getsize(self.pcap)
            except OSError:
                return 0
        total = 0
        for path in glob.glob(os.path.join(self.folder or "", "capture*.etl")):
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    def elapsed(self):
        return 0.0 if self.started is None else time.monotonic() - self.started

    def stop(self):
        if self.running:
            self.running = False
            if self.process is not None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                self.process = None
            else:
                pktmon.stop()
        if self.promiscuous is not None:
            self.promiscuous.stop()
        if self.feed is not None:
            self.feed.stop()

    def save(self, output_path):
        """Convert the capture to pcapng at output_path. Raises CommandError or OSError."""
        folder = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(folder, exist_ok=True)
        if self.pcap and os.path.isfile(self.pcap) and not self.etl:
            # On Linux: copy or convert the recorded pcap to output_path
            shutil.copyfile(self.pcap, output_path)
        else:
            pktmon.to_pcapng(self.etl, output_path)
        if not os.path.isfile(output_path):
            raise OSError("Packet capture didn't write the capture file. The capture may have been empty.")
        return os.path.getsize(output_path)

    def cleanup(self):
        """Stop pktmon/tcpdump if still running, remove its filters and the temporary files, and release the lock."""
        try:
            if self.promiscuous is not None:
                self.promiscuous.stop()
            if self.feed is not None:
                self.feed.stop()
            if self.running:
                self.running = False
                if self.process is not None:
                    try:
                        self.process.terminate()
                        self.process.wait(timeout=2)
                    except Exception:
                        try:
                            self.process.kill()
                        except Exception:
                            pass
                    self.process = None
                elif os.name == "nt" or self.etl:
                    pktmon.quietly("stop")
            if os.name == "nt" or self.etl:
                pktmon.quietly("filter", "remove")
        finally:
            if self.folder:
                shutil.rmtree(self.folder, ignore_errors=True)
                self.folder = None
            if self.holds_lock:
                self.holds_lock = False
                pktmon.lock.release()
