"""Double-clickable launcher for Garmin Ingestor.

Behavior:
- No arguments (e.g. user double-clicks the bundled app): start the local
  web server and open the browser UI. Stays running until the user closes
  the window / presses Ctrl-C.
- With arguments (e.g. ``LrfIngestor export --start ...``): behave exactly
  like the ``garmin-ingestor`` CLI. That includes ``ui --no-browser
  --port 0``, which is how the desktop shell starts the engine.

This module is the entry point for the PyInstaller one-file build so a user
never needs Python, pip, or a terminal.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from typing import Optional

# Absolute import on purpose: PyInstaller executes this file as the program
# entry point (__main__, no package context), so a relative import here makes
# the frozen binary fail before it can print its readiness line.
from garmin_engine.version import __version__

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Machine-readable readiness marker. The desktop shell spawns the engine,
# reads stdout until this prefix appears, then loads the reported URL. One
# line, stdout, flushed immediately -- never localize or reformat it.
READY_PREFIX = "LRF_READY"


def _ensure_std_streams() -> None:
    """Give windowed (console-less) builds something to write to.

    A PyInstaller ``console=False`` build sets ``sys.stdout``/``sys.stderr``
    to ``None``; uvicorn's logging handlers then raise on the first write.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _bind(host: str, preferred: int, attempts: int = 20) -> socket.socket:
    """Bind a listening socket and hand it to uvicorn.

    ``preferred == 0`` lets the OS pick (the desktop shell uses this and reads
    the real port from the ready line). A fixed port walks forward a few slots
    so a stale instance does not hard-fail on launch.
    """
    last_error: Optional[OSError] = None
    ports = [preferred] if preferred == 0 else range(preferred, preferred + attempts)
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # SO_REUSEADDR is safe (and needed for fast restarts) on POSIX. On
            # Windows it permits stealing a port that is still in use, so skip.
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise SystemExit(f"Could not listen on {host}:{preferred} ({last_error})")


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    def _open() -> None:
        import time

        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def ready_line(url: str, port: int) -> str:
    """The single line the desktop shell parses."""
    return f"{READY_PREFIX} url={url} port={port} pid={os.getpid()} version={__version__}"


def run_ui(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    log_level: str = "info",
) -> None:
    """Start the FastAPI server and optionally open the browser (blocking)."""
    _ensure_std_streams()
    import uvicorn

    from garmin_engine.web_server import app

    sock = _bind(host, port)
    bound_port = sock.getsockname()[1]
    url = f"http://{host}:{bound_port}"
    print(ready_line(url, bound_port), flush=True)
    if sys.stdout.isatty():
        print(f"Starting Garmin Ingestor UI at: {url}")
        print("Keep this window open while using the app. Ctrl-C to stop.")
    if open_browser:
        _open_browser_later(url)

    server = uvicorn.Server(uvicorn.Config(app, log_level=log_level))
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()


def main() -> None:
    _ensure_std_streams()
    # Double-clicked (no CLI args) -> launch the browser UI.
    if len(sys.argv) <= 1:
        run_ui()
        return
    # Called with args -> delegate to the full Typer CLI.
    from garmin_engine.cli import app as cli_app

    cli_app()


if __name__ == "__main__":
    main()
