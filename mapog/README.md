# MAPOG QGIS Plugin

Connect QGIS to [MAPOG](https://mapog.com) — log in, browse your maps and
layers, load them into QGIS, upload QGIS layers back to MAPOG, and (next
phase) push per-feature edits back. All traffic
goes through MAPOG's **external API** (`/v1/external/*`) using **API key +
HMAC** auth, so every write runs through the real MAPOG views and triggers
the correct tile-cache invalidation, bbox fan-out, and dump-table backups.

## How auth works

You can **log in**, **create an account**, **reset a password**, or **sign in
with Google** — all from the panel's sign-in screen.

1. You log in with your MAPOG email + password (or paste an API key).
2. The plugin exchanges the login for a JWT, then calls
   `/v1/external/keys/list|create/` to reuse or provision a key named
   `"QGIS Plugin"`. The JWT is then discarded.
3. The `pk_`/`sk_` key pair is stored (secret in QGIS's encrypted
   `QgsAuthManager` when a master password is set; otherwise QgsSettings with
   a warning). Subsequent launches reconnect automatically.

**Create account** (`Create account` link): enter your name + email →
`/user-company/signup/` emails a 6-digit code → enter the code and choose a
password → `/user-company/verify/` sets the password and returns a JWT, which
is then bootstrapped into an API key exactly like a normal login.

**Forgot password** (`Forgot password?` link): enter your email →
`/user-company/forget-password/` emails a code → the same OTP screen
(`/user-company/verify/`) sets a new password and connects you. The `Resend
code` button re-mails the OTP for both flows.

**Google / SSO accounts** have no password — sign in on the MAPOG web app,
then connect QGIS via the **"Paste API key"** tab (generate a key in
MAPOG → Settings → API keys).

## Install (for development)

Symlink or copy `qgis_plugin/mapog` into your QGIS plugins directory:

```bash
# macOS
ln -s "$(pwd)/qgis_plugin/mapog" \
  "$HOME/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/mapog"

# Linux
ln -s "$(pwd)/qgis_plugin/mapog" \
  "$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/mapog"
```

Then in QGIS: **Plugins → Manage and Install Plugins → Installed → enable "MAPOG"**.
Use the [Plugin Reloader](https://plugins.qgis.org/plugins/plugin_reloader/)
plugin to reload after edits without restarting QGIS.

To package for distribution: zip the `mapog/` folder and install via
**Plugins → Install from ZIP**.

## Files

| File | Role |
|------|------|
| `mapog_client.py` | Pure-Python API client (login, key bootstrap, HMAC signing, reads/writes). No QGIS deps. |
| `settings_store.py` | Credential storage (`QgsAuthManager` / `QgsSettings`). |
| `layer_io.py` | Export `.zip` → extract → `QgsVectorLayer`; and `QgsVectorLayer` → GeoJSON for upload. |
| `mapog_dockwidget.py` | The dock UI (login + browse/load + upload). |
| `mapog_plugin.py` | Plugin entry point (toolbar/menu). |
| `__init__.py` | `classFactory` hook. |

## Status — v1 (read path)

Done:
- Login + automatic API-key provisioning, with paste-key fallback.
- List maps → list layers → load a layer into QGIS. **Vector** layers load via
  the export endpoint (zip → extract). **Raster** layers (shown as `[Raster] …`,
  i.e. `RasterLayer` entries) load directly from their public XYZ `tile_url`
  template as a `QgsRasterLayer` — no export, no S3, no download gate.
- **GIS Data:** pick a country → add an admin-boundary level (ADM0/1/2…) or an
  OSM/"other" layer to the selected map, then auto-load it into QGIS. Uses new
  external proxy endpoints `/v1/external/gisdata/{countries,admin-layers,
  other-layers,add-admin-layer,add-other-layer}/`. gisdata ids are sent as plain
  integers; only `mapid` is base64. Adding is data-limited (10k free / 100k paid)
  but not subject to the export download-cooldown; the auto-load afterwards still
  goes through the gated export path.
- **Upload (QGIS → MAPOG):** pick a target map, then tick one or more project
  vector layers (with a **Select all** toggle). Each ticked layer is exported to
  GeoJSON (reprojected to EPSG:4326) and POSTed to `/v1/external/layers/upload/`
  as multipart `files[]` + base64 `map_id` — one request per layer — creating a
  new layer in the map. Multipart requests skip HMAC server-side, so only
  `x-api-key` is sent (`MapogClient.upload_layer`). After upload, the layer's
  single-symbol **fill/stroke color** is read off its QGIS renderer
  (`layer_io.basic_style_from_layer`) and POSTed to `/v1/external/layers/style/`
  (`MapogClient.update_layer_style`) so the new layer keeps its color instead of
  MAPOG's default blue (`#005bad`). Style copy is best-effort — it won't fail the
  upload. Round-trips with "Add existing layer".
- **Share & links:** after a successful upload (and when browsing an existing
  map's layers), a **Share & links** panel shows an **Open in MAPOG** deep link
  to the map (`{web_origin}/maps/{base64_map_id}`, where `web_origin` is the
  server URL with a trailing `/api` stripped), with **Copy**/**Open** buttons.
  Others can open it only if the map is set **Public** in MAPOG. For **raster**
  layers, the panel also shows the **XYZ tile URL** (`raster_info.tile_url`),
  which can be added in QGIS via *Layer → Add Layer → XYZ*; for rasters just
  uploaded, it appears once server-side processing completes.
  **WMS/WFS is not offered** — MAPOG has no OGC service — and **vector layers
  have no tile link** in the external API.
- HMAC-signed per-feature write methods implemented in the client
  (`insert_features`, `update_attributes`, `update_geometry`, `delete_features`).

## Known limitations / next steps

- **Network runs on the GUI thread.** Move `export_layer` and writes to a
  `QThread`/`QgsTask` worker so the UI doesn't freeze on large layers.
- **Write-back is in the client but not yet wired to QGIS edit sessions.**
  Next: connect `committedFeaturesAdded` / `committedGeometriesChanges` /
  `committedAttributeValuesChanges` / `committedFeaturesRemoved` signals on a
  loaded layer to the corresponding client methods, keying on a stashed MAPOG
  `gid` attribute.
- **Reads use the export endpoint**, which is subscription-gated for
  non-GISDATA layers (402 surfaced to the user). The datatable endpoint
  returns attributes only (no geometry), so it isn't used for loading.
- **Export flow:** `POST /layers/export/` does ogr2ogr → zip → **S3 upload**
  server-side and returns a presigned `.zip` URL (it does NOT stream the file).
  The plugin downloads the zip, extracts it, and loads the vector file. This
  means the **local backend must have working AWS/S3 credentials** for export
  to succeed — a slow or misconfigured S3 upload shows up as a request timeout.
  `output_crs` is an integer EPSG code (e.g. `4326`), not `"EPSG:4326"`.
- **Non-GeoJSON formats** download but aren't auto-added to the canvas yet.
- Add a real `icon.png` (a 24–32px PNG); the plugin falls back to a blank
  icon if absent.

## Contract notes (for maintainers)

- IDs (`map_id`, `layerid`) are sent as `base64(str(id))`. List endpoints
  (e.g. `/maps/`) already return ids in base64 form, so `encode_id()` is
  idempotent — it passes an already-encoded id through and only encodes raw
  integers (avoids double-encoding → 400).
- **Every** non-multipart request is HMAC-signed — including GET. The server
  verifies `x-signature = HMAC_SHA256(secret_key, timestamp + body)` over the
  **exact** transmitted bytes (`body` is empty for GET, so it signs just the
  timestamp). `x-api-key = pk_`. Query params are in the URL and are NOT part
  of the signature.
- File uploads (multipart) skip HMAC server-side, so `upload_layer` sends only
  `x-api-key` and lets `requests` set the multipart boundary (never set
  `Content-Type` manually for it).
- Response envelope: `{data, status: PASS|FAIL, http_code, message}`.
