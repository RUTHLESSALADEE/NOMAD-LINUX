"""SNMPv3 with the User-based Security Model (RFC 3414): reading devices as a user with authentication and privacy,
and checking and decrypting the v3 traps they send.

A client discovers a device's engine (its engine ID, boots and time) once, keeps it for every request to that
device (a crawl's per-VLAN clients included), and localizes the user's passwords to it. Authentication is HMAC with
MD5, SHA-1 or SHA-2 (RFC 7860); privacy is DES (RFC 3414) or AES-128/192/256 in CFB mode (RFC 3826), with AES-192
and AES-256 keys extended the way Cisco does it (draft-reeder-snmpv3-usm-3desede, net-snmp's AES-192-C/AES-256-C).

A device that can't take the request answers with a Report (unknown user, wrong password...), which turns into an
SnmpError saying so, so a crawl moves on to the next credential at once instead of waiting for a timeout.
"""
import hashlib
import hmac
import os
import struct
import threading
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .snmp import GET, INFORM, OCTET_STRING, REPORT, RESPONSE, SEQUENCE, TRAP_V2, V3, SnmpError, build_pdu, \
    encode_integer, oid_text, parse_pdu, random_id, read_tlv, tlv, v2_trap

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:  # cryptography before 43
    TripleDES = algorithms.TripleDES
try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # cryptography before 47
    CFB = modes.CFB

MAX_SIZE = 65507  # The largest reply this side takes (msgMaxSize)
USM = 3  # msgSecurityModel
FLAG_AUTH, FLAG_PRIV, FLAG_REPORTABLE = 0x01, 0x02, 0x04
PASSWORD_STRETCH = 1048576  # RFC 3414 A.2: the password repeated to a megabyte, then hashed
MIN_PASSWORD = 8

# Authentication: name -> (hash, length of the message authentication code it puts in messages)
AUTH_PROTOCOLS = {"md5": (hashlib.md5, 12), "sha": (hashlib.sha1, 12), "sha224": (hashlib.sha224, 16),
                  "sha256": (hashlib.sha256, 24), "sha384": (hashlib.sha384, 32), "sha512": (hashlib.sha512, 48)}
AUTH_NAMES = {"none": "None", "md5": "MD5", "sha": "SHA-1", "sha224": "SHA-224", "sha256": "SHA-256",
              "sha384": "SHA-384", "sha512": "SHA-512"}
# Privacy: name -> bytes of localized key it uses (DES: the key, then the pre-IV)
PRIV_KEY_SIZES = {"des": 16, "aes128": 16, "aes192": 24, "aes256": 32}
PRIV_NAMES = {"none": "None", "des": "DES", "aes128": "AES-128", "aes192": "AES-192", "aes256": "AES-256"}
NO_AUTH, AUTH_NO_PRIV, AUTH_PRIV = "noAuthNoPriv", "authNoPriv", "authPriv"

USM_STATS = (1, 3, 6, 1, 6, 3, 15, 1, 1)  # usmStats: the counters a Report names
UNSUPPORTED_LEVEL, NOT_IN_TIME, UNKNOWN_USER, UNKNOWN_ENGINE, WRONG_DIGEST, DECRYPTION_ERROR = 1, 2, 3, 4, 5, 6
UNKNOWN_CONTEXT = (1, 3, 6, 1, 6, 3, 12, 1, 5, 0)  # snmpUnknownContexts.0


