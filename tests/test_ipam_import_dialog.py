"""Import defaults and source preferences keep subnets from either workbook section."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from test_integration import app  # noqa: F401
from test_ipam import sample_page, store  # noqa: F401

from nomad.ipam.spreadsheet import DETAIL, SKIP, SUMMARY, import_page, parse_page
from nomad.ui.ipam_dialogs import CHOICE_COLUMN, ImportDialog


def test_only_one_source_defaults_to_import_and_conflicts_need_a_choice(app, store):
    sheet = parse_page("Page 1", sample_page())
    dialog = ImportDialog(None, store, "test.xlsx", [sheet])
    try:
        assert sheet.undecided()
        assert not dialog.import_button.isEnabled()
        for row, difference in enumerate(sheet.differences):
            combo = dialog.difference_table.cellWidget(row, CHOICE_COLUMN)
            assert combo.itemText(combo.findData(SKIP)) == "Skip this subnet"
            if difference.summary is None:
                assert combo.currentData() == DETAIL
                assert combo.findData(None) == -1
                assert combo.findData(SUMMARY) == -1
                assert combo.currentText() == "Import subnet from Detailed Info"
            elif difference.detail is None:
                assert combo.currentData() == SUMMARY
                assert combo.findData(None) == -1
                assert combo.findData(DETAIL) == -1
                assert combo.currentText() == "Import subnet from Summary"
            else:
                assert combo.currentData() is None
                assert combo.currentText() == "Choose..."

        dialog.choose_all(SUMMARY)
        assert dialog.import_button.isEnabled()
        skipped = sheet.differences[0]
        dialog.difference_table.cellWidget(0, CHOICE_COLUMN).setCurrentIndex(
            dialog.difference_table.cellWidget(0, CHOICE_COLUMN).findData(SKIP))
        dialog.show_page()
        assert skipped.choice == SKIP
        network = import_page(store, sheet, "Default import")
        assert {subnet.cidr for subnet in store.subnets(network.id)} == {
            subnet.cidr for subnet in sheet.matched
        } | {difference.cidr for difference in sheet.differences if difference is not skipped}
    finally:
        dialog.close()


@pytest.mark.parametrize("preference", [SUMMARY, DETAIL])
def test_bulk_source_preference_keeps_subnets_from_both_sources(app, store, preference):
    sheet = parse_page("Page 1", sample_page())
    dialog = ImportDialog(None, store, "test.xlsx", [sheet])
    try:
        dialog.choose_all(None)
        assert all(difference.choice is None for difference in sheet.differences
                   if difference.summary and difference.detail)
        dialog.choose_all(preference)
        assert all(difference.resolved() is not None for difference in sheet.differences)
        for difference in sheet.differences:
            expected = preference if difference.summary and difference.detail else (
                SUMMARY if difference.summary else DETAIL)
            assert difference.choice == expected
        network = import_page(store, sheet, "Bulk import")
        assert {subnet.cidr for subnet in store.subnets(network.id)} == {
            "10.0.0.0/29", "10.0.0.8/29", "10.0.1.0/24", "10.0.2.0/30", "10.0.3.0/30", "10.0.4.0/29"
        }
    finally:
        dialog.close()
