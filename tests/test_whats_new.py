"""Help > What's New: the short changelog stays in step with CHANGELOG.md and shows in its window."""
import datetime
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from nomad import __version__
from nomad.version import CHANGELOG
from nomad.whats_new import WHATS_NEW, entries_html, inline_html, parse_entries, read_entries

COLORS = {"accent": "#0f0", "muted": "#888"}


def released_versions(text):
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})", text, re.MULTILINE)


def test_every_release_has_a_summary_with_the_same_date():
    changelog = released_versions(CHANGELOG.read_text(encoding="utf-8"))
    summary = released_versions(WHATS_NEW.read_text(encoding="utf-8"))
    assert summary == changelog


def test_every_summary_has_lines():
    entries = read_entries()
    assert entries[0][0] in (__version__, "Unreleased")
    assert all(lines for _, _, lines in entries)


def test_current_version_has_a_summary():
    assert __version__ in [version for version, _, _ in read_entries()]


def test_parse_entries():
    text = ("# What's New\n\nIntro.\n\n## [Unreleased]\n\n## [1.1.0] - 2026-02-03\n\n- **New:** a thing\n  wrapped\n"
            "- **Fixed:** another\n\n## [1.0.0] - 2026-01-01\n\n- First release.\n")
    assert parse_entries(text) == [
        ("1.1.0", datetime.date(2026, 2, 3), ["**New:** a thing wrapped", "**Fixed:** another"]),
        ("1.0.0", datetime.date(2026, 1, 1), ["First release."]),
    ]
    assert parse_entries("## [Unreleased]\n\n- Coming\n")[0] == ("Unreleased", None, ["Coming"])


def test_entries_html():
    assert inline_html("**New:** `show <x>` & more") == "<b>New:</b> <code>show &lt;x&gt;</code> &amp; more"
    page = entries_html([("1.1.0", datetime.date(2026, 2, 3), ["a"]), ("1.0.0", datetime.date(2026, 1, 1), ["b"])],
                        "1.1.0", COLORS)
    assert "February 3, 2026" in page and page.count("(this version)") == 1
    assert page.index("1.1.0") < page.index("1.0.0")
    assert "No release notes" in entries_html([], "1.1.0", COLORS)


def test_dialog_shows_the_notes():
    from nomad.ui.dialogs import WhatsNewDialog
    app = QApplication.instance() or QApplication([])
    dialog = WhatsNewDialog(None)
    text = dialog.notes.toPlainText()
    assert __version__ in text and "(this version)" in text
    dialog.deleteLater()
    app.processEvents()