@dataclass(frozen=True)
class V3User:
    """An SNMPv3 user and its passwords: what a community string is to v1 and v2c. Hashable, so it can sit in the
    same lists as community strings."""
    user: str
    auth: str = "sha"
    auth_password: str = field(default="", repr=False)
    priv: str = "aes128"
    priv_password: str = field(default="", repr=False)

    @property
    def level(self):
        if self.auth == "none":
            return NO_AUTH
        return AUTH_NO_PRIV if self.priv == "none" else AUTH_PRIV

    @property
    def label(self):
        """How it's shown and logged (never with its passwords)."""
        if self.auth == "none":
            return f"v3 user {self.user} (no authentication)"
        privacy = "" if self.priv == "none" else f", {PRIV_NAMES.get(self.priv, self.priv)}"
        return f"v3 user {self.user} ({AUTH_NAMES.get(self.auth, self.auth)}{privacy})"

    def problem(self):
        """What's wrong with it, or ""."""
        if not self.user.strip() or len(self.user.encode("utf-8")) > 32 or any(ord(c) < 33 for c in self.user):
            return "An SNMPv3 user name is 1 to 32 characters, without spaces."
        if self.auth not in AUTH_NAMES or self.priv not in PRIV_NAMES:
            return f"SNMPv3 user {self.user} has an authentication or privacy protocol NOMAD doesn't know."
        if self.auth == "none" and self.priv != "none":
            return f"SNMPv3 user {self.user}: privacy needs authentication too."
        if self.auth != "none" and len(self.auth_password) < MIN_PASSWORD:
            return f"SNMPv3 user {self.user}: the authentication password needs at least {MIN_PASSWORD} characters."
        if self.priv != "none" and len(self.priv_password) < MIN_PASSWORD:
            return f"SNMPv3 user {self.user}: the privacy password needs at least {MIN_PASSWORD} characters."
        return ""

    def to_json(self):
        return {"user": self.user, "auth": self.auth, "auth_password": self.auth_password, "priv": self.priv,
                "priv_password": self.priv_password}

    @classmethod
    def from_json(cls, data):
        return cls(user=str(data.get("user", "")), auth=str(data.get("auth", "sha")),
                   auth_password=str(data.get("auth_password", "")), priv=str(data.get("priv", "aes128")),
                   priv_password=str(data.get("priv_password", "")))


# --------------------------------------------------------------------- Credentials: community strings or users

def is_v3(credential):
    return isinstance(credential, V3User)


def credential_label(credential):
    """A community string as itself, a user as its label."""
    return credential.label if is_v3(credential) else str(credential)


def credential_to_json(credential):
    return {"v3": credential.to_json()} if is_v3(credential) else credential


def credential_from_json(data):
    if isinstance(data, dict) and isinstance(data.get("v3"), dict):
        return V3User.from_json(data["v3"])
    return str(data)


def users_from_json(items):
    """V3Users from saved settings or tribe secrets, leaving out anything that isn't one."""
    users = []
    for item in items or []:
        if isinstance(item, dict):
            user = V3User.from_json(item.get("v3", item))
            if user.user:
                users.append(user)
    return users


# --------------------------------------------------------------------- Keys

_keys = {}  # (kind, ...) -> key bytes; localizing takes a megabyte of hashing, so each is done once
_keys_lock = threading.Lock()


