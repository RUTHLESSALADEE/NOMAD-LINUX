import dataclasses
import hashlib
import socket
import threading

import pytest

from nomad import snmpv3
from nomad.snmp import COUNTER32, END_OF_MIB_VIEW, GET, GET_BULK, GET_NEXT, NULL, REPORT, RESPONSE, SEQUENCE, \
    SNMP_TRAPS, V3, SnmpClient, SnmpError, Value, build_pdu, build_request, encode_integer, encode_oid, \
    interface_summary, parse_pdu, parse_trap, tlv, OCTET_STRING, OBJECT_ID, SNMP_TRAP_OID, TRAP_V2, INFORM
from nomad.snmpv3 import AUTH_PRIV, NO_AUTH, V3User, build_message, credential_from_json, credential_label, \
    credential_to_json, localize_key, message_level, open_message, parse_message, password_to_key, priv_key, \
    users_from_json, verify
from test_snmp import MIB, encode_value

ENGINE_ID = bytes.fromhex("800000090300001a2b000001")
VLAN_MIB = {(1, 3, 6, 1, 2, 1, 17, 4, 3, 1, 2, 0, 0x50, 0x56, 1, 2, 3): Value(0x02, 5)}

SHA_AES = V3User("nomad", "sha", "authpass1", "aes128", "privpass1")
SHA256_AES256 = V3User("nomad256", "sha256", "authpass2", "aes256", "privpass2")
MD5_DES = V3User("legacy", "md5", "authpass3", "des", "privpass3")
SHA_AES192 = V3User("cisco192", "sha", "authpass4", "aes192", "privpass4")
AUTH_ONLY = V3User("watcher", "sha512", "authpass5", "none", "")
NO_AUTH_USER = V3User("public3", "none", "", "none", "")
USERS = [SHA_AES, SHA256_AES256, MD5_DES, SHA_AES192, AUTH_ONLY, NO_AUTH_USER]


def report_pdu(request_id, oid):
    varbind = tlv(SEQUENCE, encode_oid(oid) + tlv(COUNTER32, b"\x01"))
    return tlv(REPORT, encode_integer(request_id) + encode_integer(0) + encode_integer(0) + tlv(SEQUENCE, varbind))


