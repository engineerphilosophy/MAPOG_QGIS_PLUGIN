"""
MapogClient — pure-Python client for the MAPOG external API.

This module has NO QGIS dependencies so it can be unit-tested standalone
(`python -m pytest`) and reused outside QGIS.

Auth model
----------
1. login(email, password)            -> JWT  (one-time bootstrap credential)
2. bootstrap_api_key(jwt)            -> ensures a pk_/sk_ key pair exists,
                                        reusing an existing one named KEY_NAME
3. all data calls use pk_/sk_ + HMAC; the JWT is discarded after step 2.

Endpoints (confirmed against the backend):
    POST {base}/user-company/login/                 -> {data:{token, user:{company_id}}}
    POST {base}/user-company/signup/                 -> {data:{user_id, email}} (emails an OTP)
    POST {base}/user-company/forget-password/        -> emails a password-reset OTP
    POST {base}/user-company/verify/                 -> {data:{token, user:{company_id}}}
                                                        (sets the password via OTP; used for
                                                         both signup-verify and password reset)
    GET  {base}/v1/external/keys/list/   (JWT)      -> [{name, publishable_key, secret_key}]
    POST {base}/v1/external/keys/create/ (JWT)      -> {publishable_key, secret_key}
    GET  {base}/v1/external/maps/                   (x-api-key)
    GET  {base}/v1/external/layers/?map_id=<b64>    (x-api-key)
    POST {base}/v1/external/layers/export/          (x-api-key + HMAC)  -> file bytes
    POST {base}/v1/external/geometry/insert/        (x-api-key + HMAC)
    POST {base}/v1/external/geometry/attributes/    (x-api-key + HMAC)
    POST {base}/v1/external/geometry/update/        (x-api-key + HMAC)
    POST {base}/v1/external/geometry/delete/        (x-api-key + HMAC)

IDs (map_id / layerid) are base64(str(id)) — the backend base64-decodes them.
HMAC: x-signature = HMAC_SHA256(secret_key, timestamp + body); x-api-key = pk_.
"""

import base64
import hashlib
import hmac
import json
import os
import time

try:
    import requests
except ImportError:  # pragma: no cover - requests ships with QGIS' Python
    requests = None


KEY_NAME = "QGIS Plugin"
# Production API base. The server mounts both /user-company/* and /v1/external/*
# under /api on the live host (e.g. https://story.mapog.com/api/user-company/login/).
DEFAULT_BASE_URL = "https://story.mapog.com/api"
SIGNATURE_HEADER = "x-signature"
TIMEOUT = 60
# Export does ogr2ogr -> zip -> S3 upload server-side, then returns a download
# URL. That can take a while, so give the export POST a much longer budget.
EXPORT_TIMEOUT = 300
# Upload runs ogr2ogr + S3 upload server-side, same as export — give it room.
UPLOAD_TIMEOUT = 300


class MapogError(Exception):
    """Raised for any non-success response from the MAPOG API."""

    def __init__(self, message, http_code=None, payload=None):
        super().__init__(message)
        self.http_code = http_code
        self.payload = payload


class MapogAuthError(MapogError):
    """Raised specifically for auth failures (bad login, invalid/expired key)."""


def encode_id(value):
    """
    Return the base64(str(id)) form every external endpoint expects for
    map/layer ids — but idempotently.

    List endpoints (e.g. /maps/) already return ids in base64 form
    (e.g. "NjU1NQ==" for 6555). A raw integer id (6555) must be encoded.
    Encoding an already-encoded id would double-encode it and the server
    would 400. So: if `value` is already a base64 string that decodes to a
    pure-integer string, pass it through unchanged; otherwise encode it.
    """
    s = str(value)
    try:
        decoded = base64.b64decode(s, validate=True).decode()
        if decoded.isdigit():
            return s  # already encoded — don't re-encode
    except Exception:
        pass
    return base64.b64encode(s.encode()).decode()


class MapogClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, publishable_key=None,
                 secret_key=None, session=None):
        if requests is None:
            raise RuntimeError("The 'requests' library is required.")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.publishable_key = publishable_key
        self.secret_key = secret_key
        self.session = session or requests.Session()
        # The signed-in user object from the most recent login/verify (uname,
        # email, company_id, login_method, …), or None for key-only connects.
        # The plugin reads this to show a profile without re-fetching.
        self.profile = None

    # ---- credentials state -------------------------------------------------

    @property
    def has_keys(self):
        return bool(self.publishable_key and self.secret_key)

    def set_keys(self, publishable_key, secret_key):
        self.publishable_key = publishable_key
        self.secret_key = secret_key

    # ---- low-level helpers -------------------------------------------------

    def _url(self, path):
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _unwrap(resp):
        """Validate the {data,status,http_code,message} envelope, return `data`."""
        try:
            body = resp.json()
        except ValueError:
            if resp.status_code >= 400:
                raise MapogError(resp.text or "Request failed", resp.status_code)
            return resp.content  # binary (e.g. export file)

        status = body.get("status")
        message = body.get("message", "")
        if resp.status_code >= 400 or status == "FAIL":
            exc = MapogAuthError if resp.status_code in (401, 403) else MapogError
            raise exc(message or "Request failed", resp.status_code, body)
        return body.get("data")

    def _auth_headers(self, body_str=None):
        """Build x-api-key (+ HMAC headers when a signed body is supplied)."""
        if not self.publishable_key:
            raise MapogAuthError("Not authenticated — no API key configured.")
        headers = {"x-api-key": self.publishable_key}
        if body_str is not None:
            if not self.secret_key:
                raise MapogAuthError("Secret key required to sign write requests.")
            ts = str(int(time.time()))
            sig = hmac.new(
                self.secret_key.encode(),
                (ts + body_str).encode(),
                hashlib.sha256,
            ).hexdigest()
            headers["x-timestamp"] = ts
            headers[SIGNATURE_HEADER] = sig
            headers["Content-Type"] = "application/json"
        return headers

    def _get(self, path, params=None):
        # The server runs HMAC verification on every non-multipart request,
        # including GET. For a GET the body is empty, so we sign timestamp + "".
        # Query params live in the URL and are NOT part of the signature.
        try:
            resp = self.session.get(
                self._url(path), params=params,
                headers=self._auth_headers(body_str=""), timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise MapogError(f"Network error: {e}")
        return self._unwrap(resp)

    def _signed_post(self, path, payload, timeout=TIMEOUT):
        # Serialize ONCE; sign and transmit the exact same bytes.
        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self._auth_headers(body_str=body_str)
        try:
            resp = self.session.post(
                self._url(path), data=body_str, headers=headers, timeout=timeout,
            )
        except requests.RequestException as e:
            raise MapogError(f"Network error: {e}")
        return self._unwrap(resp)

    # ---- auth / key bootstrap ---------------------------------------------

    def login(self, email, password):
        """POST /user-company/login/ -> returns (jwt, company_id)."""
        resp = self.session.post(
            self._url("/user-company/login/"),
            json={"email": email, "password": password},
            timeout=TIMEOUT,
        )
        data = self._unwrap(resp)
        if not isinstance(data, dict) or not data.get("token"):
            raise MapogAuthError("Login failed: no token returned.")
        company_id = (data.get("user") or {}).get("company_id")
        self._capture_profile(data, company_id)
        return data["token"], company_id

    def _capture_profile(self, data, company_id):
        """Stash the response `user` object (login/verify both return one) so the
        UI can show a profile. company_id is merged in since some responses carry
        it alongside, not inside, the user object."""
        user = dict(data.get("user") or {})
        if company_id is not None and "company_id" not in user:
            user["company_id"] = company_id
        self.profile = user or None

    def signup(self, email, uname="", **extra):
        """POST /user-company/signup/ -> create an account and email a 6-digit
        verification OTP. The account has NO password at this point; finish it
        with verify_otp(email, otp, password).

        Returns the response `data` ({user_id, email}). `extra` may carry the
        optional signup fields the serializer accepts (contact_no, work_role,
        work_name, looking_for, location).
        """
        payload = {"email": email}
        if uname:
            payload["uname"] = uname
        payload.update(extra)
        resp = self.session.post(
            self._url("/user-company/signup/"), json=payload, timeout=TIMEOUT,
        )
        return self._unwrap(resp)

    def request_password_reset(self, email):
        """POST /user-company/forget-password/ -> email a password-reset OTP.

        The backend regenerates the user's OTP and mails it; the same /verify/
        endpoint then sets the new password. Doubles as an OTP resend for an
        in-progress signup (the user already exists after signup()).
        """
        resp = self.session.post(
            self._url("/user-company/forget-password/"),
            json={"email": email}, timeout=TIMEOUT,
        )
        return self._unwrap(resp)

    def verify_otp(self, email, otp, password):
        """POST /user-company/verify/ -> set the account password via the
        emailed OTP and return (jwt, company_id).

        Used to finish BOTH signup and a password reset — the backend sets the
        password and returns a fresh JWT in the same {token, user:{company_id}}
        shape as login(), so the caller can bootstrap API keys immediately.
        """
        resp = self.session.post(
            self._url("/user-company/verify/"),
            json={"email": email, "otp": otp, "password": password},
            timeout=TIMEOUT,
        )
        data = self._unwrap(resp)
        if not isinstance(data, dict) or not data.get("token"):
            raise MapogAuthError("Verification failed: no token returned.")
        company_id = (data.get("user") or {}).get("company_id")
        self._capture_profile(data, company_id)
        return data["token"], company_id

    def verify_and_bootstrap(self, email, otp, password):
        """Convenience: verify the OTP then ensure a key pair. JWT not retained."""
        jwt, _ = self.verify_otp(email, otp, password)
        return self.bootstrap_api_key(jwt)

    def bootstrap_api_key(self, jwt):
        """Reuse or create the '{KEY_NAME}' key via JWT; store pk_/sk_ on self."""
        jwt_headers = {"Authorization": jwt}

        # 1. Try to reuse an existing key with our name.
        resp = self.session.get(
            self._url("/v1/external/keys/list/"),
            headers=jwt_headers, timeout=TIMEOUT,
        )
        keys = self._unwrap(resp) or []
        for k in keys:
            if k.get("name") == KEY_NAME and k.get("publishable_key") and k.get("secret_key"):
                self.set_keys(k["publishable_key"], k["secret_key"])
                return self.publishable_key, self.secret_key

        # 2. None found — create one.
        resp = self.session.post(
            self._url("/v1/external/keys/create/"),
            json={"name": KEY_NAME}, headers=jwt_headers, timeout=TIMEOUT,
        )
        data = self._unwrap(resp)
        if not data or not data.get("secret_key"):
            raise MapogAuthError("Could not provision an API key.")
        self.set_keys(data["publishable_key"], data["secret_key"])
        return self.publishable_key, self.secret_key

    def login_and_bootstrap(self, email, password):
        """Convenience: login then ensure a key pair. JWT is not retained."""
        jwt, _ = self.login(email, password)
        return self.bootstrap_api_key(jwt)

    def verify_keys(self):
        """Cheap call to confirm stored keys still work (used on startup)."""
        self.list_maps()
        return True

    # ---- data: reads -------------------------------------------------------

    def list_maps(self):
        """GET /v1/external/maps/ -> raw `data` (list/dict of the user's maps)."""
        return self._get("/v1/external/maps/")

    def create_map(self, name, description=""):
        """POST /v1/external/maps/create/ -> create a new map (HMAC-signed).

        Only `map_name` is required; the server fills the rest (blank story,
        marked temporary if the company has no paid plan). Returns the created
        map dict, including a base64 `id` and `map_name`.
        """
        payload = {"map_name": name}
        if description:
            payload["map_desc"] = description
        return self._signed_post("/v1/external/maps/create/", payload)

    def set_map_share_status(self, map_id, public):
        """POST /v1/external/maps/share-status/ -> set the map PUBLIC or PRIVATE
        (HMAC-signed). `public` is a bool. A PUBLIC map can be opened by anyone
        via its public link; PRIVATE hides it. Returns the updated map dict
        (carrying the new `share_status`)."""
        payload = {
            "map_id": encode_id(map_id),
            "share_status": "PUBLIC" if public else "PRIVATE",
        }
        if public:
            # Mirror the server's own default for private maps so making a map
            # public doesn't blank out the public-viewer tool config.
            payload["share_map_tools_status"] = {
                "enable_filter_by_geometry": True,
                "enable_tool_box": True,
            }
        return self._signed_post("/v1/external/maps/share-status/", payload)

    def list_layers(self, map_id):
        """GET /v1/external/layers/?map_id=<b64> -> {map_layers:[...], ...}."""
        return self._get("/v1/external/layers/", params={"map_id": encode_id(map_id)})

    def get_annotation_layer(self, map_id):
        """GET /v1/external/layers/annotation/?map_id=<b64> -> annotation layer.

        Annotation layers are excluded from list_layers, so they have their own
        endpoint. Returns the response `data` dict, e.g.
            {"layer_id": "NDU2", "groups": [{"group_name": .., "features": [..]}],
             "features": [..global..], "count": N, "bbox": "{...}"}
        When the map has no annotation layer the server returns
            {"message": "No annotation layer found for this map", "features": []}
        (so the caller should treat an empty `features`/`groups` as "none").
        """
        return self._get(
            "/v1/external/layers/annotation/", params={"map_id": encode_id(map_id)}
        )

    def request_layer_export(self, layer_id, output_extension="geojson", output_crs=4326):
        """
        POST /v1/external/layers/export/ -> returns a presigned S3 download URL
        (string) for a .zip containing the exported file.

        The server exports the layer, zips it, uploads to S3, and returns the
        URL — it does NOT stream the file. `output_crs` is an integer EPSG code
        (the serializer is an IntegerField), e.g. 4326.

        NOTE: subscription-gated for non-GISDATA layers (returns 402). The caller
        should surface MapogError.payload on 402 to the user.
        """
        data = self._signed_post(
            "/v1/external/layers/export/",
            {
                "layerid": encode_id(layer_id),
                "output_extension": output_extension,
                "output_crs": int(output_crs),
            },
            timeout=EXPORT_TIMEOUT,
        )
        # `data` is the download URL string (APIResponse.success(data=download_url)).
        if isinstance(data, dict):
            data = data.get("download_url") or data.get("url") or data
        if not isinstance(data, str) or not data:
            raise MapogError("Export did not return a download URL.")
        return data

    def download_export(self, download_url):
        """GET the presigned S3 URL -> raw .zip bytes (no MAPOG auth needed)."""
        try:
            resp = self.session.get(download_url, timeout=EXPORT_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise MapogError(f"Failed to download exported file: {e}")
        return resp.content

    def export_layer_zip(self, layer_id, output_extension="geojson", output_crs=4326):
        """Convenience: request export + download the resulting .zip bytes."""
        url = self.request_layer_export(layer_id, output_extension, output_crs)
        return self.download_export(url)

    # ---- data: writes ------------------------------------------------------

    def upload_layer(self, map_id, file_paths):
        """
        POST /v1/external/layers/upload/ (multipart) -> create a new layer in a map.

        `file_paths` is a list of local paths; each is sent under the 'files[]'
        field (the backend reads request.FILES.getlist('files[]')). `map_id` is
        sent base64-encoded.

        Multipart requests skip HMAC server-side, so only x-api-key is sent — we
        must NOT set Content-Type here (requests sets the multipart boundary).
        Returns the response `data` (typically {"layers": [...]}).
        """
        if not self.publishable_key:
            raise MapogAuthError("Not authenticated — no API key configured.")
        headers = {"x-api-key": self.publishable_key}
        files = []
        open_handles = []
        try:
            for p in file_paths:
                fh = open(p, "rb")
                open_handles.append(fh)
                files.append(("files[]", (os.path.basename(p), fh, "application/octet-stream")))
            resp = self.session.post(
                self._url("/v1/external/layers/upload/"),
                data={"map_id": encode_id(map_id)},
                files=files,
                headers=headers,
                timeout=UPLOAD_TIMEOUT,
            )
        except requests.RequestException as e:
            raise MapogError(f"Network error: {e}")
        finally:
            for fh in open_handles:
                fh.close()
        return self._unwrap(resp)

    def upload_raster_layer(self, map_id, file_path, name=None):
        """
        POST /v1/external/layers/upload-raster/ (multipart) -> create a new
        raster layer in a map from a GeoTIFF/COG file.

        The raster file is sent under the single 'file' field (the backend's
        RasterLayerCreateSerializer reads `request.FILES['file']`). `map_id` is
        sent base64-encoded; `name` defaults to the file name server-side.

        Like the vector upload, multipart requests skip HMAC server-side, so we
        send only x-api-key and must NOT set Content-Type (requests sets the
        multipart boundary).

        Ingestion is asynchronous: the response `data` is the new raster layer
        in get-map-layers shape with `raster_info.processing_status == "pending"`
        and a null `tile_url`. Load it from the import tab once processing
        completes.
        """
        if not self.publishable_key:
            raise MapogAuthError("Not authenticated — no API key configured.")
        headers = {"x-api-key": self.publishable_key}
        data = {"map_id": encode_id(map_id)}
        if name:
            data["name"] = name
        fh = open(file_path, "rb")
        try:
            files = [("file", (os.path.basename(file_path), fh, "application/octet-stream"))]
            resp = self.session.post(
                self._url("/v1/external/layers/upload-raster/"),
                data=data,
                files=files,
                headers=headers,
                timeout=UPLOAD_TIMEOUT,
            )
        except requests.RequestException as e:
            raise MapogError(f"Network error: {e}")
        finally:
            fh.close()
        return self._unwrap(resp)

    def update_layer_style(self, layer_id, style_attributes, style_type="Basic"):
        """
        POST /v1/external/layers/style/ -> set a layer's applied style.

        `style_attributes` is the MAPOG style dict, e.g.
            {"color": "#b0232a", "opacity": 0.6, "stroke_color": "#000000",
             "stroke_size": 1, "line_type": "solid", "dash": None, "space": None}
        Sets is_applied=1 so it replaces the auto-created default (#005bad blue).
        This is a JSON write, so it is HMAC-signed.
        """
        return self._signed_post(
            "/v1/external/layers/style/",
            {
                "type": "LAYER",
                "layerid": encode_id(layer_id),
                "style_type": style_type,
                "style_attributes": style_attributes,
                "is_applied": 1,
            },
        )

    def insert_features(self, layer_id, features):
        """
        POST /v1/external/geometry/insert/

        `features` is a list of {type, coordinates, attribute:{...}} dicts
        (GeoJSON geometry + optional attribute map). Returns {count, gids}.
        """
        return self._signed_post(
            "/v1/external/geometry/insert/",
            {"layerid": encode_id(layer_id), "geometry": features},
        )

    def update_attributes(self, layer_id, gid, attribute):
        """POST /v1/external/geometry/attributes/ -> update one row's attributes."""
        return self._signed_post(
            "/v1/external/geometry/attributes/",
            {"layerid": encode_id(layer_id), "gid": gid, "attribute": attribute},
        )

    def update_geometry(self, layer_id, gid, geometry):
        """POST /v1/external/geometry/update/ -> update one row's geometry."""
        return self._signed_post(
            "/v1/external/geometry/update/",
            {"layerid": encode_id(layer_id), "gid": gid, "geometry": geometry},
        )

    def delete_features(self, layer_id, gids):
        """POST /v1/external/geometry/delete/ -> delete rows by gid."""
        return self._signed_post(
            "/v1/external/geometry/delete/",
            {"layerid": encode_id(layer_id), "gid": gids},
        )

    # ---- GISDATA catalog ---------------------------------------------------
    # NOTE: gisdata ids (gisdata_country_id, gisdata_layer_id) are PLAIN INTEGERS
    # server-side — do NOT base64-encode them. Only `mapid` is base64.

    def list_gisdata_countries(self):
        """GET /v1/external/gisdata/countries/ -> list of {gisdata_country_id, country_name}."""
        return self._get("/v1/external/gisdata/countries/")

    def list_gisdata_admin_layers(self, country_id):
        """GET /v1/external/gisdata/admin-layers/ -> list of admin-level layers for a country."""
        return self._get(
            "/v1/external/gisdata/admin-layers/",
            params={"gisdata_country_id": country_id},
        )

    def list_gisdata_other_layers(self, country_id):
        """
        GET /v1/external/gisdata/other-layers/ -> list of OSM/other layers for a country.

        The internal response wraps the list as {"layers": [...]}; return that list.
        """
        data = self._get(
            "/v1/external/gisdata/other-layers/",
            params={"gisdata_country_id": country_id},
        )
        if isinstance(data, dict):
            return data.get("layers") or []
        return data or []

    def add_gisdata_admin_layer(self, gisdata_layer_id, gisdata_country_id, map_id,
                                layer_type="ALL"):
        """
        POST /v1/external/gisdata/add-admin-layer/ -> clone an admin level into the map.
        Returns a list of created layer objects (each with a base64 'layerid').
        """
        return self._signed_post(
            "/v1/external/gisdata/add-admin-layer/",
            {
                "gisdata_layer_id": gisdata_layer_id,
                "gisdata_country_id": gisdata_country_id,
                "mapid": encode_id(map_id),
                "layer_type": layer_type,
                "admin_filter": {},
                "bounding_coordinates": "",
            },
            timeout=EXPORT_TIMEOUT,
        )

    def add_gisdata_other_layer(self, gisdata_layer_id, gisdata_country_id, map_id,
                                layer_type="ALL"):
        """
        POST /v1/external/gisdata/add-other-layer/ -> clone an OSM/other layer into the map.
        With layer_type 'ALL' the server may create up to 3 layers (POINT/LINE/POLYGON).
        Returns a list of created layer objects (each with a base64 'layerid').
        """
        return self._signed_post(
            "/v1/external/gisdata/add-other-layer/",
            {
                "gisdata_layer_id": gisdata_layer_id,
                "gisdata_country_id": gisdata_country_id,
                "mapid": encode_id(map_id),
                "layer_type": layer_type,
                "admin_filter": {},
                "bounding_coordinates": "",
            },
            timeout=EXPORT_TIMEOUT,
        )
