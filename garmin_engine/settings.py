"""Server-side persistence for the LetsRunFaster push settings.

The browser UI already kept the ingest URL + API key in localStorage, but
that never reached the CLI or the RPC sidecar — every export needed
``--remote-url`` / ``--remote-key`` flags again. This module stores the same
pair in ``config.json`` inside the app state directory (``LRF_INGESTOR_HOME``
or ``~/.garmin-ingestor``; file mode 0600 because the API key is a bearer
secret) so the web UI, CLI, and RPC server all default to it.

It also owns the two import choices added with non-block pushes:

``remote_mode``
    ``block``    only attach runs to a matching training-block day;
    ``auto``     a block day when the date matches, otherwise a standalone run;
    ``activity`` always a standalone run (existing block days stay untouched).
    A fresh install starts on ``auto`` because that is the feature's point,
    while a config saved before modes existed keeps the old ``block`` behavior.
    The resolved mode is frozen into the file on the first write, so a later
    default change can never silently flip an existing install.

``import_private``
    ``true`` sends ``is_private=1`` so runs outside a block are visible only on
    the owner's profile. Off by default (the server's public default).

Only the key *prefix* (``lrf_...`` first 12 chars, same as the site's profile
page shows) is ever returned to UI callers — never the full key.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths

DEFAULT_REMOTE_URL = "https://www.letsrunfaster.com/api/activities/import"

REMOTE_MODES = ("block", "auto", "activity")
DEFAULT_REMOTE_MODE = "auto"
LEGACY_REMOTE_MODE = "block"


def canonicalize_url(url: str) -> str:
    """Rewrite the bare-apex site host to www.

    The apex (https://letsrunfaster.com) 301-redirects to www, and HTTP
    clients drop the Authorization header when following a cross-host
    redirect — so pushing to the apex always surfaces as a 401 on the site.
    Canonicalizing here (one place, covering UI/CLI/RPC) self-heals configs
    saved before this fix. Local/dev hosts pass through untouched.
    """
    cleaned = (url or "").strip()
    lowered = cleaned.lower()
    for scheme in ("https://", "http://"):
        if lowered.startswith(scheme + "letsrunfaster.com"):
            rest = cleaned[len(scheme + "letsrunfaster.com"):]
            if rest == "" or rest.startswith("/"):
                return "https://www.letsrunfaster.com" + rest
    return cleaned


def config_path() -> Path:
    """Location of the JSON settings file (overridable in tests)."""
    override = os.environ.get("LRF_INGESTOR_CONFIG")
    if override:
        return Path(override).expanduser()
    return paths.config_file()


def load_config() -> Dict[str, Any]:
    """Read the settings file; missing/corrupt files yield {} (never raise)."""
    try:
        raw = config_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def normalize_remote_mode(value: Optional[str]) -> Optional[str]:
    """Validate a mode; ``None``/blank means "no opinion" (keep saved/default).

    Raises ``ValueError`` for an unknown non-blank value so callers can return
    a 400 instead of silently keeping a mode the runner did not choose.
    """
    if value is None:
        return None
    mode = str(value).strip().lower()
    if not mode:
        return None
    if mode not in REMOTE_MODES:
        raise ValueError(f"remote mode must be one of: {', '.join(REMOTE_MODES)}")
    return mode


def resolve_remote_mode(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Effective mode, including the upgrade-safe default.

    No saved settings at all = fresh install = ``auto``. A config written
    before modes existed has no ``remote_mode`` key, so it keeps ``block``
    until the runner opts in; ``save_config`` then freezes that choice.
    """
    cfg = load_config() if cfg is None else cfg
    raw = cfg.get("remote_mode")
    if isinstance(raw, str) and raw.strip().lower() in REMOTE_MODES:
        return raw.strip().lower()
    return LEGACY_REMOTE_MODE if cfg else DEFAULT_REMOTE_MODE


def get_import_private(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether new runs outside a block should be private (default public)."""
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("import_private", False))


def site_url_for(remote_url: Optional[str], path: str) -> Optional[str]:
    """Absolute site URL for a relative import response path.

    The server returns ``/activity/514`` or ``/block/7``; the UI needs a
    clickable absolute link. The import URL already names the site, so drop
    its ``/api/...`` suffix. Absolute paths pass through untouched.
    """
    cleaned = (path or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    base = canonicalize_url(
        (remote_url or "").strip() or load_config().get("remote_url") or DEFAULT_REMOTE_URL
    )
    marker = base.find("/api/")
    base = base[:marker] if marker != -1 else base.rstrip("/")
    return base.rstrip("/") + "/" + cleaned.lstrip("/")


def save_config(
    remote_url: Optional[str] = None,
    remote_api_key: Optional[str] = None,
    clear_key: bool = False,
    push_enabled: Optional[bool] = None,
    export_dir: Optional[str] = None,
    remote_mode: Optional[str] = None,
    import_private: Optional[bool] = None,
) -> Dict[str, Any]:
    """Merge one or both push settings into the settings file.

    ``remote_api_key=None`` leaves the stored key untouched; ``clear_key=True``
    (or an empty-string key) removes it. ``export_dir=""`` clears the saved
    destination. ``remote_mode=""`` removes an explicit mode (back to the
    resolved default); an unknown mode raises ``ValueError``. Returns the
    stored mapping minus the secret (``remote_url``, ``has_key``,
    ``key_prefix``, ``remote_mode``, ``import_private``).
    """
    cfg = load_config()
    # Freeze the resolved default on the first write. Done before this write
    # can add keys, so "fresh" is judged on the config that existed.
    frozen_mode = resolve_remote_mode(cfg) if "remote_mode" not in cfg else None
    if remote_url is not None:
        cleaned = canonicalize_url(remote_url)
        if cleaned:
            cfg["remote_url"] = cleaned
        elif "remote_url" in cfg:
            del cfg["remote_url"]
    if clear_key or (remote_api_key is not None and not remote_api_key.strip()):
        cfg.pop("remote_api_key", None)
    elif remote_api_key is not None and remote_api_key.strip():
        cfg["remote_api_key"] = remote_api_key.strip()

    if push_enabled is not None:
        cfg["push_enabled"] = bool(push_enabled)
    if export_dir is not None:
        if export_dir.strip():
            # Absolute so the value survives a different working directory
            # (the desktop shell launches the engine from its userData dir).
            cfg["export_dir"] = str(paths.resolve_export_dir(export_dir))
        else:
            cfg.pop("export_dir", None)

    if remote_mode is not None:
        cleaned_mode = normalize_remote_mode(remote_mode)
        if cleaned_mode:
            cfg["remote_mode"] = cleaned_mode
        else:
            cfg.pop("remote_mode", None)
    elif frozen_mode:
        cfg["remote_mode"] = frozen_mode
    if import_private is not None:
        cfg["import_private"] = bool(import_private)

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    return public_config(cfg)


def public_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Settings safe to return to UI callers (prefix, never the secret)."""
    cfg = cfg if cfg is not None else load_config()
    key = cfg.get("remote_api_key") or ""
    raw_mode = cfg.get("remote_mode")
    explicit_mode = isinstance(raw_mode, str) and raw_mode.strip().lower() in REMOTE_MODES
    return {
        "remote_url": cfg.get("remote_url") or DEFAULT_REMOTE_URL,
        "url_customized": bool(cfg.get("remote_url")),
        "has_key": bool(key.strip()),
        "key_prefix": key.strip()[:12] if key.strip() else None,
        # Preferences live here rather than in localStorage: the desktop shell
        # binds a fresh port on every launch, and localStorage is origin-scoped,
        # so anything stored there would be silently lost each restart.
        "push_enabled": bool(cfg.get("push_enabled", True)),
        "export_dir": cfg.get("export_dir") or None,
        "remote_mode": resolve_remote_mode(cfg),
        # False means the displayed mode is the upgrade-safe fallback, not a
        # choice the runner made. The UI explains that once, instead of
        # pretending a legacy config opted into auto.
        "remote_mode_explicit": explicit_mode,
        "import_private": get_import_private(cfg),
    }


def get_push_settings() -> Dict[str, Any]:
    """Resolved push target for an export (never the raw secret listing)."""
    cfg = load_config()
    url = canonicalize_url(cfg.get("remote_url") or DEFAULT_REMOTE_URL) or DEFAULT_REMOTE_URL
    return {
        "remote_url": url,
        "remote_api_key": (cfg.get("remote_api_key") or "").strip() or None,
        "remote_mode": resolve_remote_mode(cfg),
        "import_private": get_import_private(cfg),
    }


def get_remote_settings() -> Tuple[Optional[str], Optional[str]]:
    """Saved (url, key) pair; key is None when never saved."""
    push = get_push_settings()
    return push["remote_url"], push["remote_api_key"]


def resolve_latest_url(remote_url: Optional[str] = None) -> str:
    """Derive the sibling ``/activities/latest`` URL from an import URL."""
    base = canonicalize_url((remote_url or "").strip() or load_config().get("remote_url") or DEFAULT_REMOTE_URL)
    base = base.strip()
    if base.rstrip("/").endswith("/activities/import"):
        return base.rstrip("/")[: -len("/activities/import")] + "/activities/latest"
    return base.rstrip("/") + "/activities/latest"
