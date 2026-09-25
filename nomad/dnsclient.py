"""A small DNS client that asks one server directly over UDP, for timing and comparing DNS servers.

Unlike Resolve-DnsName it bypasses Windows' DNS cache and client, so the time measured is the server's.
"""
import ipaddress
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

TYPE_A, TYPE_CNAME, TYPE_PTR, TYPE_AAAA = 1, 5, 12, 28
TYPE_NAMES = {TYPE_A: "A", TYPE_CNAME: "CNAME", TYPE_PTR: "PTR", TYPE_AAAA: "AAAA"}
CLASS_IN = 1
DNS_PORT = 53
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
NOERROR, NXDOMAIN = 0, 3
USABLE_RCODES = {NOERROR, NXDOMAIN}  # The server did its job, even if the name doesn't exist
RCODE_MEANINGS = {
    "SERVFAIL": "the server couldn't get an answer (often no route to the internet or its upstream servers)",
    "REFUSED": "the server won't answer queries from this computer",
    "NOTIMP": "the server doesn't support this kind of query",
    "FORMERR": "the server didn't understand the query",
}


@dataclass
class DnsAnswer:
    rcode: int
    rtt: float  # Milliseconds
    records: list = field(default_factory=list)  # (type name, value)
    truncated: bool = False

    @property
    def rcode_name(self):
        return RCODES.get(self.rcode, f"RCODE {self.rcode}")

    def values(self, type_name):
        return [value for record_type, value in self.records if record_type == type_name]


def encode_name(name):
    labels = [label for label in name.strip().rstrip(".").split(".") if label]
    encoded = b""
    for label in labels:
        raw = label.encode("idna") if not label.isascii() else label.encode("ascii")
        if len(raw) > 63:
            raise ValueError(f"'{label}' is longer than the 63 characters DNS allows in one part of a name.")
        encoded += bytes([len(raw)]) + raw
    return encoded + b"\0"


def build_query(transaction_id, name, record_type):
    header = struct.pack(">HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)  # Recursion desired
    return header + encode_name(name) + struct.pack(">HH", record_type, CLASS_IN)


def read_name(data, offset):
    """Decode a (possibly compressed) name. Returns (name, offset just past it)."""
    labels = []
    end = None
    for _ in range(128):  # Guards against compression loops
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        offset += 1
        if length == 0:
            return ".".join(labels), end if end is not None else offset
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length
    raise ValueError("The response's names loop back on themselves.")


def parse_response(data, transaction_id=None):
    """Parse a response into a DnsAnswer (with rtt 0). Raises ValueError if it isn't a valid response."""
    try:
        response_id, flags, questions, answers = struct.unpack_from(">HHHH", data)
        if transaction_id is not None and response_id != transaction_id:
            raise ValueError("Response is for a different query.")
        if not flags & 0x8000:
            raise ValueError("Not a response.")
        offset = 12
        for _ in range(questions):
            _, offset = read_name(data, offset)
            offset += 4
        records = []
        for _ in range(answers):
            _, offset = read_name(data, offset)
            record_type, _, _, length = struct.unpack_from(">HHIH", data, offset)
            offset += 10
            rdata = data[offset:offset + length]
            if record_type == TYPE_A and length == 4:
                records.append(("A", socket.inet_ntoa(rdata)))
            elif record_type == TYPE_AAAA and length == 16:
                records.append(("AAAA", str(ipaddress.IPv6Address(rdata))))
            elif record_type in (TYPE_CNAME, TYPE_PTR):
                records.append((TYPE_NAMES[record_type], read_name(data, offset)[0]))
            offset += length
        return DnsAnswer(flags & 0x000F, 0.0, records, bool(flags & 0x0200))
    except (IndexError, struct.error):
        raise ValueError("Response is too short.") from None


def reverse_name(address):
    """The PTR name for an address, like 1.1.168.192.in-addr.arpa."""
    return ipaddress.ip_address(address.partition("%")[0]).reverse_pointer


def query(server, name, record_type=TYPE_A, timeout=2000):
    """Ask server for name's records. Raises TimeoutError if it doesn't answer, OSError if it can't be reached."""
    transaction_id = struct.unpack(">H", os.urandom(2))[0]
    address = ipaddress.ip_address(server.partition("%")[0])
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    request = build_query(transaction_id, name, record_type)
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout / 1000)
        started = time.perf_counter()
        sock.sendto(request, (server, DNS_PORT))
        deadline = started + timeout / 1000
        while True:
            try:
                data, sender = sock.recvfrom(4096)
            except socket.timeout:
                raise TimeoutError(f"{server} didn't answer within {timeout} ms.") from None
            except ConnectionResetError:  # Windows reports ICMP port unreachable this way
                raise OSError(f"{server} isn't running a DNS server (port 53 is closed).") from None
            elapsed = (time.perf_counter() - started) * 1000
            try:
                answer = parse_response(data, transaction_id)
            except ValueError:
                sock.settimeout(max(0.01, deadline - time.perf_counter()))
                continue
            answer.rtt = elapsed
            return answer


