"""Importing an addressing spreadsheet (.xlsx or .csv) laid out like the team's LDIF workbooks.

Each page is one network:
    a header row (Network Name, Telephony Rng, Subnet, Mask, Gateway, Reserved, Assignment),
    a row naming the unit: unit | location | revision date | revision,
    a summary of the subnets, one per row,
    "Unit Base Info": ASN, UA, the unit's IP block, telephony (and SNMP strings, which are never imported),
    "Detailed Info": each subnet again, address by address (Y under Reserved; the host or use under Assignment),
    "End".
The summary and the detailed section can disagree; each disagreement is a Difference the user decides before
importing. Rows that can't be used are reported as problems rather than guessed at.

Loopbacks are listed with the mask 255.255.255.255, a row per address under their subnet's name; they're imported
as a loopback subnet (no network, broadcast or gateway address) covering the rows, named as the sheet names them.
"""
import csv
import datetime
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

from .store import RESERVED, USED, IpamError, parse_subnet

SUMMARY, DETAIL, SKIP = "summary", "detail", "skip"
NAME, SUBNET, MASK, GATEWAY, RESERVED_COLUMN, ASSIGNMENT = "name", "subnet", "mask", "gateway", "reserved", "assignment"
HEADERS = {"network name": NAME, "subnet": SUBNET, "mask": MASK, "gateway": GATEWAY, "reserved": RESERVED_COLUMN,
           "assignment": ASSIGNMENT}
UNIT_FIELDS = ["Unit", "Location", "Revision date", "Revision"]  # The row after the header, in order
SECRET_LABEL_ENDINGS = ("string", "community", "password")  # Base-info labels whose values are never imported
IMPLIED_ASSIGNMENTS = {"network", "broadcast"}
RESERVED_MARKS = ("y", "yes", "x", "true")  # Marker rows the subnet itself stands for


class SpreadsheetError(Exception):
    pass


def cell_text(value):
    """A cell's value as text: whole numbers without ".0", dates as 2025-04-25."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime.datetime):
        return value.date().isoformat() if value.time() == datetime.time() else value.isoformat(sep=" ")
    return " ".join(str(value).split())


def read_pages(path):
    """[(page title, rows)] from an .xlsx workbook (every page) or a .csv file (one page); rows are lists of text."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as file:
            return [(path.stem, [[cell_text(value) for value in row] for row in csv.reader(file)])]
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise SpreadsheetError("Open an .xlsx workbook or a .csv file (for an old .xls workbook, save it as .xlsx "
                               "in Excel first).")
    import openpyxl  # Only needed here, so NOMAD starts without loading it
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as error:  # openpyxl raises many kinds for damaged or protected files
        raise SpreadsheetError(f"Couldn't read {path.name}: {error}") from error
    try:
        return [(sheet.title, [[cell_text(value) for value in row] for row in sheet.iter_rows(values_only=True)])
                for sheet in workbook.worksheets]
    finally:
        workbook.close()


@dataclass
class SheetSubnet:
    row: int
    cidr: str
    name: str = ""
    gateway: str = ""
    description: str = ""
    fields: dict = field(default_factory=dict)
    written: str = ""  # As the sheet has it, when that isn't the subnet's own CIDR (172.28.101.0/16)
    loopbacks: bool = False  # Listed as host routes (255.255.255.255): a pool of loopback addresses

    @property
    def network(self):
        return parse_subnet(self.cidr)


@dataclass
class SheetAddress:
    row: int
    ip: str
    status: str
    name: str = ""


def _same(first, second):
    return first.strip().casefold() == second.strip().casefold()


