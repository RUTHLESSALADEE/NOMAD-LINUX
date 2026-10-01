"""Excel-style column filters on a table."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from nomad.ui.common import SortableTableItem, read_only_table  # noqa: E402
from nomad.ui.table_filter import FilterPopup, TableFilter, natural_key  # noqa: E402

ROWS = [["sw1", "Switch", "10"], ["sw2", "Switch", "20"], ["rtr1", "Router", "10"], ["ap1", "Access point", ""],
        ["sw10", "Switch", "10"]]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def table(app):
    widget = read_only_table(["Name", "Kind", "VLAN"])
    widget.setRowCount(len(ROWS))
    for row, values in enumerate(ROWS):
        for column, text in enumerate(values):
            widget.setItem(row, column, SortableTableItem(text))
    return widget


def shown(table):
    return [table.item(row, 0).text() for row in range(table.rowCount()) if not table.isRowHidden(row)]


def test_filters_combine_and_values_follow_other_filters(table):
    counts = []
    table_filter = TableFilter(table, lambda visible, total: counts.append((visible, total)))
    table.setSortingEnabled(True)
    assert [table.item(row, 0).text() for row in range(5)] == ["ap1", "rtr1", "sw1", "sw10", "sw2"]  # A to Z
    table_filter.set_filter(1, {"Switch"})
    assert shown(table) == ["sw1", "sw10", "sw2"] and counts[-1] == (3, 5)
    assert table_filter.values(2) == {"10": 2, "20": 1}  # Only what the Kind filter lets through
    table_filter.set_filter(2, {"10"})
    assert shown(table) == ["sw1", "sw10"]
    assert table_filter.values(1) == {"Switch": 2, "Router": 1}
    table_filter.set_filter(1, None)
    assert shown(table) == ["rtr1", "sw1", "sw10"]
    table_filter.clear()
    assert len(shown(table)) == 5 and not table_filter.active


def test_filter_survives_sorting(table):
    table_filter = TableFilter(table)
    table_filter.set_filter(1, {"Switch"})
    table_filter.sort(0, Qt.DescendingOrder)
    assert shown(table) == ["sw2", "sw10", "sw1"]


def test_popup_ticks_search_and_select_all(table):
    table_filter = TableFilter(table)
    popup = FilterPopup(table_filter, 1, table)
    labels = [popup.list.item(row).text() for row in range(popup.list.count())]
    assert labels == ["(Select All)", "Access point  (1)", "Router  (1)", "Switch  (3)"]
    popup.search.setText("sw")  # Ticks just what matches
    popup.accept()
    assert shown(table) == ["sw1", "sw2", "sw10"]  # Not sorted: the table's in the order it was filled

    popup = FilterPopup(table_filter, 2, table)
    popup.all_item.setCheckState(Qt.Unchecked)
    router = next(item for item in popup.value_items() if item.data(Qt.UserRole) == "10")
    router.setCheckState(Qt.Checked)
    assert popup.all_item.checkState() == Qt.PartiallyChecked
    popup.accept()
    assert shown(table) == ["sw1", "sw10"]


def test_blanks_can_be_filtered(table):
    table_filter = TableFilter(table)
    popup = FilterPopup(table_filter, 2, table)
    assert popup.list.item(popup.list.count() - 1).text() == "(Blanks)  (1)"
    table_filter.set_filter(2, {""})
    assert shown(table) == ["ap1"]


def test_natural_order():
    assert sorted(["Gi1/0/10", "Gi1/0/2", "VLAN 100", "VLAN 20"], key=natural_key) == \
        ["Gi1/0/2", "Gi1/0/10", "VLAN 20", "VLAN 100"]


def test_filter_button_and_right_click_open_the_filter(table, app):
    from PyQt5.QtCore import QEvent, QPoint, QPointF
    from PyQt5.QtGui import QContextMenuEvent, QMouseEvent
    table_filter = TableFilter(table)
    table.resize(600, 300)
    table.show()
    app.processEvents()
    header = table_filter.header
    opened = []
    header.filter_requested.disconnect()
    header.filter_requested.connect(lambda column, position: opened.append(column))
    kind = header.section_rect(1)
    button = header.button_rect(kind)
    assert button.left() < kind.center().x()  # At the left of the column, clear of the sort arrow

    def click(point):
        header.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(point), Qt.LeftButton, Qt.LeftButton,
                                           Qt.NoModifier))
    click(button.center())
    assert opened == [1]
    assert header.button_at(QPoint(kind.right() - 5, kind.center().y())) == -1  # The rest of the header sorts
    header.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(kind.right() - 5, 5)))
    assert opened == [1, 1]
    table.hide()


def test_popup_puts_the_filter_first(table):
    table_filter = TableFilter(table)
    table_filter.set_filter(2, {"10"})
    popup = FilterPopup(table_filter, 1, table)
    texts = [action.text() for action in popup.actions() if action.text()]
    assert texts == ["Clear All Filters", "Sort A to Z", "Sort Z to A"]  # After the list of values
    assert popup.actions()[0].defaultWidget() is not None  # The search and the values come first
