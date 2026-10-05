"""The tribe's VLANs on a laptop: read from the copy of the server's data (so they work offline), changed through the
server.

VLAN changes made while the server can't be reached are kept as pending, as address changes are: made in the copy at
once, several changes to one VLAN becoming one, and sent in order when the server is back (the IPAM page's sync does
the sending). Any the server refuses, because someone else changed that VLAN first, are kept as refused for the user
to resolve (use the next free number, or discard), and the copy goes back to the server's version. VLAN domains, like
subnets and networks, can only be changed online.
"""
import json

from .client import OldServerError, ServerUnreachable, TeamKeyError, now_text
from .store import IpamError, vlan_key
from .vlans import DELETE_VLAN, SET_VLAN, VlanStore, check_number, clean_subnets

PENDING, REFUSED = "pending", "refused"
OLD_SERVER = "The IPAM server is running an older version of NOMAD that doesn't keep VLANs: it needs updating " \
             "(Tools > Tribe Management > Update Service, on the server)."


class TeamVlanStore:
    """VlanStore's reading methods from the copy, and its changing methods sent to the server (or queued). Use on
    the UI thread, except send_pending, which only talks to the server."""

    def __init__(self, team):
        self.team = team
        self.local = VlanStore(team.copy)

    @property
    def copy(self):
        return self.team.copy

    @property
    def online(self):
        return self.team.online

    def __getattr__(self, name):
        if name in ("domains", "domain", "domain_named", "domains_for_vtp", "vlans", "vlan", "vlan_with_subnet",
                    "next_free", "deleted_numbers", "search"):
            return getattr(self.local, name)
        raise AttributeError(name)

    def _require_vlans(self):
        if not self.team.server_keeps_vlans:
            raise OldServerError(OLD_SERVER)

    def _send(self, action, **arguments):
        self._require_vlans()
        try:
            reply = self.team.client.edit(action, **arguments)
        except ServerUnreachable as error:
            self.team.online, self.team.last_error = False, str(error)
            raise ServerUnreachable("The IPAM server can't be reached, so VLAN domains can't be changed right now "
                                    "(VLANs can: they're sent when it's back).") from None
        self.team.online = True
        self.copy.apply_rows(reply["items"])
        return reply

    # ----------------------------------------------------------------- Domains (online only)

    def add_domain(self, name, network_id="", vtp_domain="", description="", ranges=None, fields=None):
        reply = self._send("add_vlan_domain", name=name, network_id=network_id, vtp_domain=vtp_domain,
                           description=description, ranges=ranges or [], fields=fields or {})
        created = [item["row"]["id"] for item in reply["items"] if item["entity"] == "vlan_domains"]
        return self.local.domain(created[-1])

    def update_domain(self, domain_id, **changes):
        self._send("update_vlan_domain", domain_id=domain_id, changes=changes,
                   expected_version=self.local.domain(domain_id).version)
        return self.local.domain(domain_id)

    def delete_domain(self, domain_id):
        self._send("delete_vlan_domain", domain_id=domain_id, expected_version=self.local.domain(domain_id).version)

    # ----------------------------------------------------------------- VLANs (offline too)

    def set_vlan(self, domain_id, number, name="", status="active", subnets=(), description="", fields=None):
        """Record a VLAN: straight to the server when it can be reached (and nothing is waiting to go before it),
        otherwise queued as pending."""
        number = check_number(number)
        data = dict(name=name.strip(), status=status, subnets=clean_subnets(subnets), description=description,
                    fields=fields or {})
        if self.online and not self.pending_count():
            current = self.local.vlan(domain_id, number)
            try:
                self._send(SET_VLAN, domain_id=domain_id, vlan=number,
                           expected_version=current.version if current else None, **data)
                return self.local.vlan(domain_id, number)
            except ServerUnreachable:
                pass  # Lost the server just now: keep the change for later
        self._queue(domain_id, number, SET_VLAN, data)
        return self.local.vlan(domain_id, number)

    def delete_vlan(self, domain_id, number):
        number = check_number(number)
        current = self.local.vlan(domain_id, number)
        if current is None:
            return
        if self.online and not self.pending_count():
            try:
                self._send(DELETE_VLAN, domain_id=domain_id, vlan=number, expected_version=current.version)
                return
            except ServerUnreachable:
                pass
        self._queue(domain_id, number, DELETE_VLAN, {})

    def set_vlans(self, domain_id, items):
        """Record several VLANs ([{"vlan", "name", "status", "subnets", "description", "fields"}]): in one request
        when the server can be reached (all or none), otherwise each queued."""
        items = [dict(item, vlan=check_number(item["vlan"]), subnets=clean_subnets(item.get("subnets")))
                 for item in items]
        if self.online and not self.pending_count():
            sent = []
            for item in items:
                current = self.local.vlan(domain_id, item["vlan"])
                sent.append(dict(item, expected_version=current.version if current else None))
            try:
                self._send("set_vlans", domain_id=domain_id, vlans=sent)
                return
            except ServerUnreachable:
                pass
        for item in items:
            values = {name: value for name, value in item.items() if name != "vlan"}
            self._queue(domain_id, item["vlan"], SET_VLAN, dict(values, name=values.get("name", "").strip()))

    # ----------------------------------------------------------------- Pending

    def pending_count(self):
        return self.copy.db.execute("SELECT COUNT(*) FROM vlan_pending WHERE state = ?", (PENDING,)).fetchone()[0]

    def pending_numbers(self, domain_id):
        rows = self.copy.db.execute("SELECT vlan FROM vlan_pending WHERE domain_id = ? AND state = ?",
                                    (domain_id, PENDING))
        return {row[0] for row in rows}

    def refused(self):
        """VLAN changes made offline that the server refused: [dict] with domain_id, vlan, action, data, error."""
        rows = self.copy.db.execute("SELECT * FROM vlan_pending WHERE state = ? ORDER BY seq", (REFUSED,)).fetchall()
        return [dict(row, data=json.loads(row["data"])) for row in rows]

    def discard(self, seq):
        with self.copy.transaction():
            self.copy.db.execute("DELETE FROM vlan_pending WHERE seq = ?", (seq,))

    def _queue(self, domain_id, number, action, data):
        """Make a VLAN change in the copy now and remember it for the server (merged with any earlier one)."""
        self._require_vlans()
        key = vlan_key(number)
        with self.copy.transaction():
            current = self.local.vlan(domain_id, number)
            if current is None and action == DELETE_VLAN:
                return
            original = self.copy.row_of("vlans", current.id)["row"] if current else None  # Before it changes
            # Made in the copy first: a change the copy can't take (a subnet in another VLAN) isn't queued
            if action == SET_VLAN:
                self.local.set_vlan(domain_id, number, **data)
            else:
                self.local.delete_vlan(domain_id, number)
            entry = self.copy.db.execute("SELECT * FROM vlan_pending WHERE domain_id = ? AND sort_key = ? AND state = ?",
                                         (domain_id, key, PENDING)).fetchone()
            if entry is None:
                self.copy.db.execute("INSERT INTO vlan_pending (domain_id, vlan, sort_key, action, data, "
                                     "expected_version, original, made) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (domain_id, number, key, action, json.dumps(data),
                                      current.version if current else None,
                                      json.dumps(original) if original else None, now_text()))
            elif action == DELETE_VLAN and entry["original"] is None:
                # Recorded offline and deleted again before the server heard about it: nothing to send
                self.copy.db.execute("DELETE FROM vlan_pending WHERE seq = ?", (entry["seq"],))
            else:
                self.copy.db.execute("UPDATE vlan_pending SET action = ?, data = ?, made = ? WHERE seq = ?",
                                     (action, json.dumps(data), now_text(), entry["seq"]))

    def outgoing(self):
        """The pending changes to send, oldest first (plain data, for a worker thread). None of them while the
        server is too old to take them: they wait until it's updated."""
        if not self.team.server_keeps_vlans:
            return []
        rows = self.copy.db.execute("SELECT * FROM vlan_pending WHERE state = ? ORDER BY seq", (PENDING,)).fetchall()
        return [dict(row, data=json.loads(row["data"])) for row in rows]

    def send_pending(self, entries):
        """Send pending VLAN changes in order (safe on a worker thread: it only talks to the server). Stops at the
        first that can't reach the server. Returns [(seq, "sent", reply) or (seq, "refused", message)]."""
        results = []
        for entry in entries:
            arguments = dict(domain_id=entry["domain_id"], vlan=entry["vlan"], expected_version=entry["expected_version"])
            if entry["action"] == SET_VLAN:
                arguments.update(entry["data"])
            try:
                reply = self.team.client.edit(entry["action"], **arguments)
            except (ServerUnreachable, OldServerError):
                break
            except TeamKeyError:
                raise
            except IpamError as error:  # Someone else changed it first, or the server couldn't make it
                results.append((entry["seq"], REFUSED, str(error)))
                continue
            results.append((entry["seq"], "sent", reply))
        return results

    def apply_sent(self, results):
        """Record what send_pending did (on the UI thread). Returns (sent, refused) counts."""
        sent = refused = 0
        for seq, outcome, detail in results:
            entry = self.copy.db.execute("SELECT * FROM vlan_pending WHERE seq = ?", (seq,)).fetchone()
            if entry is None:
                continue
            if outcome == "sent":
                self.copy.apply_rows(detail["items"])
                with self.copy.transaction():
                    self.copy.db.execute("DELETE FROM vlan_pending WHERE seq = ?", (seq,))
                sent += 1
                continue
            with self.copy.transaction():
                self.copy.db.execute("UPDATE vlan_pending SET state = ?, error = ? WHERE seq = ?",
                                     (REFUSED, detail, seq))
                self._restore(entry)
            refused += 1
        if results:
            self.team.online = True
        return sent, refused

    def _restore(self, entry):
        """Put the copy's VLAN back to the server's latest version (as of the last sync) after a refusal."""
        self.copy.db.execute("UPDATE vlans SET deleted = 1 WHERE domain_id = ? AND sort_key = ? AND deleted = 0",
                             (entry["domain_id"], entry["sort_key"]))
        if entry["original"]:
            self.copy.apply_rows([{"entity": "vlans", "row": json.loads(entry["original"])}])

    def flush(self):
        """Send every pending VLAN change now (blocking). Returns (sent, refused)."""
        return self.apply_sent(self.send_pending(self.outgoing()))