@dataclass
class Difference:
    """A subnet the summary and the detailed section disagree about, or that only one of them has."""
    cidr: str
    summary: SheetSubnet = None
    detail: SheetSubnet = None
    choice: str = None  # SUMMARY, DETAIL or SKIP; None until the user decides

    @property
    def network(self):
        return parse_subnet(self.cidr)

    def options(self):
        """[(choice, label)] the user picks from."""
        if self.summary is None:
            return [(DETAIL, "Add it"), (SKIP, "Leave it out")]
        if self.detail is None:
            return [(SUMMARY, "Add it"), (SKIP, "Leave it out")]
        return [(SUMMARY, "Use the summary's"), (DETAIL, "Use the detailed info's"), (SKIP, "Leave it out")]

    def describe(self):
        if self.summary is None:
            return f"Only in the detailed info (row {self.detail.row})"
        if self.detail is None:
            return f"Only in the summary (row {self.summary.row})"
        parts = []
        for label, attribute in (("name", "name"), ("gateway", "gateway")):
            first, second = getattr(self.summary, attribute), getattr(self.detail, attribute)
            if not _same(first, second):
                parts.append(f"{label} {first or '(none)'!r} vs {second or '(none)'!r}")
        for name in sorted(set(self.summary.fields) | set(self.detail.fields)):
            first, second = self.summary.fields.get(name, ""), self.detail.fields.get(name, "")
            if first and second and not _same(first, second):
                parts.append(f"{name} {first!r} vs {second!r}")
        return f"Rows {self.summary.row} and {self.detail.row} differ: " + "; ".join(parts)

    def resolved(self):
        """The subnet to import (blanks filled from the other version), or None to leave it out."""
        if self.choice in (None, SKIP):
            return None
        return merged(self.summary if self.choice == SUMMARY else self.detail,
                      self.detail if self.choice == SUMMARY else self.summary)


@dataclass
class GatewayFix:
    """A gateway the sheet gives that isn't in its subnet, with the one to use instead (which the user can change).

    The chosen gateway is kept on the subnet itself, so it's what gets compared and imported.
    """
    subnet: SheetSubnet
    section: str  # "Summary" or "Detailed Info"
    given: str
    reason: str = ""  # Why the suggested gateway was chosen, in words
    suggested: str = ""

    @property
    def gateway(self):
        return self.subnet.gateway

    def usable_range(self):
        """"10.0.0.1 to 10.0.0.254": the addresses a gateway can be (not the network or broadcast address)."""
        block = self.subnet.network
        if block.num_addresses <= 2:
            return f"{block.first} to {block.last}"
        return f"{_offset(block.first, 1)} to {_offset(block.last, -1)}"

    @property
    def problem(self):
        """What's wrong with the sheet's gateway, in words."""
        try:
            given = ipaddress.ip_address(self.given)
        except ValueError:
            return f"{self.given!r} isn't a valid IP address."
        if given.version != self.subnet.network.version:
            return f"{given} is IPv{given.version} but {self.subnet.cidr} is IPv{self.subnet.network.version}."
        return f"{given} is outside {self.subnet.cidr}, which only holds {self.usable_range()}."

    def set_gateway(self, text):
        """Use this gateway ("" to leave it out). Returns an error message, or None if it's fine."""
        text = text.strip()
        if not text:
            self.subnet.gateway = ""
            return None
        block = self.subnet.network
        advice = f"Use an address from {self.usable_range()}, or clear it to leave the gateway out."
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return f"{text!r} isn't an IP address. {advice}"
        if address not in block:
            return f"{address} is outside {self.subnet.cidr}. {advice}"
        if block.num_addresses > 2 and address in (block.first, block.last):
            which = "network" if address == block.first else "broadcast"
            return f"{address} is {self.subnet.cidr}'s {which} address, which can't be a gateway. {advice}"
        self.subnet.gateway = str(address)
        return None

    def suggest(self, counterpart=None):
        """Put the likeliest gateway on the subnet (see _choose), remembering it as the suggestion."""
        self._choose(counterpart)
        self.suggested = self.subnet.gateway

    def _choose(self, counterpart):
        """Choose the likeliest gateway: the other section's, if it's in the subnet; else the given one's host part
        in this subnet (fixing a typo in the network part, like 144.38.206.126 for 148.38.206.96/27); else the last
        usable address, where the team's gateways usually are. `reason` says which, in words."""
        block = self.subnet.network
        other_section = "Detailed Info" if self.section == "Summary" else "summary"
        if counterpart is not None and counterpart.gateway:
            try:
                if ipaddress.ip_address(counterpart.gateway) in block:
                    self.subnet.gateway = counterpart.gateway
                    self.reason = f"The {other_section} gives {counterpart.gateway} as this subnet's gateway " \
                                  f"(row {counterpart.row})."
                    return
            except ValueError:
                pass
        usable = block.num_addresses <= 2
        why_not_host_part = "the sheet's value isn't an IP address"
        try:
            given = ipaddress.ip_address(self.given)
            why_not_host_part = f"{given} is a different kind of address"
            if given.version == block.version:
                size = 1 << (block.first.max_prefixlen - block.prefixlen)
                candidate = _offset(block.first, (int(given) - int(block.first)) % size)
                if candidate in block and (usable or candidate not in (block.first, block.last)):
                    self.subnet.gateway = str(candidate)
                    self.reason = f"Keeps the end of {given} but puts it inside this subnet, fixing what looks " \
                                  "like a typo in the first part."
                    return
                which = "network" if candidate == block.first else "broadcast"
                why_not_host_part = f"keeping the end of {given} would give the subnet's {which} address, " \
                                    f"{candidate}"
        except ValueError:
            pass
        last_usable = block.last if usable else _offset(block.last, -1)
        self.subnet.gateway = str(last_usable)
        self.reason = f"The subnet's last usable address, where gateways usually are ({why_not_host_part})."


