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
import re

from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QPalette, QColor, QPixmap, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QStackedWidget, QTabWidget,
    QGroupBox, QComboBox, QCheckBox, QMessageBox, QApplication, QScrollArea,
    QFrame, QProgressBar, QInputDialog,
)
from qgis.core import Qgis, QgsProject, QgsVectorLayer, QgsRasterLayer, QgsMessageLog
from qgis.gui import QgsDockWidget

from .mapog_client import MapogClient, MapogError, MapogAuthError, DEFAULT_BASE_URL, encode_id
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
        self._profile = {}  # signed-in user info shown in the Profile tab

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
        """The not-connected area is its own little router so the user can move
        between sign in, create account, password reset, and the shared OTP
        step without leaving the panel.

        auth_stack: 0=login, 1=signup, 2=forgot password, 3=verify OTP.
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        self.auth_stack = QStackedWidget()
        v.addWidget(self.auth_stack)
        self.auth_stack.addWidget(self._build_login_page())   # 0
        self.auth_stack.addWidget(self._build_signup_page())  # 1
        self.auth_stack.addWidget(self._build_forgot_page())  # 2
        self.auth_stack.addWidget(self._build_otp_page())     # 3
        return page

    def _build_login_page(self):
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

        self.auth_tabs = QTabWidget()

        # --- Tab 1: email / password login ---
        login_tab = QWidget()
        login_col = QVBoxLayout(login_tab)
        login_col.setContentsMargins(12, 14, 12, 14)
        login_col.setSpacing(10)

        login_form = QFormLayout()
        login_form.setSpacing(10)
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("Your password")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.returnPressed.connect(self._on_login)
        login_form.addRow("Email", self.email_edit)
        login_form.addRow("Password", self.password_edit)
        login_col.addLayout(login_form)

        self.login_btn = QPushButton("Log in")
        self.login_btn.setObjectName("primary")
        self.login_btn.setMinimumHeight(38)
        self.login_btn.clicked.connect(self._on_login)
        login_col.addWidget(self.login_btn)

        # Forgot password (left) and Create account (right) as inline links.
        links_row = QHBoxLayout()
        forgot_link = QPushButton("Forgot password?")
        forgot_link.setObjectName("link")
        forgot_link.setCursor(Qt.PointingHandCursor)
        forgot_link.clicked.connect(self._open_forgot)
        links_row.addWidget(forgot_link)
        links_row.addStretch(1)
        signup_link = QPushButton("Create account")
        signup_link.setObjectName("link")
        signup_link.setCursor(Qt.PointingHandCursor)
        signup_link.clicked.connect(self._open_signup)
        links_row.addWidget(signup_link)
        login_col.addLayout(links_row)
        login_col.addStretch(1)
        self.auth_tabs.addTab(login_tab, "Email login")

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
        self.auth_tabs.addTab(key_tab, "Paste API key")

        outer.addWidget(self.auth_tabs)
        outer.addStretch(1)
        return page

    def _build_signup_page(self):
        """Step 1 of sign up: collect name + email, request a verification OTP."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)
        outer.addLayout(self._header("Create your account", on_back=self._show_login))
        outer.addWidget(self._banner(
            "Sign up with your email — we'll send a verification code so you can "
            "set your password."))

        box = QGroupBox("Account")
        form = QFormLayout(box)
        form.setSpacing(10)
        self.su_name_edit = QLineEdit()
        self.su_name_edit.setPlaceholderText("Jane Doe")
        self.su_email_edit = QLineEdit()
        self.su_email_edit.setPlaceholderText("you@example.com")
        form.addRow("Name", self.su_name_edit)
        form.addRow("Email", self.su_email_edit)
        outer.addWidget(box)

        self.su_btn = QPushButton("Send verification code")
        self.su_btn.setObjectName("primary")
        self.su_btn.setMinimumHeight(38)
        self.su_btn.clicked.connect(self._on_signup)
        outer.addWidget(self.su_btn)
        outer.addStretch(1)
        return page

    def _build_forgot_page(self):
        """Step 1 of password reset: collect the account email, request an OTP."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)
        outer.addLayout(self._header("Reset your password", on_back=self._show_login))
        outer.addWidget(self._banner(
            "Enter your account email and we'll send a code to set a new password."))

        box = QGroupBox("Account email")
        form = QFormLayout(box)
        form.setSpacing(10)
        self.fp_email_edit = QLineEdit()
        self.fp_email_edit.setPlaceholderText("you@example.com")
        form.addRow("Email", self.fp_email_edit)
        outer.addWidget(box)

        self.fp_btn = QPushButton("Send reset code")
        self.fp_btn.setObjectName("primary")
        self.fp_btn.setMinimumHeight(38)
        self.fp_btn.clicked.connect(self._on_forgot)
        outer.addWidget(self.fp_btn)
        outer.addStretch(1)
        return page

    def _build_otp_page(self):
        """Shared step 2 for sign up and password reset: enter the emailed OTP
        and choose a password. The backend's /verify/ endpoint sets the password
        and logs in (returns a JWT) for both flows."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)
        outer.addLayout(self._header("Enter verification code", on_back=self._show_login))
        self.otp_hint = QLabel("")
        self.otp_hint.setObjectName("hint")
        self.otp_hint.setWordWrap(True)
        outer.addWidget(self.otp_hint)

        box = QGroupBox("Verification")
        form = QFormLayout(box)
        form.setSpacing(10)
        self.otp_code_edit = QLineEdit()
        self.otp_code_edit.setPlaceholderText("6-digit code")
        self.otp_pass_edit = QLineEdit()
        self.otp_pass_edit.setPlaceholderText("At least 6 characters")
        self.otp_pass_edit.setEchoMode(QLineEdit.Password)
        self.otp_pass2_edit = QLineEdit()
        self.otp_pass2_edit.setPlaceholderText("Re-enter password")
        self.otp_pass2_edit.setEchoMode(QLineEdit.Password)
        self.otp_pass2_edit.returnPressed.connect(self._on_verify_otp)
        form.addRow("Code", self.otp_code_edit)
        form.addRow("New password", self.otp_pass_edit)
        form.addRow("Confirm", self.otp_pass2_edit)
        outer.addWidget(box)

        self.otp_btn = QPushButton("Verify & connect")
        self.otp_btn.setObjectName("primary")
        self.otp_btn.setMinimumHeight(38)
        self.otp_btn.clicked.connect(self._on_verify_otp)
        outer.addWidget(self.otp_btn)

        resend_row = QHBoxLayout()
        resend_row.addStretch(1)
        self.otp_resend_btn = QPushButton("Resend code")
        self.otp_resend_btn.setObjectName("link")
        self.otp_resend_btn.setCursor(Qt.PointingHandCursor)
        self.otp_resend_btn.clicked.connect(self._on_resend_otp)
        resend_row.addWidget(self.otp_resend_btn)
        outer.addLayout(resend_row)
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

    def _target_map_field(self, combo, on_create, label_text="Target map"):
        """A 'Target map' field: caption + a row with the map combo and a
        '+ New map' button so users can create a map without leaving the page."""
        box = QVBoxLayout()
        box.setSpacing(4)
        cap = QLabel(label_text)
        cap.setObjectName("sectionLabel")
        box.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(combo, 1)
        new_btn = QPushButton("+ New map")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(on_create)
        row.addWidget(new_btn)
        box.addLayout(row)
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

        # --- Tab 3: Profile (signed-in account) ---
        self.menu_tabs.addTab(self._build_profile_tab(), "Profile")

        # --- Tab 4: About (plugin identity, version, links) ---
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

    # ---- shareable links (map deep link + raster XYZ tile link) ------------

    def _web_app_url(self):
        """The MAPOG web app origin, derived from the API base URL by dropping a
        trailing '/api' (e.g. https://story.mapog.com/api -> https://story.mapog.com).
        NOTE: in local dev the API host (e.g. :8000) may differ from the web app
        (:8100); production is correct. Falls back to the prod web origin."""
        base = (self.base_url_edit.text().strip() or DEFAULT_BASE_URL).rstrip("/")
        if base.endswith("/api"):
            base = base[:-len("/api")]
        return base or "https://story.mapog.com"

    @staticmethod
    def _slugify(name):
        """Turn a map name into a URL slug: lowercase, runs of non-alphanumeric
        characters collapsed to a single hyphen, ends trimmed
        (e.g. "Visualize Population Data" -> "visualize-population-data").
        Falls back to "map" for an empty / symbol-only name."""
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
        return slug or "map"

    def _map_name_for_id(self, map_id):
        """Find a map's name among the cached maps (self._maps), matching on the
        base64-normalized id. Returns '' when the map isn't in the cache."""
        target = encode_id(map_id)
        for m in self._maps or []:
            mid = m.get("id") or m.get("mapid") or m.get("map_id")
            if mid is not None and encode_id(mid) == target:
                return self._map_title(m)
        return ""

    def _map_share_status_for_id(self, map_id):
        """Find a map's share_status among the cached maps (self._maps), matching
        on the base64-normalized id. Returns the upper-cased status string
        (e.g. 'PUBLIC', 'PROTECTED', 'PRIVATE'); a missing/blank status is
        treated as 'PRIVATE' (the server's own default). Returns '' only when the
        map isn't in the cache at all."""
        target = encode_id(map_id)
        for m in self._maps or []:
            mid = m.get("id") or m.get("mapid") or m.get("map_id")
            if mid is not None and encode_id(mid) == target:
                return (m.get("share_status") or "PRIVATE").strip().upper()
        return ""

    def _map_share_url(self, map_id, map_name=None):
        """Public shareable link that opens this map in the MAPOG web app, of the
        form  {web_app}/public/{name-slug}/{base64-id}  (e.g.
        https://teststory.mapog.com/public/visualize-population-data/NjU3NQ==).

        The map id is base64-encoded (encode_id is idempotent, so already-encoded
        ids pass through). `map_name` builds the slug; when omitted it's looked up
        from the cached maps list. Viewable by others only if the map is set
        Public in MAPOG."""
        if map_name is None:
            map_name = self._map_name_for_id(map_id)
        return f"{self._web_app_url()}/public/{self._slugify(map_name)}/{encode_id(map_id)}"

    def _pricing_url(self):
        """MAPOG pricing/subscription page (tracks the configured server, e.g.
        https://teststory.mapog.com/pricing)."""
        return f"{self._web_app_url()}/pricing"

    def _show_payment_required(self, message):
        """Surface a 402 (subscription required) with a button that opens the
        MAPOG pricing page in the browser."""
        url = self._pricing_url()
        try:
            bar = self.iface.messageBar()
            # Drop the transient "Exporting…/loading…" infos so the payment
            # notice isn't stacked behind a now-misleading "loading" message.
            bar.clearWidgets()
            item = bar.createMessage("MAPOG", message)
            btn = QPushButton("View plans")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda: self._open_url(url))
            item.layout().addWidget(btn)
            bar.pushWidget(item, Qgis.Warning, 12)
        except Exception:
            # Fall back to a plain message with the URL inline.
            self._info(f"{message} Subscribe at {url}", level=Qgis.Warning)

    def _copy_to_clipboard(self, text):
        QApplication.clipboard().setText(text or "")
        self._info("Link copied to clipboard.", level=Qgis.Info)

    def _link_row(self, label_text, url):
        """A field showing a copyable, read-only URL with Copy + Open buttons."""
        box = QVBoxLayout()
        box.setSpacing(4)
        cap = QLabel(label_text)
        cap.setObjectName("sectionLabel")
        box.addWidget(cap)
        row = QHBoxLayout()
        row.setSpacing(6)
        field = QLineEdit(url)
        field.setReadOnly(True)
        field.setCursorPosition(0)
        row.addWidget(field, 1)
        copy_btn = QPushButton("Copy")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.clicked.connect(lambda: self._copy_to_clipboard(url))
        row.addWidget(copy_btn)
        open_btn = QPushButton("Open")
        open_btn.setObjectName("primary")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(lambda: self._open_url(url))
        row.addWidget(open_btn)
        box.addLayout(row)
        return box

    def _build_links_box(self, title="Share & links"):
        """A collapsible-feeling group that holds the per-map/per-layer links.
        Hidden until populated. Rows are rebuilt by _populate_links_box()."""
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        v.setSpacing(8)
        box.setVisible(False)
        return box

    def _populate_links_box(self, box, map_id, raster_tile_url=None,
                            extra_tile_rows=None, map_name=None):
        """(Re)fill a links box: always the public map link; a raster XYZ tile row
        when a tile_url is available; otherwise a note that WMS/WFS and vector
        tile links aren't offered. `map_name` builds the share-link slug (looked
        up from the cached maps when omitted). `extra_tile_rows` is an optional
        list of (label, url) for multiple completed rasters."""
        # Remember how this box was filled so a share-status toggle can rebuild
        # it identically (keeping any raster tile rows) without the caller.
        box._mapog_links_args = dict(
            map_id=map_id, raster_tile_url=raster_tile_url,
            extra_tile_rows=extra_tile_rows, map_name=map_name)

        layout = box.layout()
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                self._clear_layout(item.layout())

        if map_id is not None:
            status = self._map_share_status_for_id(map_id)
            if status == "PUBLIC":
                layout.addLayout(self._link_row(
                    "Public map link", self._map_share_url(map_id, map_name)))
                hint = QLabel("Anyone can open this public map link.")
                hint.setObjectName("hint")
                hint.setWordWrap(True)
                layout.addWidget(hint)
                # Let the user revoke public access from QGIS.
                btn = QPushButton("Make private")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(
                    lambda _=False, b=box, mid=map_id: self._toggle_map_share(b, mid, False))
                layout.addWidget(btn, 0, Qt.AlignLeft)
            else:
                # PRIVATE / PROTECTED (or unknown) — don't expose a link that
                # would 404 / deny access; offer to make it public right here.
                title = QLabel("Public map link")
                title.setObjectName("sectionLabel")
                layout.addWidget(title)
                hint = QLabel("This map is private, so it has no public link. "
                              "Make it public to share it with anyone.")
                hint.setObjectName("hint")
                hint.setWordWrap(True)
                layout.addWidget(hint)
                btn = QPushButton("Make public & get link")
                btn.setObjectName("primary")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(
                    lambda _=False, b=box, mid=map_id: self._toggle_map_share(b, mid, True))
                layout.addWidget(btn, 0, Qt.AlignLeft)

        tile_rows = list(extra_tile_rows or [])
        if raster_tile_url:
            tile_rows.append(("Raster tile URL (XYZ)", raster_tile_url))
        for label, url in tile_rows:
            layout.addLayout(self._link_row(label, url))

        if not tile_rows:
            note = QLabel("No tile/WMS/WFS service link: MAPOG has no OGC "
                          "(WMS/WFS) service, and vector layers don't expose a "
                          "tile URL. Raster layers show an XYZ tile link here.")
            note.setObjectName("hint")
            note.setWordWrap(True)
            layout.addWidget(note)

        box.setVisible(True)

    def _set_cached_share_status(self, map_id, status):
        """Update self._maps so the next _map_share_status_for_id() reflects a
        just-changed share status (the cache is otherwise only refreshed on a
        full maps reload). Inserts a minimal stub if the map isn't cached yet
        (e.g. a map created during this upload session)."""
        target = encode_id(map_id)
        for m in self._maps or []:
            mid = m.get("id") or m.get("mapid") or m.get("map_id")
            if mid is not None and encode_id(mid) == target:
                m["share_status"] = status
                return
        if self._maps is None:
            self._maps = []
        self._maps.append({"id": encode_id(map_id), "share_status": status})

    def _toggle_map_share(self, box, map_id, make_public):
        """Flip a map between PUBLIC and PRIVATE via the external API, then rebuild
        the same links box so the link (or the private notice) updates in place."""
        if not self.client:
            return
        try:
            self._busy(True)
            self.client.set_map_share_status(map_id, make_public)
        except Exception as e:
            self._info(f"Could not update map visibility: {e}", level=Qgis.Warning)
            return
        finally:
            self._busy(False)

        self._set_cached_share_status(map_id, "PUBLIC" if make_public else "PRIVATE")
        self._info("Map is now public — share link ready." if make_public
                   else "Map is now private.", level=Qgis.Success)
        # Rebuild this box exactly as it was last populated (preserves tile rows).
        args = getattr(box, "_mapog_links_args", None) or {"map_id": map_id}
        self._populate_links_box(box, **args)

    @staticmethod
    def _clear_layout(layout):
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                MapogDockWidget._clear_layout(item.layout())

    @staticmethod
    def _raster_tile_url(lyr):
        """Pull the XYZ tile_url from a layer dict's raster_info, or None."""
        info = (lyr or {}).get("raster_info") or {}
        return info.get("tile_url")

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

    def _build_profile_tab(self):
        """The signed-in account: a hero (username) plus username and email.
        Values are filled in by _refresh_profile_tab() on connect."""
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.setContentsMargins(14, 16, 14, 14)
        v.setSpacing(14)

        # --- Hero: avatar + username ---
        hero = QFrame()
        hero.setObjectName("aboutHero")
        hero.setAttribute(Qt.WA_StyledBackground, True)
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(16, 18, 16, 18)
        hv.setSpacing(6)
        avatar = QLabel("👤")
        avatar.setObjectName("cardIcon")
        avatar.setAlignment(Qt.AlignHCenter)
        hv.addWidget(avatar)
        self.pf_name = QLabel("—")
        self.pf_name.setObjectName("aboutTitle")
        self.pf_name.setAlignment(Qt.AlignHCenter)
        self.pf_name.setWordWrap(True)
        hv.addWidget(self.pf_name)
        v.addWidget(hero)

        # --- The three fields ---
        details = QGroupBox("Profile")
        df = QFormLayout(details)
        df.setSpacing(8)
        self.pf_username = self._selectable_value("—")
        self.pf_email = self._selectable_value("—")
        df.addRow("Username", self.pf_username)
        df.addRow("Email", self.pf_email)
        v.addWidget(details)

        v.addStretch(1)
        return tab

    @staticmethod
    def _selectable_value(text):
        """A value QLabel the user can select/copy (e.g. their email)."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lbl

    def _refresh_profile_tab(self):
        """Fill the Profile tab from the captured/persisted profile. Safe to call
        before the tab is built (no-op then)."""
        if getattr(self, "pf_name", None) is None:
            return
        prof = self._profile or {}
        QgsMessageLog.logMessage(
            f"[profile] refresh with: {prof!r}", "MAPOG", Qgis.Info)
        username = str(prof.get("uname") or "—")
        self.pf_name.setText(username if username != "—" else "MAPOG user")
        self.pf_username.setText(username)
        self.pf_email.setText(str(prof.get("email") or "—"))

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
        self.layers_list.itemSelectionChanged.connect(self._refresh_existing_links)
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

        # Links for the selected map (and the selected raster layer's tile URL).
        self.ex_links_box = self._build_links_box("Share & links")
        layout.addWidget(self.ex_links_box)

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
        layout.addLayout(self._target_map_field(
            self.gd_map_combo, self._on_create_gis_map))

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

        # Shown after a successful add: the target map's deep link.
        self.gd_links_box = self._build_links_box("Share & links")
        layout.addWidget(self.gd_links_box)

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
        self.up_map_combo = QComboBox()
        layout.addLayout(self._target_map_field(
            self.up_map_combo, self._on_create_upload_map))

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

        # Shown after a successful upload: the map deep link + any raster XYZ
        # tile links (rasters' tile_url is filled in by the processing watcher).
        self.up_links_box = self._build_links_box("Share & links")
        layout.addWidget(self.up_links_box)

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
        self.gd_links_box.setVisible(False)  # no add yet on a fresh landing
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
                self._profile = settings_store.load_profile()
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
            self._profile = self.client.profile or {}
            QgsMessageLog.logMessage(
                f"[profile] login captured: {self.client.profile!r}",
                "MAPOG", Qgis.Info)
            settings_store.save_profile(self._profile)
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
            # Key-only connect has no login response; show any persisted profile.
            self._profile = settings_store.load_profile()
            self.sk_edit.clear()
            self._enter_browse_state()
        except MapogError as e:
            self._info(f"Key validation failed: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    # ---- sign up / password reset -----------------------------------------

    def _show_login(self):
        self.auth_stack.setCurrentIndex(0)

    def _open_signup(self):
        self.su_name_edit.clear()
        self.su_email_edit.clear()
        self.auth_stack.setCurrentIndex(1)

    def _open_forgot(self):
        self.fp_email_edit.clear()
        self.auth_stack.setCurrentIndex(2)

    def _open_otp(self, email, mode):
        """Show the shared OTP step. `mode` ('signup' | 'reset') only tunes the
        wording — both call the same /verify/ endpoint."""
        self._pending_email = email
        self._otp_mode = mode
        self.otp_code_edit.clear()
        self.otp_pass_edit.clear()
        self.otp_pass2_edit.clear()
        if mode == "signup":
            self.otp_hint.setText(
                f"We sent a 6-digit code to {email}. Enter it and choose a "
                "password to finish creating your account.")
        else:
            self.otp_hint.setText(
                f"We sent a 6-digit code to {email}. Enter it and choose a new "
                "password.")
        self.auth_stack.setCurrentIndex(3)

    def _on_signup(self):
        name = self.su_name_edit.text().strip()
        email = self.su_email_edit.text().strip()
        if not email:
            self._info("Enter your email to sign up.", level=Qgis.Warning)
            return
        base_url = self.base_url_edit.text().strip() or DEFAULT_BASE_URL
        self.client = MapogClient(base_url)
        try:
            self._busy(True)
            self.client.signup(email, uname=name)
            self._open_otp(email, mode="signup")
            self._info("Verification code sent — check your email.", level=Qgis.Success)
        except MapogError as e:
            # Most common case: "Email already exists" — surface the server text.
            self._info(f"Could not sign up: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_forgot(self):
        email = self.fp_email_edit.text().strip()
        if not email:
            self._info("Enter your account email.", level=Qgis.Warning)
            return
        base_url = self.base_url_edit.text().strip() or DEFAULT_BASE_URL
        self.client = MapogClient(base_url)
        try:
            self._busy(True)
            self.client.request_password_reset(email)
            self._open_otp(email, mode="reset")
            self._info("Reset code sent — check your email.", level=Qgis.Success)
        except MapogError as e:
            self._info(f"Could not start password reset: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_resend_otp(self):
        """Re-send the OTP. forget-password regenerates and mails the code for
        any existing user, so it works for both an in-progress signup (the user
        row already exists) and a password reset."""
        email = getattr(self, "_pending_email", "")
        if not email or not self.client:
            return
        try:
            self._busy(True)
            self.client.request_password_reset(email)
            self._info("A new code has been sent to your email.", level=Qgis.Success)
        except MapogError as e:
            self._info(f"Could not resend the code: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_verify_otp(self):
        email = getattr(self, "_pending_email", "")
        otp = self.otp_code_edit.text().strip()
        pwd = self.otp_pass_edit.text()
        pwd2 = self.otp_pass2_edit.text()
        if not email or not self.client:
            self._info("Start sign up or password reset first.", level=Qgis.Warning)
            return
        if not otp:
            self._info("Enter the verification code from your email.", level=Qgis.Warning)
            return
        if len(pwd) < 6:
            self._info("Password must be at least 6 characters.", level=Qgis.Warning)
            return
        if pwd != pwd2:
            self._info("Passwords don't match.", level=Qgis.Warning)
            return
        base_url = self.base_url_edit.text().strip() or DEFAULT_BASE_URL
        try:
            self._busy(True)
            # verify -> JWT -> provision/reuse the 'QGIS Plugin' key pair.
            pk, sk = self.client.verify_and_bootstrap(email, otp, pwd)
            settings_store.save_config(base_url, pk, sk)
            self._profile = self.client.profile or {}
            QgsMessageLog.logMessage(
                f"[profile] verify captured: {self.client.profile!r}",
                "MAPOG", Qgis.Info)
            settings_store.save_profile(self._profile)
            self.otp_pass_edit.clear()
            self.otp_pass2_edit.clear()
            self._enter_browse_state()
            self._info("You're connected to MAPOG.", level=Qgis.Success)
        except MapogAuthError as e:
            self._info(f"Verification failed: {e}", level=Qgis.Critical)
        except MapogError as e:
            self._info(f"Could not connect: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    def _on_logout(self):
        self._stop_raster_watch()  # don't keep polling once signed out
        settings_store.clear_config()
        self.client = None
        self._profile = {}
        self.maps_search.clear()
        self.maps_list.clear()
        self.layers_list.clear()
        self.gd_layers_list.clear()
        self.gd_country_combo.clear()
        self.gd_map_combo.clear()
        self.up_map_combo.clear()
        self.up_layer_list.clear()
        self._ex_map_id = None
        self.ex_links_box.setVisible(False)
        self.up_links_box.setVisible(False)
        self.gd_links_box.setVisible(False)
        self._show_menu()
        self._show_login()
        self.stack.setCurrentIndex(0)

    def _enter_browse_state(self):
        self._refresh_profile_tab()
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
            # No map selected → hide its links until one is picked again.
            self._ex_map_id = None
            self.ex_links_box.setVisible(False)
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

    # ---- create map (inline) ----------------------------------------------

    def _prompt_and_create_map(self):
        """Ask for a name and create a new MAPOG map. Returns the created map
        dict (with id) on success, else None."""
        if not self.client:
            self._info("Not connected.", level=Qgis.Warning)
            return None
        name, ok = QInputDialog.getText(self, "Create map", "Map name:")
        if not ok:
            return None
        name = name.strip()
        if not name:
            self._info("Enter a map name.", level=Qgis.Warning)
            return None
        try:
            self._busy(True)
            created = self.client.create_map(name)
            self._info(f"Created map '{name}'.", level=Qgis.Success)
            return created if isinstance(created, dict) else None
        except MapogError as e:
            self._info(f"Could not create map: {e}", level=Qgis.Critical)
            return None
        finally:
            self._busy(False)

    @staticmethod
    def _select_combo_map(combo, created):
        """Select the newly created map in a target-map combo by its id."""
        map_id = (created or {}).get("id") or (created or {}).get("mapid") \
            or (created or {}).get("map_id")
        if map_id is None:
            return
        for i in range(combo.count()):
            if combo.itemData(i) == map_id:
                combo.setCurrentIndex(i)
                return

    def _on_create_gis_map(self):
        created = self._prompt_and_create_map()
        if created:
            self._load_maps_into_combo()
            self._select_combo_map(self.gd_map_combo, created)

    def _on_create_upload_map(self):
        created = self._prompt_and_create_map()
        if created:
            self._load_maps_into_upload_combo()
            self._select_combo_map(self.up_map_combo, created)

    def _on_map_selected(self):
        item = self.maps_list.currentItem()
        if not item or not self.client:
            return
        m = item.data(Qt.UserRole)
        map_id = m.get("id") or m.get("mapid") or m.get("map_id")
        self._ex_map_id = map_id
        self._ex_map_name = self._map_title(m)
        # Step 1 is done — light up step 2 ("Pick a layer").
        self._set_step2_active(True)
        # Show the map's public link now; a raster selection adds its tile link.
        self._populate_links_box(self.ex_links_box, map_id, map_name=self._ex_map_name)
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
            # Annotation layers are excluded from list_layers, so fetch them
            # separately and list them as their own selectable item.
            self._add_annotation_item(map_id)
        except MapogError as e:
            self._info(f"Failed to load layers: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    @staticmethod
    def _flatten_annotation_features(data):
        """Flatten the annotation endpoint's grouped + global features into one
        list. Returns [] when the map has no annotation layer."""
        if not isinstance(data, dict):
            return []
        features = list(data.get("features") or [])
        for grp in data.get("groups") or []:
            features.extend((grp or {}).get("features") or [])
        return features

    def _add_annotation_item(self, map_id):
        """Fetch the map's annotation layer and, if it has features, append it to
        the layers list as a selectable item.

        Best-effort: any failure is logged and skipped so a missing/empty
        annotation layer never disrupts the regular layers list."""
        try:
            data = self.client.get_annotation_layer(map_id)
        except MapogError as e:
            QgsMessageLog.logMessage(
                f"[annotation] fetch failed for map {map_id}: {e}", "MAPOG", Qgis.Info)
            return
        features = self._flatten_annotation_features(data)
        if not features:
            return
        name = "Annotation Layer"
        marker = {"layer_name": name, "__annotation__": True, "_features": features}
        it = QListWidgetItem(f"[Annotation] {name}")
        it.setData(Qt.UserRole, marker)
        self.layers_list.addItem(it)
        # Keep self._layers consistent with the listed items.
        self._layers.append(marker)

    @staticmethod
    def _is_annotation(lyr):
        return bool(lyr.get("__annotation__"))

    @staticmethod
    def _is_raster(lyr):
        return lyr.get("layer_type") == "RASTER" or "raster_info" in lyr

    def _refresh_existing_links(self):
        """Update the existing-page links box for the current map + selected
        layer (raster layers add their XYZ tile URL; vector layers don't)."""
        map_id = getattr(self, "_ex_map_id", None)
        if map_id is None:
            return
        tile_url = None
        item = self.layers_list.currentItem()
        if item:
            lyr = item.data(Qt.UserRole)
            if self._is_raster(lyr):
                tile_url = self._raster_tile_url(lyr)
        self._populate_links_box(
            self.ex_links_box, map_id, raster_tile_url=tile_url,
            map_name=getattr(self, "_ex_map_name", None))

    def _on_load_layer(self):
        item = self.layers_list.currentItem()
        if not item or not self.client:
            self._info("Select a layer first.", level=Qgis.Warning)
            return
        lyr = item.data(Qt.UserRole)
        name = lyr.get("layer_name") or lyr.get("name") or "Layer"
        # Annotation layers are built locally from their GeoJSON features (no export).
        if self._is_annotation(lyr):
            try:
                self._busy(True)
                self._load_annotation_into_qgis(lyr, name)
            finally:
                self._busy(False)
            return
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

    def _load_annotation_into_qgis(self, lyr, name):
        """Build QGIS vector layers from a MAPOG annotation layer's features and
        add them to the project.

        Annotation layers can mix point/line/polygon geometry, which OGR's GeoJSON
        driver can't hold in one layer, so layer_io splits them into one layer per
        geometry type. Caller owns the busy cursor. Returns True on success."""
        try:
            qgs_layers = layer_io.annotation_features_to_layers(
                lyr.get("_features") or [], base_name=name)
            for qgs_layer in qgs_layers:
                layer_io.add_layer_to_project(qgs_layer)
            self._info(
                f"Loaded '{name}' into QGIS ({len(qgs_layers)} layer(s)).",
                level=Qgis.Success)
            return True
        except ValueError as e:
            self._info(str(e), level=Qgis.Critical)
        return False

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

    def _load_layer_into_qgis(self, layer_id, name, fmt="geojson",
                              announce_payment=True):
        """Export a layer (by base64 layerid) and render it on the QGIS canvas.

        Shared by the Layers-list Load button and the GIS Data auto-load. Caller
        is responsible for the busy cursor. Surfaces gating/errors via messages
        and returns a status: "ok", "payment" (402), or "error".

        Set announce_payment=False to suppress the per-layer 402 message so the
        caller can show one combined message (used by the GIS Data add, where the
        layer IS added to the map even though loading into QGIS is gated).
        """
        try:
            # Export returns a presigned S3 .zip URL; download + unzip + load.
            self._info(f"Exporting '{name}' — this may take a moment…", level=Qgis.Info)
            zip_bytes = self.client.export_layer_zip(layer_id, output_extension=fmt)
            qgs_layer = layer_io.zip_bytes_to_layer(zip_bytes, name)
            layer_io.add_layer_to_project(qgs_layer)
            self._info(f"Loaded '{name}' into QGIS.", level=Qgis.Success)
            return "ok"
        except MapogError as e:
            # 402 = subscription required; 429 = GISDATA download cooldown.
            if e.http_code == 402:
                if announce_payment:
                    self._show_payment_required(
                        "Exporting this layer requires an active MAPOG subscription.")
                return "payment"
            elif e.http_code == 429:
                # Server message already reads "Please wait X before downloading again".
                self._info(str(e), level=Qgis.Warning)
            else:
                self._info(f"Failed to load layer: {e}", level=Qgis.Critical)
        except ValueError as e:
            self._info(str(e), level=Qgis.Critical)
        return "error"

    # ---- Upload (QGIS -> MAPOG) --------------------------------------------

    def _refresh_upload_page(self):
        """Populate the target-map combo (from MAPOG) and the QGIS-layer combo."""
        self._load_maps_into_upload_combo()
        self._refresh_project_layers()
        # Hide stale links from a previous upload until a new one succeeds.
        if getattr(self, "up_links_box", None) is not None:
            self.up_links_box.setVisible(False)

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
                        self._show_payment_required(
                            "Uploading layers requires an active MAPOG subscription.")
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
            # Surface share/links for the target map once anything uploaded.
            # Raster tile URLs are added later by the processing watcher.
            if uploaded:
                self._populate_links_box(
                    self.up_links_box, map_id,
                    map_name=self.up_map_combo.currentText())
            # Show a persistent loader until server-side raster processing ends.
            if pending_rasters:
                self._start_raster_watch(
                    map_id, pending_rasters,
                    map_name=self.up_map_combo.currentText())
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

    def _start_raster_watch(self, map_id, rasters, map_name=None):
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
            "map_name": map_name,
            "pending": list(rasters),
            "done": [],
            "failed": [],
            "tile_links": [],  # (label, xyz_tile_url) for each completed raster
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
                    tile = (info or {}).get("tile_url")
                    if tile:
                        w["tile_links"].append(
                            (f"Raster tile URL (XYZ) — {r['name']}", tile))
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
        map_id, tile_links = w["map_id"], list(w["tile_links"])
        map_name = w.get("map_name")
        self._stop_raster_watch()
        # Add the completed rasters' XYZ tile links to the upload page's box
        # (alongside the public map link populated right after upload).
        if tile_links and getattr(self, "up_links_box", None) is not None:
            self._populate_links_box(self.up_links_box, map_id,
                                     extra_tile_rows=tile_links,
                                     map_name=map_name)
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
            self._info(f"Added '{sel['name']}' to the map — loading into QGIS…",
                       level=Qgis.Info)
            # Auto-load each created layer into QGIS, tracking outcomes so we can
            # show one coherent result (a 402 means it's added but loading is gated).
            loaded, payment_gated = 0, False
            for lyr in created_list:
                lid = lyr.get("layerid") or lyr.get("id") or lyr.get("layer_id")
                lname = lyr.get("layer_name") or sel["name"]
                if not lid:
                    continue
                status = self._load_layer_into_qgis(lid, lname, announce_payment=False)
                if status == "ok":
                    loaded += 1
                elif status == "payment":
                    payment_gated = True
            if payment_gated:
                # The layer IS in the map; only loading into QGIS needs a plan.
                self._show_payment_required(
                    f"'{sel['name']}' was added to your map in MAPOG, but loading "
                    "it into QGIS requires an active subscription.")
            elif not loaded:
                self._info(f"Added '{sel['name']}' to your map.", level=Qgis.Success)
            # When layers loaded ok, their per-layer "Loaded …" messages suffice.
            # The layer is now in the map either way — surface the map's share link.
            self._populate_links_box(self.gd_links_box, map_id,
                                     map_name=self.gd_map_combo.currentText())
        except MapogError as e:
            self._info(f"Failed to add GIS Data layer: {e}", level=Qgis.Critical)
        finally:
            self._busy(False)

    # ---- small helpers -----------------------------------------------------

    def _busy(self, on):
        QApplication.setOverrideCursor(Qt.WaitCursor) if on else QApplication.restoreOverrideCursor()
        for w in (getattr(self, n, None) for n in
                  ("login_btn", "key_btn", "load_btn", "refresh_btn",
                   "gd_add_btn", "su_btn", "fp_btn", "otp_btn",
                   "otp_resend_btn")):
            if w is not None:
                w.setEnabled(not on)
        QApplication.processEvents()

    def _info(self, message, level=Qgis.Info):
        self.iface.messageBar().pushMessage("MAPOG", message, level=level, duration=6)
