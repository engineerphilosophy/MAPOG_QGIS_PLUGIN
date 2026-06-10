"""MAPOG QGIS plugin — package entry point."""


def classFactory(iface):
    """Required QGIS hook: return the plugin instance."""
    from .mapog_plugin import MapogPlugin
    return MapogPlugin(iface)
