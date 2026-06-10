# MAPOG QGIS Plugin

Connect QGIS to [MAPOG](https://mapog.com) — browse your maps and layers, load
them into QGIS, and push vector **and raster** layers back through the MAPOG
external API.

The plugin lives in the [`mapog/`](mapog/) folder; see
[`mapog/README.md`](mapog/README.md) for full usage details.

## Features

- **MAPOG → QGIS:** browse maps, load vector layers (GeoJSON/KML/Shapefile) and
  raster layers (dynamic XYZ tile URLs, auto-zoom to footprint).
- **QGIS → MAPOG:** upload ticked layers to a target map.
  - Vector layers are exported to GeoJSON (EPSG:4326).
  - Raster layers are uploaded as GeoTIFF — local files go up losslessly, while
    tile-backed / remote rasters are rendered to a GeoTIFF. A live progress bar
    tracks server-side tiling until the layer is ready.
- Secure access via the MAPOG external API (API key + HMAC).

## Install

**From ZIP (recommended):**

1. Download/zip the [`mapog/`](mapog/) folder so the archive contains a
   top-level `mapog/` directory.
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**, choose
   the zip, and click *Install Plugin*.

**Manual:** copy the `mapog/` folder into your QGIS plugins directory:

- macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
- Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
- Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`

Then enable **MAPOG** in the Plugins manager.

## Requirements

- QGIS **3.22** or newer.
- A MAPOG account (or a MAPOG API key pair).

## License

See [LICENSE](LICENSE) if present, otherwise contact support@mapog.com.
