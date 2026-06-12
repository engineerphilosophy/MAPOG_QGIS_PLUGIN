"""
Credential / config storage for the MAPOG plugin.

Stores base_url + publishable_key in QgsSettings, and the secret_key in the
encrypted QgsAuthManager DB when available (falls back to QgsSettings with a
warning if the auth DB has no master password configured).

Kept separate from mapog_client so the client stays QGIS-free and testable.
"""

from qgis.core import QgsApplication, QgsSettings, QgsAuthMethodConfig, Qgis, QgsMessageLog

_GROUP = "mapog"
_AUTH_NAME = "MAPOG QGIS Plugin"


def _settings():
    s = QgsSettings()
    s.beginGroup(_GROUP)
    return s


def save_config(base_url, publishable_key, secret_key):
    s = _settings()
    s.setValue("base_url", base_url)
    s.setValue("publishable_key", publishable_key or "")
    s.endGroup()
    _store_secret(secret_key)


def load_config():
    """Returns (base_url, publishable_key, secret_key) — any may be None/empty."""
    s = _settings()
    base_url = s.value("base_url", "")
    pk = s.value("publishable_key", "")
    s.endGroup()
    return base_url, pk, _load_secret()


def clear_config():
    s = _settings()
    s.remove("")  # remove everything under the mapog group (incl. profile/*)
    s.endGroup()
    _clear_secret()


# ---- non-secret profile (for the Profile tab) -----------------------------
# The signed-in user object is captured at login/verify and persisted here so
# the profile survives restarts (where we only restore keys, never re-login).
# Cleared by clear_config() along with the rest of the group.

_PROFILE_KEYS = ("uname", "email")


def save_profile(profile):
    if not profile:
        return
    s = _settings()
    for k in _PROFILE_KEYS:
        v = profile.get(k)
        if v not in (None, ""):
            s.setValue(f"profile/{k}", str(v))
    s.endGroup()


def load_profile():
    """Returns a dict of the persisted profile fields (may be empty)."""
    s = _settings()
    prof = {}
    for k in _PROFILE_KEYS:
        v = s.value(f"profile/{k}", "")
        if v:
            prof[k] = v
    s.endGroup()
    return prof


# ---- secret_key via QgsAuthManager (encrypted) ----------------------------

def _auth_manager():
    return QgsApplication.authManager()


def _auth_config_id():
    s = _settings()
    cid = s.value("auth_config_id", "")
    s.endGroup()
    return cid


def _store_secret(secret_key):
    if not secret_key:
        return
    am = _auth_manager()
    # If the auth DB isn't unlocked (no master password), fall back to QgsSettings.
    if am.masterPasswordIsSet() is False and not am.setMasterPassword(verify=True):
        QgsMessageLog.logMessage(
            "Auth DB master password not set — storing secret in QgsSettings (less secure).",
            "MAPOG", Qgis.Warning,
        )
        s = _settings()
        s.setValue("secret_key_fallback", secret_key)
        s.endGroup()
        return

    config = QgsAuthMethodConfig()
    config.setName(_AUTH_NAME)
    config.setMethod("Basic")
    config.setConfig("password", secret_key)
    existing = _auth_config_id()
    if existing:
        config.setId(existing)
        am.updateAuthenticationConfig(config)
    else:
        am.storeAuthenticationConfig(config)
        s = _settings()
        s.setValue("auth_config_id", config.id())
        s.endGroup()


def _load_secret():
    cid = _auth_config_id()
    if cid:
        am = _auth_manager()
        config = QgsAuthMethodConfig()
        if am.loadAuthenticationConfig(cid, config, full=True):
            secret = config.config("password")
            if secret:
                return secret
    s = _settings()
    secret = s.value("secret_key_fallback", "")
    s.endGroup()
    return secret


def _clear_secret():
    cid = _auth_config_id()
    if cid:
        _auth_manager().removeAuthenticationConfig(cid)
    s = _settings()
    s.remove("auth_config_id")
    s.remove("secret_key_fallback")
    s.endGroup()
