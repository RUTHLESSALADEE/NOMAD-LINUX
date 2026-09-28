"""Sidebar navigation: pages grouped under section headings, with the selected page shown beside them.

The sidebar can be hidden to give the pages more room; a slim strip then offers a menu of every page and a button
to bring the sidebar back.
"""
from PyQt5.QtCore import QEvent, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPen
from PyQt5.QtWidgets import QAbstractItemView, QHBoxLayout, QListWidget, QListWidgetItem, QMenu, QShortcut, \
    QStackedWidget, QStyle, QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget

from .theme import COLORS

PAGE_ROLE = Qt.UserRole
SECTION_ROLE = Qt.UserRole + 1  # A heading's name as written, before it is shown in capitals
ITEM_PADDING = 22 + 18 + 3 + 8  # Left and right padding and the selection bar (theme.py), plus breathing room
HEADER_PADDING = 12  # Left indent of a heading's text
HEADER_GAP = 10  # Space above each heading after the first, separating the sections
HEADER_MARGIN = 6  # Space above and below a heading's text inside its band


def header_font(base):
    """Headings: bold, slightly smaller capitals with a little letter spacing. Built from the list's own font, so
    they follow View > Text Size."""
    font = QFont(base)
    font.setBold(True)
    font.setPointSizeF(base.pointSizeF() * 0.9)
    font.setLetterSpacing(QFont.PercentageSpacing, 110)
    return font


def is_header(index):
    return index.data(PAGE_ROLE) is None


class NavigationDelegate(QStyledItemDelegate):
    """Draws section headings as shaded bands with a divider above; pages are drawn normally (styled by theme.py)."""

    def paint(self, painter, option, index):
        if not is_header(index):
            super().paint(painter, option, index)
            return
        painter.save()
        gap = HEADER_GAP if index.row() > 0 else 0
        band = QRect(option.rect.left(), option.rect.top() + gap, option.rect.width(), option.rect.height() - gap)
        painter.fillRect(band, QColor(COLORS["panel_alt"]))
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.drawLine(band.topLeft(), band.topRight())
        painter.drawLine(band.bottomLeft(), band.bottomRight())
        painter.setFont(header_font(option.font))
        painter.setPen(QColor(COLORS["accent"]))
        text_rect = band.adjusted(HEADER_PADDING, 0, -4, 0)
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, index.data(Qt.DisplayRole))
        painter.restore()

    def sizeHint(self, option, index):
        if not is_header(index):
            return super().sizeHint(option, index)
        metrics = QFontMetrics(header_font(option.font))
        gap = HEADER_GAP if index.row() > 0 else 0
        return QSize(metrics.horizontalAdvance(index.data(Qt.DisplayRole)) + HEADER_PADDING + 8,
                     metrics.height() + 2 * HEADER_MARGIN + gap)


def navigation_button(text, tooltip):
    button = QToolButton()
    button.setObjectName("navigationButton")
    button.setText(text)
    button.setToolTip(tooltip)
    button.setAutoRaise(True)
    return button


