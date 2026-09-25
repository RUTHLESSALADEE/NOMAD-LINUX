"""TCP port checks: is a port open, closed (refused) or filtered (no answer) on a host."""
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

OPEN = "Open"
CLOSED = "Closed"
FILTERED = "Filtered"
UNREACHABLE = "Unreachable"
ERROR = "Error"

MAX_PORT = 65535
SCAN_PASSES = 2  # Filtered ports get a second try, since a single lost SYN looks the same as a firewall
TCP_NOSYNRETRIES = 9  # Windows retries a SYN that gets a reset for about 2 seconds unless this is set
UNREACHABLE_ERRORS = {10051, 10065}  # WSAENETUNREACH, WSAEHOSTUNREACH

# Port presets offered in the Ports tab, and the service names shown next to results
COMMON_PORTS = {
    20: "FTP data", 21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP", 88: "Kerberos",
    102: "Siemens S7", 110: "POP3", 111: "RPC portmapper", 135: "Microsoft RPC", 139: "NetBIOS session",
    143: "IMAP", 389: "LDAP", 443: "HTTPS", 445: "SMB", 465: "SMTPS", 502: "Modbus TCP", 515: "LPD printing",
    548: "AFP", 587: "SMTP submission", 631: "IPP printing", 636: "LDAPS", 873: "rsync", 993: "IMAPS",
    995: "POP3S", 1080: "SOCKS proxy", 1433: "SQL Server", 1521: "Oracle", 1723: "PPTP", 1883: "MQTT",
    2049: "NFS", 2222: "SSH (alternate)", 3268: "LDAP global catalog", 3306: "MySQL", 3389: "Remote Desktop",
    4840: "OPC UA", 5060: "SIP", 5061: "SIP TLS", 5432: "PostgreSQL", 5900: "VNC", 5985: "WinRM HTTP",
    5986: "WinRM HTTPS", 6379: "Redis", 8006: "Proxmox", 8080: "HTTP (alternate)", 8291: "MikroTik Winbox",
    8443: "HTTPS (alternate)", 8883: "MQTT TLS", 9100: "Printer (raw)", 9443: "HTTPS (alternate)",
    10000: "Webmin", 27017: "MongoDB", 44818: "EtherNet/IP",
}
PORT_PRESETS = [
    ("Common ports", ",".join(str(port) for port in COMMON_PORTS)),
    ("Web (80, 443, 8080, 8443)", "80,443,8080,8443"),
    ("Remote access (22, 23, 3389, 5900, 5985)", "22,23,3389,5900,5985,5986"),
    ("Windows file sharing (135, 139, 445)", "135,139,445"),
    ("Well-known (1-1024)", "1-1024"),
    ("All ports (1-65535)", "1-65535"),
]
WEB_PORTS = {80: "http", 443: "https", 8006: "https", 8080: "http", 8443: "https", 9443: "https", 10000: "https"}


@dataclass
class PortResult:
    port: int
    state: str
    rtt: Optional[float] = None  # Milliseconds to connect, for open and closed ports
    detail: str = ""

    @property
    def service(self):
        return COMMON_PORTS.get(self.port, "")


def parse_ports(text):
    """Parse a port list like "22, 80, 8000-8100" into a sorted list of unique ports.

    Raises ValueError with a message suitable for showing to the user.
    """
    ports = set()
    parts = [part.strip() for part in text.replace(";", ",").replace(" ", ",").split(",") if part.strip()]
    if not parts:
        raise ValueError("Enter one or more ports, such as 22, 80, 443 or 8000-8100.")
    for part in parts:
        first, dash, last = part.partition("-")
        try:
            low = int(first)
            high = int(last) if dash else low
        except ValueError:
            raise ValueError(f"'{part}' is not a port or a range of ports like 8000-8100.") from None
        if not (1 <= low <= MAX_PORT and 1 <= high <= MAX_PORT):
            raise ValueError(f"'{part}': ports go from 1 to {MAX_PORT}.")
        if low > high:
            raise ValueError(f"'{part}': the range should go from low to high.")
        ports.update(range(low, high + 1))
    return sorted(ports)


def check_port(address, family, port, timeout):
    """Try a TCP connection to address:port. timeout is in milliseconds."""
    socket_family = socket.AF_INET6 if family == 6 else socket.AF_INET
    host, _, scope = address.partition("%")
    target = (host, port, 0, int(scope)) if family == 6 and scope.isdigit() else (address, port)
    with socket.socket(socket_family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout / 1000)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, TCP_NOSYNRETRIES, 1)
        except OSError:  # Older Windows: closed ports just take a couple of seconds to report
            pass
        started = time.perf_counter()
        try:
            sock.connect(target)
        except (socket.timeout, TimeoutError):
            return PortResult(port, FILTERED, detail="No answer (a firewall may be dropping it)")
        except ConnectionRefusedError:
            return PortResult(port, CLOSED, (time.perf_counter() - started) * 1000, "Connection refused")
        except OSError as error:
            if getattr(error, "winerror", None) in UNREACHABLE_ERRORS:
                return PortResult(port, UNREACHABLE, detail=error.strerror or str(error))
            return PortResult(port, ERROR, detail=error.strerror or str(error))
        return PortResult(port, OPEN, (time.perf_counter() - started) * 1000)


def scan_ports(ports, probe, workers, passes=SCAN_PASSES, should_stop=lambda: False,
               result=lambda port_result: None, progress=lambda done, total: None):
    """Check every port, giving filtered ones another try on later passes.

    probe(port) returns a PortResult. result(port_result) is called once per port with its final
    result: straight away for open, closed and unreachable ports, or after the last pass for ports
    that stayed filtered. Returns {port: PortResult} for the ports checked before any stop request.
    """
    total = len(ports)
    done = 0
    results = {}
    pending = list(ports)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for pass_number in range(1, passes + 1):
            if should_stop() or not pending:
                break
            last_pass = pass_number == passes
            futures = {executor.submit(lambda port=port: None if should_stop() else probe(port)): port
                       for port in pending}
            next_pending = []
            for future in as_completed(futures):
                if should_stop():
                    for waiting in futures:
                        waiting.cancel()
                    break
                port = futures[future]
                port_result = future.result()
                if port_result is None:
                    continue
                results[port] = port_result
                if port_result.state == FILTERED and not last_pass:
                    next_pending.append(port)
                    continue
                done += 1
                result(port_result)
                progress(done, total)
            pending = next_pending
    return results


def summarize(results):
    """Counts of each state, like "3 open, 2 closed, 995 filtered"."""
    counts = {}
    for port_result in results:
        counts[port_result.state] = counts.get(port_result.state, 0) + 1
    order = [OPEN, CLOSED, FILTERED, UNREACHABLE, ERROR]
    return ", ".join(f"{counts[state]} {state.lower()}" for state in order if counts.get(state))