def _offset(address, delta):
    value = int(address) + delta
    return ipaddress.IPv4Address(value) if address.version == 4 else ipaddress.IPv6Address(value)


def merged(chosen, other):
    if other is None:
        return chosen
    return SheetSubnet(chosen.row, chosen.cidr, chosen.name or other.name, chosen.gateway or other.gateway,
                       chosen.description or other.description, dict(other.fields, **{
                           name: value for name, value in chosen.fields.items() if value}), chosen.written,
                       chosen.loopbacks or other.loopbacks)


@dataclass
class SheetImport:
    """What one page holds, ready to import once every difference has a choice."""
    title: str
    fields: dict = field(default_factory=dict)  # The network's details (unit, location, revision, ASN...)
    summary: list = field(default_factory=list)  # [SheetSubnet]
    detail: list = field(default_factory=list)  # [SheetSubnet]
    addresses: list = field(default_factory=list)  # [SheetAddress]
    problems: list = field(default_factory=list)  # [(row, message)]
    agreed: list = field(default_factory=list)  # [(SheetSubnet, other section's or None)] the sections agree on
    differences: list = field(default_factory=list)  # [Difference]
    gateway_fixes: list = field(default_factory=list)  # [GatewayFix]
    has_detail: bool = False  # Whether the page has a Detailed Info section to compare the summary with

    @property
    def suggested_name(self):
        """The unit and page, such as "11AB SIPR Template" (every page of a unit's workbook has the same unit)."""
        unit = self.fields.get("Unit", "")
        return f"{unit} {self.title}" if unit and unit.casefold() not in self.title.casefold() else self.title

    @property
    def matched(self):
        """The subnets both sections agree on (or all of them, on a page without Detailed Info)."""
        return [merged(subnet, other) for subnet, other in self.agreed]

    def undecided(self):
        return [difference for difference in self.differences if difference.choice is None]

    def subnets_to_import(self):
        chosen = [difference.resolved() for difference in self.differences]
        return sorted(self.matched + [subnet for subnet in chosen if subnet is not None],
                      key=lambda subnet: subnet.network.sort_key)

    def problem(self, row, message):
        self.problems.append((row, message))


def _find_header(rows):
    """(row index, {key: column}, {column: header} for other columns, enclave label) of the header row.

    The name column is headed "Network Name", or (in the team's workbooks) with the page's enclave, such as SIPR,
    CX or Colorless: then it's the first heading left of Subnet.
    """
    for index, row in enumerate(rows):
        columns = {}
        for column, value in enumerate(row):
            key = HEADERS.get(value.casefold())
            if key and key not in columns:
                columns[key] = column
        if not {SUBNET, MASK} <= set(columns):
            continue
        enclave = ""
        if NAME not in columns:
            before = [column for column in range(columns[SUBNET]) if row[column] and column not in columns.values()]
            if not before:
                continue
            columns[NAME] = before[0]
            enclave = row[before[0]]
        extra = {column: value for column, value in enumerate(row) if value and column not in columns.values()}
        return index, columns, extra, enclave
    return None, None, None, None


def _extra_fields(row, extra_columns):
    """Columns beyond the known ones (such as Telephony Rng), kept as the subnet's own fields."""
    return {header: _cell(row, column) for column, header in extra_columns.items() if _cell(row, column)}


