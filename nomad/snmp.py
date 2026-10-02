"""SNMP v1/v2c get and walk, for reading a switch's, router's or printer's details and interface counters.

Speaks the protocol directly (BER encoding over UDP), so nothing extra needs installing.
"""
import os
import socket
from dataclasses import dataclass, field
from typing import Optional

SNMP_PORT = 161
V1, V2C = 0, 1
VERSIONS = {"v2c": V2C, "v1": V1}

# BER tags
INTEGER, OCTET_STRING, NULL, OBJECT_ID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
IP_ADDRESS, COUNTER32, GAUGE32, TIMETICKS, OPAQUE, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW = 0x80, 0x81, 0x82
GET, GET_NEXT, RESPONSE, GET_BULK = 0xA0, 0xA1, 0xA2, 0xA5
TRAP_V1, INFORM, TRAP_V2 = 0xA4, 0xA6, 0xA7
TRAP_PORT = 162
SNMP_TRAP_OID = (1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0)  # snmpTrapOID.0: which trap a v2c trap is
SNMP_TRAPS = (1, 3, 6, 1, 6, 3, 1, 1, 5)  # coldStart.1, warmStart.2, linkDown.3, linkUp.4, ...: v1 generic traps + 1
TYPE_NAMES = {INTEGER: "Integer", OCTET_STRING: "String", NULL: "Null", OBJECT_ID: "OID", IP_ADDRESS: "IpAddress",
              COUNTER32: "Counter32", GAUGE32: "Gauge32", TIMETICKS: "Timeticks", OPAQUE: "Opaque",
              COUNTER64: "Counter64", NO_SUCH_OBJECT: "No such object", NO_SUCH_INSTANCE: "No such instance",
              END_OF_MIB_VIEW: "End of MIB view"}
EXCEPTIONS = {NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW}
ERRORS = {1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess", 7: "wrongType",
          8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation", 12: "inconsistentValue",
          13: "resourceUnavailable", 14: "commitFailed", 15: "undoFailed", 16: "authorizationError",
          17: "notWritable", 18: "inconsistentName"}
NO_SUCH_NAME, TOO_BIG = 2, 1
MAX_ROWS = 200000

SYSTEM = "1.3.6.1.2.1.1"
IF_ENTRY = "1.3.6.1.2.1.2.2.1"
IFX_ENTRY = "1.3.6.1.2.1.31.1.1.1"

WALK_PRESETS = [
    ("System (name, description, uptime)", SYSTEM),
    ("Interfaces (ifTable)", "1.3.6.1.2.1.2.2"),
    ("Interface names and aliases (ifXTable)", "1.3.6.1.2.1.31.1.1"),
    ("IP addresses", "1.3.6.1.2.1.4.20"),
    ("ARP table (IP to MAC)", "1.3.6.1.2.1.4.22"),
    ("MAC address table (switches)", "1.3.6.1.2.1.17.4.3"),
    ("LLDP neighbors", "1.0.8802.1.1.2.1.4"),
    ("CDP neighbors (Cisco)", "1.3.6.1.4.1.9.9.23.1.2.1"),
    ("VLANs (Cisco VTP)", "1.3.6.1.4.1.9.9.46.1.3.1"),
    ("Hardware and serial numbers (ENTITY-MIB)", "1.3.6.1.2.1.47.1.1.1"),
    ("Host resources (storage, software)", "1.3.6.1.2.1.25"),
    ("All standard MIBs (MIB-2)", "1.3.6.1.2.1"),
    ("Vendor-specific (enterprises)", "1.3.6.1.4.1"),
]

