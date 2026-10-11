"""The short changelog shown under Help > What's New, read from nomad/data/whats_new.md (bundled with the exe).

The file is laid out like CHANGELOG.md: a "## [1.2.3] - 2026-01-02" heading per version, newest first, each with a few
"- " lines, and an Unreleased section at the top that `python -m nomad.version bump` dates.
"""
import datetime
import html
import re
from pathlib import Path

WHATS_NEW = Path(__file__).parent / "data" / "whats_new.md"
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$")


def read_entries(path=WHATS_NEW):
    """[(version, date or None, [line, ...]), ...] newest first; an empty Unreleased section is left out."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_entries(text)


def parse_entries(text):
    entries = []
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            date = heading["date"]
            entries.append((heading["version"], datetime.date.fromisoformat(date) if date else None, []))
        elif entries and line.strip():
            if line.startswith(("- ", "* ")):
                entries[-1][2].append(line[2:].strip())
            elif entries[-1][2]:
                entries[-1][2][-1] += " " + line.strip()  # A line wrapped onto the next
            else:
                entries[-1][2].append(line.strip())
    return [entry for entry in entries if entry[2] or entry[0] != "Unreleased"]


def inline_html(text):
    """A line's text as HTML: escaped, with **bold** and `code`."""
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def entries_html(entries, current_version, colors):
    """The entries as one HTML page for a QTextBrowser, the version running marked."""
    if not entries:
        return f"<p style='color:{colors['muted']}'>No release notes were found.</p>"
    parts = []
    for version, date, lines in entries:
        when = f"{date:%B} {date.day}, {date.year}" if date else "Not released yet"
        this = (f" &nbsp;<span style='color:{colors['accent']}'>(this version)</span>"
                if version == current_version else "")
        items = "".join(f"<li style='margin-bottom:4px'>{inline_html(line)}</li>" for line in lines)
        parts.append(f"<h3 style='margin-top:14px; margin-bottom:2px'>{html.escape(version)}{this}</h3>"
                     f"<div style='color:{colors['muted']}'>{when}</div>"
                     f"<ul style='margin-top:4px'>{items}</ul>")
    return "".join(parts)
