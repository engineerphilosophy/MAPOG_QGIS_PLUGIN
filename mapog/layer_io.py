"""
Conversion helpers between MAPOG export output and QGIS layers.

The MAPOG export endpoint returns a presigned URL to a **.zip** that contains
the exported file (e.g. a .geojson, or the parts of a .shp) plus a credits
text file. We download the zip, extract it to a temp dir, find the first
loadable vector file, and load it via the OGR provider.
"""

import os
import tempfile
import zipfile

from qgis.core import (
    QgsVectorLayer, QgsRasterLayer, QgsProject, QgsRectangle,
    QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)

# Extensions QGIS/OGR can open directly as a vector layer.
VECTOR_EXTS = (".geojson", ".json", ".gpkg", ".kml", ".gml", ".shp", ".gpx",
               ".tab", ".sqlite", ".mif")

# Raster file extensions MAPOG's raster ingestion accepts, used for the
# upload "fast path" (upload a local GDAL file as-is rather than re-rendering).
RASTER_EXTS = (".tif", ".tiff", ".geotiff", ".img", ".vrt", ".jp2", ".ecw",
               ".asc", ".grib", ".grib2", ".nc", ".hdf", ".hdf5",
               ".png", ".jpg", ".jpeg")


def zip_bytes_to_layer(content, layer_name):
    """
    Extract export .zip bytes to a temp dir, load the first vector file found,
    and return a QgsVectorLayer. Raises ValueError if nothing loadable is found.
    """
    tmp_dir = tempfile.mkdtemp(prefix="mapog_")
    zip_path = os.path.join(tmp_dir, "export.zip")
    with open(zip_path, "wb") as fh:
        fh.write(content if isinstance(content, bytes) else content.encode("utf-8"))

    extract_dir = os.path.join(tmp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
    except zipfile.BadZipFile:
        raise ValueError(
            "Downloaded export was not a valid zip (it may be an error page "
            "or an unsupported format)."
        )

    vector_path = _find_vector_file(extract_dir)
    if not vector_path:
        found = [f for _, _, fs in os.walk(extract_dir) for f in fs]
        raise ValueError(
            f"No loadable vector file in the export. Contents: {found}"
        )

    layer = QgsVectorLayer(vector_path, layer_name, "ogr")
    if not layer.isValid():
        raise ValueError(f"Could not load '{os.path.basename(vector_path)}' as a vector layer.")
    return layer


def _find_vector_file(root):
    """Walk `root` and return the path of the first file with a vector extension."""
    # Prefer the more self-contained formats first.
    candidates = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in VECTOR_EXTS:
                candidates.append(os.path.join(dirpath, name))
    if not candidates:
        return None
    # Sort by preference: geojson/gpkg/shp ahead of others.
    pref = {".geojson": 0, ".json": 0, ".gpkg": 1, ".shp": 2}
    candidates.sort(key=lambda p: pref.get(os.path.splitext(p)[1].lower(), 9))
    return candidates[0]


def xyz_url_to_raster_layer(tile_url, layer_name, zmin=0, zmax=22):
    """
    Build a QGIS XYZ raster layer from a MAPOG raster tile-URL template.

    `tile_url` is a public template like
        https://<tile-endpoint>/tiles/<uuid>/{z}/{x}/{y}.png?bidx=..&colormap=..

    The datasource URI is built with QgsDataSourceUri — the same code path QGIS
    uses for a hand-made "New XYZ Connection". Hand-rolling
    `type=xyz&url=<quote(url, safe="")>&...` looks valid (the layer even loads)
    but over-encodes the URL (`/`, `:`, `?` become %2F/%3A/%3F); the XYZ
    provider does not fully reverse that, so every tile request 404s and the
    layer renders blank. QgsDataSourceUri.setParam stores the raw value and
    encodes it the way the provider expects, so the `{z}/{x}/{y}` template and
    the `?company_id=..&bidx=..&rescale=..&colormap=..` query both survive.
    """
    from qgis.core import QgsDataSourceUri

    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", tile_url)
    uri.setParam("zmin", str(zmin))
    uri.setParam("zmax", str(zmax))
    src = bytes(uri.encodedUri()).decode("ascii")

    layer = QgsRasterLayer(src, layer_name, "wms")
    if not layer.isValid():
        raise ValueError(f"Could not load raster '{layer_name}' from its tile URL.")
    return layer


def vector_layer_to_geojson(layer, dest_dir=None):
    """
    Write a QgsVectorLayer to a temp .geojson reprojected to EPSG:4326 and return
    the file path. Raises ValueError on write failure.

    EPSG:4326 is what MAPOG stores layers in (SRID 4326), so we reproject on the
    way out regardless of the layer's source CRS.
    """
    dest_dir = dest_dir or tempfile.mkdtemp(prefix="mapog_upload_")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in layer.name()) or "layer"
    out_path = os.path.join(dest_dir, f"{safe_name}.geojson")

    target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    ctx = QgsProject.instance().transformContext()

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    # Reproject to EPSG:4326 on write when the source layer is in another CRS.
    if layer.crs().isValid() and layer.crs() != target_crs:
        options.ct = QgsCoordinateTransform(layer.crs(), target_crs, ctx)
    # writeAsVectorFormatV3 is the current (QGIS 3.20+) signature; min QGIS is 3.22.
    # Returns a tuple whose first element is the error code (NoError on success).
    err = QgsVectorFileWriter.writeAsVectorFormatV3(layer, out_path, ctx, options)
    if err[0] != QgsVectorFileWriter.NoError:
        raise ValueError(f"Failed to export '{layer.name()}' to GeoJSON: {err[1]}")
    return out_path