class Navigator(QWidget):
    """A sidebar of pages under section headings. Offers the parts of QTabWidget's interface the app uses."""
    currentChanged = pyqtSignal(int)  # Index of the page in the stack
    sidebarToggled = pyqtSignal(bool)  # True when the sidebar is shown

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Shown while the sidebar is hidden: a menu of every page, and a button to bring the sidebar back
        self.rail = QWidget(self)
        self.rail.setObjectName("navigationRail")
        rail_layout = QVBoxLayout(self.rail)
        rail_layout.setContentsMargins(0, 2, 0, 2)
        rail_layout.setSpacing(2)
        self.pages_button = navigation_button("≡", "Go to a page")
        self.pages_menu = QMenu(self.pages_button)
        self.pages_menu.aboutToShow.connect(self.fill_pages_menu)
        self.pages_button.setMenu(self.pages_menu)
        self.pages_button.setPopupMode(QToolButton.InstantPopup)
        self.show_button = navigation_button("»", "Show the sidebar (Ctrl+B)")
        self.show_button.clicked.connect(lambda: self.set_sidebar_visible(True))
        rail_layout.addWidget(self.pages_button)
        rail_layout.addWidget(self.show_button)
        rail_layout.addStretch()
        self.rail.setVisible(False)

        self.panel = QWidget(self)
        self.panel.setObjectName("navigationPanel")
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 2, 2, 0)
        top_row.addStretch()
        self.hide_button = navigation_button("«", "Hide the sidebar for more room (Ctrl+B)")
        self.hide_button.clicked.connect(lambda: self.set_sidebar_visible(False))
        top_row.addWidget(self.hide_button)
        panel_layout.addLayout(top_row)

        self.sidebar = QListWidget(self.panel)
        self.sidebar.setObjectName("navigation")
        self.sidebar.setItemDelegate(NavigationDelegate(self.sidebar))
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.sidebar.setSelectionMode(QAbstractItemView.SingleSelection)
        self.sidebar.installEventFilter(self)  # Resize to fit the names when the text size changes
        panel_layout.addWidget(self.sidebar, 1)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("pages")
        layout.addWidget(self.rail)
        layout.addWidget(self.panel)
        layout.addWidget(self.stack, 1)
        self.sidebar.currentRowChanged.connect(self._on_row_changed)

        for keys, step in ((("Ctrl+Tab", "Ctrl+PgDown"), 1), (("Ctrl+Shift+Tab", "Ctrl+PgUp"), -1)):
            for key in keys:
                shortcut = QShortcut(QKeySequence(key), self)
                shortcut.setContext(Qt.WindowShortcut)
                shortcut.activated.connect(lambda step=step: self.step(step))

    def add_section(self, title):
        item = QListWidgetItem(title.upper())
        item.setFlags(Qt.NoItemFlags)  # A heading: can't be selected, and arrow keys skip it
        item.setData(PAGE_ROLE, None)
        item.setData(SECTION_ROLE, title)
        self.sidebar.addItem(item)
        self.update_width()

    def add_page(self, widget, title):
        self.stack.addWidget(widget)
        item = QListWidgetItem(title)
        item.setData(PAGE_ROLE, widget)
        self.sidebar.addItem(item)
        if self.sidebar.currentRow() < 0:
            self.sidebar.setCurrentItem(item)
        self.update_width()

    def update_width(self):
        """As wide as the longest name at the current text size, plus a scrollbar only if the list needs one."""
        metrics = self.sidebar.fontMetrics()
        headers = QFontMetrics(header_font(self.sidebar.font()))
        widths = [headers.horizontalAdvance(item.text()) + HEADER_PADDING + 8 if item.data(PAGE_ROLE) is None
                  else metrics.horizontalAdvance(item.text()) + ITEM_PADDING
                  for item in (self.sidebar.item(row) for row in range(self.sidebar.count()))]
        width = max(widths, default=0)
        content_height = sum(max(self.sidebar.sizeHintForRow(row), metrics.height())
                             for row in range(self.sidebar.count()))
        if content_height > self.sidebar.viewport().height() > 0:
            width += self.sidebar.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        width += 2 * self.sidebar.frameWidth()
        if width != self.sidebar.width():
            self.sidebar.setFixedWidth(width)

    def eventFilter(self, watched, event):
        if watched is self.sidebar and event.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.Resize):
            self.update_width()
        return False

    # ----------------------------------------------------------------- Showing and hiding the sidebar

    def sidebar_visible(self):
        """The user's choice (kept while focus mode hides the navigation entirely)."""
        return getattr(self, "sidebar_shown", True)

    def set_sidebar_visible(self, visible):
        if visible == self.sidebar_visible():
            return
        self.sidebar_shown = visible
        self.apply_navigation()
        if visible and not self.navigation_hidden:
            self.sidebar.setFocus()
        self.sidebarToggled.emit(visible)

    navigation_hidden = False

    def set_navigation_hidden(self, hidden):
        """Hide the sidebar and its slim strip altogether (focus mode), without changing the user's choice."""
        self.navigation_hidden = hidden
        self.apply_navigation()

    def apply_navigation(self):
        shown = self.sidebar_visible()
        self.panel.setVisible(shown and not self.navigation_hidden)
        self.rail.setVisible(not shown and not self.navigation_hidden)

    def toggle_sidebar(self):
        self.set_sidebar_visible(not self.sidebar_visible())

    def fill_pages_menu(self):
        """The menu on the slim strip: every page under its section, with the current one ticked."""
        self.pages_menu.clear()
        current = self.currentWidget()
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            widget = item.data(PAGE_ROLE)
            if widget is None:
                if not self.pages_menu.isEmpty():
                    self.pages_menu.addSeparator()
                heading = self.pages_menu.addAction(item.data(SECTION_ROLE))
                heading.setEnabled(False)
                font = QFont(self.pages_menu.font())
                font.setBold(True)
                heading.setFont(font)
                continue
            action = self.pages_menu.addAction(item.text(), lambda widget=widget: self.setCurrentWidget(widget))
            action.setCheckable(True)
            action.setChecked(widget is current)

    # ----------------------------------------------------------------- QTabWidget-like interface

    def setCurrentWidget(self, widget):
        row = self._row_of(widget)
        if row is not None:
            self.sidebar.setCurrentRow(row)

    def currentWidget(self):
        return self.stack.currentWidget()

    def widget(self, index):
        return self.stack.widget(index)

    def count(self):
        return self.stack.count()

    def title(self, widget):
        row = self._row_of(widget)
        return self.sidebar.item(row).text() if row is not None else ""

    def page_rows(self):
        return [row for row in range(self.sidebar.count()) if self.sidebar.item(row).data(PAGE_ROLE) is not None]

    def step(self, direction):
        """Move to the next (1) or previous (-1) page, wrapping around."""
        rows = self.page_rows()
        if not rows:
            return
        current = self.sidebar.currentRow()
        position = rows.index(current) if current in rows else 0
        self.sidebar.setCurrentRow(rows[(position + direction) % len(rows)])

    def _row_of(self, widget):
        return next((row for row in range(self.sidebar.count())
                     if self.sidebar.item(row).data(PAGE_ROLE) is widget), None)

    def _on_row_changed(self, row):
        item = self.sidebar.item(row)
        widget = item.data(PAGE_ROLE) if item is not None else None
        if widget is None:
            return
        self.stack.setCurrentWidget(widget)
        self.currentChanged.emit(self.stack.indexOf(widget))
