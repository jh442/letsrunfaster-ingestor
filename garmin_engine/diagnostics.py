"""Crash and shutdown logging for the packaged engine.

The desktop build runs without a terminal: stdout/stderr are pipes into the
Electron shell's ``engine.log``. That is enough for normal traffic but not for
the two failure modes that matter when something goes badly wrong:

* a **native crash** inside a C extension (curl_cffi/libcurl, OpenSSL) never
  raises a Python exception, so a traceback is never printed;
* an **external kill** (security software, a forced ``TerminateProcess``) ends
  the process with no output at all.

This module makes both of those legible. It writes to
``<app home>/logs/engine-python.log``, which sits next to the shell's own
log, and records:

* a startup banner with the platform and the versions of the libraries that
  touch the network;
* every uncaught exception, including ones raised on worker threads (FastAPI
  runs sync endpoints in a threadpool, so these would otherwise vanish);
* a stack dump from :mod:`faulthandler` if the interpreter dies from a
  signal or a Windows access violation;
* an ``atexit`` marker when the interpreter reaches its own exit, which a
  crash or a forced kill never produces.

Stopping the UI server is a special case worth knowing about: uvicorn
restores the default signal handler and re-raises ``SIGTERM`` after its
graceful shutdown, so a stopped server logs uvicorn's "Shutting down" lines
in ``engine.log`` but not the ``atexit`` marker here. The desktop shell also
force-terminates the engine on Windows. Read the marker as "Python exited on
its own", not as "shutdown was clean".

Everything here is best-effort: a read-only or missing directory must never
stop the engine from starting.
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import platform
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Optional, TextIO

from . import paths

_LOGGER = logging.getLogger("lrf.diagnostics")

_LOG_NAME = "engine-python.log"
_HANDLER_MARKER = "_lrf_diagnostics_handler"

_installed = False
_fault_file: Optional[TextIO] = None
_log_path: Optional[Path] = None


def log_path() -> Path:
    """Where diagnostics go: the shell's log folder, else the temp folder."""
    try:
        folder = paths.app_home() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / _LOG_NAME
    except OSError:
        return Path(tempfile.gettempdir()) / _LOG_NAME


def current_log_path() -> Optional[Path]:
    """Path in use after :func:`install`, ``None`` before it."""
    return _log_path


def write(message: str) -> None:
    """Append one line straight to the diagnostics log (bypasses logging)."""
    if _fault_file is None:
        return
    try:
        _fault_file.write(message.rstrip("\n") + "\n")
        _fault_file.flush()
    except (OSError, ValueError):
        # The file can disappear under us (temp cleanup, AV quarantine);
        # diagnostics must never turn into a crash of their own.
        pass


def _reporthook_text(exc_type, exc_value, exc_tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    write(f"FATAL uncaught exception in main thread:\n{_reporthook_text(exc_type, exc_value, exc_tb)}")
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_excepthook(args) -> None:
    thread = args.thread.name if args.thread is not None else "?"
    write(
        f"FATAL uncaught exception in thread '{thread}':\n"
        f"{_reporthook_text(args.exc_type, args.exc_value, args.exc_traceback)}"
    )
    threading.__excepthook__(args)


def _unraisablehook(unraisable) -> None:
    write(
        f"unraisable exception in {unraisable.object!r}: "
        f"{unraisable.exc_type.__name__}: {unraisable.exc_value}\n"
        f"{''.join(traceback.format_tb(unraisable.exc_traceback))}"
    )
    sys.__unraisablehook__(unraisable)


def _on_exit() -> None:
    # Its absence in the log is the signal that the process did not exit
    # through Python at all (native crash or TerminateProcess).
    write(f"engine process exiting normally (pid {os.getpid()})")


def _configure_logging() -> None:
    """Send our own INFO logs to the log file and stderr.

    uvicorn configures only its own loggers (``propagate = False``), so this
    root handler adds our messages without duplicating the access log.
    """
    root = logging.getLogger()
    if any(getattr(handler, _HANDLER_MARKER, False) for handler in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    setattr(stream, _HANDLER_MARKER, True)
    root.addHandler(stream)

    if _fault_file is not None:
        to_file = logging.StreamHandler(_fault_file)
        to_file.setFormatter(fmt)
        setattr(to_file, _HANDLER_MARKER, True)
        root.addHandler(to_file)


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        pass
    # A PyInstaller one-file build ships no .dist-info, so fall back to the
    # module attribute that every library we care about exposes.
    try:
        module = __import__(name.replace("-", "_"))
        return str(getattr(module, "__version__", "unknown"))
    except Exception:
        return "unknown"


def _record_environment() -> None:
    from .version import get_version

    write(
        "engine startup "
        f"version={get_version()} "
        f"pid={os.getpid()} "
        f"python={sys.version.split()[0]} "
        f"platform={platform.platform()} "
        f"machine={platform.machine()} "
        f"frozen={getattr(sys, 'frozen', False)}"
    )
    write(
        "engine deps "
        f"garminconnect={_package_version('garminconnect')} "
        f"curl_cffi={_package_version('curl_cffi')} "
        f"requests={_package_version('requests')} "
        f"fastapi={_package_version('fastapi')} "
        f"uvicorn={_package_version('uvicorn')}"
    )
    try:
        from curl_cffi import requests as _cffi  # noqa: F401

        write("curl_cffi import: ok")
    except Exception as exc:  # pragma: no cover - depends on the platform
        write(f"curl_cffi import: unavailable ({exc.__class__.__name__}: {exc})")
    write(f"diagnostics log: {_log_path}")


def install() -> Optional[Path]:
    """Install the fault handler, hooks, and log handlers. Idempotent."""
    global _installed, _fault_file, _log_path
    if _installed:
        return _log_path
    _installed = True

    path = log_path()
    _log_path = path
    try:
        _fault_file = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        _fault_file = None

    if _fault_file is not None:
        try:
            # Dumps the Python stack of every thread on SIGSEGV/SIGABRT and,
            # on Windows, on access violations. This is the only way to see
            # where a crash inside libcurl happened.
            faulthandler.enable(file=_fault_file, all_threads=True)
        except (AttributeError, OSError, ValueError):
            pass

    _configure_logging()
    sys.excepthook = _excepthook
    sys.unraisablehook = _unraisablehook
    threading.excepthook = _thread_excepthook
    atexit.register(_on_exit)
    _record_environment()
    _LOGGER.info("diagnostics installed at %s", path)
    return path


def log_exception(context: str) -> None:
    """Record the exception currently being handled, with its traceback."""
    write(f"{context}:\n{traceback.format_exc()}")