# ----------------------------------------------------------------- Comparing servers

@dataclass
class ServerResult:
    server: str
    label: str = ""
    sent: int = 0
    rtts: list = field(default_factory=list)  # Milliseconds, for queries the server handled
    failures: dict = field(default_factory=dict)  # Problem -> count
    first_rtt: Optional[float] = None  # Usually uncached at the server, so slower

    @property
    def answered(self):
        return len(self.rtts)

    @property
    def average(self):
        return sum(self.rtts) / len(self.rtts) if self.rtts else None

    @property
    def median(self):
        if not self.rtts:
            return None
        ordered = sorted(self.rtts)
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2

    @property
    def status(self):
        if not self.sent:
            return ""
        if not self.failures:
            return "OK"
        problems = ", ".join(f"{problem} ×{count}" if count > 1 else problem
                             for problem, count in self.failures.items())
        return problems if self.answered else f"Failed: {problems}"


def benchmark_server(server, names, rounds=3, timeout=2000, label="", should_stop=lambda: False, ask=None):
    """Time how quickly a server answers each name, several times over.

    ask(server, name, record_type, timeout) defaults to query(). NXDOMAIN counts as a good answer: the
    server did its job. SERVFAIL, REFUSED, timeouts and unreachable servers count as failures.
    """
    ask = ask or query
    result = ServerResult(server, label)
    for _ in range(rounds):
        for name in names:
            if should_stop():
                return result
            result.sent += 1
            try:
                answer = ask(server, name, TYPE_A, timeout)
            except TimeoutError:
                problem = "timed out"
            except OSError as error:
                problem = str(error).rstrip(".") or "unreachable"
            else:
                if answer.rcode in USABLE_RCODES:
                    if result.first_rtt is None:
                        result.first_rtt = answer.rtt
                    result.rtts.append(answer.rtt)
                    continue
                problem = answer.rcode_name
            result.failures[problem] = result.failures.get(problem, 0) + 1
    return result


# ----------------------------------------------------------------- Forward / reverse check

@dataclass
class ForwardReverse:
    address: str
    ptr_names: list  # What the address's PTR record says
    matches: bool  # A PTR name resolves back to the address
    problem: str = ""


def forward_reverse(name, server, timeout=2000, ask=None):
    """Check that name's addresses have PTR records that point back to name (forward-confirmed reverse DNS).

    Returns (addresses, [ForwardReverse]). Raises ValueError if name doesn't resolve at all.
    """
    ask = ask or query
    addresses, errors, answered = [], [], False
    for record_type, type_name in ((TYPE_A, "A"), (TYPE_AAAA, "AAAA")):
        try:
            answer = ask(server, name, record_type, timeout)
        except (TimeoutError, OSError) as error:
            errors.append(str(error) or f"{server} didn't answer.")
            continue
        answered = True
        addresses += answer.values(type_name)
    if not answered:
        raise ValueError(errors[0])
    if not addresses:
        raise ValueError(f"{name} has no A or AAAA records on {server}.")
    checks = []
    for address in addresses:
        try:
            ptr = ask(server, reverse_name(address), TYPE_PTR, timeout)
        except (TimeoutError, OSError) as error:
            checks.append(ForwardReverse(address, [], False, str(error) or "No answer"))
            continue
        names = ptr.values("PTR")
        if not names:
            checks.append(ForwardReverse(address, [], False, "No PTR record"))
            continue
        matched = False
        for ptr_name in names:
            record_type, type_name = (TYPE_AAAA, "AAAA") if ":" in address else (TYPE_A, "A")
            try:
                back = ask(server, ptr_name, record_type, timeout)
            except (TimeoutError, OSError):
                continue
            if address in back.values(type_name):
                matched = True
                break
        checks.append(ForwardReverse(address, names, matched,
                                     "" if matched else "The PTR name doesn't resolve back to this address"))
    return addresses, checks
