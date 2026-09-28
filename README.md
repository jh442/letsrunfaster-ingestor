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
  --remote-url https://letsrunfaster.com/api/activities/import \
  --remote-key "lrf_..."
```

### Saved Push Target (API Key)

Save the ingest URL + API key once (stored in `~/.garmin-ingestor/config.json`,
file mode `0600`) and every interface — browser UI, CLI, RPC sidecar — defaults
to pushing there. An explicit empty value (`--remote-url ""`, UI checkbox off)
still exports locally only.

```bash
garmin-ingestor configure --remote-url https://letsrunfaster.com/api/activities/import --remote-key "lrf_..."
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

## Running Tests
```bash
python3 -m unittest discover tests
```
