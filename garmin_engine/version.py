"""Single source of truth for the application version.

``pyproject.toml`` reads this attribute at build time, the CLI exposes it as
``--version``, and ``GET /api/health`` reports it so the desktop shell can
verify it launched the engine it shipped.
"""

__version__ = "0.2.2"


def get_version() -> str:
    """The application version string.

    The in-package constant is authoritative: ``pyproject.toml`` derives the
    distribution version from it, and a PyInstaller build has no ``.dist-info``
    for the app at all. Consulting installed metadata as well only creates
    drift (an editable install keeps reporting the old version after a bump).
    """
    return __version__
