"""Tool navigation preserves pages while search and favorites change."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QWidget
from nomad.ui.navigation import Navigator

@pytest.fixture
def nav():
    app = QApplication.instance() or QApplication([])
    navigation = Navigator()
    navigation.resize(1000, 700)
    navigation.add_section("This Computer")
    navigation.add_page(QWidget(), "Interfaces")
    navigation.add_section("Diagnostics")
    navigation.add_page(QWidget(), "Ping")
    navigation.add_page(QWidget(), "iperf")
    navigation.show()
    app.processEvents()
    yield navigation
    navigation.close()
    navigation.deleteLater()
    app.processEvents()

def test_search_alias_and_activation(nav):
    original = nav.currentWidget()
    nav.open_search()
    nav.search.setText("bandwidth")
    assert [nav.sidebar.item(row).text() for row in nav.page_rows()
            if not nav.sidebar.item(row).isHidden()] == ["iperf"]
    nav.activate_first()
    assert nav.title(nav.currentWidget()) == "iperf"
    assert not nav.panel.isVisible()
    nav.activate_title("Interfaces")
    assert nav.currentWidget() is original

def test_favorites_settings_and_empty_list(nav, tmp_path):
    settings = QSettings(str(tmp_path / "navigation.ini"), QSettings.IniFormat)
    nav.favorites = ["Ping", "Interfaces"]
    nav.save_settings(settings)
    nav.favorites = []
    nav.restore_settings(settings)
    assert nav.favorites == ["Ping", "Interfaces"]
    assert nav.favorite_list.item(0).data(256) == "Ping"
    nav.toggle_favorite("Ping")
    nav.toggle_favorite("Interfaces")
    nav.save_settings(settings)
    nav.restore_settings(settings)
    assert nav.favorites == []

def test_focus_mode_and_pinned_drawer(nav):
    nav.set_sidebar_visible(True)
    nav.activate_title("Ping")
    assert nav.panel.isVisible()
    assert nav.content.layout().contentsMargins().left() == 360
    nav.set_navigation_hidden(True)
    assert not nav.panel.isVisible() and not nav.rail.isVisible()
    nav.set_navigation_hidden(False)
    assert nav.panel.isVisible() and nav.rail.isVisible()
    nav.dismiss_drawer()
    assert nav.content.layout().contentsMargins().left() == 0

def test_keyboard_cycle_does_not_follow_search_filter(nav):
    nav.search.setText("Ping")
    nav.step(1)
    assert nav.title(nav.currentWidget()) == "Ping"
    nav.step(1)
    assert nav.title(nav.currentWidget()) == "iperf"


def test_category_collapse_does_not_hide_search_results(nav):
    heading = nav.sidebar.item(0)
    nav.activate_item(heading)
    assert nav.sidebar.item(1).isHidden()
    nav.search.setText("adapter")
    assert not nav.sidebar.item(1).isHidden()
    nav.search.clear()
    assert nav.sidebar.item(1).isHidden()

def test_drag_order_persists(nav, tmp_path):
    nav.favorites = ["Interfaces", "Ping"]
    nav.rebuild_rail()
    model = nav.favorite_list.model()
    from PyQt5.QtCore import QModelIndex
    assert model.moveRows(QModelIndex(), 0, 1, QModelIndex(), 2)
    assert nav.favorites == ["Ping", "Interfaces"]
    settings = QSettings(str(tmp_path / "order.ini"), QSettings.IniFormat)
    nav.save_settings(settings)
    nav.restore_settings(settings)
    assert nav.favorites == ["Ping", "Interfaces"]

def test_outside_click_closes_drawer(nav):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    nav.open_drawer()
    QTest.mouseClick(nav.currentWidget(), Qt.LeftButton)
    assert not nav.panel.isVisible()
