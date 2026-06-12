"""
MapogDockWidget — the docked panel UI, built in code (no .ui file) so the
plugin is self-contained.

States:
  * Not connected  -> login form (email/password) + "Paste API key" fallback.
  * Connected menu -> three choices:
       - "Add existing layer"    -> Maps list -> Layers list -> Load into QGIS.
       - "Add GIS layer"         -> target map + country -> admin/OSM catalog -> Add.
       - "Upload layer to MAPOG" -> target map + QGIS layer -> upload as new layer.

Network calls run on the GUI thread for v1 simplicity (export can be slow;
a QThread worker is a clear next step — see README).
"""

import os

from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QPalette, QColor, QPixmap, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QStackedWidget, QTabWidget,
    QGroupBox, QComboBox, QCheckBox, QMessageBox, QApplication, QScrollArea,
    QFrame, QProgressBar,
)
from qgis.core import Qgis, QgsProject, QgsVectorLayer, QgsRasterLayer
from qgis.gui import QgsDockWidget

from .mapog_client import MapogClient, MapogError, MapogAuthError, DEFAULT_BASE_URL
from . import settings_store
from . import layer_io


# MAPOG panel stylesheet. Scoped to the dock's widget subtree (set via
# self.setStyleSheet), so it never leaks into the rest of QGIS. Self-contained
# light surfaces + MAPOG green accent so it reads consistently on both the light
# and dark QGIS themes.
MAPOG_QSS = """
/* Base: force a white panel even under the QGIS dark theme. Targets every
   widget in the dock subtree; specific rules below override it. Palette mirrors
   the MAPOG web app: white surfaces, navy text, blue accent, pill controls. */
QWidget { background-color: #FFFFFF; color: #2B3A4B; font-size: 12px; }

QWidget#brandBar { background-color: #FFFFFF; border-bottom: 1px solid #ECEFF3; }
QLabel#brandIcon { font-size: 18px; }
QLabel#brandTitle { color: #16356B; font-size: 16px; font-weight: 700; }
QLabel#brandSubtitle { color: #8A99AC; font-size: 11px; }

QStackedWidget, QScrollArea { background-color: #FFFFFF; border: none; }

QLabel { color: #2B3A4B; font-size: 12px; background-color: transparent; }
QLabel#pageTitle { font-size: 15px; font-weight: 700; color: #16356B; }
QLabel#sectionLabel { font-size: 11px; font-weight: 700; color: #7A8CA3; }
QLabel#hint { color: #8A99AC; font-size: 12px; }

/* Soft light-blue info banner (like the web "Watch video tutorial" strip). */
QFrame#banner { background-color: #E9F0FB; border: none; border-radius: 10px; }
QLabel#bannerText { color: #2C5AA0; font-size: 12px; font-weight: 600; }

QGroupBox {
    background: #FFFFFF; border: 1px solid #E6EAF0; border-radius: 14px;
    margin-top: 16px; padding: 14px 12px 12px 12px;
    font-weight: 700; color: #16356B;
}
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 4px; }

/* Search-style inputs: white pill with a soft border. */
QLineEdit {
    background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 20px;
    padding: 9px 16px; color: #2B3A4B; min-height: 20px;
    selection-background-color: #2D6BE0; selection-color: #FFFFFF;
}
QLineEdit:focus { border: 1px solid #2D6BE0; }

/* Dropdowns: light-blue pill with navy bold text (like "Select Country"). */
QComboBox {
    background: #EAF1FC; border: 1px solid #DCE7F8; border-radius: 20px;
    padding: 9px 16px; color: #16356B; font-weight: 700; min-height: 20px;
}
QComboBox:focus, QComboBox:hover { border: 1px solid #BBD2F2; }
/* A divider before the arrow zone gives a clear "click to open" affordance so
   the pill reads as a dropdown, not a static value label. The arrow itself is
   an SVG chevron (Qt's stylesheet engine can't draw a CSS triangle); its path
   is injected at runtime in place of __ARROW_URL__. */
QComboBox::drop-down { border-left: 1px solid #DCE7F8; width: 26px; }
QComboBox::down-arrow { image: url(__ARROW_URL__); width: 12px; height: 8px; }
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 8px;
    selection-background-color: #EAF1FC; selection-color: #16356B; outline: none;
}

QListWidget {
    background: #FFFFFF; border: 1px solid #E6EAF0; border-radius: 12px;
    padding: 4px; outline: none;
}
QListWidget::item { padding: 8px 10px; border-radius: 8px; color: #2B3A4B; }
/* Selected row gets the MAPOG blue accent so the chosen map is unmistakable.
   The :!active duplicate keeps the highlight visible after focus moves to the
   Layers list (Qt otherwise dims selection on an unfocused list). */
QListWidget::item:selected,
QListWidget::item:selected:!active { background: #2D6BE0; color: #FFFFFF; font-weight: 600; }
QListWidget::item:selected:hover { background: #2057C4; }
QListWidget::item:hover { background: #F3F6FB; }

/* Pill buttons. */
QPushButton {
    background: #FFFFFF; border: 1px solid #DCE2EA; border-radius: 20px;
    padding: 9px 16px; color: #334E68; font-weight: 600; min-height: 20px;
}
QPushButton:hover { background: #F3F6FB; border-color: #C9D4E2; }
QPushButton:disabled { color: #A8B4C2; background: #F4F6F9; border-color: #E6EAF0; }

QPushButton#primary { background: #2D6BE0; border: 1px solid #2D6BE0; color: #FFFFFF; }
QPushButton#primary:hover { background: #2057C4; border-color: #2057C4; }
QPushButton#primary:disabled { background: #A9C4F0; border-color: #A9C4F0; color: #FFFFFF; }

QPushButton#link { background: transparent; border: none; color: #2C5AA0; padding: 4px 6px; font-weight: 600; }
QPushButton#link:hover { color: #16356B; }

QTabWidget::pane { border: 1px solid #E6EAF0; border-radius: 12px; top: -1px; background: #FFFFFF; }
QTabBar::tab { background: transparent; padding: 9px 16px; color: #7A8CA3; border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:selected { color: #2D6BE0; border-bottom: 2px solid #2D6BE0; }
QTabBar::tab:hover { color: #16356B; }

QFrame#card { background: #FFFFFF; border: 1px solid #E6EAF0; border-radius: 14px; }
QFrame#card:hover { border: 1px solid #2D6BE0; background: #F8FBFF; }
QLabel#cardIcon { font-size: 22px; }
QLabel#cardTitle { font-size: 13px; font-weight: 700; color: #16356B; }
QLabel#cardSubtitle { font-size: 11px; color: #8A99AC; }
QLabel#cardChevron { font-size: 20px; color: #B8C4D4; font-weight: 700; }

/* Numbered step flow (Pick a map → Pick a layer): a bordered panel per step
   with a circular number badge. The badge/title grey out ([pending="true"])
   until that step is reachable, so the sequence reads at a glance without the
   banner. */
QFrame#stepPanel { background: #FFFFFF; border: 1px solid #E6EAF0; border-radius: 14px; }
QLabel#stepBadge {
    background: #2D6BE0; color: #FFFFFF; font-weight: 700; font-size: 12px;
    border-radius: 11px; min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;
}
QLabel#stepBadge[pending="true"] { background: #C9D4E2; }
QLabel#stepTitle { font-size: 13px; font-weight: 700; color: #16356B; }
QLabel#stepTitle[pending="true"] { color: #A8B4C2; }
QLabel#stepConnector { color: #B8C4D4; font-size: 16px; font-weight: 700; }

/* Compact icon button (e.g. ↻ refresh) for placement inside a header row, so
   it reads as a small affordance rather than a full-width primary action. */
QPushButton#iconBtn {
    background: transparent; border: none; color: #7A8CA3; font-size: 16px;
    padding: 0; min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px;
    border-radius: 13px; font-weight: 700;
}
QPushButton#iconBtn:hover { background: #EAF1FC; color: #2D6BE0; }
QPushButton#iconBtn:disabled { background: transparent; color: #C9D4E2; }

/* About tab: a centered hero card with the wordmark, a version badge, the
   tagline, feature highlights, and pill link buttons. */
QFrame#aboutHero {
    background: #F4F8FF; border: 1px solid #E2ECFB; border-radius: 16px;
}
QLabel#aboutTitle { font-size: 17px; font-weight: 800; color: #16356B; }
QLabel#aboutTagline { color: #5B6B80; font-size: 12px; }
QLabel#versionPill {
    background: #E5EEFC; color: #2057C4; font-size: 11px; font-weight: 700;
    border-radius: 11px; padding: 3px 12px;
}
QFrame#featureRow { background: transparent; }
QLabel#featureIcon { font-size: 16px; }
QLabel#featureText { color: #2B3A4B; font-size: 12px; font-weight: 600; }
QPushButton#aboutLink {
    background: #FFFFFF; border: 1px solid #DCE7F8; border-radius: 20px;
    padding: 9px 16px; color: #2057C4; font-weight: 700; min-height: 20px;
}
QPushButton#aboutLink:hover { background: #EAF1FC; border-color: #BBD2F2; color: #16356B; }
QLabel#aboutFooter { color: #A8B4C2; font-size: 11px; }
"""