class FakeV3Agent:
    """An SNMPv3 agent the way a switch acts: discovery, the user's checks and their Reports, contexts, and GET,
    GETNEXT and GETBULK from MIB."""

    def __init__(self, users=USERS, mib=MIB, contexts=None, boots=7, engine_time=1000, silent=False):
        self.users = {user.user: user for user in users}
        self.mib = dict(sorted(mib.items()))
        self.contexts = {"": self.mib}
        self.contexts.update({name: dict(sorted(table.items())) for name, table in (contexts or {}).items()})
        self.boots, self.engine_time = boots, engine_time
        self.reports, self.requests = [], []
        self.silent = silent  # Like Cisco: drop what can't be decrypted instead of reporting it
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def report(self, client, message, request_id, number, user=None, oid=None):
        self.reports.append(number)
        pdu = report_pdu(request_id, oid or snmpv3.USM_STATS + (number, 0))
        user = user or V3User("", "none")
        reply = build_message(user, ENGINE_ID, self.boots, self.engine_time, pdu, message.msg_id, reportable=False)
        self.sock.sendto(reply, client)

    def serve(self):
        while True:
            try:
                data, client = self.sock.recvfrom(65535)
            except OSError:
                return
            message = parse_message(data)
            if not message.engine_id:
                self.report(client, message, 0, snmpv3.UNKNOWN_ENGINE)
                continue
            user = self.users.get(message.user_name.decode())
            if user is None:
                self.report(client, message, 0, snmpv3.UNKNOWN_USER)
                continue
            if message_level(message.flags) != user.level:
                self.report(client, message, 0, snmpv3.UNSUPPORTED_LEVEL)
                continue
            if user.auth != "none":
                try:
                    verify(user, message)
                except ValueError:
                    self.report(client, message, 0, snmpv3.WRONG_DIGEST)
                    continue
                if message.boots != self.boots or abs(message.engine_time - self.engine_time) > 150:
                    self.report(client, message, 0, snmpv3.NOT_IN_TIME, dataclasses.replace(user, priv="none"))
                    continue
            try:
                context, pdu_type, pdu = open_message(user, message)
                request_id, _, max_repetitions, varbinds = parse_pdu(pdu)
            except (ValueError, IndexError):
                if not self.silent:
                    self.report(client, message, 0, snmpv3.DECRYPTION_ERROR)
                continue
            mib = self.contexts.get(context.decode())
            if mib is None:
                self.report(client, message, request_id, 0, oid=snmpv3.UNKNOWN_CONTEXT)
                continue
            self.requests.append((user.user, context.decode(), pdu_type))
            oid = varbinds[0][0]
            results = []
            if pdu_type == GET:
                results = [(item, mib.get(item, Value(0x81, None))) for item, _ in varbinds]
            elif pdu_type == GET_NEXT:
                found = next(((key, value) for key, value in mib.items() if key > oid), None)
                results = [found or (oid, Value(END_OF_MIB_VIEW, None))]
            elif pdu_type == GET_BULK:
                current = oid
                for _ in range(max_repetitions):
                    found = next(((key, value) for key, value in mib.items() if key > current), None)
                    if not found:
                        results.append((current, Value(END_OF_MIB_VIEW, None)))
                        break
                    results.append(found)
                    current = found[0]
            body = b"".join(tlv(SEQUENCE, encode_oid(key) + encode_value(value)) for key, value in results)
            response = tlv(RESPONSE, encode_integer(request_id) + encode_integer(0) + encode_integer(0) +
                           tlv(SEQUENCE, body))
            self.sock.sendto(build_message(user, ENGINE_ID, self.boots, self.engine_time, response, message.msg_id,
                                           context.decode(), reportable=False), client)

    def close(self):
        self.sock.close()


@pytest.fixture
def agent():
    snmpv3.forget_engines()
    fake = FakeV3Agent(contexts={"vlan-20": VLAN_MIB})
    yield fake
    fake.close()
    snmpv3.forget_engines()


def client(agent, user, **options):
    return SnmpClient("127.0.0.1", user, timeout=options.pop("timeout", 1000), retries=0, port=agent.port, **options)


def test_keys_match_rfc_3414_appendix_a3():
    engine_id = bytes(11) + b"\x02"
    md5 = password_to_key("maplesyrup", hashlib.md5)
    assert md5.hex() == "9faf3283884e92834ebc9847d8edd963"
    assert localize_key(md5, engine_id, hashlib.md5).hex() == "526f5eed9fcce26f8964c2930787d82b"
    sha = password_to_key("maplesyrup", hashlib.sha1)
    assert sha.hex() == "9fb5cc0381497b3793528939ff788d5d79145211"
    assert localize_key(sha, engine_id, hashlib.sha1).hex() == "6695febc9288e36282235fc7151f128497b38f3f"


def test_priv_keys_are_long_enough_for_the_cipher():
    assert len(priv_key(SHA_AES, ENGINE_ID)) == 16
    assert len(priv_key(SHA_AES192, ENGINE_ID)) == 24
    assert len(priv_key(MD5_DES, ENGINE_ID)) == 16
    assert len(priv_key(V3User("u", "md5", "authpass1", "aes256", "privpass1"), ENGINE_ID)) == 32
    # Extended Reeder-style: the first part is the plain localized key
    plain = localize_key(password_to_key("privpass4", hashlib.sha1), ENGINE_ID, hashlib.sha1)
    assert priv_key(SHA_AES192, ENGINE_ID)[:20] == plain


