"""Server-side persistence for the LetsRunFaster push settings.

The browser UI already kept the ingest URL + API key in localStorage, but
that never reached the CLI or the RPC sidecar — every export needed
``--remote-url`` / ``--remote-key`` flags again. This module stores the same
pair in ``~/.garmin-ingestor/config.json`` (file mode 0600: the API key is a
bearer secret) so the web UI, CLI, and RPC server all default to it.

Only the key *prefix* (``lrf_...`` first 12 chars, same as the site's profile
page shows) is ever returned to UI callers — never the full key.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_REMOTE_URL = "https://www.letsrunfaster.com/api/activities/import"


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
        return Path(override)
    return Path.home() / ".garmin-ingestor" / "config.json"


def load_config() -> Dict[str, Any]:
    """Read the settings file; missing/corrupt files yield {} (never raise)."""
    try:
        raw = config_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(
    remote_url: Optional[str] = None,
    remote_api_key: Optional[str] = None,
    clear_key: bool = False,
) -> Dict[str, Any]:
    """Merge one or both push settings into the settings file.

    ``remote_api_key=None`` leaves the stored key untouched; ``clear_key=True``
    (or an empty-string key) removes it. Returns the stored mapping minus the
    secret (``remote_url``, ``has_key``, ``key_prefix``).
    """
    cfg = load_config()
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
    return {
        "remote_url": cfg.get("remote_url") or DEFAULT_REMOTE_URL,
        "url_customized": bool(cfg.get("remote_url")),
        "has_key": bool(key.strip()),
        "key_prefix": key.strip()[:12] if key.strip() else None,
    }


def get_remote_settings() -> Tuple[Optional[str], Optional[str]]:
    """Saved (url, key) pair; key is None when never saved."""
    cfg = load_config()
    url = canonicalize_url(cfg.get("remote_url") or DEFAULT_REMOTE_URL) or DEFAULT_REMOTE_URL
    key = (cfg.get("remote_api_key") or "").strip() or None
    return url, key


def resolve_latest_url(remote_url: Optional[str] = None) -> str:
    """Derive the sibling ``/activities/latest`` URL from an import URL."""
    base = canonicalize_url((remote_url or "").strip() or load_config().get("remote_url") or DEFAULT_REMOTE_URL)
    base = base.strip()
    if base.rstrip("/").endswith("/activities/import"):
        return base.rstrip("/")[: -len("/activities/import")] + "/activities/latest"
    return base.rstrip("/") + "/activities/latest"