def _cell(row, column):
    return row[column] if column is not None and column < len(row) else ""


def _written(address, network):
    """The address and prefix as the sheet has them, when that isn't the subnet's own CIDR (else "")."""
    address = address.split("/")[0].strip()
    return "" if address == str(network.first) else f"{address}/{network.prefixlen}"


def _network(address, mask):
    """The subnet from an address and a mask (255.255.255.0, /24 or 24), or CIDR in the address cell (then the mask,
    if given, must agree); raises IpamError."""
    address, mask = address.strip(), mask.strip().lstrip("/")
    if "/" in address:
        network = parse_subnet(address)
        try:
            given = ipaddress.ip_network(f"{network.network_address}/{mask}", strict=False).prefixlen if mask else None
        except ValueError:
            raise IpamError(f"{mask!r} isn't a mask") from None
        if given is not None and given != network.prefixlen:
            raise IpamError(f"{address} doesn't match the mask {mask}")
        return network
    if not address:
        raise IpamError("no subnet address")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise IpamError(f"{address!r} isn't an IP address") from None
    if not mask:
        raise IpamError(f"{address} has no mask")
    return parse_subnet(f"{address}/{mask}")


def _add_subnet(sheet, section, subnet, gateway):
    """Add a subnet from the summary or the Detailed Info; a gateway outside it becomes a GatewayFix."""
    if gateway:
        try:
            address = ipaddress.ip_address(gateway)
            if address not in subnet.network:
                raise ValueError
            subnet.gateway = str(address)
        except ValueError:
            sheet.gateway_fixes.append(GatewayFix(subnet, section, gateway))
    (sheet.summary if section == "Summary" else sheet.detail).append(subnet)


def _host_in_listed_subnet(sheet, row, columns):
    """The row's address if it's a host (not the first address) inside a subnet already listed on the page."""
    try:
        address = ipaddress.ip_address(_cell(row, columns[SUBNET]))
        network = ipaddress.ip_network(f"{address}/{_cell(row, columns[MASK]).lstrip('/')}", strict=False)
    except ValueError:
        return None
    if address == network.network_address:
        return None
    return address if any(subnet.cidr == str(network) for subnet in sheet.summary) else None


def _marker(text):
    return text.casefold().rstrip(":")


def parse_page(title, rows):
    """Read one page into a SheetImport. Raises SpreadsheetError if it doesn't look like an addressing page."""
    header_index, columns, extra_columns, enclave = _find_header(rows)
    if header_index is None:
        raise SpreadsheetError(f"{title}: no header row with Subnet and Mask columns.")
    sheet = SheetImport(title)
    if enclave:
        sheet.fields["Enclave"] = enclave
    sheet.has_detail = any(_marker(_cell(row, columns[NAME])) == "detailed info" for row in rows[header_index + 1:])
    name_column = columns[NAME]
    state = "summary"
    unit_row_seen = False
    pending_label = None
    group = None  # [header row number, header row, [address rows]] while reading the detailed section

    for index in range(header_index + 1, len(rows)):
        row, number = rows[index], index + 1
        if not any(row):
            continue
        name = _cell(row, name_column)
        marker = _marker(name)
        if marker == "unit base info":
            state = "base"
            continue
        if marker == "detailed info":
            state = "detail"
            continue
        if state == "detail" and marker == "end":
            break

        if state == "summary":
            if not unit_row_seen and not _cell(row, columns[MASK]):
                unit_row_seen = True
                values = [value for value in row if value]
                sheet.fields.update({label: value for label, value in zip(UNIT_FIELDS, values)})
                continue
            unit_row_seen = True
            # A row for a device (or interface) in a subnet already listed: a host list, like HSMC XLESS
            host = _host_in_listed_subnet(sheet, row, columns)
            if host is not None:
                reserved = _cell(row, columns.get(RESERVED_COLUMN)).casefold() in RESERVED_MARKS
                assignment = _cell(row, columns.get(ASSIGNMENT))
                if assignment.casefold() not in IMPLIED_ASSIGNMENTS:
                    sheet.addresses.append(SheetAddress(number, str(host), RESERVED if reserved else USED,
                                                        assignment or name))
                continue
            try:
                network = _network(_cell(row, columns[SUBNET]), _cell(row, columns[MASK]))
            except IpamError as error:
                sheet.problem(number, f"Summary {name or '(no name)'}: {str(error).rstrip('.')}, so it can't be imported.")
                continue
            loopbacks = network.num_addresses == 1
            _add_subnet(sheet, "Summary", SheetSubnet(number, str(network), name, "",
                                                      _cell(row, columns.get(ASSIGNMENT)),
                                                      _extra_fields(row, extra_columns),
                                                      _written(_cell(row, columns[SUBNET]), network), loopbacks),
                        "" if loopbacks else _cell(row, columns.get(GATEWAY)))

        elif state == "base":
            values = [value for value in row if value]
            if pending_label and len(values) == 1:
                sheet.fields[pending_label] = values[0]
                pending_label = None
                continue
            pending_label = None
            position = 0
            while position < len(values):
                label = values[position].rstrip(":")
                if label.casefold().endswith(SECRET_LABEL_ENDINGS):
                    position += 2  # Skip the label and its value: secrets aren't stored in IPAM
                    continue
                if position + 1 < len(values):
                    sheet.fields[label] = values[position + 1]
                else:
                    pending_label = label  # "Unit IP" with its value on the next row
                position += 2

        else:  # Detailed section: a named row starts a subnet; unnamed rows below it are its addresses
            if name:
                _finish_group(sheet, group, columns, extra_columns)
                group = [number, row, []]
            if group is None:
                sheet.problem(number, "Address row before any subnet in the detailed info; skipped.")
                continue
            group[2].append((number, row))
    _finish_group(sheet, group, columns, extra_columns)
    _find_differences(sheet)
    return sheet