@pytest.mark.parametrize("user", USERS, ids=lambda user: user.user)
def test_messages_round_trip(user):
    pdu = build_pdu(GET, 42, [(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    data = build_message(user, ENGINE_ID, 3, 500, pdu, 99, "vlan-5")
    message = parse_message(data)
    assert (message.msg_id, message.engine_id, message.boots, message.engine_time) == (99, ENGINE_ID, 3, 500)
    assert message_level(message.flags) == user.level
    assert message.encrypted == (user.level == AUTH_PRIV)
    context, pdu_type, inner = open_message(user, message)
    assert (context, pdu_type, parse_pdu(inner)[0]) == (b"vlan-5", GET, 42)


def test_tampering_and_wrong_passwords_are_caught():
    data = build_message(SHA_AES, ENGINE_ID, 1, 1, build_pdu(GET, 1, []), 5)
    tampered = bytearray(data)
    tampered[-1] ^= 1
    with pytest.raises(ValueError):
        verify(SHA_AES, parse_message(bytes(tampered)))
    with pytest.raises(ValueError):
        verify(dataclasses.replace(SHA_AES, auth_password="different1"), parse_message(data))


@pytest.mark.parametrize("user", USERS, ids=lambda user: user.user)
def test_get_and_walk_at_every_level(agent, user):
    reader = client(agent, user)
    assert reader.version == V3
    assert reader.get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])[0][1].value == b"access-sw-02"
    rows = list(reader.walk((1, 3, 6, 1, 2, 1, 2, 2, 1, 2)))
    assert [value.value for _, value in rows] == [b"GigabitEthernet0/1", b"GigabitEthernet0/2"]


def test_interface_summary_over_v3(agent):
    rows = interface_summary(client(agent, SHA_AES))
    assert [row.name for row in rows] == ["Gi0/1", "Gi0/2"]


def test_engine_is_discovered_once_per_device(agent):
    client(agent, SHA_AES).get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    client(agent, MD5_DES).get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    assert agent.reports.count(snmpv3.UNKNOWN_ENGINE) == 1


def test_context_reads_a_vlan_table(agent):
    rows = list(client(agent, SHA_AES, context="vlan-20").walk((1, 3, 6, 1, 2, 1, 17, 4, 3, 1, 2)))
    assert len(rows) == 1 and agent.requests[-1][1] == "vlan-20"
    with pytest.raises(SnmpError, match="no SNMPv3 context 'vlan-30'"):
        client(agent, SHA_AES, context="vlan-30").get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])


@pytest.mark.parametrize("user, message", [
    (V3User("stranger", "sha", "authpass1", "aes128", "privpass1"), "doesn't know SNMPv3 user stranger"),
    (dataclasses.replace(SHA_AES, auth_password="wrongpass"), "authentication password or protocol"),
    (dataclasses.replace(SHA_AES, auth="md5"), "authentication password or protocol"),
    (dataclasses.replace(SHA_AES, priv_password="wrongpass"), "privacy password or protocol"),
    (dataclasses.replace(SHA_AES, priv="none"), "doesn't allow authNoPriv"),
])
def test_reports_become_clear_errors(agent, user, message):
    with pytest.raises(SnmpError, match=message):
        client(agent, user).get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])


def test_engine_time_is_resynchronized(agent):
    client(agent, SHA_AES).get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    agent.boots, agent.engine_time = 8, 5  # The switch restarted
    assert client(agent, SHA_AES).get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])[0][1].value == b"access-sw-02"
    assert snmpv3.NOT_IN_TIME in agent.reports


def test_invalid_user_is_refused_before_sending(agent):
    with pytest.raises(SnmpError, match="at least 8 characters"):
        client(agent, V3User("short", "sha", "1234", "none")).get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    assert agent.reports == []