class ClickableCard(QFrame):
    """A flat card (icon + title + subtitle + chevron) that emits `clicked`.

    Used for the connected-menu choices so they read as tappable tiles rather
    than plain buttons. WA_StyledBackground lets the QSS background/hover paint.
    """

    clicked = pyqtSignal()

    def __init__(self, emoji, title, subtitle, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)

        icon = QLabel(emoji)
        icon.setObjectName("cardIcon")
        row.addWidget(icon, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        s = QLabel(subtitle)
        s.setObjectName("cardSubtitle")
        s.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(s)
        row.addLayout(col, 1)

        chevron = QLabel("›")
        chevron.setObjectName("cardChevron")
        row.addWidget(chevron, 0, Qt.AlignVCenter)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _extract_list(data, *keys):
    """MAPOG payloads vary (list, or dict under 'maps'/'map_layers'). Normalize."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
        # single-key dict wrapping a list
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


class MapogDockWidget(QgsDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("MAPOG", parent)
        self.iface = iface
        self.client = None
        self._maps = []
        self._layers = []

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_auth_page())     # index 0
        self.stack.addWidget(self._build_browse_page())   # index 1

        # Persistent brand bar above the page stack, so the panel always reads
        # as MAPOG regardless of which state it's in.
        container = QWidget()
        container.setObjectName("MapogRoot")
        root = QVBoxLayout(container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_brand_bar())
        root.addWidget(self.stack, 1)

        # A light palette is what actually forces the panel white under a dark
        # OS/QGIS theme: QSS `background-color` doesn't reliably repaint plain
        # QWidget/QFrame backgrounds or native (macOS) buttons, but the palette
        # is inherited by every child. The stylesheet then refines the styled
        # controls (pills, cards, lists) on top of this base.
        self._apply_light_palette(container)
        container.setAutoFillBackground(True)
        container.setStyleSheet(self._stylesheet())
        self.setWidget(container)

        self._try_restore_session()

    def _stylesheet(self):
        """MAPOG_QSS with the combo-arrow image path injected (empty if the
        asset can't be written — then combos fall back to no arrow image)."""
        arrow = self._ensure_arrow_asset() or ""
        return MAPOG_QSS.replace("__ARROW_URL__", arrow)

    @staticmethod
    def _ensure_arrow_asset():
        """Write the dropdown chevron SVG next to this module (once) and return
        a QSS-friendly (forward-slash) path. Combos need a real image for the
        arrow — Qt's stylesheet engine won't draw a CSS triangle, and the
        native arrow is dropped once a QComboBox is styled (notably on macOS)."""
        path = os.path.join(os.path.dirname(__file__), "dropdown_arrow.svg")
        if not os.path.exists(path):
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8" '
                'viewBox="0 0 12 8"><path d="M1 1 L6 6 L11 1" fill="none" '
                'stroke="#16356B" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round"/></svg>'
            )
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(svg)
            except OSError:
                return None
        return path.replace(os.sep, "/")

    @staticmethod
    def _apply_light_palette(widget):
        pal = widget.palette()
        for role in (QPalette.Window, QPalette.Base, QPalette.Button):
            pal.setColor(role, QColor("#FFFFFF"))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            pal.setColor(role, QColor("#2B3A4B"))
        pal.setColor(QPalette.Highlight, QColor("#2D6BE0"))
        pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        widget.setPalette(pal)

    # ---- UI construction ---------------------------------------------------

    def _build_brand_bar(self):
        """White MAPOG header strip (wordmark logo + subtitle) shown above every page."""
        bar = QWidget()
        bar.setObjectName("brandBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(10)

        col = QVBoxLayout()
        col.setSpacing(2)
        logo = QLabel()
        logo.setObjectName("brandLogo")
        logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
        pix = QPixmap(logo_path)
        if not pix.isNull():
            # Render at 2x and tag the device-pixel-ratio so the wordmark stays
            # crisp on HiDPI/Retina while occupying ~26px of logical height.
            target_h = 26
            scaled = pix.scaledToHeight(target_h * 2, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            logo.setPixmap(scaled)
        else:
            # Fallback to text if the asset is missing for any reason.
            logo.setText("MAPOG")
            logo.setObjectName("brandTitle")
        col.addWidget(logo)
        subtitle = QLabel("Cloud GIS for QGIS")
        subtitle.setObjectName("brandSubtitle")
        col.addWidget(subtitle)
        row.addLayout(col)
        row.addStretch(1)
        return bar

    def _build_auth_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        heading = QLabel("Sign in to MAPOG")
        heading.setObjectName("pageTitle")
        outer.addWidget(heading)
        sub = QLabel("Connect QGIS to your maps and layers.")
        sub.setObjectName("hint")
        outer.addWidget(sub)

        form_box = QGroupBox("Server")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignLeft)
        self.base_url_edit = QLineEdit(DEFAULT_BASE_URL)
        self.base_url_edit.setPlaceholderText(DEFAULT_BASE_URL)
        form.addRow("Base URL", self.base_url_edit)
        outer.addWidget(form_box)
        # Restore any previously saved base URL, else keep the default.
        self._init_base_url_from_saved()

        tabs = QTabWidget()

        # --- Tab 1: email / password login ---
        login_tab = QWidget()
        login_form = QFormLayout(login_tab)
        login_form.setContentsMargins(12, 14, 12, 14)
        login_form.setSpacing(10)
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Your password")
        self.password_edit.setEchoMode(QLineEdit.Password)
        login_form.addRow("Email", self.email_edit)
        login_form.addRow("Password", self.password_edit)
        self.login_btn = QPushButton("Log in")
        self.login_btn.setObjectName("primary")
        self.login_btn.setMinimumHeight(38)
        self.login_btn.clicked.connect(self._on_login)
        login_form.addRow(self.login_btn)
        tabs.addTab(login_tab, "Email login")

        # --- Tab 2: paste API key (Google/SSO users) ---
        key_tab = QWidget()
        key_form = QFormLayout(key_tab)
        key_form.setContentsMargins(12, 14, 12, 14)
        key_form.setSpacing(10)
        self.pk_edit = QLineEdit()
        self.pk_edit.setPlaceholderText("pk_...")
        self.sk_edit = QLineEdit()
        self.sk_edit.setPlaceholderText("sk_...")
        self.sk_edit.setEchoMode(QLineEdit.Password)
        key_form.addRow("Publishable key", self.pk_edit)
        key_form.addRow("Secret key", self.sk_edit)
        self.key_btn = QPushButton("Connect with key")
        self.key_btn.setObjectName("primary")
        self.key_btn.setMinimumHeight(38)
        self.key_btn.clicked.connect(self._on_connect_key)
        key_form.addRow(self.key_btn)
        tabs.addTab(key_tab, "Paste API key")

        outer.addWidget(tabs)
        outer.addStretch(1)
        return page

    def _init_base_url_from_saved(self):
        """Restore a previously saved base URL into the editable field, else default."""
        saved, _, _ = settings_store.load_config()
        self.base_url_edit.setText(saved or DEFAULT_BASE_URL)

    def _build_browse_page(self):
        # Connected view is a small router. browse_stack: 0=tabbed menu (the two
        # directions, each listing its options), 1=existing, 2=gis, 3=upload.
        page = QWidget()
        outer = QVBoxLayout(page)
        self.browse_stack = QStackedWidget()
        outer.addWidget(self.browse_stack)
        self.browse_stack.addWidget(self._build_menu_page())        # 0
        self.browse_stack.addWidget(self._build_existing_page())    # 1
        self.browse_stack.addWidget(self._build_gis_page())         # 2
        self.browse_stack.addWidget(self._build_upload_page())      # 3
        return page

    def _header(self, title, with_back=True, on_back=None):
        """A row with an optional Back button and a title.

        `on_back` lets a page choose where Back goes (defaults to the main
        menu); the import sub-pages route back to the MAPOG → QGIS sub-menu.
        Log out lives at the bottom of the main menu, not in every header.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        if with_back:
            back = QPushButton("‹ Back")
            back.setObjectName("link")
            back.setMaximumWidth(70)
            back.setCursor(Qt.PointingHandCursor)
            back.clicked.connect(on_back or self._show_menu)
            row.addWidget(back)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("pageTitle")
        row.addWidget(title_lbl)
        row.addStretch(1)
        return row

    def _banner(self, text, emoji="💡"):
        """A soft light-blue info strip (mirrors the web app's tip banner)."""
        frame = QFrame()
        frame.setObjectName("banner")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(8)
        icon = QLabel(emoji)
        row.addWidget(icon, 0, Qt.AlignTop)
        lbl = QLabel(text)
        lbl.setObjectName("bannerText")
        lbl.setWordWrap(True)
        row.addWidget(lbl, 1)
        return frame

    def _field(self, label_text, widget):
        """Stack a small caption above a control as a tidy form field (the
        caption sits close to its input, with normal spacing between fields)."""
        box = QVBoxLayout()
        box.setSpacing(4)
        cap = QLabel(label_text)
        cap.setObjectName("sectionLabel")
        box.addWidget(cap)
        box.addWidget(widget)
        return box

    @staticmethod
    def _scroll_list(min_h, max_h):
        """A QListWidget bounded to a fixed scroll window: vertical scrollbar
        as needed, no horizontal scrollbar (long item text wraps instead)."""
        lst = QListWidget()
        lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lst.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        lst.setWordWrap(True)
        lst.setMinimumHeight(min_h)
        lst.setMaximumHeight(max_h)
        return lst

    def _step_header(self, number, title, badge_attr=None, title_attr=None,
                     pending=False):
        """A numbered step header (circular badge + title) for a step panel.

        `pending=True` greys the badge/title for a step that isn't reachable
        yet; pass `badge_attr`/`title_attr` to stash the widgets on self so the
        step can later be toggled via `_set_step2_active`.
        """
        row = QHBoxLayout()
        row.setSpacing(8)
        badge = QLabel(str(number))
        badge.setObjectName("stepBadge")
        badge.setAttribute(Qt.WA_StyledBackground, True)
        badge.setAlignment(Qt.AlignCenter)
        lbl = QLabel(title)
        lbl.setObjectName("stepTitle")
        if pending:
            badge.setProperty("pending", True)
            lbl.setProperty("pending", True)
        if badge_attr:
            setattr(self, badge_attr, badge)
        if title_attr:
            setattr(self, title_attr, lbl)
        row.addWidget(badge, 0, Qt.AlignVCenter)
        row.addWidget(lbl, 0, Qt.AlignVCenter)
        row.addStretch(1)
        return row

    def _step_connector(self):
        """A centered downward arrow between step panels, reinforcing the flow."""
        lbl = QLabel("↓")
        lbl.setObjectName("stepConnector")
        lbl.setAlignment(Qt.AlignCenter)
        return lbl

    def _set_step2_active(self, active):
        """Toggle the 'Pick a layer' step between pending (grey) and active.

        Activated once a map is selected; reset to pending when maps reload.
        """
        for w in (getattr(self, "step2_badge", None),
                  getattr(self, "step2_title", None)):
            if w is None:
                continue
            w.setProperty("pending", not active)
            w.style().unpolish(w)
            w.style().polish(w)

    def _build_menu_page(self):
        """Top-level connected view: a tab per direction, each listing its
        available options as cards."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        self.menu_tabs = QTabWidget()

        # --- Tab 1: MAPOG → QGIS (bring data in) ---
        import_tab = QWidget()
        it = QVBoxLayout(import_tab)
        it.setContentsMargins(12, 14, 12, 14)
        it.setSpacing(10)
        it.addWidget(self._banner(
            "Bring maps and layers from MAPOG into your QGIS project."))
        existing_card = ClickableCard(
            "🗺️", "Add existing layer",
            "Load a layer from one of your maps.")
        existing_card.clicked.connect(self._open_existing)
        it.addWidget(existing_card)
        gis_card = ClickableCard(
            "🌍", "Add GIS layer",
            "Add admin boundaries or OSM data to your QGIS project.")
        gis_card.clicked.connect(self._open_gis)
        it.addWidget(gis_card)
        it.addStretch(1)
        self.menu_tabs.addTab(import_tab, "MAPOG → QGIS")

        # --- Tab 2: QGIS → MAPOG (push data up) ---
        export_tab = QWidget()
        et = QVBoxLayout(export_tab)
        et.setContentsMargins(12, 14, 12, 14)
        et.setSpacing(10)
        et.addWidget(self._banner(
            "Upload your QGIS layers to a MAPOG map."))
        upload_card = ClickableCard(
            "⬆️", "Upload layer to MAPOG",
            "Export ticked QGIS layers as new layers in a map.")
        upload_card.clicked.connect(self._open_upload)
        et.addWidget(upload_card)
        et.addStretch(1)
        self.menu_tabs.addTab(export_tab, "QGIS → MAPOG")

        # --- Tab 3: About (plugin identity, version, links) ---
        self.menu_tabs.addTab(self._build_about_tab(), "About")

        # --- Tab 4: Help (getting started + support links) ---
        self.menu_tabs.addTab(self._build_help_tab(), "Help")

        v.addWidget(self.menu_tabs, 1)

        # Log out lives at the bottom of the connected view (removed from the
        # per-page top-right header).
        logout_row = QHBoxLayout()
        logout_row.addStretch(1)
        logout_btn = QPushButton("Log out")
        logout_btn.setObjectName("link")
        logout_btn.setCursor(Qt.PointingHandCursor)
        logout_btn.clicked.connect(self._on_logout)
        logout_row.addWidget(logout_btn)
        logout_row.addStretch(1)
        v.addLayout(logout_row)
        return page

    # ---- About / Help tabs -------------------------------------------------

    # Canonical MAPOG links (kept here so the About/Help tabs share one source).
    HOMEPAGE_URL = "https://mapog.com"
    REPO_URL = "https://github.com/engineerphilosophy/MAPOG-QGIS-PLUGIN"
    TRACKER_URL = "https://github.com/engineerphilosophy/MAPOG-QGIS-PLUGIN/issues"
    SUPPORT_EMAIL = "support@mapog.com"

    @staticmethod
    def _plugin_version():
        """Read `version=` from metadata.txt so About stays in sync with the
        packaged version without a second place to bump. Returns '' on failure."""
        path = os.path.join(os.path.dirname(__file__), "metadata.txt")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("version="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    @staticmethod
    def _open_url(url):
        QDesktopServices.openUrl(QUrl(url))

    def _link_button(self, text, url, object_name="link"):
        """A link-style button that opens `url` in the browser (or the mail
        client for a mailto: URL). `object_name` selects the QSS styling."""
        btn = QPushButton(text)
        btn.setObjectName(object_name)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self._open_url(url))
        return btn

    @staticmethod
    def _feature_row(emoji, text):
        """An icon + label row used in the About tab's feature highlights."""
        frame = QFrame()
        frame.setObjectName("featureRow")
        row = QHBoxLayout(frame)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(10)
        icon = QLabel(emoji)
        icon.setObjectName("featureIcon")
        row.addWidget(icon, 0, Qt.AlignVCenter)
        lbl = QLabel(text)
        lbl.setObjectName("featureText")
        lbl.setWordWrap(True)
        row.addWidget(lbl, 1)
        return frame

    def _build_about_tab(self):
        """Plugin identity: a centered hero (logo + version badge), tagline,
        feature highlights, and pill link buttons."""
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.setContentsMargins(14, 16, 14, 14)
        v.setSpacing(14)

        # --- Hero: centered wordmark, title, and version badge ---
        hero = QFrame()
        hero.setObjectName("aboutHero")
        hero.setAttribute(Qt.WA_StyledBackground, True)
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(16, 20, 16, 20)
        hv.setSpacing(8)

        logo = QLabel()
        pix = QPixmap(os.path.join(os.path.dirname(__file__), "logo.png"))
        if not pix.isNull():
            scaled = pix.scaledToHeight(72, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(2.0)
            logo.setPixmap(scaled)
        else:
            logo.setText("MAPOG")
            logo.setObjectName("brandTitle")
        hv.addWidget(logo, 0, Qt.AlignHCenter)

        title = QLabel("MAPOG for QGIS")
        title.setObjectName("aboutTitle")
        title.setAlignment(Qt.AlignCenter)
        hv.addWidget(title)

        version = self._plugin_version()
        if version:
            # Wrap the pill in a centered row so its background hugs the text
            # instead of stretching the full width.
            pill_row = QHBoxLayout()
            pill_row.addStretch(1)
            ver = QLabel(f"Version {version}")
            ver.setObjectName("versionPill")
            ver.setAttribute(Qt.WA_StyledBackground, True)
            pill_row.addWidget(ver)
            pill_row.addStretch(1)
            hv.addLayout(pill_row)

        tagline = QLabel("Cloud GIS, connected to your QGIS project.")
        tagline.setObjectName("aboutTagline")
        tagline.setAlignment(Qt.AlignCenter)
        tagline.setWordWrap(True)
        hv.addWidget(tagline)
        v.addWidget(hero)

        # --- What you can do (feature highlights) ---
        features = QGroupBox("What you can do")
        fv = QVBoxLayout(features)
        fv.setSpacing(10)
        fv.addWidget(self._feature_row(
            "🗺️", "Browse your MAPOG maps and load their layers onto the canvas."))
        fv.addWidget(self._feature_row(
            "🌍", "Add country admin boundaries and OSM data to any map."))
        fv.addWidget(self._feature_row(
            "⬆️", "Upload QGIS vector and raster layers back to MAPOG."))
        v.addWidget(features)

        # --- Links (pill buttons) ---
        links = QGroupBox("Links")
        lv = QVBoxLayout(links)
        lv.setSpacing(8)
        lv.addWidget(self._link_button(
            "🌐  Visit mapog.com", self.HOMEPAGE_URL, object_name="aboutLink"))
        lv.addWidget(self._link_button(
            "⭐  View source on GitHub", self.REPO_URL, object_name="aboutLink"))
        v.addWidget(links)

        v.addStretch(1)
        footer = QLabel("© MAPOG · mapog.com")
        footer.setObjectName("aboutFooter")
        footer.setAlignment(Qt.AlignCenter)
        v.addWidget(footer)
        return tab

    def _build_help_tab(self):
        """A short welcome banner plus support links."""
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.setContentsMargins(14, 16, 14, 14)
        v.setSpacing(14)

        v.addWidget(self._banner(
            "Questions about MAPOG for QGIS? We're here to help — reach out below."))

        support = QGroupBox("Need help?")
        hv = QVBoxLayout(support)
        hv.setSpacing(8)
        hv.addWidget(self._link_button(
            "✉️  Contact support", f"mailto:{self.SUPPORT_EMAIL}",
            object_name="aboutLink"))
        hv.addWidget(self._link_button(
            "📖  Documentation", self.HOMEPAGE_URL, object_name="aboutLink"))
        v.addWidget(support)

        v.addStretch(1)
        return tab

    def _build_existing_page(self):
        """Browse a map's existing layers and load one into QGIS."""
        page = QWidget()
        wrap = QVBoxLayout(page)
        wrap.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap.addWidget(scroll)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addLayout(self._header("Add existing layer", on_back=self._show_import_menu))
        layout.addWidget(self._banner(
            "Pick a map, choose a layer, then load it onto the QGIS canvas."))

        # Two numbered step panels with a connector arrow between them, so the
        # pick-a-map → pick-a-layer sequence is obvious without reading the banner.
        maps_panel = QFrame()
        maps_panel.setObjectName("stepPanel")
        maps_panel.setAttribute(Qt.WA_StyledBackground, True)
        maps_layout = QVBoxLayout(maps_panel)
        maps_layout.setContentsMargins(12, 12, 12, 12)
        maps_layout.setSpacing(8)
        # Refresh lives in the step header as a small ↻ icon, not a full-width
        # button (which read as a primary action below the list).
        maps_header = self._step_header(1, "Pick a map")
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("iconBtn")
        self.refresh_btn.setToolTip("Refresh maps")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._load_maps)
        maps_header.addWidget(self.refresh_btn, 0, Qt.AlignVCenter)
        maps_layout.addLayout(maps_header)

        # Search/filter box for accounts with many maps.
        self.maps_search = QLineEdit()
        self.maps_search.setPlaceholderText("Search maps…")
        self.maps_search.setClearButtonEnabled(True)
        self.maps_search.textChanged.connect(self._filter_maps_list)
        maps_layout.addWidget(self.maps_search)

        self.maps_list = self._scroll_list(170, 230)
        self.maps_list.itemSelectionChanged.connect(self._on_map_selected)
        maps_layout.addWidget(self.maps_list)
        layout.addWidget(maps_panel)

        layout.addWidget(self._step_connector())

        layers_panel = QFrame()
        layers_panel.setObjectName("stepPanel")
        layers_panel.setAttribute(Qt.WA_StyledBackground, True)
        layers_layout = QVBoxLayout(layers_panel)
        layers_layout.setContentsMargins(12, 12, 12, 12)
        layers_layout.setSpacing(8)
        # Step 2 starts pending (grey) and activates once a map is picked.
        layers_layout.addLayout(self._step_header(
            2, "Pick a layer", badge_attr="step2_badge",
            title_attr="step2_title", pending=True))
        self.layers_list = self._scroll_list(160, 260)
        layers_layout.addWidget(self.layers_list)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Export format"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["geojson", "kml", "shp", "csv"])
        self.format_combo.setToolTip("Choose the export format for the selected layer")
        # Fill the row like the "Target map" dropdowns so it clearly reads as a
        # selectable control rather than a static value pill.
        opts.addWidget(self.format_combo, 1)
        layers_layout.addLayout(opts)

        self.load_btn = QPushButton("Load selected layer into QGIS")
        self.load_btn.setObjectName("primary")
        self.load_btn.setMinimumHeight(38)
        self.load_btn.clicked.connect(self._on_load_layer)
        layers_layout.addWidget(self.load_btn)
        layout.addWidget(layers_panel)
        layout.addStretch(1)
        return page

    def _build_gis_page(self):
        """Add a GISDATA admin / OSM layer of a country to a chosen map."""
        page = QWidget()
        wrap = QVBoxLayout(page)
        wrap.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap.addWidget(scroll)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addLayout(self._header("Add GIS layer", on_back=self._show_import_menu))
        layout.addWidget(self._banner(
            "Add country admin boundaries or OSM layers to a map, then load "
            "them into QGIS."))

        # Target map (where the layer will be added).
        self.gd_map_combo = QComboBox()
        layout.addLayout(self._field("Target map", self.gd_map_combo))

        # Country — auto-loaded when the page opens (see _open_gis).
        self.gd_country_combo = QComboBox()
        self.gd_country_combo.setEnabled(False)
        self.gd_country_combo.currentIndexChanged.connect(self._on_gd_country_changed)
        layout.addLayout(self._field("Country", self.gd_country_combo))

        # Admin levels & OSM layers — a bounded, scrollable list (long names
        # wrap instead of forcing a horizontal scrollbar).
        self.gd_layers_list = self._scroll_list(220, 320)
        layout.addLayout(self._field("Admin levels & OSM layers", self.gd_layers_list))

        self.gd_add_btn = QPushButton("Add to target map")
        self.gd_add_btn.setObjectName("primary")
        self.gd_add_btn.setMinimumHeight(38)
        self.gd_add_btn.clicked.connect(self._on_add_gisdata_layer)
        layout.addWidget(self.gd_add_btn)
        layout.addStretch(1)
        return page

    def _build_upload_page(self):
        """Upload a QGIS project vector layer to a chosen MAPOG map as a new layer."""
        page = QWidget()
        wrap = QVBoxLayout(page)
        wrap.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap.addWidget(scroll)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addLayout(self._header("Upload layer to MAPOG", on_back=self._show_export_menu))
        layout.addWidget(self._banner(
            "Ticked vector layers are exported to GeoJSON (EPSG:4326); raster "
            "layers are uploaded as GeoTIFF (processed into tiles server-side). "
            "Both become new layers in the target map."))

        # Target map (where the new layer will be created).
        map_row = QHBoxLayout()
        map_row.addWidget(QLabel("Target map"))
        self.up_map_combo = QComboBox()
        map_row.addWidget(self.up_map_combo, 1)
        layout.addLayout(map_row)

        # QGIS vector layers to upload (tick one or more).
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("QGIS layers"))
        sel_row.addStretch(1)
        self.up_select_all = QCheckBox("Select all")
        self.up_select_all.toggled.connect(self._on_upload_select_all)
        sel_row.addWidget(self.up_select_all)
        layout.addLayout(sel_row)

        self.up_layer_list = QListWidget()
        layout.addWidget(self.up_layer_list)

        self.up_refresh_btn = QPushButton("Refresh maps & layers")
        self.up_refresh_btn.clicked.connect(self._refresh_upload_page)
        layout.addWidget(self.up_refresh_btn)

        self.up_btn = QPushButton("Upload to MAPOG")
        self.up_btn.setObjectName("primary")
        self.up_btn.setMinimumHeight(38)
        self.up_btn.clicked.connect(self._on_upload_layer)
        layout.addWidget(self.up_btn)
        layout.addStretch(1)
        return page

    # ---- navigation --------------------------------------------------------

    def _show_menu(self):
        self.browse_stack.setCurrentIndex(0)

    def _show_import_menu(self):
        # Back target for the import detail pages: menu, MAPOG → QGIS tab.
        self.menu_tabs.setCurrentIndex(0)
        self.browse_stack.setCurrentIndex(0)

    def _show_export_menu(self):
        # Back target for the upload page: menu, QGIS → MAPOG tab.
        self.menu_tabs.setCurrentIndex(1)
        self.browse_stack.setCurrentIndex(0)

    def _open_existing(self):
        self.browse_stack.setCurrentIndex(1)
        self._load_maps()

    def _open_gis(self):
        self.browse_stack.setCurrentIndex(2)
        self._load_maps_into_combo()
        # Load countries automatically on first landing; picking the first
        # country also populates its admin/OSM layers.
        if self.gd_country_combo.count() == 0:
            self._load_gisdata_countries()

    def _open_upload(self):
        self.browse_stack.setCurrentIndex(3)
        self._refresh_upload_page()

    # ---- session lifecycle -------------------------------------------------

    def _try_restore_session(self):
        base_url, pk, sk = settings_store.load_config()
        if pk and sk:
            self.client = MapogClient(base_url or DEFAULT_BASE_URL, pk, sk)
            try:
                self._busy(True)
                self.client.verify_keys()
                self._enter_browse_state()
                return
            except MapogError:
                self._info("Stored credentials are no longer valid — please log in again.",
                           level=Qgis.Warning)
            finally:
                self._busy(False)
        self.stack.setCurrentIndex(0)

    def _on_login(self):
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email or not password:
            self._info("Enter email and password.", level=Qgis.Warning)
            return
        base_url = self.base_url_edit.text().strip() or DEFAULT_BASE_URL
        self.client = MapogClient(base_url)
        try:
            self._busy(True)
            pk, sk = self.client.login_and_bootstrap(email, password)
            settings_store.save_config(base_url, pk, sk)
            self.password_edit.clear()
            self._enter_browse_state()
        except MapogAuthError as e:
            self._info(f"Login failed: {e}", level=Qgis.Critical)
        except MapogError as e:
            self._info(f"Could not connect: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_connect_key(self):
        pk = self.pk_edit.text().strip()
        sk = self.sk_edit.text().strip()
        if not (pk.startswith("pk_") and sk.startswith("sk_")):
            self._info("Enter a valid pk_ and sk_ key pair.", level=Qgis.Warning)
            return
        base_url = self.base_url_edit.text().strip() or DEFAULT_BASE_URL
        self.client = MapogClient(base_url, pk, sk)
        try:
            self._busy(True)
            self.client.verify_keys()
            settings_store.save_config(base_url, pk, sk)
            self.sk_edit.clear()
            self._enter_browse_state()
        except MapogError as e:
            self._info(f"Key validation failed: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_logout(self):
        self._stop_raster_watch()  # don't keep polling once signed out
        settings_store.clear_config()
        self.client = None
        self.maps_search.clear()
        self.maps_list.clear()
        self.layers_list.clear()
        self.gd_layers_list.clear()
        self.gd_country_combo.clear()
        self.gd_map_combo.clear()
        self.up_map_combo.clear()
        self.up_layer_list.clear()
        self._show_menu()
        self.stack.setCurrentIndex(0)

    def _enter_browse_state(self):
        self.stack.setCurrentIndex(1)
        self._show_menu()  # show the two-choice menu; pages load maps on open

    # ---- data actions ------------------------------------------------------

    def _fetch_maps(self):
        """Fetch maps once and cache in self._maps; returns the list."""
        data = self.client.list_maps()
        self._maps = _extract_list(data, "maps", "data")
        return self._maps

    @staticmethod
    def _map_title(m):
        return m.get("map_name") or m.get("name") or m.get("title") or f"Map {m.get('id')}"

    def _load_maps(self):
        if not self.client:
            return
        try:
            self._busy(True)
            maps = self._fetch_maps()
            self.maps_list.clear()
            # No map is selected after a (re)load, so reset step 2 to pending
            # and drop any layers from a previously selected map.
            self.layers_list.clear()
            self._set_step2_active(False)
            for m in maps:
                item = QListWidgetItem(str(self._map_title(m)))
                item.setData(Qt.UserRole, m)
                self.maps_list.addItem(item)
            # Keep any active search applied to the freshly loaded items.
            self._filter_maps_list(self.maps_search.text())
            if not maps:
                self._info("No maps found for this account.", level=Qgis.Info)
        except MapogError as e:
            self._info(f"Failed to load maps: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _filter_maps_list(self, text):
        """Hide maps whose title doesn't contain the search text (case-insensitive)."""
        needle = (text or "").strip().lower()
        for i in range(self.maps_list.count()):
            it = self.maps_list.item(i)
            it.setHidden(needle not in it.text().lower())

    def _load_maps_into_combo(self):
        if not self.client:
            return
        try:
            self._busy(True)
            maps = self._fetch_maps()
            self.gd_map_combo.clear()
            for m in maps:
                map_id = m.get("id") or m.get("mapid") or m.get("map_id")
                self.gd_map_combo.addItem(str(self._map_title(m)), map_id)
            if not maps:
                self._info("No maps found — create a map first to add GIS layers to.",
                           level=Qgis.Warning)
        except MapogError as e:
            self._info(f"Failed to load maps: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_map_selected(self):
        item = self.maps_list.currentItem()
        if not item or not self.client:
            return
        m = item.data(Qt.UserRole)
        map_id = m.get("id") or m.get("mapid") or m.get("map_id")
        # Step 1 is done — light up step 2 ("Pick a layer").
        self._set_step2_active(True)
        try:
            self._busy(True)
            data = self.client.list_layers(map_id)
            vector_layers = _extract_list(data, "map_layers", "layers")
            raster_layers = data.get("raster_layers", []) if isinstance(data, dict) else []
            self._layers = list(vector_layers) + list(raster_layers)
            self.layers_list.clear()
            for lyr in vector_layers:
                name = lyr.get("layer_name") or lyr.get("name") or f"Layer {lyr.get('layerid') or lyr.get('id')}"
                it = QListWidgetItem(str(name))
                it.setData(Qt.UserRole, lyr)
                self.layers_list.addItem(it)
            for lyr in raster_layers:
                name = lyr.get("layer_name") or lyr.get("name") or "Raster"
                it = QListWidgetItem(f"[Raster] {name}")
                it.setData(Qt.UserRole, lyr)
                self.layers_list.addItem(it)
        except MapogError as e:
            self._info(f"Failed to load layers: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    @staticmethod
    def _is_raster(lyr):
        return lyr.get("layer_type") == "RASTER" or "raster_info" in lyr

    def _on_load_layer(self):
        item = self.layers_list.currentItem()
        if not item or not self.client:
            self._info("Select a layer first.", level=Qgis.Warning)
            return
        lyr = item.data(Qt.UserRole)
        name = lyr.get("layer_name") or lyr.get("name") or "Layer"
        # Raster layers load directly from their XYZ tile URL (no export).
        if self._is_raster(lyr):
            try:
                self._busy(True)
                self._load_raster_into_qgis(lyr, name)
            finally:
                self._busy(False)
            return
        # Vector layers go through the export path.
        layer_id = lyr.get("layerid") or lyr.get("id") or lyr.get("layer_id")
        fmt = self.format_combo.currentText()
        if fmt == "csv":
            self._info("CSV export has no geometry to render on the canvas — pick "
                       "geojson/kml/shp.", level=Qgis.Warning)
            return
        try:
            self._busy(True)
            self._load_layer_into_qgis(layer_id, name, fmt=fmt)
        finally:
            self._busy(False)

    def _load_raster_into_qgis(self, lyr, name):
        """Load a MAPOG raster layer into QGIS from its tile-URL template.

        Caller owns the busy cursor. Returns True on success."""
        info = lyr.get("raster_info") or {}
        tile_url = info.get("tile_url")
        if not tile_url:
            status = info.get("processing_status")
            if status == "completed":
                # Completed but no URL means the backend failed to build the
                # tile template (e.g. missing tile host) — not a processing
                # delay. Report it as an error so it isn't mistaken for "wait".
                self._info(
                    f"Raster '{name}' is processed but the server returned no "
                    "tile URL. This is a backend configuration issue, not a "
                    "processing delay.",
                    level=Qgis.Critical,
                )
            else:
                self._info(
                    f"Raster '{name}' has no tile URL yet"
                    + (f" (status: {status})" if status else "")
                    + " — it may still be processing.",
                    level=Qgis.Warning,
                )
            return False
        try:
            # Respect the layer's real zoom range when known; fall back to 0/22.
            zmin = info.get("min_zoom")
            zmax = info.get("max_zoom")
            qgs_layer = layer_io.xyz_url_to_raster_layer(
                tile_url, name,
                zmin=zmin if zmin is not None else 0,
                zmax=zmax if zmax is not None else 22,
            )
            layer_io.add_layer_to_project(qgs_layer)
            qgs_layer.triggerRepaint()
            # XYZ layers report a global extent, so drive the canvas to the
            # layer's real footprint (EPSG:4326 bbox) so the data is in view.
            bbox = lyr.get("layer_bbox") or info.get("bounds")
            layer_io.zoom_canvas_to_bbox_4326(self.iface, bbox)
            self._info(f"Loaded raster '{name}' into QGIS.", level=Qgis.Success)
            return True
        except ValueError as e:
            self._info(str(e), level=Qgis.Critical)
        return False

    def _load_layer_into_qgis(self, layer_id, name, fmt="geojson"):
        """Export a layer (by base64 layerid) and render it on the QGIS canvas.

        Shared by the Layers-list Load button and the GIS Data auto-load. Caller
        is responsible for the busy cursor. Surfaces gating/errors via messages
        and returns True on success, False otherwise.
        """
        try:
            # Export returns a presigned S3 .zip URL; download + unzip + load.
            self._info(f"Exporting '{name}' — this may take a moment…", level=Qgis.Info)
            zip_bytes = self.client.export_layer_zip(layer_id, output_extension=fmt)
            qgs_layer = layer_io.zip_bytes_to_layer(zip_bytes, name)
            layer_io.add_layer_to_project(qgs_layer)
            self._info(f"Loaded '{name}' into QGIS.", level=Qgis.Success)
            return True
        except MapogError as e:
            # 402 = subscription required; 429 = GISDATA download cooldown.
            if e.http_code == 402:
                self._info("Exporting this layer requires an active MAPOG subscription.",
                           level=Qgis.Warning)
            elif e.http_code == 429:
                # Server message already reads "Please wait X before downloading again".
                self._info(str(e), level=Qgis.Warning)
            else:
                self._info(f"Failed to load layer: {e}", level=Qgis.Critical)
        except ValueError as e:
            self._info(str(e), level=Qgis.Critical)
        return False

    # ---- Upload (QGIS -> MAPOG) --------------------------------------------

    def _refresh_upload_page(self):
        """Populate the target-map combo (from MAPOG) and the QGIS-layer combo."""
        self._load_maps_into_upload_combo()
        self._refresh_project_layers()

    def _load_maps_into_upload_combo(self):
        if not self.client:
            return
        try:
            self._busy(True)
            maps = self._fetch_maps()
            self.up_map_combo.clear()
            for m in maps:
                map_id = m.get("id") or m.get("mapid") or m.get("map_id")
                self.up_map_combo.addItem(str(self._map_title(m)), map_id)
            if not maps:
                self._info("No maps found — create a map first to upload layers to.",
                           level=Qgis.Warning)
        except MapogError as e:
            self._info(f"Failed to load maps: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _refresh_project_layers(self):
        """List the current QGIS project's vector and raster layers as tickable
        items. Rasters are tagged so they're easy to tell apart; the upload
        handler re-checks each layer's type to pick the export path."""
        self.up_layer_list.clear()
        self.up_select_all.blockSignals(True)
        self.up_select_all.setChecked(False)
        self.up_select_all.blockSignals(False)
        for layer in QgsProject.instance().mapLayers().values():
            if not layer.isValid():
                continue
            if isinstance(layer, QgsVectorLayer):
                label = layer.name()
            elif isinstance(layer, QgsRasterLayer):
                label = f"[Raster] {layer.name()}"
            else:
                continue
            it = QListWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, layer.id())
            self.up_layer_list.addItem(it)
        if self.up_layer_list.count() == 0:
            self._info("No vector or raster layers in the current QGIS project to upload.",
                       level=Qgis.Info)

    def _on_upload_select_all(self, checked):
        """Tick / untick every layer in the upload list."""
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.up_layer_list.count()):
            self.up_layer_list.item(i).setCheckState(state)

    def _checked_upload_layers(self):
        """Return the layer ids of all ticked items in the upload list."""
        ids = []
        for i in range(self.up_layer_list.count()):
            it = self.up_layer_list.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        return ids

    def _on_upload_layer(self):
        if not self.client:
            self._info("Not connected.", level=Qgis.Warning)
            return
        map_id = self.up_map_combo.currentData()
        if map_id is None:
            self._info("Select a target map first.", level=Qgis.Warning)
            return
        layer_ids = self._checked_upload_layers()
        if not layer_ids:
            self._info("Tick at least one QGIS layer to upload.", level=Qgis.Warning)
            return
        try:
            self._busy(True)
            uploaded, failed = [], []
            pending_rasters = []
            for layer_id in layer_ids:
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer is None or not layer.isValid():
                    failed.append(layer_id)
                    continue
                name = layer.name()
                is_raster = isinstance(layer, QgsRasterLayer)
                if not is_raster and not isinstance(layer, QgsVectorLayer):
                    failed.append(name)
                    continue
                try:
                    self._info(f"Uploading '{name}' to MAPOG — this may take a moment…",
                               level=Qgis.Info)
                    if is_raster:
                        path = layer_io.raster_layer_to_geotiff(layer, iface=self.iface)
                        # Raster ingestion is async server-side and applies its
                        # own default style, so there's no style to copy back.
                        res = self.client.upload_raster_layer(map_id, path, name=name)
                        info = (res or {}).get("raster_info") or {} if isinstance(res, dict) else {}
                        pending_rasters.append({
                            "layerid": res.get("layerid") if isinstance(res, dict) else None,
                            "uuid": info.get("uuid"),
                            "name": name,
                        })
                    else:
                        path = layer_io.vector_layer_to_geojson(layer)
                        result = self.client.upload_layer(map_id, [path])
                        self._apply_layer_style(result, layer)
                    uploaded.append(name)
                except MapogError as e:
                    # 402 = subscription required; gating won't change for the rest,
                    # so surface it once and stop.
                    if e.http_code == 402:
                        self._info("Uploading layers requires an active MAPOG subscription.",
                                   level=Qgis.Warning)
                        failed.append(name)
                        break
                    self._info(f"Failed to upload '{name}': {e}", level=Qgis.Critical)
                    failed.append(name)
                except ValueError as e:
                    self._info(f"Failed to upload '{name}': {e}", level=Qgis.Critical)
                    failed.append(name)
            # Summary line. Rasters are ingested asynchronously (converted to
            # tiles server-side); a live progress watcher (below) tracks them, so
            # the summary just confirms the upload was accepted.
            raster_note = (
                " Raster layers are processing — tracking progress below."
                if pending_rasters else ""
            )
            if uploaded and not failed:
                self._info(
                    f"Uploaded {len(uploaded)} layer(s) to MAPOG: {', '.join(uploaded)}."
                    + raster_note,
                    level=Qgis.Success,
                )
            elif uploaded:
                self._info(
                    f"Uploaded {len(uploaded)} layer(s); {len(failed)} failed." + raster_note,
                    level=Qgis.Warning,
                )
            # Show a persistent loader until server-side raster processing ends.
            if pending_rasters:
                self._start_raster_watch(map_id, pending_rasters)
            # If nothing uploaded, the per-layer error(s) above already explain why.
        finally:
            self._busy(False)

    def _apply_layer_style(self, upload_result, qgis_layer):
        """Replicate the QGIS layer's symbology onto the freshly uploaded MAPOG
        layer(s), overriding MAPOG's default blue.

        Categorized layers carry their full per-value palette (style_type
        "CATEGORY"); single-symbol layers carry a "Basic" fill/stroke.

        Best-effort: a style failure must not fail the upload itself, since the
        layer is already created server-side.
        """
        style_type, style_attributes = layer_io.vector_style_from_layer(qgis_layer)
        if not style_attributes:
            return
        for c in _extract_list(upload_result, "layers"):
            new_id = c.get("layerid") or c.get("id") or c.get("layer_id")
            if not new_id:
                continue
            try:
                self.client.update_layer_style(new_id, style_attributes, style_type=style_type)
            except MapogError as e:
                self._info(f"Uploaded '{qgis_layer.name()}', but could not copy its "
                           f"style: {e}", level=Qgis.Warning)

    # ---- raster processing watcher -----------------------------------------
    # Raster upload returns immediately; the server then converts the file to a
    # COG and builds tiles. We poll the map's layers until each raster reports
    # processing_status completed/failed, showing a persistent progress bar so
    # the user isn't left wondering whether anything is happening.
    _RASTER_POLL_MS = 3000        # poll interval
    _RASTER_POLL_MAX_TICKS = 200  # give up after ~10 min (3s × 200)

    def _start_raster_watch(self, map_id, rasters):
        """Show a persistent message-bar loader and poll MAPOG until every
        freshly uploaded raster finishes (or fails) server-side processing."""
        self._stop_raster_watch()  # only one watch at a time

        bar = self.iface.messageBar()
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(0)
        progress.setTextVisible(True)
        progress.setMaximumWidth(220)
        n = len(rasters)
        item = bar.createMessage(
            "MAPOG", f"Processing {n} raster{'s' if n != 1 else ''} on MAPOG…")
        item.layout().addWidget(progress)
        bar.pushWidget(item, Qgis.Info)

        self._raster_watch = {
            "map_id": map_id,
            "pending": list(rasters),
            "done": [],
            "failed": [],
            "item": item,
            "progress": progress,
            "ticks": 0,
        }
        if getattr(self, "up_btn", None) is not None:
            self.up_btn.setEnabled(False)

        self._raster_timer = QTimer(self)
        self._raster_timer.setInterval(self._RASTER_POLL_MS)
        self._raster_timer.timeout.connect(self._poll_raster_watch)
        self._raster_timer.start()
        # Poll once now so the bar reflects current status without a 3s wait.
        self._poll_raster_watch()

    def _poll_raster_watch(self):
        w = getattr(self, "_raster_watch", None)
        if not w or not self.client:
            self._stop_raster_watch()
            return
        w["ticks"] += 1

        try:
            data = self.client.list_layers(w["map_id"])
        except MapogError:
            data = None  # transient — keep waiting rather than killing the watch

        if isinstance(data, dict):
            by_id, by_uuid = {}, {}
            for lyr in data.get("raster_layers", []) or []:
                info = lyr.get("raster_info") or {}
                if lyr.get("layerid") is not None:
                    by_id[lyr.get("layerid")] = info
                if info.get("uuid"):
                    by_uuid[info.get("uuid")] = info

            still_pending, progress_vals = [], []
            for r in w["pending"]:
                info = by_id.get(r["layerid"]) or by_uuid.get(r["uuid"])
                status = (info or {}).get("processing_status")
                if status == "completed":
                    w["done"].append(r["name"])
                elif status == "failed":
                    w["failed"].append((r["name"], (info or {}).get("processing_error")))
                else:
                    progress_vals.append((info or {}).get("processing_progress") or 0)
                    still_pending.append(r)
            w["pending"] = still_pending

            # Bar = average progress, with finished rasters counting as 100.
            total = len(w["done"]) + len(w["failed"]) + len(w["pending"])
            finished = len(w["done"]) + len(w["failed"])
            try:
                if total and (finished or sum(progress_vals)):
                    avg = (finished * 100 + sum(progress_vals)) // total
                    w["progress"].setRange(0, 100)
                    w["progress"].setValue(max(0, min(100, int(avg))))
                else:
                    # No numeric progress yet — show an indeterminate "busy" bar
                    # so it animates instead of sitting at a confusing 0%.
                    w["progress"].setRange(0, 0)
            except RuntimeError:
                pass  # widget gone (user dismissed the message bar)

        if not w["pending"]:
            self._finish_raster_watch()
        elif w["ticks"] >= self._RASTER_POLL_MAX_TICKS:
            names = ", ".join(r["name"] for r in w["pending"])
            self._stop_raster_watch()
            self._info(
                f"Still processing: {names}. It's taking longer than expected — "
                "check the import tab (QGIS ← MAPOG) later to load it.",
                level=Qgis.Warning,
            )

    def _finish_raster_watch(self):
        w = getattr(self, "_raster_watch", None)
        if not w:
            return
        done, failed = list(w["done"]), list(w["failed"])
        self._stop_raster_watch()
        if done and not failed:
            self._info(
                f"Raster processing complete: {', '.join(done)}. Load it from the "
                "import tab (QGIS ← MAPOG).",
                level=Qgis.Success,
            )
        elif done:
            self._info(
                f"{len(done)} raster(s) ready; {len(failed)} failed: "
                f"{', '.join(name for name, _ in failed)}.",
                level=Qgis.Warning,
            )
        elif failed:
            first_err = next((e for _, e in failed if e), None)
            self._info(
                f"Raster processing failed: {', '.join(name for name, _ in failed)}."
                + (f" ({first_err})" if first_err else ""),
                level=Qgis.Critical,
            )

    def _stop_raster_watch(self):
        timer = getattr(self, "_raster_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._raster_timer = None
        w = getattr(self, "_raster_watch", None)
        if w:
            try:
                self.iface.messageBar().popWidget(w["item"])
            except Exception:
                pass
        self._raster_watch = None
        if getattr(self, "up_btn", None) is not None:
            try:
                self.up_btn.setEnabled(True)
            except RuntimeError:
                pass

    # ---- GIS Data ----------------------------------------------------------

    def _load_gisdata_countries(self):
        if not self.client:
            return
        try:
            self._busy(True)
            data = self.client.list_gisdata_countries()
            countries = _extract_list(data, "countries", "data")
            self.gd_country_combo.blockSignals(True)
            self.gd_country_combo.clear()
            for c in countries:
                cid = c.get("gisdata_country_id") or c.get("id")
                cname = c.get("country_name") or c.get("name") or f"Country {cid}"
                self.gd_country_combo.addItem(str(cname).title(), cid)
            self.gd_country_combo.blockSignals(False)
            self.gd_country_combo.setEnabled(bool(countries))
            if countries:
                self._on_gd_country_changed()  # populate layers for the first country
            else:
                self._info("No countries available in the GIS Data catalog.", level=Qgis.Info)
        except MapogError as e:
            self._info(f"Failed to load countries: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_gd_country_changed(self):
        if not self.client:
            return
        country_id = self.gd_country_combo.currentData()
        if country_id is None:
            return
        try:
            self._busy(True)
            self.gd_layers_list.clear()
            # Admin levels
            admin = _extract_list(self.client.list_gisdata_admin_layers(country_id),
                                  "admin_layers", "data")
            for a in admin:
                lvl = a.get("admin_level_name") or f"ADM{a.get('admin_level')}"
                label = f"[Admin] {lvl} — {a.get('layer_name', '')}".strip(" —")
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, {
                    "kind": "admin",
                    "gisdata_layer_id": a.get("gisdata_layer_id") or a.get("id"),
                    "gisdata_country_id": country_id,
                    "name": a.get("layer_name") or lvl,
                })
                self.gd_layers_list.addItem(it)
            # OSM / other layers
            others = self.client.list_gisdata_other_layers(country_id)
            for o in (others or []):
                lt = o.get("layer_type") or ""
                label = f"[OSM] {o.get('layer', '')} ({lt})".strip()
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, {
                    "kind": "other",
                    "gisdata_layer_id": o.get("id"),
                    "gisdata_country_id": country_id,
                    "name": o.get("layer") or f"OSM {lt}",
                })
                self.gd_layers_list.addItem(it)
            if self.gd_layers_list.count() == 0:
                self._info("No GIS Data layers found for this country.", level=Qgis.Info)
        except MapogError as e:
            self._info(f"Failed to load GIS Data layers: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_add_gisdata_layer(self):
        if not self.client:
            return
        item = self.gd_layers_list.currentItem()
        if not item:
            self._info("Select an admin level or OSM layer to add.", level=Qgis.Warning)
            return
        if self.gd_map_combo.count() == 0 or self.gd_map_combo.currentData() is None:
            self._info("Select a target map first.", level=Qgis.Warning)
            return
        sel = item.data(Qt.UserRole)
        map_id = self.gd_map_combo.currentData()
        if not sel.get("gisdata_layer_id"):
            self._info("This catalog item has no id to add.", level=Qgis.Critical)
            return
        try:
            self._busy(True)
            self._info(f"Adding '{sel['name']}' to the map…", level=Qgis.Info)
            if sel["kind"] == "admin":
                created = self.client.add_gisdata_admin_layer(
                    sel["gisdata_layer_id"], sel["gisdata_country_id"], map_id)
            else:
                created = self.client.add_gisdata_other_layer(
                    sel["gisdata_layer_id"], sel["gisdata_country_id"], map_id)

            created_list = created if isinstance(created, list) else _extract_list(created, "data")
            self._info(f"Added '{sel['name']}' ({len(created_list)} layer(s)). Loading…",
                       level=Qgis.Success)
            # Auto-load each created layer into QGIS.
            for lyr in created_list:
                lid = lyr.get("layerid") or lyr.get("id") or lyr.get("layer_id")
                lname = lyr.get("layer_name") or sel["name"]
                if lid:
                    self._load_layer_into_qgis(lid, lname)
        except MapogError as e:
            self._info(f"Failed to add GIS Data layer: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    # ---- small helpers -----------------------------------------------------

    def _busy(self, on):
        QApplication.setOverrideCursor(Qt.WaitCursor) if on else QApplication.restoreOverrideCursor()
        for w in (getattr(self, n, None) for n in
                  ("login_btn", "key_btn", "load_btn", "refresh_btn",
                   "gd_add_btn")):
            if w is not None:
                w.setEnabled(not on)
        QApplication.processEvents()

    def _info(self, message, level=Qgis.Info):
        self.iface.messageBar().pushMessage("MAPOG", message, level=level, duration=6)
