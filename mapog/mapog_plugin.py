"""
MapogPlugin — QGIS plugin entry point. Adds a toolbar button + menu action
that toggles the MAPOG dock widget.
"""

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt
import os

from .mapog_dockwidget import MapogDockWidget

PLUGIN_NAME = "MAPOG"


class MapogPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self._dir = os.path.dirname(__file__)

    def initGui(self):
        icon_path = os.path.join(self._dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "MAPOG", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.action)

    def unload(self):
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(PLUGIN_NAME, self.action)
            self.action = None

    def _toggle(self, checked):
        if self.dock is None:
            self.dock = MapogDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(self._on_visibility_changed)
        self.dock.setUserVisible(checked)

    def _on_visibility_changed(self, visible):
        if self.action is not None:
            self.action.setChecked(visible)
