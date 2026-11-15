"""Where the ingestor keeps state on disk.

One directory holds everything the app writes outside exported activity
files: the cached Garmin session tokens and the LetsRunFaster API key. The
desktop shell points ``LRF_INGESTOR_HOME`` at Electron's ``userData`` folder,
so state survives app updates and never lands inside the (read-only) app
bundle. CLI users keep the historical ``~/.garmin-ingestor`` location.

Exported activity files default to ``~/Documents/LetsRunFaster`` rather than
the process working directory: when the app runs from ``/Applications`` or
``C:\\Program Files`` the working directory is not writable.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = ".garmin-ingestor"
EXPORT_DIR_NAME = "LetsRunFaster"


def app_home() -> Path:
    """Root directory for tokens and settings (``LRF_INGESTOR_HOME`` wins)."""
    override = os.environ.get("LRF_INGESTOR_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / APP_DIR_NAME


def ensure_app_home() -> Path:
    """Create and return :func:`app_home` (mode 0700 on POSIX)."""
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    return home


def token_dir() -> Path:
    """Garmin session token cache (cookie-equivalent, treat as a secret)."""
    return app_home() / "tokens"


def config_file() -> Path:
    """JSON settings file holding the LetsRunFaster API key."""
    return app_home() / "config.json"


def documents_dir() -> Path:
    """Best-effort per-user Documents folder; home when there is none."""
    candidate = Path.home() / "Documents"
    return candidate if candidate.is_dir() else Path.home()


def default_export_dir() -> Path:
    """Default destination for exported activity files."""
    return documents_dir() / EXPORT_DIR_NAME


def resolve_export_dir(raw: str | None) -> Path:
    """Turn a user-entered destination into an absolute path.

    Relative paths resolve against the home directory rather than the process
    working directory, because a packaged app may be launched from a location
    it cannot write to (``/`` on macOS, ``C:\\Windows\\System32`` on Windows).
    An empty value means "use the default folder".
    """
    text = (raw or "").strip()
    if not text:
        return default_export_dir()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    return path