def _finish_group(sheet, group, columns, extra_columns):
    if group is None:
        return
    header_number, header, address_rows = group
    name = _cell(header, columns[NAME])
    addresses, bad_rows = [], []
    gateway = ""
    for number, row in address_rows:
        text = _cell(row, columns[SUBNET])
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            if text or _cell(row, columns.get(ASSIGNMENT)):
                bad_rows.append((number, text))
            continue
        addresses.append((number, address, row))
        gateway = gateway or _cell(row, columns.get(GATEWAY))
    bad_text = ""
    if len(bad_rows) == 1:
        bad_text = f"row {bad_rows[0][0]} ({bad_rows[0][1] or '(blank)'!r}) isn't an IP address and was skipped"
    elif bad_rows:
        bad_text = (f"{len(bad_rows)} rows from {bad_rows[0][0]} to {bad_rows[-1][0]} aren't IP addresses "
                    f"(such as {bad_rows[0][1] or '(blank)'!r}) and were skipped")

    network = None
    try:
        network = _network(_cell(header, columns[SUBNET]), _cell(header, columns[MASK]))
    except IpamError as error:
        parts = [f"{str(error).rstrip('.')}, so the subnet isn't imported"]
        if addresses:
            parts.append("its addresses are")
        if bad_text:
            parts.append(bad_text)
        sheet.problem(header_number, f"Detailed {name}: " + "; ".join(parts) + ".")
    else:
        if bad_text:
            sheet.problem(bad_rows[0][0], f"{name}: {bad_text}.")
    if network is not None and network.num_addresses == 1:
        # Loopbacks are listed as host routes (/32 each); the subnet is the range the rows cover, split into as
        # few blocks as it takes when the range isn't one (each keeps the sheet's name)
        low = min([address for _, address, _ in addresses] + [network.first])
        high = max([address for _, address, _ in addresses] + [network.first])
        for block in ipaddress.summarize_address_range(low, high):
            block = parse_subnet(str(block))
            _add_subnet(sheet, "Detailed Info", SheetSubnet(header_number, str(block), name,
                                                            fields=_extra_fields(header, extra_columns),
                                                            loopbacks=True), "")
    elif network is not None:
        _add_subnet(sheet, "Detailed Info", SheetSubnet(header_number, str(network), name,
                                                        fields=_extra_fields(header, extra_columns),
                                                        written=_written(_cell(header, columns[SUBNET]), network)),
                    gateway)

    for number, address, row in addresses:
        assignment = _cell(row, columns.get(ASSIGNMENT))
        reserved = _cell(row, columns.get(RESERVED_COLUMN)).casefold() in RESERVED_MARKS
        if assignment.casefold() in IMPLIED_ASSIGNMENTS or not (reserved or assignment):
            continue
        sheet.addresses.append(SheetAddress(number, str(address), RESERVED if reserved else USED, assignment))


