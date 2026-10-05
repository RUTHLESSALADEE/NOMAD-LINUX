"""The tribe's subnet placement on a laptop: read from the copy of the server's data (so the page works offline),
changed through the server only (like subnets and networks, these change rarely and matter to everyone, so they wait
for the server rather than risk two people moving one subnet at once)."""
from .client import OldServerError, ServerUnreachable
from .roles import AUTO
from .placement import PlacementStore
from .store import parse_subnet

OLD_SERVER = "The IPAM server is running an older version of NOMAD that doesn't keep subnet placement: it needs " \
             "updating (Tools > Tribe Management > Update Service, on the server)."
OLD_SERVER_ROLES = "The IPAM server is running an older version of NOMAD that doesn't keep what subnets are for "                    "(their roles): it needs updating (Tools > Tribe Management > Update Service, on the server)."


class TeamPlacementStore:
    def __init__(self, team):
        self.team = team
        self.local = PlacementStore(team.copy)

    def __getattr__(self, name):
        if name in ("placements", "placement", "moves", "move", "open_move", "roles", "role"):
            return getattr(self.local, name)
        raise AttributeError(name)

    @property
    def can_change(self):
        return self.team.online and self.team.server_keeps_placement

    def _send(self, action, **arguments):
        if not self.team.server_keeps_placement:
            raise OldServerError(OLD_SERVER)
        try:
            reply = self.team.client.edit(action, **arguments)
        except ServerUnreachable as error:
            self.team.online, self.team.last_error = False, str(error)
            raise ServerUnreachable("The IPAM server can't be reached, so subnet placement and moves can't be "
                                    "changed right now.") from None
        self.team.online = True
        self.team.copy.apply_rows(reply["items"])
        return reply

    def set_placement(self, network_id, cidr, scope="auto", one_segment=False, note=""):
        current = self.local.placement(network_id, cidr)
        self._send("set_placement", network_id=network_id, cidr=str(parse_subnet(cidr)), scope=scope,
                   one_segment=bool(one_segment), note=note, expected_version=current.version if current else None)
        return self.local.placement(network_id, cidr)

    @property
    def can_change_roles(self):
        return self.can_change and self.team.server_keeps_roles

    def set_role(self, network_id, cidr, role=AUTO):
        current = self.local.role(network_id, cidr)
        if not self.team.server_keeps_roles:
            raise OldServerError(OLD_SERVER_ROLES)
        self._send("set_role", network_id=network_id, cidr=str(parse_subnet(cidr)), role=role,
                   expected_version=current.version if current else None)
        return self.local.role(network_id, cidr)

    def plan_move(self, network_id, cidr, **details):
        reply = self._send("plan_move", network_id=network_id, cidr=str(parse_subnet(cidr)), **details)
        created = [item["row"]["id"] for item in reply["items"] if item["entity"] == "subnet_moves"]
        return self.local.move(created[-1])

    def update_move(self, move_id, **changes):
        self._send("update_move", move_id=move_id, changes=changes, expected_version=self.local.move(move_id).version)
        return self.local.move(move_id)

    def complete_move(self, move_id):
        self._send("complete_move", move_id=move_id, expected_version=self.local.move(move_id).version)
        return self.local.move(move_id)