# Cap the longest side of a rendered raster (px). Keeps tile-backed / huge
# rasters to a sane upload size; local files take the lossless fast path instead.
MAX_RASTER_DIM = 8192


def raster_layer_to_geotiff(layer, dest_dir=None, iface=None):
    """
    Produce a local GeoTIFF path for a QgsRasterLayer, ready to upload to MAPOG.

    Fast path: when the layer is backed by a local GDAL raster file, return that
    path directly — lossless and instant.

    Otherwise (remote /vsicurl COGs, WMS/WCS/XYZ tile layers — anything without a
    local file) render the layer's data to a temp GeoTIFF via QgsRasterFileWriter.
    QGIS fetches the tiles/pixels and assembles them; the longest side is capped
    at MAX_RASTER_DIM so a tile-backed layer doesn't produce a gigantic upload.

    The raster is NOT reprojected here: MAPOG's raster ingestion converts the
    upload to a Cloud-Optimized GeoTIFF and reprojects from the file's CRS, so
    shipping native pixels keeps the upload lossless.

    `iface` (optional) is used only as a fallback extent source for layers that
    report no finite extent of their own (e.g. a global XYZ basemap) — we then
    capture the current map canvas view.

    Raises ValueError if the layer can't be turned into a file.
    """
    provider = layer.dataProvider()
    provider_name = provider.name() if provider else ""

    # Fast path: a local GDAL-backed file we can upload as-is. The source may
    # carry GDAL subdataset/option suffixes after a '|', so split those off.
    source = layer.source() or ""
    path_part = source.split("|", 1)[0]
    if provider_name == "gdal" and os.path.isfile(path_part):
        ext = os.path.splitext(path_part)[1].lower()
        if ext in RASTER_EXTS:
            return path_part

    # Everything else: render to a temp GeoTIFF.
    return _write_raster_to_geotiff(layer, dest_dir, iface=iface)


def _write_raster_to_geotiff(layer, dest_dir=None, iface=None):
    """Render a QgsRasterLayer's data to a temp GeoTIFF and return the path.

    Handles both file-backed and tile-backed (WMS/WCS/XYZ) layers. Output pixel
    dimensions come from the layer's native size when known and reasonable;
    otherwise they're derived from the export extent with the longest side
    capped at MAX_RASTER_DIM (aspect ratio preserved)."""
    from qgis.core import QgsRasterFileWriter, QgsRasterPipe

    provider = layer.dataProvider()
    if provider is None:
        raise ValueError(f"Raster '{layer.name()}' has no data provider to export.")

    # Export extent + CRS: prefer the layer's own; fall back to the canvas view
    # for layers with no finite extent (e.g. a global XYZ basemap).
    extent = layer.extent()
    crs = layer.crs()
    if (extent is None or extent.isEmpty()) and iface is not None:
        canvas = iface.mapCanvas()
        extent = canvas.extent()
        crs = canvas.mapSettings().destinationCrs()
    if extent is None or extent.isEmpty() or extent.width() <= 0 or extent.height() <= 0:
        raise ValueError(
            f"Raster '{layer.name()}' has no usable extent to export — open it "
            "with a defined footprint, or zoom to the area of interest."
        )

    # Pixel dimensions: use the layer's native size when known and within the
    # cap; otherwise derive from the extent, capping the longest side.
    cols = layer.width()
    rows = layer.height()
    if not (0 < cols <= MAX_RASTER_DIM and 0 < rows <= MAX_RASTER_DIM):
        w, h = extent.width(), extent.height()
        if w >= h:
            cols = MAX_RASTER_DIM
            rows = max(1, int(round(MAX_RASTER_DIM * h / w)))
        else:
            rows = MAX_RASTER_DIM
            cols = max(1, int(round(MAX_RASTER_DIM * w / h)))

    dest_dir = dest_dir or tempfile.mkdtemp(prefix="mapog_raster_")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in layer.name()) or "raster"
    out_path = os.path.join(dest_dir, f"{safe_name}.tif")

    pipe = QgsRasterPipe()
    if not pipe.set(provider.clone()):
        raise ValueError(f"Could not build a render pipe for raster '{layer.name()}'.")

    writer = QgsRasterFileWriter(out_path)
    writer.setOutputFormat("GTiff")
    ctx = QgsProject.instance().transformContext()
    err = writer.writeRaster(pipe, cols, rows, extent, crs, ctx)
    if err != QgsRasterFileWriter.NoError:
        raise ValueError(
            f"Failed to export raster '{layer.name()}' to GeoTIFF (error code {err})."
        )
    return out_path


