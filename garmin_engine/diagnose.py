"""Bisect a native crash in the packaged engine.

The engine can die without a Python traceback when native code misbehaves:
TLS libraries, libcurl (via curl_cffi), or an external kill. This module runs
the same primitives the app uses, one at a time, and writes a line before and
after each one. If the process disappears during a step, the last line in
``logs/engine-python.log`` names the primitive that killed it.

Run it from a terminal so the progress is visible live:

    LrfIngestor diagnose

Windows (installed app):

    & "$env:LOCALAPPDATA\\Programs\\LetsRunFaster Ingestor\\resources\\bin\\lrf-ingestor.exe" diagnose

Exit code 0 means every step completed.
"""

from __future__ import annotations

import gc
import os
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import diagnostics

Step = Tuple[str, Callable[[], str]]


def _log(message: str) -> None:
    """Progress goes to the diagnostics log and to the console, flushed."""
    diagnostics.write(f"diagnose: {message}")
    print(f"diagnose: {message}", flush=True)


def _collect() -> None:
    """Force the refcount/GC teardown that a request leaves behind.

    A crash during cleanup ("free an SSL object") looks like a crash during
    use when the object is freed by the garbage collector moments later.
    """
    gc.collect()


def _step_ssl_context() -> str:
    import socket
    import ssl

    context = ssl.create_default_context()
    with socket.create_connection(("www.garmin.com", 443), timeout=15) as raw:
        with context.wrap_socket(raw, server_hostname="www.garmin.com") as tls:
            peer = tls.version() or "unknown"
    _collect()
    return f"TLS handshake ok ({peer})"


def _step_requests_get() -> str:
    import requests

    response = requests.get("https://www.garmin.com/", timeout=20)
    _collect()
    return f"HTTP {response.status_code} via requests"


def _step_requests_teardown() -> str:
    import requests

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    response = session.get("https://www.garmin.com/", timeout=20)
    status = response.status_code
    response.close()
    session.close()
    del adapter, session, response
    _collect()
    return f"HTTP {status}, session torn down"


def _step_garmin_client() -> str:
    from garminconnect import Garmin

    client = Garmin()
    del client
    _collect()
    return "Garmin client constructed and released"


def _step_garmin_tokens(token_dir: Path) -> str:
    from garminconnect import Garmin

    client = Garmin()
    try:
        client.login(tokenstore=str(token_dir))
        name = client.get_full_name() or "unknown"
    except Exception as exc:  # no token cache, expired tokens, offline...
        name = f"no session ({type(exc).__name__})"
    del client
    _collect()
    return f"Garmin session: {name}"


def _step_curl_cffi() -> str:
    try:
        from curl_cffi import requests as cffi_requests
    except Exception as exc:
        return f"curl_cffi unavailable ({type(exc).__name__})"
    session = cffi_requests.Session()
    try:
        response = session.get(
            "https://www.garmin.com/", impersonate="chrome", timeout=20
        )
        status = response.status_code
    finally:
        session.close()
        del session
        _collect()
    return f"HTTP {status} via curl_cffi"


def _step_config_write() -> str:
    from . import settings as app_settings

    # Never touch the user's real config from a diagnostic.
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("LRF_INGESTOR_CONFIG")
        os.environ["LRF_INGESTOR_CONFIG"] = str(Path(tmp) / "config.json")
        try:
            app_settings.save_config(remote_url="https://www.letsrunfaster.com/api/activities/import")
            saved = app_settings.public_config()
        finally:
            if previous is None:
                os.environ.pop("LRF_INGESTOR_CONFIG", None)
            else:
                os.environ["LRF_INGESTOR_CONFIG"] = previous
    return f"config.json written and read back (url_customized={saved['url_customized']})"


def _step_export_dir() -> str:
    from . import paths

    folder = paths.default_export_dir()
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / ".lrf-diagnose-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return f"export folder writable ({folder})"


def build_steps(token_dir: Optional[Path] = None) -> List[Step]:
    from . import paths

    tokens = token_dir or paths.token_dir()
    return [
        ("ssl-context", _step_ssl_context),
        ("requests-get", _step_requests_get),
        ("requests-teardown", _step_requests_teardown),
        ("garmin-client", _step_garmin_client),
        ("garmin-tokens", lambda: _step_garmin_tokens(tokens)),
        ("curl-cffi", _step_curl_cffi),
        ("config-write", _step_config_write),
        ("export-dir", _step_export_dir),
    ]


def run(steps: Optional[List[Step]] = None) -> int:
    """Run every step, logging around each one. Returns a process exit code."""
    if steps is None:
        steps = build_steps()

    _log(f"starting {len(steps)} checks")
    _log(f"diagnostics log: {diagnostics.current_log_path()}")
    for name, step in steps:
        # The begin line is the one that matters: if the process dies inside
        # the step, this is the last thing written.
        _log(f"begin {name}")
        try:
            detail = step()
        except Exception as exc:
            _log(f"end {name} (failed: {type(exc).__name__}: {exc})")
            continue
        _log(f"end {name} ({detail})")

    _log("all steps completed")
    print(
        "\nAll steps completed. If an earlier run stopped mid-step, that step is\n"
        "the one that kills the engine -- send logs/engine-python.log.",
        flush=True,
    )
    return 0
