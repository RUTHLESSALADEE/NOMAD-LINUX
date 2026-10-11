"""Problems found in a capture, the ones worth a look when troubleshooting from a mirrored port: TCP trouble
(retransmissions, resets, refused or unanswered connections, slow handshakes, zero windows, packets missing from
the capture), DNS failures and slow answers, DHCP that gets no offer (or from two servers), duplicate IP addresses,
spanning tree changes, broadcast storms, unreachables, HSRP/VRRP fights, 802.1X and RADIUS rejections, and more.
Each problem knows the display filter that shows its packets."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .. import oui
from . import decode

ERROR, WARNING, NOTE = "Error", "Warning", "Note"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, NOTE: 2}
SLOW_DNS_SECONDS = 0.5
SLOW_HANDSHAKE_SECONDS = 0.2
VERY_SLOW_HANDSHAKE_SECONDS = 1.0
QUIET_END_SECONDS = 1.0  # A question asked this close to the end of the capture may have been answered after it
STORM_PER_SECOND = 1000
BUSY_BROADCAST_PER_SECOND = 300
UNANSWERED_ARP_TRIES = 3
ARP_SCAN_TARGETS = 100
MAX_CHILDREN = 50
MAX_NUMBERS = 300  # Packets listed in a frame.number filter
RETRANSMIT_FLAGS = ("retransmission", "fast_retransmission", "spurious_retransmission")
BAD_SMB = {0xC0000022, 0xC000006D, 0xC0000072, 0xC0000071, 0xC0000234, 0xC00000CC, 0xC000015B}
QUIET_SMB = {0, 0xC0000016, 0x00000103, 0x80000005, 0x80000006}
VIRTUAL_MAC_PREFIXES = ("00:00:0c:07:ac", "00:00:0c:9f:f", "00:00:5e:00:01", "00:00:5e:00:02", "00:07:b4:00")


@dataclass
class Problem:
    severity: str
    group: str  # TCP, DNS, DHCP, ARP, ...
    summary: str
    detail: str = ""
    count: int = 0  # Packets
    filter: str = ""
    first: int = 0  # The first packet's number
    children: list = field(default_factory=list)  # Problem: each instance, when there are several


def numbers_filter(numbers):
    numbers = sorted(set(numbers))[:MAX_NUMBERS]
    return "frame.number in {" + " ".join(str(number) for number in numbers) + "}" if numbers else ""


def address_field(address, role="addr"):
    return f"ipv6.{role} == {address}" if ":" in address else f"ip.{role} == {address}"


def endpoint(address, port):
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def plural(count, word, many=None):
    return f"{count:,} {word if count == 1 else many or word + 's'}"


def mac_name(address):
    vendor = oui.vendor(address)
    return f"{address} ({vendor})" if vendor else address


class Collector:
    def __init__(self, view):
        self.view = view
        self.last_time = 0.0
        self.malformed = []
        self.dns = {}  # (client, client port, id) -> [time, number, name, server, numbers]
        self.dns_errors = defaultdict(list)  # (server, rcode, name) -> numbers
        self.dns_slow = defaultdict(list)  # server -> [(seconds, number, name)]
        self.dhcp = {}  # xid -> dict
        self.dhcp_servers = {}  # server ID -> (number, MAC that sent it)
        self.arp_macs = defaultdict(dict)  # IP -> {MAC: first number}
        self.arp_requests = defaultdict(list)  # target IP -> [(number, sender MAC)]
        self.arp_answered = set()
        self.arp_targets = defaultdict(set)  # sender MAC -> targets asked for
        self.unicast_from = defaultdict(set)  # MAC -> MACs seen sending it unicast frames (a few)
        self.unicast_frames = Counter()  # MAC -> unicast frames it sent or was sent
        self.unicast_total = 0
        self.source_ips = defaultdict(set)  # MAC -> IP addresses it sent from (a few: a router sends from many)
        self.icmp = defaultdict(list)  # (kind text, reporter, about) -> numbers
        self.stp_changes = defaultdict(list)  # bridge MAC -> numbers
        self.stp_roots = defaultdict(dict)  # VLAN -> {root text: first number}
        self.stp_bridges = Counter()
        self.broadcast_seconds = Counter()
        self.hsrp = defaultdict(lambda: defaultdict(list))  # group -> sender -> [(state, number)]
        self.vrrp = defaultdict(set)  # VRID -> senders
        self.vrrp_numbers = defaultdict(list)
        self.eap_failures = defaultdict(list)  # client MAC -> numbers
        self.radius = {}  # (client, id) -> (user, number, server)
        self.radius_rejects = defaultdict(list)  # user -> numbers
        self.smb = defaultdict(list)  # status -> numbers
        self.tls_alerts = defaultdict(list)  # (sender, alert) -> numbers
        self.http = defaultdict(list)  # code -> numbers
        self.tftp = defaultdict(list)  # message -> numbers
        self.snmp = {}  # (manager, port, request id) -> (number, agent, time)
        self.snmp_errors = defaultdict(list)
        self.syslog_errors = []
        self.cdp_half = {}  # (device, port) -> number
        self.dtp = {}
        self.vtp = {}

    # ----------------------------------------------------------------- Each packet

    def add(self, packet, index, d):
        self.last_time = packet.time
        number = packet.number
        values = d.values
        chain = packet.chain
        if d.malformed:
            self.malformed.append(number)
        if d.macs:
            destination = d.macs[1]
            if destination == decode.BROADCAST:
                self.broadcast_seconds[int(packet.time)] += 1
            elif not int(destination[1], 16) & 1:
                senders = self.unicast_from[destination]
                if len(senders) < 3:
                    senders.add(d.macs[0])
                self.unicast_frames[destination] += 1
                self.unicast_frames[d.macs[0]] += 1
                self.unicast_total += 1
            if d.addresses:
                addresses = self.source_ips[d.macs[0]]
                if len(addresses) < 3:
                    addresses.add(d.addresses[0])
        if "arp" in chain and "arp.opcode" in values:
            self.add_arp(number, values)
        if "dns" in chain and "dns.id" in values and d.ports:
            self.add_dns(packet, d, values)
        if "dhcp" in chain and "dhcp.option.dhcp" in values:
            self.add_dhcp(number, packet, d, values)
        if "icmp" in chain or "icmpv6" in chain:
            self.add_icmp(number, d, values)
        if "stp" in chain:
            self.add_stp(number, d, values)
        if "hsrp" in chain and "hsrp.state" in values and d.addresses:
            self.hsrp[(values["hsrp.group"][0], str(values["hsrp.virt_ip"][0]))][d.addresses[0]].append(
                (values["hsrp.state"][0], number))
        if "vrrp" in chain and "vrrp.virt_rtr_id" in values and d.addresses:
            self.vrrp[values["vrrp.virt_rtr_id"][0]].add(d.addresses[0])
            self.vrrp_numbers[values["vrrp.virt_rtr_id"][0]].append(number)
        if "eap" in chain and values.get("eap.code") == [4] and d.macs:
            self.eap_failures[d.macs[1]].append(number)
        if "radius" in chain and "radius.code" in values and d.addresses and d.ports:
            self.add_radius(number, d, values)
        if "smb2" in chain and values.get("smb2.flags.response") == [1]:
            status = values["smb2.nt_status"][0]
            if status not in QUIET_SMB:
                self.smb[status].append(number)
        if "tls.alert_message.desc" in values and d.addresses:
            for alert in values["tls.alert_message.desc"]:
                if alert:
                    self.tls_alerts[(endpoint(d.addresses[0], d.ports[1]), alert)].append(number)
        if "http.response.code" in values:
            code = values["http.response.code"][0]
            if code >= 400:
                self.http[code].append(number)
        if "tftp.error.message" in values:
            self.tftp[values["tftp.error.message"][0]].append(number)
        if "snmp" in chain and "snmp.request_id" in values and d.addresses and d.ports:
            self.add_snmp(packet, d, values)
        if "syslog.level" in values and values["syslog.level"][0] <= 3:
            self.syslog_errors.append(number)
        if "cdp" in chain:
            duplex = d.values.get("cdp.duplex")
            if duplex and duplex[0] == "Half":
                self.cdp_half.setdefault((values.get("cdp.deviceid", [""])[0], values.get("cdp.portid", [""])[0]),
                                         number)
        if "dtp" in chain and "dtp.tos" in values and d.macs:
            self.dtp.setdefault(d.macs[0], (packet.info, number))
        if "vtp" in chain and "vtp.md" in values:
            self.vtp.setdefault(values["vtp.md"][0], (packet.info, number))

    def add_arp(self, number, values):
        opcode, sender_mac = values["arp.opcode"][0], values["arp.src.hw_mac"][0]
        sender, target = str(values["arp.src.proto_ipv4"][0]), str(values["arp.dst.proto_ipv4"][0])
        if sender != "0.0.0.0":
            self.arp_macs[sender].setdefault(sender_mac, number)
            self.arp_answered.add(sender)
        if opcode == 1 and sender != target:
            self.arp_requests[target].append((number, sender_mac))
            self.arp_targets[sender_mac].add(target)

    def add_dns(self, packet, d, values):
        if d.ports[0] != "udp" or (53 not in d.ports[1:]):
            return
        ident = values["dns.id"][0]
        name = values.get("dns.qry.name", [""])[0]
        source, destination = d.addresses
        if values["dns.flags.response"][0] == 0:
            key = (source, d.ports[1], ident)
            entry = self.dns.get(key)
            if entry is None:
                self.dns[key] = [packet.time, packet.number, name, destination, [packet.number]]
            else:
                entry[4].append(packet.number)
            return
        key = (destination, d.ports[2], ident)
        entry = self.dns.pop(key, None)
        rcode = values.get("dns.flags.rcode", [0])[0]
        if rcode:
            self.dns_errors[(source, rcode, name)].append(packet.number)
        if entry is not None:
            seconds = packet.time - entry[0]
            if seconds > SLOW_DNS_SECONDS:
                self.dns_slow[source].append((seconds, packet.number, name, entry[1]))

    def add_dhcp(self, number, packet, d, values):
        kind, xid = values["dhcp.option.dhcp"][0], values["dhcp.id"][0]
        transaction = self.dhcp.setdefault(xid, {"mac": values["dhcp.hw.mac_addr"][0], "discover": [],
                                                 "offers": {}, "request": [], "ack": [], "nak": [], "time": 0.0,
                                                 "numbers": []})
        transaction["numbers"].append(number)
        server = str(values.get("dhcp.option.dhcp_server_id", [""])[0]) or (d.addresses[0] if d.addresses else "")
        if kind == 1:
            transaction["discover"].append(number)
            transaction["time"] = packet.time
        elif kind == 2:
            transaction["offers"][server] = number
        elif kind == 3:
            transaction["request"].append(number)
            transaction["time"] = packet.time
        elif kind == 5:
            transaction["ack"].append(number)
        elif kind == 6:
            transaction["nak"].append((number, server))
        if kind in (2, 5) and server:
            self.dhcp_servers.setdefault(server, (number, d.macs[0] if d.macs else ""))

    def add_icmp(self, number, d, values):
        reporter = d.addresses[0] if d.addresses else ""
        if "icmp.type" in values:
            kind, code = values["icmp.type"][0], values["icmp.code"][0]
            about = str(values.get("icmp.orig.dst", [""])[0])
            if kind == 3:
                text = decode.UNREACHABLE.get(code, f"Unreachable (code {code})")
                if code == 4:
                    text += f" (next-hop MTU {values.get('icmp.mtu', ['?'])[0]})"
                self.icmp[(text, reporter, about, f"icmp.type == 3 && icmp.code == {code}")].append(number)
            elif kind == 11:
                self.icmp[("Time to live exceeded", reporter, about, "icmp.type == 11")].append(number)
            elif kind == 5:
                self.icmp[("Redirect", reporter, about, "icmp.type == 5")].append(number)
        elif "icmpv6.type" in values:
            kind = values["icmpv6.type"][0]
            if kind in (1, 2, 3):
                text = decode.ICMPV6_TYPES[kind]
                if kind == 2:
                    text += f" (MTU {values.get('icmpv6.mtu', ['?'])[0]})"
                self.icmp[(text, reporter, "", f"icmpv6.type == {kind}")].append(number)

    def add_stp(self, number, d, values):
        bridge = values.get("stp.bridge.hw", [d.macs[0] if d.macs else ""])[0]
        if values.get("stp.tcn") or values.get("stp.flags.tc") == [1]:
            self.stp_changes[bridge].append(number)
        if "stp.root.hw" in values:
            vlan = values.get("stp.pvst.origvlan", values.get("stp.root.ext", [0]))[0]
            root = f"{values['stp.root.prio'][0]}/{values['stp.root.ext'][0]}/{values['stp.root.hw'][0]}"
            self.stp_roots[vlan].setdefault(root, number)
            self.stp_bridges[bridge] += 1

    def add_radius(self, number, d, values):
        code, ident = values["radius.code"][0], values["radius.id"][0]
        if code in (1, 4, 12):
            user = values.get("radius.User_Name", [""])[0]
            self.radius[(d.addresses[0], d.ports[1], ident)] = (user, number, d.addresses[1], code)
            return
        request = self.radius.pop((d.addresses[1], d.ports[2], ident), None)
        if code == 3:
            user = request[0] if request else ""
            self.radius_rejects[user or "(unknown user)"].append(number)

    def add_snmp(self, packet, d, values):
        pdu = values.get("snmp.pdu_type", [0])[0]
        request_id = values["snmp.request_id"][0]
        if pdu in (0xA0, 0xA1, 0xA3, 0xA5):
            self.snmp[(d.addresses[0], d.ports[1], request_id)] = (packet.number, d.addresses[1], packet.time)
        elif pdu == 0xA2:
            self.snmp.pop((d.addresses[1], d.ports[2], request_id), None)
            error = values.get("snmp.error_status", [0])[0]
            if error:
                self.snmp_errors[(d.addresses[0], error)].append(packet.number)

    # ----------------------------------------------------------------- At the end

    def finish(self, capture):
        problems = []
        problems += self.capture_notes(capture)
        problems += self.tcp_problems()
        problems += self.dns_problems()
        problems += self.dhcp_problems()
        problems += self.arp_problems()
        problems += self.layer2_problems()
        problems += self.icmp_problems()
        problems += self.redundancy_problems()
        problems += self.application_problems()
        if self.malformed:
            problems.append(Problem(NOTE, "Decoding", f"{plural(len(self.malformed), 'packet')} NOMAD couldn't "
                                    "fully decode", "Cut short, or a protocol variant NOMAD doesn't know. Wireshark "
                                    "decodes more.", len(self.malformed), "_ws.malformed", self.malformed[0]))
        problems.sort(key=lambda problem: (SEVERITY_ORDER[problem.severity], -problem.count))
        return problems

    def capture_notes(self, capture):
        notes = []
        view = self.view
        if view.unwrapped:
            result = capture.erspan
            senders = ", ".join(sorted(result.senders))
            sessions = ", ".join(str(session) for session in sorted(result.sessions) if session)
            notes.append(Problem(NOTE, "Capture", f"Showing the frames {senders} mirrored over ERSPAN"
                                 f"{' (session ' + sessions + ')' if sessions else ''}",
                                 f"{plural(len(capture.captured) - len(capture.mirrored), 'other packet')} captured "
                                 "aren't shown. Choose Show as Captured to see everything."))
            if result.incomplete:
                notes.append(Problem(WARNING, "Capture", f"{plural(result.incomplete, 'mirrored frame')} arrived "
                                     "split in two and a piece never came",
                                     "The network between the switch and this PC dropped part of them."))
        elif capture.mirrored:
            notes.append(Problem(NOTE, "Capture", f"{plural(len(capture.mirrored), 'frame')} mirrored to this PC "
                                 f"over ERSPAN {'is' if len(capture.mirrored) == 1 else 'are'} in this capture",
                                 "Choose Show Mirrored Frames to see them unwrapped.", len(capture.mirrored),
                                 "erspan"))
        if capture.duplicates:
            notes.append(Problem(NOTE, "Capture", f"{plural(capture.duplicates, 'packet')} captured twice "
                                 "were hidden", "pktmon can see one packet at more than one point, and a mirror of "
                                 "two ports (or both directions) copies a frame passing between them twice. Untick "
                                 "Hide Duplicates to see them."))
        if capture.truncated:
            notes.append(Problem(NOTE, "Capture", f"Only the start of {plural(capture.truncated, 'packet')} "
                                 "was kept", "Tick Capture Whole Packets to keep all of each (needed to follow "
                                 "streams).", capture.truncated, "frame.len > frame.cap_len"))
        if capture.too_many:
            notes.append(Problem(WARNING, "Capture", f"Only the first {len(capture.captured):,} packets were "
                                 "opened", "Open the file in Wireshark to see the rest, or capture with a filter."))
        return notes

    def tcp_problems(self):
        problems = []
        streams = [stream for stream in self.view.tracker.streams if stream.kind == "tcp"]
        if not streams:
            return problems
        total = sum(len(stream.packets) for stream in streams)

        resent = [(sum(stream.counts.get(flag, 0) for flag in RETRANSMIT_FLAGS), stream) for stream in streams]
        resent = sorted([item for item in resent if item[0]], key=lambda item: -item[0])
        count = sum(item[0] for item in resent)
        if count:
            share = count * 100 / total
            problem = Problem(WARNING if share >= 1 else NOTE, "TCP",
                              f"{plural(count, 'packet')} resent ({share:.1f}% of TCP) in "
                              f"{plural(len(resent), 'connection')}",
                              "Resent because they (or their acknowledgment) were lost or late. Over 1% slows "
                              "connections down: look for errors or drops on the path.", count,
                              "tcp.analysis.retransmission || tcp.analysis.fast_retransmission || "
                              "tcp.analysis.spurious_retransmission")
            problem.children = [Problem(problem.severity, "TCP", f"{plural(number, 'resent')} in "
                                        f"{stream.describe()}", "", number,
                                        f"tcp.stream == {stream.index} && (tcp.analysis.retransmission || "
                                        f"tcp.analysis.fast_retransmission || tcp.analysis.spurious_retransmission)")
                                for number, stream in resent[:MAX_CHILDREN]]
            problems.append(problem)

        gaps = sorted([(stream.counts.get("lost_segment", 0) + stream.counts.get("ack_lost_segment", 0), stream)
                       for stream in streams], key=lambda item: -item[0])
        gaps = [item for item in gaps if item[0]]
        count = sum(item[0] for item in gaps)
        if count:
            problem = Problem(WARNING, "TCP", f"Packets missing from the capture in "
                              f"{plural(len(gaps), 'connection')} ({count:,} gaps)",
                              "Packets the conversation shows were sent never reached the capture. On a mirrored "
                              "port this usually means the mirror dropped copies: the ports mirrored carry more than "
                              "the destination port can send, or the switch is busy. It can also be real loss "
                              "before the mirrored port.", count,
                              "tcp.analysis.lost_segment || tcp.analysis.ack_lost_segment")
            problem.children = [Problem(WARNING, "TCP", f"{plural(number, 'gap')} in {stream.describe()}", "", number,
                                        f"tcp.stream == {stream.index} && (tcp.analysis.lost_segment || "
                                        "tcp.analysis.ack_lost_segment)") for number, stream in gaps[:MAX_CHILDREN]]
            problems.append(problem)

        one_way = [stream for stream in streams if len(stream.packets) >= 5 and
                   min(stream.flows[0].packets, stream.flows[1].packets) == 0]
        busy = [stream for stream in streams if len(stream.packets) >= 5]
        if busy and len(one_way) * 2 >= len(busy):
            problems.append(Problem(WARNING, "Capture", f"Only one direction of {len(one_way)} of "
                                    f"{len(busy)} busy connections is in the capture",
                                    "The mirror may copy only received (rx) or only sent (tx) traffic, or the replies "
                                    "take another path (another port, an EtherChannel member not mirrored). Mirror "
                                    "both directions, or all the members of a port channel.",
                                    sum(len(stream.packets) for stream in one_way),
                                    " || ".join(f"tcp.stream == {stream.index}" for stream in one_way[:30])))

        refused = defaultdict(list)
        unanswered = defaultdict(list)
        slow = defaultdict(list)
        resets = []
        for stream in streams:
            server = endpoint(*stream.b)
            if stream.syn is not None and stream.syn_ack is None:
                if stream.reset_by == 1:
                    refused[server].append(stream)
                elif stream.reset_by is None and stream.flows[1].packets == 0 and \
                        self.last_time - stream.syn > QUIET_END_SECONDS:
                    unanswered[server].append(stream)
            elif stream.handshake_seconds is not None and stream.handshake_seconds > SLOW_HANDSHAKE_SECONDS:
                slow[server].append(stream)
            if stream.reset_by is not None and stream.syn_ack is not None:
                resets.append(stream)
        for server, items in sorted(refused.items(), key=lambda item: -len(item[1])):
            problems.append(Problem(WARNING, "TCP", f"{server} refused {plural(len(items), 'connection')}",
                                    "It answered with a reset: nothing listening on that port, or a firewall "
                                    "rejecting it.", sum(len(stream.packets) for stream in items),
                                    stream_filter(items)))
        for server, items in sorted(unanswered.items(), key=lambda item: -len(item[1])):
            tries = sum(stream.syn_count for stream in items)
            problems.append(Problem(WARNING, "TCP", f"No answer from {server} to "
                                    f"{plural(len(items), 'connection attempt')} ({plural(tries, 'SYN')})",
                                    "It's down or unreachable, or a firewall drops it. (Or the mirror only copies one "
                                    "direction.)", tries, stream_filter(items)))
        for server, items in sorted(slow.items(), key=lambda item: -max(s.handshake_seconds for s in item[1])):
            worst = max(stream.handshake_seconds for stream in items)
            problems.append(Problem(WARNING if worst >= VERY_SLOW_HANDSHAKE_SECONDS else NOTE, "TCP",
                                    f"{server} was slow to answer {plural(len(items), 'connection')} (up to "
                                    f"{worst * 1000:,.0f} ms)",
                                    "Its SYN/ACK took over 200 ms: a slow or distant server, a busy path, or a SYN "
                                    "lost and resent.", len(items), stream_filter(items)))
        if resets:
            problem = Problem(NOTE, "TCP", f"{plural(len(resets), 'connection')} ended with a reset",
                              "Often normal (browsers and servers close this way); worth a look if users see "
                              "dropped connections.", len(resets),
                              f"tcp.flags.reset == 1 && ({stream_filter(resets)})")
            problem.children = [Problem(NOTE, "TCP", f"{stream.describe()} reset by "
                                        f"{endpoint(*(stream.a if stream.reset_by == 0 else stream.b))}", "", 1,
                                        f"tcp.stream == {stream.index}") for stream in resets[:MAX_CHILDREN]]
            problems.append(problem)

        zero = [stream for stream in streams if stream.counts.get("zero_window")]
        if zero:
            problem = Problem(WARNING, "TCP", f"A receiver ran out of room in {plural(len(zero), 'connection')} "
                              "(zero window)", "The receiving program isn't keeping up: the slowness is in that "
                              "computer, not the network.", sum(stream.counts["zero_window"] for stream in zero),
                              "tcp.analysis.zero_window")
            problem.children = [Problem(WARNING, "TCP", stream.describe(), "", stream.counts["zero_window"],
                                        f"tcp.stream == {stream.index} && tcp.analysis.zero_window")
                                for stream in zero[:MAX_CHILDREN]]
            problems.append(problem)
        duplicates = sum(stream.counts.get("duplicate_ack", 0) for stream in streams)
        if duplicates:
            problems.append(Problem(NOTE, "TCP", f"{plural(duplicates, 'duplicate acknowledgment')}",
                                    "A receiver asking again for data it's missing: goes with loss.", duplicates,
                                    "tcp.analysis.duplicate_ack"))
        disorder = sum(stream.counts.get("out_of_order", 0) for stream in streams)
        if disorder:
            problems.append(Problem(NOTE, "TCP", f"{plural(disorder, 'packet')} out of order",
                                    "Arrived after ones sent later: more than one path, or loss.", disorder,
                                    "tcp.analysis.out_of_order"))
        return problems

    def dns_problems(self):
        problems = []
        unanswered = defaultdict(list)
        for (client, _, _), (asked, number, name, server, numbers) in self.dns.items():
            if self.last_time - asked > QUIET_END_SECONDS:
                unanswered[server].append((number, name, numbers))
        for server, items in sorted(unanswered.items(), key=lambda item: -len(item[1])):
            names = list(dict.fromkeys(name for _, name, _ in items))
            problems.append(Problem(WARNING, "DNS", f"No answer from DNS server {server} to "
                                    f"{plural(len(items), 'query', 'queries')}",
                                    "Asked for: " + ", ".join(names[:8]) + (" ..." if len(names) > 8 else ""),
                                    sum(len(numbers) for _, _, numbers in items),
                                    numbers_filter(number for _, _, numbers in items for number in numbers),
                                    items[0][0]))
        for (server, rcode, name), numbers in sorted(self.dns_errors.items(), key=lambda item: -len(item[1])):
            severity = NOTE if rcode == 3 else WARNING
            text = decode.DNS_RCODES.get(rcode, f"error {rcode}")
            problems.append(Problem(severity, "DNS", f"{server} answered {text} for {name or '(no name)'}"
                                    f"{' (' + plural(len(numbers), 'time') + ')' if len(numbers) > 1 else ''}",
                                    "No such name: the name doesn't exist (often a search suffix tried first)."
                                    if rcode == 3 else "The DNS server couldn't or wouldn't answer.", len(numbers),
                                    f'dns.flags.rcode == {rcode} && dns.qry.name == "{name}"', numbers[0]))
        for server, items in self.dns_slow.items():
            worst = max(items)
            problems.append(Problem(WARNING, "DNS", f"Slow answers from DNS server {server}: "
                                    f"{plural(len(items), 'answer')} over {SLOW_DNS_SECONDS * 1000:.0f} ms (slowest "
                                    f"{worst[0] * 1000:,.0f} ms, {worst[2]})",
                                    "Slow name lookups make everything feel slow.", len(items) * 2,
                                    numbers_filter([item[1] for item in items] + [item[3] for item in items]),
                                    worst[3]))
        return problems

    def dhcp_problems(self):
        problems = []
        no_offer = defaultdict(list)
        for xid, transaction in self.dhcp.items():
            if transaction["discover"] and not transaction["offers"] and not transaction["ack"] and \
                    self.last_time - transaction["time"] > QUIET_END_SECONDS:
                no_offer[transaction["mac"]].append(transaction)
            for number, server in transaction["nak"]:
                problems.append(Problem(WARNING, "DHCP", f"DHCP server {server} refused (NAK) "
                                        f"{mac_name(transaction['mac'])}",
                                        "The address it asked for isn't right for this network (it moved, or its "
                                        "lease is gone).", len(transaction["numbers"]), f"dhcp.id == 0x{xid:08x}",
                                        number))
        for client, transactions in no_offer.items():
            numbers = [number for transaction in transactions for number in transaction["numbers"]]
            problems.append(Problem(ERROR, "DHCP", f"No DHCP offer for {mac_name(client)} "
                                    f"({plural(len(numbers), 'Discover')})",
                                    "No DHCP server answered: check the VLAN's ip helper-address (DHCP relay), the "
                                    "server's scope, and that the port is in the right VLAN.", len(numbers),
                                    f"dhcp.hw.mac_addr == {client}", numbers[0]))
        if len(self.dhcp_servers) > 1:
            servers = ", ".join(f"{server} ({mac_name(mac)})" if mac else server
                                for server, (_, mac) in sorted(self.dhcp_servers.items()))
            problems.append(Problem(ERROR, "DHCP", f"{len(self.dhcp_servers)} different DHCP servers answered",
                                    f"Servers: {servers}. Unless that's intended (two servers splitting a scope), one "
                                    "may be a rogue DHCP server (a home router plugged in?).", len(self.dhcp_servers),
                                    " || ".join(f"dhcp.option.dhcp_server_id == {server}"
                                                for server in sorted(self.dhcp_servers)),
                                    min(number for number, _ in self.dhcp_servers.values())))
        return problems

    def arp_problems(self):
        problems = []
        for address, macs in self.arp_macs.items():
            real = [mac for mac in macs if not mac.startswith(VIRTUAL_MAC_PREFIXES)]
            if len(real) > 1:
                problems.append(Problem(ERROR, "ARP", f"Duplicate IP address {address}",
                                        "Claimed by " + " and ".join(mac_name(mac) for mac in real) + ". Two devices "
                                        "set to the same address (or a device that moved between ports).", len(real),
                                        f"arp.src.proto_ipv4 == {address}", min(macs.values())))
        unanswered = []
        # Only for askers whose replies would be in the capture: this PC (or the mirrored port's device), which is
        # in most unicast frames and sends from one or two addresses (unlike its gateway), or one seen receiving
        # unicast frames from several others (a mirror of its port, or of an uplink)
        seen = {mac for mac, count in self.unicast_frames.items()
                if count * 2 > self.unicast_total and len(self.source_ips[mac]) <= 2} | \
            {mac for mac, senders in self.unicast_from.items() if len(senders) >= 2}
        for address, requests in self.arp_requests.items():
            mine = [item for item in requests if item[1] in seen]
            if len(mine) >= UNANSWERED_ARP_TRIES and address not in self.arp_answered:
                unanswered.append((address, mine))
        if unanswered:
            unanswered.sort(key=lambda item: -len(item[1]))
            problem = Problem(WARNING, "ARP", f"Nothing answered ARP for {plural(len(unanswered), 'address', 'addresses')}",
                              "Asked for again and again with no reply: the device is off, in another VLAN, or its "
                              "address is set wrong.", sum(len(items) for _, items in unanswered),
                              " || ".join(f"arp.dst.proto_ipv4 == {address}" for address, _ in unanswered[:30]))
            problem.children = [Problem(WARNING, "ARP", f"{address} ({plural(len(items), 'request')} from "
                                        f"{', '.join(sorted({mac for _, mac in items}))})", "", len(items),
                                        f"arp.dst.proto_ipv4 == {address} && arp.opcode == 1", items[0][0])
                                for address, items in unanswered[:MAX_CHILDREN]]
            problems.append(problem)
        for sender, targets in self.arp_targets.items():
            if len(targets) >= ARP_SCAN_TARGETS:
                problems.append(Problem(NOTE, "ARP", f"{mac_name(sender)} asked for {len(targets)} different "
                                        "addresses", "A scan (an inventory tool, a discovery sweep) or a device "
                                        "talking to a whole subnet.", len(targets),
                                        f"arp.src.hw_mac == {sender} && arp.opcode == 1"))
        return problems

    def layer2_problems(self):
        problems = []
        if self.broadcast_seconds:
            second, peak = max(self.broadcast_seconds.items(), key=lambda item: item[1])
            if peak >= BUSY_BROADCAST_PER_SECOND:
                problems.append(Problem(ERROR if peak >= STORM_PER_SECOND else WARNING, "Broadcast",
                                        f"{peak:,} broadcasts in one second (at {second} s)",
                                        "A broadcast storm (a loop?) or a very chatty device: look at who sent them "
                                        "in Summaries > Top Talkers (Ethernet).", peak,
                                        f"eth.dst == ff:ff:ff:ff:ff:ff && frame.time_relative >= {second} && "
                                        f"frame.time_relative < {second + 1}"))
        if self.stp_changes:
            count = sum(len(numbers) for numbers in self.stp_changes.values())
            bridges = ", ".join(mac_name(bridge) for bridge in self.stp_changes)
            problems.append(Problem(WARNING, "STP", f"Spanning tree topology changes: {plural(count, 'BPDU')} "
                                    "flagged", f"From {bridges}. Each change flushes MAC tables (brief flooding); "
                                    "many mean a port flapping somewhere. Find it with show spanning-tree detail "
                                    "(\"from\" and \"occurred\").", count, "stp.flags.tc == 1 || stp.tcn",
                                    min(min(numbers) for numbers in self.stp_changes.values())))
        for vlan, roots in self.stp_roots.items():
            if len(roots) > 1:
                problems.append(Problem(WARNING, "STP", f"The root bridge changed{' in VLAN ' + str(vlan) if vlan else ''}",
                                        "Roots seen: " + ", ".join(roots), len(roots), "stp",
                                        min(roots.values())))
        if self.stp_bridges:
            bridges = ", ".join(mac_name(bridge) for bridge in self.stp_bridges)
            problems.append(Problem(NOTE, "STP", "Spanning tree BPDUs heard", f"From {bridges}: a switch is on "
                                    "the other end of this port (or the mirror copies a switch link).",
                                    sum(self.stp_bridges.values()), "stp"))
        for (device, port), number in self.cdp_half.items():
            problems.append(Problem(WARNING, "CDP", f"{device} {port} is half duplex",
                                    "Half duplex on a switch port usually means a speed/duplex mismatch: collisions "
                                    "and late collisions follow.", 1, f'cdp.deviceid == "{device}"', number))
        for sender, (info, number) in self.dtp.items():
            problems.append(Problem(NOTE, "DTP", f"{mac_name(sender)} is negotiating trunking",
                                    f"{info}. Switchport nonegotiate (or access mode) stops DTP on edge ports.", 1,
                                    "dtp", number))
        for domain, (info, number) in self.vtp.items():
            problems.append(Problem(NOTE, "VTP", f"VTP advertisements for domain {domain or '(none)'}", info, 1,
                                    "vtp", number))
        return problems

    def icmp_problems(self):
        problems = []
        grouped = defaultdict(list)
        for (text, reporter, about, filter_text), numbers in self.icmp.items():
            grouped[(text, filter_text)].append((reporter, about, numbers))
        for (text, filter_text), items in sorted(grouped.items(), key=lambda item: -len(item[1])):
            count = sum(len(numbers) for _, _, numbers in items)
            traceroute = text == "Time to live exceeded"
            severity = NOTE if traceroute or text == "Redirect" else WARNING
            detail = {"Time to live exceeded": "Usually traceroute. Many for the same destination can mean a "
                                               "routing loop.",
                      "Redirect": "A router telling a host to use another gateway: the host's gateway is wrong "
                                  "for that destination."}.get(text, "Routers or hosts reporting they couldn't "
                                                                     "deliver.")
            problem = Problem(severity, "ICMP", f"{text}: {plural(count, 'message')}", detail, count, filter_text,
                              min(number for _, _, numbers in items for number in numbers))
            problem.children = [Problem(severity, "ICMP", f"From {reporter}{' about ' + about if about else ''}"
                                        f" ({len(numbers)})", "", len(numbers),
                                        f"{filter_text} && {address_field(reporter, 'src')}"
                                        + (f" && icmp.orig.dst == {about}" if about else ""), numbers[0])
                                for reporter, about, numbers in sorted(items, key=lambda item: -len(item[2]))
                                [:MAX_CHILDREN]]
            problems.append(problem)
        return problems

    def redundancy_problems(self):
        problems = []
        for (group, virtual), senders in self.hsrp.items():
            active = sorted(sender for sender, states in senders.items() if any(state == 16 for state, _ in states))
            numbers = [number for states in senders.values() for _, number in states]
            if len(active) > 1:
                problems.append(Problem(ERROR, "HSRP", f"{len(active)} routers claim Active for HSRP group {group} "
                                        f"({virtual})", f"{', '.join(active)}: they can't hear each other's hellos "
                                        "(a VLAN or trunk problem between them), or a group number is reused.",
                                        len(numbers), f"hsrp.group == {group}", min(numbers)))
            changed = [sender for sender, states in senders.items() if len({state for state, _ in states}) > 1]
            if changed:
                problems.append(Problem(WARNING, "HSRP", f"HSRP group {group} ({virtual}) changed state",
                                        f"{', '.join(sorted(changed))} changed state during the capture: a failover "
                                        "happened.", len(numbers), f"hsrp.group == {group}", min(numbers)))
        for router, senders in self.vrrp.items():
            if len(senders) > 1:
                numbers = self.vrrp_numbers[router]
                problems.append(Problem(ERROR, "VRRP", f"{len(senders)} routers advertise as master for VRRP group "
                                        f"{router}", f"{', '.join(sorted(senders))}: they can't hear each other.",
                                        len(numbers), f"vrrp.virt_rtr_id == {router}", min(numbers)))
        return problems

    def application_problems(self):
        problems = []
        for client, numbers in self.eap_failures.items():
            problems.append(Problem(WARNING, "802.1X", f"802.1X failed for {mac_name(client)}"
                                    f"{' (' + plural(len(numbers), 'time') + ')' if len(numbers) > 1 else ''}",
                                    "The switch told it authentication failed: check the RADIUS server's log for "
                                    "why.", len(numbers), f"eap.code == 4 && eth.dst == {client}", numbers[0]))
        for user, numbers in self.radius_rejects.items():
            problems.append(Problem(WARNING, "RADIUS", f"RADIUS rejected {user}"
                                    f"{' (' + plural(len(numbers), 'time') + ')' if len(numbers) > 1 else ''}",
                                    "Access-Reject: wrong credentials, or no policy matched.", len(numbers),
                                    "radius.code == 3", numbers[0]))
        unanswered = defaultdict(list)
        for (client, _, _), (user, number, server, code) in self.radius.items():
            unanswered[server].append(number)
        for server, numbers in unanswered.items():
            problems.append(Problem(WARNING, "RADIUS", f"No answer from RADIUS server {server} to "
                                    f"{plural(len(numbers), 'request')}", "Wrong shared secret (the server ignores "
                                    "it), the server is down, or a firewall drops UDP 1812/1813.", len(numbers),
                                    numbers_filter(numbers), numbers[0]))
        for status, numbers in sorted(self.smb.items(), key=lambda item: -len(item[1])):
            name = decode.NT_STATUS.get(status, f"0x{status:08x}")
            problems.append(Problem(WARNING if status in BAD_SMB else NOTE, "SMB", f"SMB error {name}"
                                    f"{' (' + plural(len(numbers), 'time') + ')' if len(numbers) > 1 else ''}",
                                    "File sharing (SMB2) answered with an error.", len(numbers),
                                    f"smb2.nt_status == 0x{status:08x}", numbers[0]))
        for (sender, alert), numbers in self.tls_alerts.items():
            name = decode.TLS_ALERTS.get(alert, f"alert {alert}")
            problems.append(Problem(WARNING, "TLS", f"{sender} ended a TLS connection: {name}",
                                    "The two sides couldn't agree (versions, ciphers, a certificate not trusted, "
                                    "the name not on the certificate).", len(numbers),
                                    f"tls.alert_message.desc == {alert}", numbers[0]))
        for code, numbers in sorted(self.http.items()):
            problems.append(Problem(WARNING if code >= 500 else NOTE, "HTTP", f"{plural(len(numbers), 'answer')}"
                                    f" of HTTP {code}", "Server errors (5xx) or requests refused or not found "
                                    "(4xx).", len(numbers), f"http.response.code == {code}", numbers[0]))
        for message, numbers in self.tftp.items():
            problems.append(Problem(WARNING, "TFTP", f"TFTP error: {message}", "The TFTP server refused the "
                                    "transfer (file not found, access, or disk full).", len(numbers),
                                    "tftp.opcode == 5", numbers[0]))
        agents = defaultdict(list)
        for (_, _, _), (number, agent, asked) in self.snmp.items():
            if self.last_time - asked > QUIET_END_SECONDS:
                agents[agent].append(number)
        for agent, numbers in agents.items():
            problems.append(Problem(WARNING, "SNMP", f"No SNMP answer from {agent} to "
                                    f"{plural(len(numbers), 'request')}",
                                    "Usually the wrong community string (it's ignored silently) or an SNMP access "
                                    "list.", len(numbers), numbers_filter(numbers), numbers[0]))
        for (agent, error), numbers in self.snmp_errors.items():
            problems.append(Problem(NOTE, "SNMP", f"{agent} answered SNMP with "
                                    f"{decode.SNMP_ERRORS.get(error, error)}", "", len(numbers),
                                    f"snmp.error_status == {error}", numbers[0]))
        if self.syslog_errors:
            problems.append(Problem(NOTE, "Syslog", f"{plural(len(self.syslog_errors), 'syslog message')} at "
                                    "ERR or worse", "", len(self.syslog_errors), "syslog.level <= 3",
                                    self.syslog_errors[0]))
        return problems


def stream_filter(streams):
    return " || ".join(f"tcp.stream == {stream.index}" for stream in streams[:40])