def basic_style_from_layer(layer):
    """
    Read a QgsVectorLayer's symbology and return a MAPOG "Basic" style dict
    (fill color, opacity, stroke color/size) so the uploaded layer keeps the
    same look. Returns None if no single representative symbol can be read
    (e.g. an unsupported renderer) — the caller then leaves MAPOG's default.

    Keys match the MAPOG style API: color, opacity, stroke_color, stroke_size,
    line_type, dash, space.
    """
    try:
        renderer = layer.renderer()
        if renderer is None:
            return None

        # Single-symbol renderers expose .symbol() directly; categorized/
        # graduated do not — fall back to the first symbol if we can get it.
        symbol = renderer.symbol() if hasattr(renderer, "symbol") else None
        if symbol is None and hasattr(renderer, "symbols"):
            try:
                from qgis.core import QgsRenderContext
                symbols = renderer.symbols(QgsRenderContext())
                symbol = symbols[0] if symbols else None
            except Exception:
                symbol = None
        if symbol is None:
            return None

        fill = symbol.color()
        opacity = float(symbol.opacity()) if hasattr(symbol, "opacity") else 1.0

        stroke_color = "#000000"
        stroke_size = 1
        try:
            sl = symbol.symbolLayer(0)
            if sl is not None:
                if hasattr(sl, "strokeColor"):
                    c = sl.strokeColor()
                    if c is not None and c.isValid():
                        stroke_color = c.name()
                if hasattr(sl, "strokeWidth"):
                    w = sl.strokeWidth()
                    if w and w > 0:
                        stroke_size = max(1, int(round(w)))
        except Exception:
            pass

        return {
            "color": fill.name(),                 # QColor.name() -> "#rrggbb"
            "opacity": round(opacity, 3),
            "stroke_color": stroke_color,
            "stroke_size": stroke_size,
            "line_type": "solid",
            "dash": None,
            "space": None,
        }
    except Exception:
        return None


def vector_style_from_layer(layer):
    """Return (style_type, style_attributes) replicating the QGIS layer's
    symbology for the MAPOG style API.

    - A categorized renderer (per-value colors/sizes) -> ("CATEGORY", {...}),
      preserving each category's matched value, color and (for points) marker
      size, so MAPOG renders the same palette instead of one flat color.
    - Anything else -> ("Basic", {...}) via basic_style_from_layer.

    Returns (None, None) when no usable style can be read."""
    try:
        from qgis.core import QgsCategorizedSymbolRenderer
        renderer = layer.renderer()
    except Exception:
        renderer = None

    if isinstance(renderer, QgsCategorizedSymbolRenderer):
        attrs = _category_style_from_renderer(layer, renderer)
        if attrs and attrs.get("attribute_parts"):
            return "CATEGORY", attrs

    basic = basic_style_from_layer(layer)
    return ("Basic", basic) if basic else (None, None)


def _is_point_layer(layer):
    try:
        from qgis.core import QgsWkbTypes
        return QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PointGeometry
    except Exception:
        return False


def _len_to_px(value, unit):
    """Convert a QGIS render length (mm/pt/in/px) to screen pixels at 96 DPI."""
    try:
        from qgis.core import QgsUnitTypes
        if unit == QgsUnitTypes.RenderPixels:
            return value
        if unit == QgsUnitTypes.RenderPoints:
            return value * 96.0 / 72.0
        if unit == QgsUnitTypes.RenderInches:
            return value * 96.0
        if unit == QgsUnitTypes.RenderMillimeters:
            return value * 96.0 / 25.4
    except Exception:
        pass
    return value * 96.0 / 25.4  # assume millimeters (QGIS default)


def _marker_radius_px(symbol):
    """Approximate a MAPOG point radius (px) from a QGIS marker symbol.
    QGIS reports the marker *diameter* in its size unit; MAPOG's radius is half
    of the on-screen diameter."""
    try:
        if not hasattr(symbol, "size"):
            return 5
        diameter_px = _len_to_px(float(symbol.size()), symbol.sizeUnit())
        return max(1, int(round(diameter_px / 2.0)))
    except Exception:
        return 5


