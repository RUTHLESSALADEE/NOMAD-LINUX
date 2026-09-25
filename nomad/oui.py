"""MAC address vendor lookup from a copy of the IEEE OUI registry bundled with the app, so it works offline.

To update the bundled copy, download https://standards-oui.ieee.org/oui/oui.csv and run:

    python -m nomad.oui build oui.csv
"""
import csv
import gzip
import logging
import re
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "oui.txt.gz"
LOCALLY_ADMINISTERED_BIT = 0x02
MULTICAST_BIT = 0x01
RANDOMIZED_VENDOR = "Randomized (private) address"
HEX_ONLY = re.compile(r"[^0-9A-Fa-f]")

_vendors = None
_lock = threading.Lock()


def normalize_mac(mac):
    """Return a MAC address as 12 uppercase hex digits, or "" if it isn't one."""
    digits = HEX_ONLY.sub("", mac or "").upper()
    return digits if len(digits) == 12 else ""


def format_mac(mac):
    """Format a MAC address as AA-BB-CC-DD-EE-FF (the way Windows shows them), or "" if it isn't one."""
    digits = normalize_mac(mac)
    return "-".join(digits[i:i + 2] for i in range(0, 12, 2)) if digits else ""


def is_multicast_mac(mac):
    """Multicast and broadcast MACs (the low bit of the first byte is set)."""
    digits = normalize_mac(mac)
    return bool(digits) and bool(int(digits[:2], 16) & MULTICAST_BIT)


def is_randomized_mac(mac):
    """Locally administered unicast MACs, which phones and laptops use for Wi-Fi privacy."""
    digits = normalize_mac(mac)
    return bool(digits) and not is_multicast_mac(digits) and bool(int(digits[:2], 16) & LOCALLY_ADMINISTERED_BIT)


def parse_registry(lines):
    """Parse the bundled file's "PREFIX<tab>Vendor" lines into {prefix: vendor}."""
    vendors = {}
    for line in lines:
        prefix, _, vendor = line.rstrip("\r\n").partition("\t")
        if len(prefix) == 6 and vendor:
            vendors[prefix.upper()] = vendor
    return vendors


def _load():
    global _vendors
    with _lock:
        if _vendors is None:
            try:
                with gzip.open(DATA_FILE, "rt", encoding="utf-8") as file:
                    _vendors = parse_registry(file)
            except OSError as error:  # Missing or damaged: lookups just come back blank
                log.warning("Couldn't load the MAC vendor list %s: %s", DATA_FILE, error)
                _vendors = {}
    return _vendors


def vendor(mac, vendors=None):
    """The organization a MAC address is registered to, or "" if unknown."""
    digits = normalize_mac(mac)
    if not digits or is_multicast_mac(digits):
        return ""
    if is_randomized_mac(digits):
        return RANDOMIZED_VENDOR
    return (vendors if vendors is not None else _load()).get(digits[:6], "")


def build(csv_path, output=DATA_FILE):
    """Convert the IEEE oui.csv download into the compact file bundled with the app."""
    vendors = {}
    with open(csv_path, newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            prefix = (row.get("Assignment") or "").strip().upper()
            name = " ".join((row.get("Organization Name") or "").split())
            if row.get("Registry") == "MA-L" and len(prefix) == 6 and name:
                vendors[prefix] = name
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8", newline="\n") as file:
        for prefix in sorted(vendors):
            file.write(f"{prefix}\t{vendors[prefix]}\n")
    return len(vendors)


def main(args):
    if len(args) == 2 and args[0] == "build":
        count = build(args[1])
        print(f"Wrote {count} vendors to {DATA_FILE}")
        return 0
    print("Usage: python -m nomad.oui build <path to oui.csv>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