def test_v2c_requests_are_unchanged():
    # A v2c GET of sysName.0, request ID 7, community public, byte for byte
    packet = build_request(1, "public", GET, 7, [(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    assert packet.hex() == ("3026" "020101" "04067075626c6963" "a019" "020107" "020100" "020100"
                            "300e" "300c" "06082b06010201010500" "0500")
    assert SnmpClient("127.0.0.1", "public").version == 1


def trap_message(user, trap_oid, pdu_type=TRAP_V2, sender=bytes.fromhex("800000090300aabbccddee01")):
    varbinds = [((1, 3, 6, 1, 2, 1, 1, 3, 0), tlv(0x43, b"\x10")),
                (SNMP_TRAP_OID, encode_oid(trap_oid))]
    body = b"".join(tlv(SEQUENCE, encode_oid(oid) + value) for oid, value in varbinds)
    pdu = tlv(pdu_type, encode_integer(9) + encode_integer(0) + encode_integer(0) + tlv(SEQUENCE, body))
    return build_message(user, sender, 2, 3600, pdu, 77, reportable=False)


@pytest.mark.parametrize("user", USERS, ids=lambda user: user.user)
def test_v3_traps_are_checked_and_decrypted(user):
    link_up = SNMP_TRAPS + (4,)
    trap = parse_trap(trap_message(user, link_up), USERS)
    assert (trap.version, trap.community, trap.trap_oid, trap.inform) == (V3, user.user, link_up, False)


def test_v3_traps_from_unknown_users_or_wrong_passwords_are_refused():
    link_up = SNMP_TRAPS + (4,)
    with pytest.raises(ValueError, match="isn't set up"):
        parse_trap(trap_message(V3User("other", "sha", "authpass1", "aes128", "privpass1"), link_up), USERS)
    with pytest.raises(ValueError, match="couldn't be checked"):
        parse_trap(trap_message(dataclasses.replace(SHA_AES, priv_password="wrongpass"), link_up), USERS)
    with pytest.raises(ValueError):
        parse_trap(trap_message(SHA_AES, link_up), [])
    with pytest.raises(ValueError, match="informs"):
        parse_trap(trap_message(SHA_AES, link_up, INFORM), USERS)


def test_credentials_round_trip_and_never_show_passwords():
    for credential in ("public", SHA_AES, NO_AUTH_USER):
        assert credential_from_json(credential_to_json(credential)) == credential
    assert credential_label("public") == "public"
    assert credential_label(SHA_AES) == "v3 user nomad (SHA-1, AES-128)"
    assert "authpass1" not in repr(SHA_AES) and "privpass1" not in credential_label(SHA_AES)
    assert users_from_json([SHA_AES.to_json(), {"v3": MD5_DES.to_json()}, "junk", {}]) == [SHA_AES, MD5_DES]
    assert {SHA_AES, SHA_AES, "public"} == {SHA_AES, "public"}  # Usable in community lists


def test_user_problems():
    assert SHA_AES.problem() == ""
    assert "1 to 32" in V3User("").problem()
    assert "needs authentication" in V3User("u", "none", "", "aes128", "privpass1").problem()
    assert "privacy password" in V3User("u", "sha", "authpass1", "aes128", "short").problem()
    assert NO_AUTH_USER.level == NO_AUTH


def test_a_device_that_drops_what_it_cannot_decrypt_is_explained():
    snmpv3.forget_engines()
    fake = FakeV3Agent(silent=True)
    try:
        reader = client(fake, dataclasses.replace(SHA_AES, priv_password="wrongpass"), timeout=300)
        with pytest.raises(SnmpError, match="answered SNMPv3 but not the request as user nomad: check its privacy "
                                            "password"):
            reader.get([(1, 3, 6, 1, 2, 1, 1, 5, 0)])
    finally:
        fake.close()
        snmpv3.forget_engines()


def test_authorization_errors_name_the_user_and_level():
    reader = SnmpClient("127.0.0.1", AUTH_ONLY)
    assert reader.refused(16) == ("127.0.0.1 won't let SNMPv3 user watcher read this at authNoPriv: check the "
                                  "security level (auth or priv) and read view of the user's group")
    assert SnmpClient("127.0.0.1", "public").refused(16) == "The device refused the request: authorizationError"