def _marker_shape(symbol):
    """Map a QGIS simple-marker shape to a MAPOG pointStyleType (best-effort)."""
    try:
        sl = symbol.symbolLayer(0)
        if hasattr(sl, "shape"):
            from qgis.core import QgsSimpleMarkerSymbolLayerBase as B
            mapping = {
                B.Circle: "circle", B.Square: "square", B.Diamond: "diamond",
                B.Triangle: "triangle", B.Star: "star", B.Pentagon: "pentagon",
            }
            return mapping.get(sl.shape(), "circle")
    except Exception:
        pass
    return "circle"


def _symbol_stroke(symbol):
    """Return (stroke_color hex or None, stroke_size px) for a symbol's outline."""
    try:
        sl = symbol.symbolLayer(0)
        if sl is None:
            return None, 1
        color = None
        if hasattr(sl, "strokeColor"):
            c = sl.strokeColor()
            if c is not None and c.isValid():
                color = c.name()
        size = 1
        if hasattr(sl, "strokeWidth"):
            w = float(sl.strokeWidth())
            unit = sl.strokeWidthUnit() if hasattr(sl, "strokeWidthUnit") else None
            size = max(0, int(round(_len_to_px(w, unit)))) if w > 0 else 0
        return color, size
    except Exception:
        return None, 1


def _category_style_from_renderer(layer, renderer):
    """Build MAPOG CATEGORY style_attributes from a QgsCategorizedSymbolRenderer."""
    is_point = _is_point_layer(layer)

    parts = []
    default_radius = 5
    stroke_color = "#ffffff"
    stroke_size = 1
    point_shape = "circle"
    captured_global = False

    for cat in renderer.categories():
        sym = cat.symbol()
        if sym is None:
            continue
        color = sym.color().name()
        radius = _marker_radius_px(sym) if is_point else None

        # QGIS' "all other values" bucket has an empty/null match value -> map it
        # to MAPOG's reserved default key.
        val = cat.value()
        if val is None or (isinstance(val, str) and val == ""):
            values = ["__default__"]
        elif isinstance(val, (list, tuple)):
            # Multi-value category: one MAPOG part per matched value, same look.
            values = [v for v in val]
        else:
            values = [val]

        for v in values:
            part = {"value": v, "color": color}
            if radius is not None:
                part["radius"] = radius
            parts.append(part)

        if not captured_global:
            captured_global = True
            sc, sw = _symbol_stroke(sym)
            if sc:
                stroke_color = sc
            stroke_size = sw
            if is_point:
                point_shape = _marker_shape(sym)
                if radius is not None:
                    default_radius = radius

    if not parts:
        return None

    # Guarantee a default bucket so unmatched rows still render (not invisible).
    if not any(p["value"] == "__default__" for p in parts):
        d = {"value": "__default__", "color": "#808080"}
        if is_point:
            d["radius"] = default_radius
        parts.append(d)

    attrs = {
        "attribute_name": renderer.classAttribute(),
        "attribute_parts": parts,
        "opacity": round(float(layer.opacity()), 3) if layer.opacity() else 1,
        "border_opacity": 1,
        "stroke_size": stroke_size,
        "stroke_color": stroke_color,
        "overlap": True,
    }
    if is_point:
        attrs["radius"] = default_radius
        attrs["pointStyleType"] = point_shape
    return attrs


def add_layer_to_project(layer):
    QgsProject.instance().addMapLayer(layer)
    return layer


def zoom_canvas_to_bbox_4326(iface, bbox, buffer_frac=0.05):
    """Pan/zoom the map canvas to an EPSG:4326 bbox [minx, miny, maxx, maxy].

    XYZ raster layers report a global extent, so QGIS's "Zoom to Layer" is
    useless for them — a small COG footprint stays an invisible speck at world
    zoom. After loading such a layer we explicitly drive the canvas to the
    layer's real bounds (reprojected to the canvas CRS) so the data lands in
    view. Returns True if the canvas was moved.
    """
    if not iface or not bbox or len(bbox) != 4:
        return False
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return False

    rect = QgsRectangle(minx, miny, maxx, maxy)
    canvas = iface.mapCanvas()
    src = QgsCoordinateReferenceSystem("EPSG:4326")
    dst = canvas.mapSettings().destinationCrs()
    if dst.isValid() and dst != src:
        try:
            ct = QgsCoordinateTransform(src, dst, QgsProject.instance())
            rect = ct.transformBoundingBox(rect)
        except Exception:
            return False

    # Pad a little so the footprint isn't edge-to-edge in the viewport.
    rect.scale(1.0 + buffer_frac)
    canvas.setExtent(rect)
    canvas.refresh()
    return True