OID_NAMES = {
    "1.3.6.1.2.1.1": "system", "1.3.6.1.2.1.1.1": "sysDescr", "1.3.6.1.2.1.1.2": "sysObjectID",
    "1.3.6.1.2.1.1.3": "sysUpTime", "1.3.6.1.2.1.1.4": "sysContact", "1.3.6.1.2.1.1.5": "sysName",
    "1.3.6.1.2.1.1.6": "sysLocation", "1.3.6.1.2.1.1.7": "sysServices", "1.3.6.1.2.1.1.8": "sysORLastChange",
    "1.3.6.1.2.1.2.1": "ifNumber", "1.3.6.1.2.1.3": "at", "1.3.6.1.2.1.4": "ip", "1.3.6.1.2.1.5": "icmp",
    "1.3.6.1.2.1.6": "tcp", "1.3.6.1.2.1.7": "udp", "1.3.6.1.2.1.11": "snmp",
    "1.3.6.1.2.1.4.20.1.1": "ipAdEntAddr", "1.3.6.1.2.1.4.20.1.2": "ipAdEntIfIndex",
    "1.3.6.1.2.1.4.20.1.3": "ipAdEntNetMask", "1.3.6.1.2.1.4.20.1.4": "ipAdEntBcastAddr",
    "1.3.6.1.2.1.4.21": "ipRouteTable",
    "1.3.6.1.2.1.4.22.1.1": "ipNetToMediaIfIndex", "1.3.6.1.2.1.4.22.1.2": "ipNetToMediaPhysAddress",
    "1.3.6.1.2.1.4.22.1.3": "ipNetToMediaNetAddress", "1.3.6.1.2.1.4.22.1.4": "ipNetToMediaType",
    "1.3.6.1.2.1.17": "dot1dBridge", "1.3.6.1.2.1.17.4.3.1.1": "dot1dTpFdbAddress",
    "1.3.6.1.2.1.17.4.3.1.2": "dot1dTpFdbPort", "1.3.6.1.2.1.17.4.3.1.3": "dot1dTpFdbStatus",
    "1.3.6.1.2.1.25": "host", "1.3.6.1.2.1.25.1": "hrSystem", "1.3.6.1.2.1.25.2": "hrStorage",
    "1.3.6.1.2.1.25.3": "hrDevice", "1.3.6.1.2.1.25.4": "hrSWRun", "1.3.6.1.2.1.43": "printmib",
    "1.3.6.1.2.1.47.1.1.1.1.2": "entPhysicalDescr", "1.3.6.1.2.1.47.1.1.1.1.7": "entPhysicalName",
    "1.3.6.1.2.1.47.1.1.1.1.8": "entPhysicalHardwareRev", "1.3.6.1.2.1.47.1.1.1.1.10": "entPhysicalSoftwareRev",
    "1.3.6.1.2.1.47.1.1.1.1.11": "entPhysicalSerialNum", "1.3.6.1.2.1.47.1.1.1.1.12": "entPhysicalMfgName",
    "1.3.6.1.2.1.47.1.1.1.1.13": "entPhysicalModelName",
    "1.0.8802.1.1.2.1.4.1.1.7": "lldpRemPortId", "1.0.8802.1.1.2.1.4.1.1.8": "lldpRemPortDesc",
    "1.0.8802.1.1.2.1.4.1.1.9": "lldpRemSysName", "1.0.8802.1.1.2.1.4.1.1.10": "lldpRemSysDesc",
    "1.0.8802.1.1.2.1.3.7.1.3": "lldpLocPortId", "1.0.8802.1.1.2.1.3.7.1.4": "lldpLocPortDesc",
    "1.0.8802.1.1.2.1.4.1.1.5": "lldpRemChassisId", "1.0.8802.1.1.2.1.4.1.1.12": "lldpRemSysCapEnabled",
    "1.0.8802.1.1.2.1.4.2.1.3": "lldpRemManAddrIfSubtype",
    "1.3.6.1.4.1.9.9.23.1.2.1.1.4": "cdpCacheAddress", "1.3.6.1.4.1.9.9.23.1.2.1.1.5": "cdpCacheVersion",
    "1.3.6.1.4.1.9.9.23.1.2.1.1.6": "cdpCacheDeviceId", "1.3.6.1.4.1.9.9.23.1.2.1.1.7": "cdpCacheDevicePort",
    "1.3.6.1.4.1.9.9.23.1.2.1.1.8": "cdpCachePlatform", "1.3.6.1.4.1.9.9.23.1.2.1.1.9": "cdpCacheCapabilities",
    "1.3.6.1.4.1.9.9.46.1.3.1.1.2": "vtpVlanState", "1.3.6.1.4.1.9.9.46.1.3.1.1.4": "vtpVlanName",
    "1.3.6.1.2.1.17.1.4.1.2": "dot1dBasePortIfIndex",
    "1.3.6.1.4.1": "enterprises", "1.3.6.1.4.1.9": "cisco", "1.3.6.1.4.1.11": "hp", "1.3.6.1.4.1.311": "microsoft",
    "1.3.6.1.4.1.2636": "juniper", "1.3.6.1.4.1.14988": "mikrotik", "1.3.6.1.4.1.41112": "ubiquiti",
    "1.3.6.1.4.1.12356": "fortinet", "1.3.6.1.4.1.25461": "paloalto", "1.3.6.1.4.1.8072": "net-snmp",
    "1.3.6.1.4.1.6876": "vmware", "1.3.6.1.4.1.674": "dell", "1.3.6.1.4.1.1916": "extreme",
    "1.3.6.1.4.1.30065": "arista", "1.3.6.1.4.1.4526": "netgear", "1.3.6.1.4.1.11863": "tp-link",
}
IF_COLUMNS = {1: "ifIndex", 2: "ifDescr", 3: "ifType", 4: "ifMtu", 5: "ifSpeed", 6: "ifPhysAddress",
              7: "ifAdminStatus", 8: "ifOperStatus", 9: "ifLastChange", 10: "ifInOctets", 11: "ifInUcastPkts",
              12: "ifInNUcastPkts", 13: "ifInDiscards", 14: "ifInErrors", 15: "ifInUnknownProtos",
              16: "ifOutOctets", 17: "ifOutUcastPkts", 18: "ifOutNUcastPkts", 19: "ifOutDiscards",
              20: "ifOutErrors", 21: "ifOutQLen"}
