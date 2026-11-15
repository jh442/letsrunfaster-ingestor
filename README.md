# Garmin Ingestor 🏃‍♂️🚴‍♀️

A tool to securely authenticate with Garmin Connect, query activities within any date range, and bulk export `.fit` (original raw data) or `.gpx` files to:
1. **Local disk**: Cleanly named activity files organized in a destination folder.
2. **Remote Web API**: Direct HTTP sync (`POST`) to feed your separate web application or database.

## Features
- **Secure Token Caching**: Authenticate once; session tokens are securely saved in `~/.garmin-ingestor/tokens` so passwords aren't stored.
- **Two-Factor Authentication (MFA)**: Built-in support for MFA SMS/email verification prompts.
- **Dual Export Formats**:
  - `fit` (extracts genuine `.fit` payload directly from Garmin's original zip)
  - `gpx` (standard GPS exchange format)
- **Rate-Limiting & Jitter**: Polite request throttling to avoid rate limits or IP blocking from Garmin Connect.
- **Remote Ingest Webhook**: Seamlessly pipe activities with metadata directly to your remote site's API.
- **Dual Modes**: Command-Line Interface (CLI) and JSON-RPC sidecar protocol for desktop (Tauri/Electron) integration.

## Download & Run (No Python Needed)

Each tagged release (`git tag v0.1.0 && git push --tags`) automatically builds
a double-clickable binary via GitHub Actions (`.github/workflows/build.yml`):

- **Windows**: `LrfIngestor.exe`
- **macOS**: `LrfIngestor-macos-arm64` (Apple Silicon) / `LrfIngestor-macos-x64` (Intel)
- **Linux**: `LrfIngestor`

Just download it from the **Releases** page and double-click it — your browser
opens with the full UI (login, activity list, export with live progress).
No Python, pip, or terminal required. Keep the window open while using the app;
close it (or Ctrl-C) to stop.

> First-run warnings are normal: Windows SmartScreen / macOS Gatekeeper flag
> unsigned downloads. Right-click → Open (Mac) or "More info → Run anyway"
> (Windows). Signing with a paid certificate removes these warnings later.

The same binary also works as a CLI when run with arguments:

```bash
LrfIngestor status
LrfIngestor export --start 2025-01-01 --end 2025-06-01 --format fit --output ./my_exports
```

### Build It Yourself

```bash
pip install pyinstaller
pyinstaller lrf-ingestor.spec
# Output: dist/LrfIngestor (double-click, or run with CLI args)
```

## Installation (Python)

```bash
# Using uv (recommended)
uv venv
uv pip install -e .

# Or standard pip
pip install -e .
```

## CLI Usage

### 1. Authenticate
```bash
garmin-ingestor login
```
You will be prompted for your email and password (plus MFA code if enabled).

### 2. Check Auth Status
```bash
garmin-ingestor status
```

### 3. Interactive Web UI (Browser)
Launch a local browser dashboard:
```bash
garmin-ingestor ui
```
This opens `http://localhost:8000` with an interface to log in, view activities, configure date presets, and track real-time download progress over WebSockets.

For embedding (the desktop shell, a launcher, CI), suppress the browser and
let the OS pick a port:
```bash
garmin-ingestor ui --no-browser --port 0
# stdout: LRF_READY url=http://127.0.0.1:53421 port=53421 pid=8123 version=0.2.0
```
Wait for that single `LRF_READY` line, then poll `GET /api/health`
(`{"ok": true, "version": "..."}`) before showing a window. `GET /api/config`
reports `default_output_dir` (the folder exports go to unless overridden) and
the app state directory.

#### When it crashes

The engine writes `<app home>/logs/engine-python.log` on every run: platform,
Python and library versions, `curl_cffi` availability, uncaught exceptions
(including ones on FastAPI's worker threads), a `faulthandler` stack dump if
the interpreter dies from a signal or an access violation, and a marker when
Python exits on its own. A log that stops with no traceback and no stack dump
means the process was killed outright (security software, a forced
`TerminateProcess`) rather than failing in code.

Login normally tries Garmin's `curl_cffi` strategies first (native libcurl and
OpenSSL, needed for TLS impersonation). On Windows ARM64 those are skipped by
default because they took the engine down with no traceback. Override with
`LRF_ENABLE_CFFI=1` to use them, or `LRF_ENABLE_CFFI=0` to force the plain
`requests` strategies everywhere.

`logs/engine-python.log` also gets a heartbeat line every 15 seconds, so a
log that stops tells you *when* the process died, not just which request it
served last.

#### Bisecting a silent crash

When the process disappears with no traceback, run the built-in self-check
from a terminal — it exercises TLS, `requests`, `curl_cffi`, a Garmin token
restore, config writes, and the export folder one step at a time, writing
`diagnose: begin <step>` / `end <step>` around each:

```bash
# Linux/macOS
./LrfIngestor diagnose

# Windows (installed desktop app)
& "$env:LOCALAPPDATA\Programs\LetsRunFaster Ingestor\resources\bin\lrf-ingestor.exe" diagnose
```

If a run stops mid-step, that step's library is the one killing the process.

### 4. List Activities in a Date Range (CLI)
```bash
garmin-ingestor list-activities --start 2025-01-01 --end 2025-06-01
```

### 5. Bulk Export Activities (CLI)
Export as `.fit` to a local folder:
```bash
garmin-ingestor export --start 2025-01-01 --end 2025-06-01 --format fit --output ./my_exports
```

Export as `.gpx` and push directly to LetsRunFaster (needs the API key from
your profile page → API Key):
```bash
garmin-ingestor export \
  --start 2025-01-01 \
  --end 2025-06-01 \
  --format fit \
  --remote-url https://www.letsrunfaster.com/api/activities/import \
  --remote-key "lrf_..."
```

### Saved Push Target (API Key)

Save the ingest URL + API key once (stored in `config.json` inside the app
state directory — `LRF_INGESTOR_HOME` if set, else `~/.garmin-ingestor`; file
mode `0600`) and every interface — browser UI, CLI, RPC sidecar — defaults to
pushing there. An explicit empty value (`--remote-url ""`, UI checkbox off)
still exports locally only.

`config.json` also remembers the UI's push toggle (`push_enabled`) and export
destination (`export_dir`, always stored absolute). They live server-side
rather than in `localStorage` because the desktop shell binds a new port on
each launch, which changes the page origin.

```bash
garmin-ingestor configure --remote-url https://www.letsrunfaster.com/api/activities/import --remote-key "lrf_..."
garmin-ingestor configure --show        # URL + key prefix (never the secret)
garmin-ingestor export --start 2025-06-01 --end 2025-07-01 --format fit   # pushes via saved target
```

### Sync New Since Last Import

In the browser UI, **Sync new since last import** asks the site (via
`GET /api/activities/latest`, Bearer-key authed, proxied through the ingestor
backend) for your latest logged block day and sets the range to
`latest + 1 day … today`, then previews the Garmin activities — press
**Export Now** to import them. The lookup counts any actual day (hand-logged
or Garmin-pushed).

## Remote Ingestion API Specification

When `--remote-url` is specified, `garmin-ingestor` will send a `multipart/form-data` `POST` request for each activity:

- **Headers**: `Authorization: Bearer <remote-key>` (`lrf_...` from your profile page)
- **Form fields**:
  - `activity_id`: Garmin numeric activity ID (string)
  - `activity_name`: Name of the activity
  - `activity_type`: E.g., `running`, `cycling`, `swimming`
  - `start_time`: Local start timestamp (e.g. `2025-03-01 07:15:00`)
  - `format`: `fit`, `gpx` or `tcx`
  - Parsed FIT payload (FIT exports only, mirrors the site's ParsedGpxResult):
    `distance_miles`, `avg_pace`, `avg_hr`, `max_hr`, `avg_cadence`,
    `elev_gain_ft`, `finish_time`, `device`, `telemetry_json`
    (downsampled to fit the site's 500KB cap)
- **File**:
  - `file`: The binary payload of the activity file (`.fit`, `.gpx` or `.tcx`)
- **Response** (always HTTP 200 on valid key):
  - `{ ok:true, matched:true, block_id, day_id, appended_to }` — appended to the
    training-block day (never replaces a *different* activity; fills primary, else second, else extra)
  - `{ ok:true, matched:true, replaced:true, slot }` — same activity already on the
    day (distance ±0.05mi, HR ±2, duration ±30s): refreshed in place, no duplicate row
  - `{ ok:true, matched:false, code:"no_block_found", activity_date }` — no block
    covers that date; export again after creating one

## Developing the UI

The renderer lives in `ui/` (Vite + Tailwind v3) and is **built into**
`garmin_engine/static`, which is what FastAPI serves and what PyInstaller
bundles. The build output is committed on purpose: installing the Python
package never requires Node.

```bash
cd ui
npm install
npm run build        # -> ../garmin_engine/static (index.html + assets/)

# live reload against a running engine
garmin-ingestor ui --no-browser &
npm run dev
```

- Edit `ui/index.html`, `ui/src/main.js`, `ui/src/styles.css`. Do **not** edit
  `garmin_engine/static/index.html`; it is generated and CI fails if it is
  stale after a UI change.
- Fonts are self-hosted (`ui/src/fonts`, OFL) and icons are one inline SVG
  sprite, so the app renders with no network access.
- Native capabilities (folder picker, later keychain access) live behind
  `ui/src/platform.js`, which is a no-op outside Electron.

## Running Tests
```bash
python3 -m unittest discover tests
```