def password_to_key(password, hash_function):
    """RFC 3414 A.2: the password (str or bytes) repeated to a megabyte and hashed."""
    data = password.encode("utf-8") if isinstance(password, str) else bytes(password)
    if not data:
        raise ValueError("An empty password can't make a key.")
    return hash_function((data * (PASSWORD_STRETCH // len(data) + 1))[:PASSWORD_STRETCH]).digest()


def localize_key(key, engine_id, hash_function):
    """RFC 3414 2.6: a key made for one engine."""
    return hash_function(key + engine_id + key).digest()


def _cached(cache_key, make):
    with _keys_lock:
        key = _keys.get(cache_key)
    if key is None:
        key = make()
        with _keys_lock:
            _keys[cache_key] = key
    return key


def auth_key(user, engine_id):
    hash_function = AUTH_PROTOCOLS[user.auth][0]
    return _cached(("auth", user.auth, user.auth_password, engine_id), lambda: localize_key(
        password_to_key(user.auth_password, hash_function), engine_id, hash_function))


def priv_key(user, engine_id):
    """The privacy password localized with the authentication hash, extended for AES-192/256 if that's too short:
    hash the key so far as if it were a password, localize that, and add it on (Reeder, as Cisco does)."""
    hash_function = AUTH_PROTOCOLS[user.auth][0]
    size = PRIV_KEY_SIZES[user.priv]

    def make():
        key = localize_key(password_to_key(user.priv_password, hash_function), engine_id, hash_function)
        while len(key) < size:
            key += localize_key(password_to_key(key, hash_function), engine_id, hash_function)
        return key[:size]
    return _cached(("priv", user.auth, user.priv, user.priv_password, engine_id), make)


# --------------------------------------------------------------------- Privacy

def encrypt(user, key, boots, engine_time, plaintext):
    """Returns (ciphertext, privacy parameters: the salt)."""
    if user.priv == "des":
        salt = struct.pack(">I", boots & 0xFFFFFFFF) + os.urandom(4)
        iv = bytes(a ^ b for a, b in zip(key[8:16], salt))
        padded = plaintext + b"\0" * (-len(plaintext) % 8)  # The scoped PDU's own length says where it ends
        encryptor = Cipher(TripleDES(key[:8] * 3), modes.CBC(iv)).encryptor()  # One key three times: DES
        return encryptor.update(padded) + encryptor.finalize(), salt
    salt = os.urandom(8)
    encryptor = Cipher(algorithms.AES(key), CFB(struct.pack(">II", boots, engine_time) + salt)).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize(), salt


def decrypt(user, key, boots, engine_time, salt, ciphertext):
    """Raises ValueError if it can't be decrypted."""
    if len(salt) != 8:
        raise ValueError("Bad privacy parameters.")
    if user.priv == "des":
        if len(ciphertext) % 8:
            raise ValueError("DES ciphertext isn't whole blocks.")
        iv = bytes(a ^ b for a, b in zip(key[8:16], salt))
        decryptor = Cipher(TripleDES(key[:8] * 3), modes.CBC(iv)).decryptor()
    else:
        decryptor = Cipher(algorithms.AES(key), CFB(struct.pack(">II", boots, engine_time) + salt)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


# --------------------------------------------------------------------- Messages

@dataclass
class Message:
    """A v3 message, taken apart."""
    msg_id: int
    flags: int
    engine_id: bytes  # The authoritative engine: the device for requests, the sender for traps
    boots: int
    engine_time: int
    user_name: bytes
    auth_params: bytes
    priv_params: bytes
    data: bytes  # The scoped PDU's contents, or the encrypted scoped PDU
    encrypted: bool
    auth_span: tuple  # Where the authentication parameters are in the whole message
    whole: bytes  # The message itself (what the MAC is over)


def _integer(raw):
    return int.from_bytes(raw, "big", signed=True) if raw else 0


def parse_message(data):
    """Take a v3 message apart. Raises ValueError if it isn't one."""
    try:
        tag, message, end = read_tlv(data, 0)
        base = end - len(message)
        if tag != SEQUENCE:
            raise ValueError("Not an SNMP message.")
        _, version, offset = read_tlv(message, 0)
        if _integer(version) != V3:
            raise ValueError("Not an SNMPv3 message.")
        _, header, offset = read_tlv(message, offset)
        _, msg_id, inner = read_tlv(header, 0)
        _, _, inner = read_tlv(header, inner)  # msgMaxSize
        _, flags, inner = read_tlv(header, inner)
        _, model, inner = read_tlv(header, inner)
        if _integer(model) != USM or len(flags) != 1:
            raise ValueError("Not a user-based security model message.")
        tag, security, offset = read_tlv(message, offset)
        security_base = base + offset - len(security)
        _, parameters, parameters_end = read_tlv(security, 0)
        parameters_base = security_base + parameters_end - len(parameters)
        fields, position, spans = [], 0, []
        for _ in range(6):
            _, raw, position = read_tlv(parameters, position)
            fields.append(bytes(raw))
            spans.append((parameters_base + position - len(raw), parameters_base + position))
        data_tag, scoped, _ = read_tlv(message, offset)
        if data_tag not in (SEQUENCE, OCTET_STRING):
            raise ValueError("Bad scoped PDU.")
        return Message(_integer(msg_id), flags[0], fields[0], _integer(fields[1]), _integer(fields[2]), fields[3],
                       fields[4], fields[5], bytes(scoped), data_tag == OCTET_STRING, spans[4], bytes(data[:end]))
    except IndexError:
        raise ValueError("Truncated SNMPv3 message.") from None


def parse_scoped(contents):
    """(context engine ID, context name, PDU type, PDU contents) from a scoped PDU's contents. Raises ValueError."""
    try:
        _, engine_id, offset = read_tlv(contents, 0)
        _, context, offset = read_tlv(contents, offset)
        pdu_type, pdu, _ = read_tlv(contents, offset)
        return bytes(engine_id), bytes(context), pdu_type, pdu
    except IndexError:
        raise ValueError("Truncated scoped PDU.") from None


def build_message(user, engine_id, boots, engine_time, pdu, msg_id, context="", context_engine_id=None,
                  reportable=True):
    """A v3 message carrying pdu as user, authenticated and encrypted as the user's level says. engine_id, boots and
    engine_time are the authoritative engine's (the device's for a request, the sender's own for a trap)."""
    scoped = tlv(SEQUENCE, tlv(OCTET_STRING, engine_id if context_engine_id is None else context_engine_id) +
                 tlv(OCTET_STRING, context.encode("utf-8")) + pdu)
    flags = FLAG_REPORTABLE if reportable else 0
    priv_params, data = b"", scoped
    if user.priv != "none" and user.auth != "none":
        flags |= FLAG_AUTH | FLAG_PRIV
        ciphertext, priv_params = encrypt(user, priv_key(user, engine_id), boots, engine_time, scoped)
        data = tlv(OCTET_STRING, ciphertext)
    elif user.auth != "none":
        flags |= FLAG_AUTH
    mac_length = AUTH_PROTOCOLS[user.auth][1] if flags & FLAG_AUTH else 0
    security = tlv(SEQUENCE, tlv(OCTET_STRING, engine_id) + encode_integer(boots) + encode_integer(engine_time) +
                   tlv(OCTET_STRING, user.user.encode("utf-8")) + tlv(OCTET_STRING, b"\0" * mac_length) +
                   tlv(OCTET_STRING, priv_params))
    header = tlv(SEQUENCE, encode_integer(msg_id) + encode_integer(MAX_SIZE) + tlv(OCTET_STRING, bytes([flags])) +
                 encode_integer(USM))
    message = tlv(SEQUENCE, encode_integer(V3) + header + tlv(OCTET_STRING, security) + data)
    if flags & FLAG_AUTH:
        start, end = parse_message(message).auth_span
        message = message[:start] + mac(user, engine_id, message) + message[end:]
    return message


def mac(user, engine_id, message):
    """The message authentication code for a message whose authentication parameters are zeros."""
    hash_function, length = AUTH_PROTOCOLS[user.auth]
    return hmac.new(auth_key(user, engine_id), message, hash_function).digest()[:length]


def verify(user, message):
    """Raises ValueError unless message (a Message) was authenticated with user's authentication password."""
    if user.auth == "none":
        raise ValueError("The user has no authentication password.")
    length = AUTH_PROTOCOLS[user.auth][1]
    start, end = message.auth_span
    if end - start != length:
        raise ValueError("Wrong length of authentication parameters.")
    zeroed = message.whole[:start] + b"\0" * length + message.whole[end:]
    if not hmac.compare_digest(mac(user, message.engine_id, zeroed), message.auth_params):
        raise ValueError("Authentication failed.")


def message_level(flags):
    if not flags & FLAG_AUTH:
        return NO_AUTH
    return AUTH_PRIV if flags & FLAG_PRIV else AUTH_NO_PRIV


def open_message(user, message):
    """Check (if authenticated) and decrypt (if encrypted) a message: (context name, PDU type, PDU contents).
    Raises ValueError."""
    if message.flags & FLAG_AUTH:
        verify(user, message)
    if message.encrypted:
        if not message.flags & FLAG_PRIV or user.priv == "none":
            raise ValueError("Encrypted, but not for this user.")
        contents = decrypt(user, priv_key(user, message.engine_id), message.boots, message.engine_time,
                           message.priv_params, message.data)
        tag, contents, _ = read_tlv(contents, 0)
        if tag != SEQUENCE:
            raise ValueError("Couldn't decrypt the scoped PDU.")
    else:
        contents = message.data
    _, context, pdu_type, pdu = parse_scoped(contents)
    return context, pdu_type, pdu


# --------------------------------------------------------------------- Engines

@dataclass
class Engine:
    """A device's SNMP engine, as discovered."""
    engine_id: bytes
    boots: int
    engine_time: int
    at: float  # time.monotonic() when engine_time was learned

    def now(self):
        return self.engine_time + int(time.monotonic() - self.at)


_engines = {}  # Socket address -> Engine
_engines_lock = threading.Lock()


def forget_engines():
    with _engines_lock:
        _engines.clear()


def discover(client):
    """The device's engine (from the cache, or asked with an empty request it answers with a Report)."""
    with _engines_lock:
        engine = _engines.get(client.address)
    if engine is not None:
        return engine
    msg_id = random_id()
    security = tlv(SEQUENCE, tlv(OCTET_STRING, b"") + encode_integer(0) + encode_integer(0) +
                   tlv(OCTET_STRING, b"") + tlv(OCTET_STRING, b"") + tlv(OCTET_STRING, b""))
    header = tlv(SEQUENCE, encode_integer(msg_id) + encode_integer(MAX_SIZE) +
                 tlv(OCTET_STRING, bytes([FLAG_REPORTABLE])) + encode_integer(USM))
    scoped = tlv(SEQUENCE, tlv(OCTET_STRING, b"") + tlv(OCTET_STRING, b"") + build_pdu(GET, random_id(), []))
    packet = tlv(SEQUENCE, encode_integer(V3) + header + tlv(OCTET_STRING, security) + scoped)

    def parse(data):
        message = parse_message(data)
        if message.msg_id != msg_id or not message.engine_id:
            raise ValueError("Not the answer to discovery.")
        return Engine(message.engine_id, message.boots, message.engine_time, time.monotonic())
    engine = client.exchange(packet, parse)
    with _engines_lock:
        _engines[client.address] = engine
    return engine


def _remember(client, engine):
    with _engines_lock:
        _engines[client.address] = engine


def _forget(client):
    with _engines_lock:
        _engines.pop(client.address, None)


# --------------------------------------------------------------------- Requests

@dataclass
class Reply:
    pdu_type: int
    status: int = 0
    index: int = 0
    varbinds: list = field(default_factory=list)
    message: Message = None


def read_reply(data, user, msg_id, request_id):
    """The reply to a request (a Response, or a Report saying why not). Raises ValueError for anything else, which
    the client ignores while it waits."""
    message = parse_message(data)
    if message.msg_id != msg_id:
        raise ValueError("Not the reply to this request.")
    if not message.flags & FLAG_AUTH and message.encrypted:
        raise ValueError("Encrypted but not authenticated.")
    context, pdu_type, pdu = open_message(user, message)
    if pdu_type == REPORT:
        _, _, _, varbinds = parse_pdu(pdu)
        return Reply(REPORT, varbinds=varbinds, message=message)
    if pdu_type != RESPONSE:
        raise ValueError("Not a response.")
    if user.auth != "none" and not message.flags & FLAG_AUTH:
        raise ValueError("A response that wasn't authenticated.")
    response_id, status, index, varbinds = parse_pdu(pdu)
    if response_id != request_id:
        raise ValueError("Response is for a different request.")
    return Reply(RESPONSE, status, index, varbinds, message)


def report_number(reply):
    oid = reply.varbinds[0][0] if reply.varbinds else ()
    return oid[len(USM_STATS)] if oid[:len(USM_STATS)] == USM_STATS and len(oid) > len(USM_STATS) else None


def report_error(reply, client):
    user, host = client.community, client.host
    number = report_number(reply)
    text = {UNSUPPORTED_LEVEL: f"{host} doesn't allow {user.level} for SNMPv3 user {user.user}: check the user's "
                               "authentication and privacy settings on both sides.",
            NOT_IN_TIME: f"{host} and this computer couldn't agree on the SNMPv3 engine time.",
            UNKNOWN_USER: f"{host} doesn't know SNMPv3 user {user.user}.",
            UNKNOWN_ENGINE: f"{host} didn't recognize its own SNMPv3 engine ID.",
            WRONG_DIGEST: f"{host} says the authentication password or protocol for SNMPv3 user {user.user} is "
                          "wrong.",
            DECRYPTION_ERROR: f"{host} couldn't decrypt the request: the privacy password or protocol for SNMPv3 "
                              f"user {user.user} is wrong."}.get(number)
    if text is None and reply.varbinds and reply.varbinds[0][0] == UNKNOWN_CONTEXT:
        text = f"{host} has no SNMPv3 context '{client.context}' for user {user.user}."
    return SnmpError(text or f"{host} refused the SNMPv3 request ("
                             f"{oid_text(reply.varbinds[0][0]) if reply.varbinds else 'report'}).")


def request(client, pdu_type, oids, **bulk):
    """An SnmpClient's request as its V3User: (error status, error index, varbinds). Raises SnmpError."""
    user = client.community
    problem = user.problem()
    if problem:
        raise SnmpError(problem)
    engine = discover(client)
    for attempt in range(3):
        request_id, msg_id = random_id(), random_id()
        pdu = build_pdu(pdu_type, request_id, oids, **bulk)
        boots, now = engine.boots, engine.now()
        packet = build_message(user, engine.engine_id, boots, now, pdu, msg_id, client.context)
        try:
            reply = client.exchange(packet, lambda data: read_reply(data, user, msg_id, request_id))
        except SnmpError as error:
            if not str(error).startswith("No answer"):
                raise
            # The engine answered discovery, so the device is there: Cisco drops what it can't decrypt, and
            # requests its access list refuses, without a word
            hint = "its privacy password and protocol, and " if user.priv != "none" else ""
            raise SnmpError(f"{client.host} answered SNMPv3 but not the request as user {user.user}: check "
                            f"{hint}that the user's access list allows this computer.") from None
        if reply.pdu_type == RESPONSE:
            return reply.status, reply.index, reply.varbinds
        number = report_number(reply)
        if attempt < 2 and number == NOT_IN_TIME and reply.message.flags & FLAG_AUTH:
            engine = Engine(engine.engine_id, reply.message.boots, reply.message.engine_time, time.monotonic())
            _remember(client, engine)
            continue
        if attempt < 2 and number == UNKNOWN_ENGINE:  # It restarted with a new engine ID: discover it again
            _forget(client)
            engine = discover(client)
            continue
        raise report_error(reply, client)
    raise SnmpError(f"{client.host} kept refusing the SNMPv3 request.")


# --------------------------------------------------------------------- Traps

def parse_v3_trap(data, users):
    """A v3 trap (snmp.Trap, its community the user name) checked and decrypted with whichever of users (V3Users)
    it's from. Raises ValueError if it isn't one, or no user's passwords fit."""
    message = parse_message(data)
    name = message.user_name.decode("utf-8", "replace")
    candidates = [user for user in users if user.user == name]
    if not candidates:
        raise ValueError(f"An SNMPv3 trap from user {name or '(none)'}, who isn't set up in NOMAD.")
    problem = "Its level doesn't match the user's."
    for user in candidates:
        if user.level != message_level(message.flags):
            continue
        try:
            _, pdu_type, pdu = open_message(user, message)
            if pdu_type == INFORM:
                raise ValueError("SNMPv3 informs aren't supported: have the switch send traps.")
            if pdu_type != TRAP_V2:
                raise ValueError("Not a trap.")
            return v2_trap(V3, name, pdu_type, pdu)
        except (ValueError, IndexError) as error:
            problem = str(error)
    raise ValueError(f"An SNMPv3 trap from user {name} that couldn't be checked: {problem}")