IFX_COLUMNS = {1: "ifName", 2: "ifInMulticastPkts", 3: "ifInBroadcastPkts", 4: "ifOutMulticastPkts",
               5: "ifOutBroadcastPkts", 6: "ifHCInOctets", 10: "ifHCOutOctets", 15: "ifHighSpeed",
               16: "ifPromiscuousMode", 17: "ifConnectorPresent", 18: "ifAlias"}
OID_NAMES.update({f"{IF_ENTRY}.{column}": name for column, name in IF_COLUMNS.items()})
OID_NAMES.update({f"{IFX_ENTRY}.{column}": name for column, name in IFX_COLUMNS.items()})
MAC_OBJECTS = {"ifPhysAddress", "ipNetToMediaPhysAddress", "dot1dTpFdbAddress"}
IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
IF_TYPES = {1: "other", 6: "ethernet", 23: "ppp", 24: "loopback", 53: "virtual", 71: "wifi", 117: "gigabitEthernet",
            131: "tunnel", 135: "vlan", 136: "l3vlan", 161: "lag (port-channel)", 166: "mpls", 209: "bridge"}


class SnmpError(Exception):
    """A problem worth showing to the user."""


@dataclass
class Value:
    tag: int
    value: object

    @property
    def type_name(self):
        return TYPE_NAMES.get(self.tag, f"Type 0x{self.tag:02X}")

    @property
    def is_exception(self):
        return self.tag in EXCEPTIONS


# ----------------------------------------------------------------- BER encoding

def encode_length(length):
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def tlv(tag, value):
    return bytes([tag]) + encode_length(len(value)) + value


