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
garmin-ingestor configure --show        # URL, key prefix, mode, visibility
garmin-ingestor export --start 2025-06-01 --end 2025-07-01 --format fit   # pushes via saved target
```

`configure --show` prints the mode, never the secret:

```
Target URL: https://www.letsrunfaster.com/api/activities/import
API key: saved (lrf_abcdef12...)
Import mode: auto (block day when the date matches, otherwise a run on your feed)
Runs outside a block: public
```

### Runs outside a training block

Garmin activities that do not line up with a block day can be stored as
standalone runs instead of being rejected. Pick how with `remote_mode`:

| Mode | A run whose date matches a block day | A run with no block |
| --- | --- | --- |
| `block` | Appended/refreshed on the block day (legacy behavior) | Skipped (`no_block_found`) |
| `auto` (fresh default) | Appended/refreshed on the block day | Saved as a standalone run on the feed |
| `activity` | Saved as a standalone run; the block day is left alone | Saved as a standalone run |

```bash
garmin-ingestor configure --remote-mode auto            # or block / activity
garmin-ingestor configure --private                     # runs outside a block are private
garmin-ingestor export --start 2026-09-01 --end 2026-09-30 --remote-mode activity
```

- **Upgrade safety:** a `config.json` written before modes existed keeps
  `block` until you opt in, so updating the ingestor never starts publishing
  unmatched runs. A fresh install starts on `auto`. The resolved value is
  frozen into `config.json` on the first write.
- **Private runs:** `--private` sends `is_private=1`; those runs appear on your
  profile/history but not the public feed, leaderboard, or OG cards. The
  block-day path ignores the flag (it is a planned block workout).
- **Per-run override:** `--remote-mode` / `--private|--public` on `export`
  override the saved choice for that run only.
- Identity dedupe still applies: the same Garmin activity id never produces a
  second row, whichever mode created it.

The browser/desktop UI exposes the same three-way choice under **Push to
LetsRunFaster → Runs outside a block**, plus an **Import runs as private**
toggle. Bulk exports over a month in `auto`/`activity` mode ask for a second
confirm click, because every date without a block (or every date, in
`activity` mode) becomes a feed post.

### Sync New Since Last Import

In the browser UI, **Sync new since last import** asks the site (via
`GET /api/activities/latest`, Bearer-key authed, proxied through the ingestor
backend) for your latest logged day and sets the range to
`latest + 1 day … today`, then previews the Garmin activities — press
**Export Now** to import them. The lookup counts both block days and
standalone runs (hand-logged or Garmin-pushed).

## Remote Ingestion API Specification

When `--remote-url` is specified, `garmin-ingestor` will send a `multipart/form-data` `POST` request for each activity:

- **Headers**: `Authorization: Bearer <remote-key>` (`lrf_...` from your profile page)
- **Form fields**:
  - `activity_id`: Garmin numeric activity ID (string) — **not** the site row id
  - `activity_name`: Name of the activity
  - `activity_type`: E.g., `running`, `cycling`, `swimming`
  - `start_time`: Local start timestamp (e.g. `2025-03-01 07:15:00`)
  - `format`: `fit`, `gpx` or `tcx`
  - `mode` (optional): `block` | `auto` | `activity`; absent = legacy `block`.
    **Must be a form field, never a query parameter** — the ingestor derives
    `/activities/latest` by suffix-matching the saved import URL.
  - `is_private` (optional): `1` makes a run created outside a block private;
    omit it to use the server's public default. Ignored for block-day imports.
  - Parsed FIT payload (FIT exports only, mirrors the site's ParsedGpxResult):
    `distance_miles`, `avg_pace`, `avg_hr`, `max_hr`, `avg_cadence`,
    `elev_gain_ft`, `finish_time`, `device`, `telemetry_json`
    (downsampled to fit the site's 500KB cap)
- **File**:
  - `file`: The binary payload of the activity file (`.fit`, `.gpx` or `.tcx`)
- **Block response** (HTTP 200 on valid key):
  - `{ ok:true, matched:true, linkage:"block", block_id, day_id, appended_to }`
    — appended to the training-block day (never replaces a *different*
    activity; fills primary, else second, else extra)
  - `{ ok:true, matched:true, linkage:"block", replaced:true, slot }` — same
    activity already on the day (distance ±0.05mi, HR ±2, duration ±30s):
    refreshed in place, no duplicate row
  - `{ ok:true, matched:false, code:"no_block_found", activity_date }` — no
    block covers that date and the request used `mode=block` (or an old
    server ignored `mode`)
- **Standalone-run response** (`mode=auto|activity`, no block match):
  - `{ ok:true, matched:false, linkage:"nonblock", created:true,
    social_activity_id, activity_id, activity_date, url:"/activity/<id>" }`
    — a new row in the unified activity store
  - `{ ok:true, linkage:"nonblock", created:false, deduped:true,
    social_activity_id, url }` — this Garmin id already exists
  - `{ ok:true, linkage:"nonblock", deduped:true, deleted:true, … }` — the
    owner deleted that row on the site; re-pushing does **not** resurrect it
  - `social_activity_id` is the site row id (`/activity/<id>`); `activity_id`
    remains the Garmin id used for dedupe.
- `linkage` is absent only on an older server. Treat missing as `block`, and
  never infer "standalone run" from `matched:false` alone.

**Deploy order:** server first, ingestor second. A new ingestor against an old
server behaves exactly like today (the server ignores `mode`); an old ingestor
against the new server still gets block-only behavior because it sends no
`mode`.

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