def _find_differences(sheet):
    by_cidr_summary, by_cidr_detail = {}, {}
    for section, subnets, by_cidr in (("Summary", sheet.summary, by_cidr_summary),
                                      ("Detailed", sheet.detail, by_cidr_detail)):
        for subnet in subnets:
            first = by_cidr.get(subnet.cidr)
            if first is None:
                by_cidr[subnet.cidr] = subnet
            elif (subnet.written or subnet.cidr) == (first.written or first.cidr):
                sheet.problem(subnet.row, f"{section} {subnet.cidr} is listed again (first on row {first.row}); "
                                          "only the first is used.")
            else:
                sheet.problem(subnet.row, f"{section} {subnet.written or subnet.cidr} ({subnet.name or 'no name'}) "
                                          f"is in the same subnet, {subnet.cidr}, as row {first.row} "
                                          f"({first.written or first.cidr}, {first.name or 'no name'}); it's "
                                          "imported once, as row " f"{first.row}. Its addresses are all imported.")
    sheet.summary = [subnet for subnet in sheet.summary if by_cidr_summary.get(subnet.cidr) is subnet]
    sheet.detail = [subnet for subnet in sheet.detail if by_cidr_detail.get(subnet.cidr) is subnet]

    seen = {}
    unique = []
    for address in sheet.addresses:
        if address.ip in seen:
            sheet.problem(address.row, f"{address.ip} is also on row {seen[address.ip].row}; only that row is used.")
            continue
        seen[address.ip] = address
        unique.append(address)
    sheet.addresses = unique

    for fix in sheet.gateway_fixes:
        counterparts = by_cidr_detail if fix.section == "Summary" else by_cidr_summary
        counterpart = counterparts.get(fix.subnet.cidr)
        fix.suggest(counterpart if counterpart is not None and
                    not any(other.subnet is counterpart for other in sheet.gateway_fixes) else None)

    for cidr in sorted(set(by_cidr_summary) | set(by_cidr_detail), key=lambda text: parse_subnet(text).sort_key):
        summary, detail = by_cidr_summary.get(cidr), by_cidr_detail.get(cidr)
        if not sheet.has_detail:  # Nothing to disagree with
            sheet.agreed.append((summary, None))
        elif summary and detail and _agree(summary, detail):
            sheet.agreed.append((detail, summary))
        else:
            sheet.differences.append(Difference(cidr, summary, detail))
    sheet.problems.sort()


def _agree(summary, detail):
    if not _same(summary.name, detail.name):
        return False
    if summary.gateway and detail.gateway and not _same(summary.gateway, detail.gateway):
        return False
    return all(_same(value, detail.fields[name]) for name, value in summary.fields.items()
               if value and detail.fields.get(name))


def import_plan(sheet, network_name, replace=False):
    """What importing a page creates, as plain data (so it can be sent to the NOMAD server): the network's name,
    details, subnets and addresses. Every difference must have a choice first."""
    if sheet.undecided():
        raise SpreadsheetError(f"{sheet.title}: {len(sheet.undecided())} differences still need a choice.")
    return {
        "name": network_name.strip(),
        "replace": replace,
        "fields": dict(sheet.fields, **{"Imported from": sheet.title}),
        # A loopback subnet has no gateway, even if the summary gives the same range one
        "subnets": [{"cidr": subnet.cidr, "name": subnet.name, "gateway": "" if subnet.loopbacks else subnet.gateway,
                     "description": subnet.description, "fields": subnet.fields, "loopbacks": subnet.loopbacks}
                    for subnet in sheet.subnets_to_import()],
        "addresses": [{"ip": address.ip, "status": address.status, "name": address.name}
                      for address in sheet.addresses],
    }


def import_page(store, sheet, network_name, replace=False):
    """Create the network (or, with replace, empty the existing one of that name) and fill it from the page.

    Returns the network. Every difference must have a choice first.
    """
    return store.import_networks([import_plan(sheet, network_name, replace)])[0]