def encode_integer(number, tag=INTEGER):
    length = max(1, (number.bit_length() + 8) // 8)  # Room for the sign bit
    return tlv(tag, number.to_bytes(length, "big", signed=True))


def parse_oid(text):
    """"1.3.6.1.2.1.1" or ".1.3.6..." to a tuple of ints. Raises ValueError."""
    parts = text.strip().strip(".").split(".")
    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError:
        raise ValueError(f"'{text}' is not an OID. Use dotted numbers, such as 1.3.6.1.2.1.1.") from None
    if len(numbers) < 2 or numbers[0] > 2 or (numbers[0] < 2 and numbers[1] >= 40) or min(numbers) < 0:
        raise ValueError(f"'{text}' is not a valid OID.")
    return numbers


def oid_text(oid):
    return ".".join(str(number) for number in oid)


def encode_oid(oid):
    body = bytes([oid[0] * 40 + oid[1]])
    for number in oid[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.append(0x80 | (number & 0x7F))
            number >>= 7
        body += bytes(reversed(chunk))
    return tlv(OBJECT_ID, body)


def build_request(version, community, pdu_type, request_id, oids, non_repeaters=0, max_repetitions=0):
    varbinds = b"".join(tlv(SEQUENCE, encode_oid(oid) + tlv(NULL, b"")) for oid in oids)
    if pdu_type == GET_BULK:
        fields = encode_integer(request_id) + encode_integer(non_repeaters) + encode_integer(max_repetitions)
    else:
        fields = encode_integer(request_id) + encode_integer(0) + encode_integer(0)
    pdu = tlv(pdu_type, fields + tlv(SEQUENCE, varbinds))
    return tlv(SEQUENCE, encode_integer(version) + tlv(OCTET_STRING, community.encode("utf-8")) + pdu)


# ----------------------------------------------------------------- BER decoding

def read_tlv(data, offset):
    """Returns (tag, value bytes, offset after it)."""
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4:
            raise ValueError("Unsupported length encoding.")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise ValueError("Truncated response.")
    return tag, data[offset:end], end


def decode_oid(raw):
    if not raw:
        return ()
    first = raw[0]
    numbers = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            numbers.append(value)
            value = 0
    return tuple(numbers)


def decode_value(tag, raw):
    if tag == INTEGER:
        return Value(tag, int.from_bytes(raw, "big", signed=True) if raw else 0)
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return Value(tag, int.from_bytes(raw, "big") if raw else 0)
    if tag == OBJECT_ID:
        return Value(tag, decode_oid(raw))
    if tag == IP_ADDRESS and len(raw) == 4:
        return Value(tag, socket.inet_ntoa(raw))
    if tag in (NULL,) + tuple(EXCEPTIONS):
        return Value(tag, None)
    return Value(tag, bytes(raw))


def parse_response(data, request_id=None):
    """Returns (error status, error index, [(oid, Value)]). Raises ValueError if it isn't a valid response."""
    try:
        tag, message, _ = read_tlv(data, 0)
        if tag != SEQUENCE:
            raise ValueError("Not an SNMP message.")
        _, _, offset = read_tlv(message, 0)  # Version
        _, _, offset = read_tlv(message, offset)  # Community
        pdu_type, pdu, _ = read_tlv(message, offset)
        if pdu_type != RESPONSE:
            raise ValueError("Not an SNMP response.")
        fields = []
        offset = 0
        for _ in range(3):
            _, raw, offset = read_tlv(pdu, offset)
            fields.append(int.from_bytes(raw, "big", signed=True) if raw else 0)
        response_id, error_status, error_index = fields
        if request_id is not None and response_id != request_id:
            raise ValueError("Response is for a different request.")
        _, varbinds, _ = read_tlv(pdu, offset)
        results = []
        offset = 0
        while offset < len(varbinds):
            _, varbind, offset = read_tlv(varbinds, offset)
            _, oid_raw, inner = read_tlv(varbind, 0)
            value_tag, value_raw, _ = read_tlv(varbind, inner)
            results.append((decode_oid(oid_raw), decode_value(value_tag, value_raw)))
        return error_status, error_index, results
    except IndexError:
        raise ValueError("Truncated response.") from None


# ----------------------------------------------------------------- Traps

@dataclass
class Trap:
    """A trap (or inform) a device sent."""
    version: int
    community: str
    trap_oid: tuple  # Which trap: linkUp is 1.3.6.1.6.3.1.1.5.4 whichever version sent it
    agent: str = ""  # The address a v1 trap says it's from ("" for v2c: use the sender's address)
    varbinds: list = field(default_factory=list)  # [(oid, Value)]
    inform: bool = False
    request_id: int = 0


def _varbinds(raw):
    results, offset = [], 0
    while offset < len(raw):
        _, varbind, offset = read_tlv(raw, offset)
        _, oid_raw, inner = read_tlv(varbind, 0)
        value_tag, value_raw, _ = read_tlv(varbind, inner)
        results.append((decode_oid(oid_raw), decode_value(value_tag, value_raw)))
    return results


def parse_trap(data):
    """A v1 Trap-PDU, v2c SNMPv2-Trap or inform. Raises ValueError if it isn't one."""
    try:
        tag, message, _ = read_tlv(data, 0)
        if tag != SEQUENCE:
            raise ValueError("Not an SNMP message.")
        _, version_raw, offset = read_tlv(message, 0)
        _, community, offset = read_tlv(message, offset)
        pdu_type, pdu, _ = read_tlv(message, offset)
        version = int.from_bytes(version_raw, "big") if version_raw else 0
        community = bytes(community).decode("utf-8", "replace")
        if pdu_type == TRAP_V1:
            _, enterprise, offset = read_tlv(pdu, 0)
            agent_tag, agent_raw, offset = read_tlv(pdu, offset)
            fields = []
            for _ in range(3):  # Generic trap, specific trap, time stamp
                _, raw, offset = read_tlv(pdu, offset)
                fields.append(int.from_bytes(raw, "big") if raw else 0)
            _, varbinds, _ = read_tlv(pdu, offset)
            generic, specific, _ = fields
            trap_oid = decode_oid(enterprise) + (0, specific) if generic == 6 else SNMP_TRAPS + (generic + 1,)
            agent = socket.inet_ntoa(agent_raw) if len(agent_raw) == 4 else ""
            return Trap(version, community, trap_oid, agent, _varbinds(varbinds))
        if pdu_type in (TRAP_V2, INFORM):
            _, request_raw, offset = read_tlv(pdu, 0)
            _, _, offset = read_tlv(pdu, offset)  # Error status
            _, _, offset = read_tlv(pdu, offset)  # Error index
            _, varbinds, _ = read_tlv(pdu, offset)
            pairs = _varbinds(varbinds)
            trap_oid = next((value.value for oid, value in pairs if oid == SNMP_TRAP_OID), ())
            return Trap(version, community, tuple(trap_oid or ()), "", pairs, pdu_type == INFORM,
                        int.from_bytes(request_raw, "big", signed=True) if request_raw else 0)
        raise ValueError("Not an SNMP trap.")
    except IndexError:
        raise ValueError("Truncated trap.") from None


def inform_response(data):
    """The reply an inform expects: the same message as a Response."""
    _, message, _ = read_tlv(data, 0)
    _, _, offset = read_tlv(message, 0)
    _, _, offset = read_tlv(message, offset)
    _, pdu, _ = read_tlv(message, offset)
    return tlv(SEQUENCE, message[:offset] + tlv(RESPONSE, pdu))


def build_trap(community, trap_oid, varbinds=(), version=V2C, request_id=1, uptime=0):
    """A v2c trap (for tests, and for sending a test trap)."""
    def encode(value):
        if isinstance(value, Value) and value.tag == OCTET_STRING:
            return tlv(OCTET_STRING, value.value)
        if isinstance(value, Value) and value.tag == INTEGER:
            return encode_integer(value.value)
        if isinstance(value, Value) and value.tag == OBJECT_ID:
            return encode_oid(value.value)
        return tlv(NULL, b"")
    pairs = [((1, 3, 6, 1, 2, 1, 1, 3, 0), None), (SNMP_TRAP_OID, Value(OBJECT_ID, tuple(trap_oid)))] + list(varbinds)
    body = b"".join(tlv(SEQUENCE, encode_oid(oid) + (encode_integer(uptime, TIMETICKS) if value is None
                                                     else encode(value))) for oid, value in pairs)
    pdu = tlv(TRAP_V2, encode_integer(request_id) + encode_integer(0) + encode_integer(0) + tlv(SEQUENCE, body))
    return tlv(SEQUENCE, encode_integer(version) + tlv(OCTET_STRING, community.encode("utf-8")) + pdu)


# ----------------------------------------------------------------- Talking to a device

class SnmpClient:
    def __init__(self, host, community="public", version=V2C, timeout=2000, retries=1, port=SNMP_PORT):
        self.host, self.community, self.version = host, community, version
        self.timeout, self.retries, self.port = timeout, retries, port
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        self.family, self.address = infos[0][0], infos[0][4]

    def request(self, pdu_type, oids, **bulk):
        """Send a request and return (error status, error index, varbinds). Raises SnmpError."""
        request_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        packet = build_request(self.version, self.community, pdu_type, request_id, oids, **bulk)
        with socket.socket(self.family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout / 1000)
            for _ in range(self.retries + 1):
                sock.sendto(packet, self.address)
                while True:
                    try:
                        data, _ = sock.recvfrom(65535)
                    except socket.timeout:
                        break
                    except ConnectionResetError:
                        raise SnmpError(f"{self.host} isn't running SNMP (UDP port {self.port} is closed).") from None
                    try:
                        return parse_response(data, request_id)
                    except ValueError:
                        continue
        raise SnmpError(f"No answer from {self.host}. Check that SNMP is turned on, the community string is "
                        "right, and the device allows SNMP from this computer.")

    def get(self, oids):
        """Values for exact OIDs. Returns [(oid, Value)]."""
        status, index, results = self.request(GET, oids)
        if status:
            raise SnmpError(f"The device refused the request: {ERRORS.get(status, status)}"
                            + (f" (item {index})" if index else "") + ".")
        return results

    def walk(self, root, max_repetitions=25, should_stop=lambda: False, limit=MAX_ROWS):
        """Yield (oid, Value) for everything under root, in order."""
        root = tuple(root)
        current = root
        count = 0
        while not should_stop():
            if self.version == V1:
                status, index, results = self.request(GET_NEXT, [current])
            else:
                status, index, results = self.request(GET_BULK, [current], non_repeaters=0,
                                                      max_repetitions=max_repetitions)
            if status == TOO_BIG and self.version != V1 and max_repetitions > 1:
                max_repetitions = max(1, max_repetitions // 2)
                continue
            if status == NO_SUCH_NAME and self.version == V1:
                return  # v1's way of saying "end of the MIB"
            if status:
                raise SnmpError(f"The device refused the request: {ERRORS.get(status, status)}.")
            if not results:
                return
            for oid, value in results:
                if value.tag == END_OF_MIB_VIEW or oid[:len(root)] != root:
                    return
                if oid <= current:
                    raise SnmpError(f"The device returned OIDs out of order at {oid_text(oid)}, so the walk "
                                    "stopped to avoid looping.")
                yield oid, value
                current = oid
                count += 1
                if count >= limit:
                    return


# ----------------------------------------------------------------- Making results readable

def oid_name(oid):
    """"1.3.6.1.2.1.1.5.0" -> "sysName.0", using the longest known prefix."""
    for length in range(len(oid), 1, -1):
        name = OID_NAMES.get(oid_text(oid[:length]))
        if name:
            rest = oid[length:]
            return name + ("." + oid_text(rest) if rest else "")
    return oid_text(oid)


def format_timeticks(ticks):
    seconds = ticks // 100
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}d " if days else "") + f"{hours}h {minutes}m {seconds}s"


def format_value(oid, value):
    """A readable form of a value, using what the OID is where it helps."""
    name = oid_name(oid).split(".")[0]
    if value.is_exception:
        return f"({value.type_name})"
    if value.tag == TIMETICKS:
        return format_timeticks(value.value)
    if value.tag == OBJECT_ID:
        text = oid_text(value.value)
        named = oid_name(value.value)
        return text if named == text else f"{named} ({text})"
    if value.tag == OCTET_STRING:
        raw = value.value
        if name in MAC_OBJECTS or (len(raw) == 6 and not _printable(raw)):
            return "-".join(f"{byte:02X}" for byte in raw) if len(raw) == 6 else raw.hex(":")
        return raw.decode("utf-8", "replace").rstrip("\0") if _printable(raw) else raw.hex(" ")
    if name in ("ifAdminStatus", "ifOperStatus") and isinstance(value.value, int):
        return IF_STATUS.get(value.value, str(value.value))
    if name == "ifType" and isinstance(value.value, int):
        return f"{IF_TYPES.get(value.value, 'type')} ({value.value})"
    return "" if value.value is None else str(value.value)


def _printable(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(character.isprintable() or character in "\r\n\t" for character in text.rstrip("\0"))


# ----------------------------------------------------------------- Interface summary

@dataclass
class InterfaceRow:
    index: int
    name: str = ""
    description: str = ""
    alias: str = ""
    kind: str = ""
    admin: str = ""
    oper: str = ""
    speed_mbps: Optional[int] = None
    mtu: Optional[int] = None
    mac: str = ""
    in_errors: Optional[int] = None
    out_errors: Optional[int] = None
    in_discards: Optional[int] = None
    out_discards: Optional[int] = None


SUMMARY_COLUMNS = {f"{IF_ENTRY}.2": "description", f"{IF_ENTRY}.3": "kind", f"{IF_ENTRY}.4": "mtu",
                   f"{IF_ENTRY}.5": "speed", f"{IF_ENTRY}.6": "mac", f"{IF_ENTRY}.7": "admin",
                   f"{IF_ENTRY}.8": "oper", f"{IF_ENTRY}.13": "in_discards", f"{IF_ENTRY}.14": "in_errors",
                   f"{IF_ENTRY}.19": "out_discards", f"{IF_ENTRY}.20": "out_errors", f"{IFX_ENTRY}.1": "name",
                   f"{IFX_ENTRY}.15": "high_speed", f"{IFX_ENTRY}.18": "alias"}


def interface_rows(values):
    """Build InterfaceRows from {(column OID text, index): (oid, Value)}."""
    rows = {}
    for (column, index), (oid, value) in values.items():
        field = SUMMARY_COLUMNS.get(column)
        if field is None or value.is_exception:
            continue
        row = rows.setdefault(index, InterfaceRow(index))
        text = format_value(oid, value)
        if field in ("in_errors", "out_errors", "in_discards", "out_discards", "mtu"):
            setattr(row, field, value.value if isinstance(value.value, int) else None)
        elif field == "speed":
            if row.speed_mbps is None and isinstance(value.value, int) and value.value < 4294967295:
                row.speed_mbps = value.value // 1000000
        elif field == "high_speed":
            if isinstance(value.value, int) and value.value:
                row.speed_mbps = value.value  # Already in Mbps, and right above 4 Gbps
        elif field == "kind":
            row.kind = IF_TYPES.get(value.value, str(value.value))
        else:
            setattr(row, field, text)
    for row in rows.values():
        row.name = row.name or row.description
    return [rows[index] for index in sorted(rows)]


def interface_summary(client, should_stop=lambda: False):
    """Walk the interface tables and return [InterfaceRow]."""
    values = {}
    for column in SUMMARY_COLUMNS:
        root = parse_oid(column)
        try:
            for oid, value in client.walk(root, should_stop=should_stop):
                if len(oid) == len(root) + 1:
                    values[(column, oid[-1])] = (oid, value)
        except SnmpError:
            if column.startswith(IFX_ENTRY):
                continue  # Older devices don't have the ifXTable
            raise
        if should_stop():
            break
    return interface_rows(values)


def community_is_valid(text):
    return 0 < len(text.encode("utf-8")) <= 255 and not any(ord(character) < 32 for character in text)
