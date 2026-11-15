# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for Garmin Ingestor.

The UI must be built first (its output is what gets bundled):
    cd ui && npm install && npm run build

Build locally:
    pip install pyinstaller
    pyinstaller lrf-ingestor.spec

Output: dist/LrfIngestor (or LrfIngestor.exe on Windows).
Double-click it with no arguments to launch the browser UI.
Run it with arguments to use the CLI, e.g.:
    ./dist/LrfIngestor export --start 2025-01-01 --end 2025-06-01
The desktop shell starts the engine headless instead:
    ./dist/LrfIngestor ui --no-browser --port 0
"""

block_cipher = None

# Keep the distribution metadata for the libraries whose versions land in the
# diagnostics log. Without it a frozen build reports every dependency as
# "unknown", which is exactly the information needed to diagnose a crash.
from PyInstaller.utils.hooks import copy_metadata  # noqa: E402

a = Analysis(
    ["garmin_engine/launcher.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("garmin_engine/static", "garmin_engine/static"),
        *copy_metadata("garminconnect"),
        *copy_metadata("curl_cffi"),
        *copy_metadata("requests"),
        *copy_metadata("fastapi"),
        *copy_metadata("uvicorn"),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "websockets",
        "fastapi",
        "starlette",
        "pydantic",
        "typer",
        "rich",
        "garminconnect",
        "garth",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    # Pydantic v2 introspects Python source at import time
    # (inspect.getsource in _patch_PluggableSchemaValidator, triggered via
    # FastAPI's models). Frozen apps ship only bytecode by default, which
    # crashes with "OSError: could not get source code". Collecting these
    # packages as pyz+py ships their .py sources alongside the bytecode.
    module_collection_mode={
        "pydantic": "pyz+py",
        "fastapi": "pyz+py",
        "starlette": "pyz+py",
        "garmin_engine": "pyz+py",
    },
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LrfIngestor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Keep a console window so users see status/errors and CLI mode works.
    # A future iteration can ship a separate windowed (console=False) GUI
    # binary plus a system-tray icon.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
