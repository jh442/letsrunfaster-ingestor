"""Double-clickable launcher for Garmin Ingestor.

Behavior:
- No arguments (e.g. user double-clicks the bundled app): start the local
  web server and open the browser UI. Stays running until the user closes
  the window / presses Ctrl-C.
- With arguments (e.g. ``LrfIngestor export --start ...``): behave exactly
  like the ``garmin-ingestor`` CLI.

This module is the entry point for the PyInstaller one-file build so a user
never needs Python, pip, or a terminal.
"""

import socket
import sys
import threading
import webbrowser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _find_free_port(host: str, preferred: int, attempts: int = 20) -> int:
    """Return `preferred` if free, otherwise the next free port after it."""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    return preferred


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    def _open() -> None:
        import time

        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def run_ui(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Start the FastAPI server and open the browser UI (blocking)."""
    import uvicorn

    from garmin_engine.web_server import app

    port = _find_free_port(host, port)
    url = f"http://{host}:{port}"
    print(f"Starting Garmin Ingestor UI at: {url}")
    print("Your browser should open automatically. Keep this window open while using the app.")
    print("To stop the app, close this window or press Ctrl-C.")
    _open_browser_later(url)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    # Double-clicked (no CLI args) -> launch the browser UI.
    if len(sys.argv) <= 1:
        run_ui()
        return
    # Called with args -> delegate to the full Typer CLI.
    from garmin_engine.cli import app as cli_app

    cli_app()


if __name__ == "__main__":
    main()
