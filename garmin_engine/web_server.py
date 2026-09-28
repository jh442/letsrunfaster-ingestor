"""Local web server providing an interactive browser UI and REST endpoints."""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import GarminAuthManager, AuthStatus
from .exporter import GarminExporter, ExportFormat
from . import settings as app_settings

app = FastAPI(title="LetsRunFaster Ingestor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

auth_mgr = GarminAuthManager()
active_client = None

# Active websocket connections for live streaming progress
connected_websockets: List[WebSocket] = []


async def broadcast_event(event_type: str, data: Dict[str, Any]):
    message = json.dumps({"event": event_type, "data": data})
    for ws in list(connected_websockets):
        try:
            await ws.send_text(message)
        except Exception:
            if ws in connected_websockets:
                connected_websockets.remove(ws)


class LoginRequest(BaseModel):
    email: str
    password: str


class MfaRequest(BaseModel):
    code: str


class ListActivitiesRequest(BaseModel):
    start_date: str
    end_date: str
    activity_type: Optional[str] = None


class ExportRequest(BaseModel):
    start_date: str
    end_date: str
    format: str = "fit"
    output_dir: str = "./garmin_exports"
    remote_url: Optional[str] = None
    remote_api_key: Optional[str] = None
    activity_type: Optional[str] = None
    delay: float = 0.5


@app.get("/api/auth/status")
def get_auth_status():
    res = auth_mgr.login_with_tokens()
    return {
        "authenticated": res.status == AuthStatus.AUTHENTICATED,
        "displayName": res.display_name,
        "message": res.message,
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    global active_client
    res, client = auth_mgr.login_with_credentials(payload.email, payload.password)
    active_client = client
    return {
        "status": res.status.value,
        "message": res.message,
        "displayName": res.display_name,
        "mfaRequired": res.status == AuthStatus.MFA_REQUIRED,
    }


@app.post("/api/auth/mfa")
def submit_mfa(payload: MfaRequest):
    global active_client
    client = active_client or auth_mgr.get_client()
    res = auth_mgr.submit_mfa(client, None, payload.code)
    return {
        "status": res.status.value,
        "message": res.message,
        "displayName": res.display_name,
        "authenticated": res.status == AuthStatus.AUTHENTICATED,
    }


@app.post("/api/auth/logout")
def logout():
    global active_client
    success = auth_mgr.clear_tokens()
    active_client = None
    return {"success": success}


@app.post("/api/activities/list")
def list_activities(payload: ListActivitiesRequest):
    try:
        client = active_client or auth_mgr.get_client()
        client.login(tokenstore=auth_mgr.token_store)
        exporter = GarminExporter(client)
        activities = exporter.get_activities_in_range(
            payload.start_date, payload.end_date, payload.activity_type
        )
        return {
            "activities": [
                {
                    "id": a.activity_id,
                    "name": a.activity_name,
                    "type": a.activity_type,
                    "startTime": a.start_time_local,
                    "distanceMiles": round(a.distance_meters * 0.000621371, 2) if a.distance_meters else None,
                    "distanceKm": round(a.distance_meters / 1000, 2) if a.distance_meters else None,
                    "durationMin": round(a.duration_seconds / 60, 1) if a.duration_seconds else None,
                }
                for a in activities
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/activities/export")
async def start_export(payload: ExportRequest):
    try:
        saved_url, saved_key = app_settings.get_remote_settings()
        # Push-target contract: omitted (None) falls back to saved settings,
        # explicit "" disables pushing (the UI sends "" when its push
        # checkbox is off, so "locally only" keeps working).
        if payload.remote_url is None:
            remote_url: Optional[str] = saved_url
        elif payload.remote_url.strip() == "":
            remote_url = None
        else:
            remote_url = payload.remote_url.strip()
        if payload.remote_api_key is None:
            remote_api_key: Optional[str] = saved_key
        elif payload.remote_api_key.strip() == "":
            remote_api_key = None
        else:
            remote_api_key = payload.remote_api_key.strip()
        client = active_client or auth_mgr.get_client()
        client.login(tokenstore=auth_mgr.token_store)
        exporter = GarminExporter(client)

        activities = exporter.get_activities_in_range(
            payload.start_date, payload.end_date, payload.activity_type
        )

        total = len(activities)
        await broadcast_event("export_started", {"total": total})

        fmt_map = {
            "fit": ExportFormat.FIT,
            "gpx": ExportFormat.GPX,
            "tcx": ExportFormat.TCX,
            "tpx": ExportFormat.TPX,
        }
        export_fmt = fmt_map.get(payload.format.lower(), ExportFormat.FIT)
        dest_dir = Path(payload.output_dir)

        # Run export loop
        results = []
        loop = asyncio.get_running_loop()

        def run_single(act):
            return exporter.export_activity(
                activity=act,
                export_format=export_fmt,
                output_dir=dest_dir,
                remote_url=remote_url,
                remote_api_key=remote_api_key,
            )

        for idx, act in enumerate(activities, start=1):
            res = await loop.run_in_executor(None, run_single, act)
            results.append(res)
            await broadcast_event(
                "export_progress",
                {
                    "current": idx,
                    "total": total,
                    "activityId": res.activity_id,
                    "filename": res.filename,
                    "success": res.success,
                    "sizeBytes": res.size_bytes,
                    "savedPath": res.saved_path,
                    "remoteSynced": res.remote_synced,
                    "remoteMatched": res.remote_matched,
                    "remoteDetail": res.remote_detail,
                    "error": res.error_message,
                },
            )
            if idx < total and payload.delay > 0:
                await asyncio.sleep(payload.delay)

        summary = {
            "total": total,
            "succeeded": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
        }
        await broadcast_event("export_completed", summary)
        return summary

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class ClearExportFolderRequest(BaseModel):
    output_dir: str = "./garmin_exports"


class RemoteSettingsRequest(BaseModel):
    remote_url: Optional[str] = None
    remote_api_key: Optional[str] = None
    clear_key: bool = False


class SyncLatestRequest(BaseModel):
    remote_url: Optional[str] = None
    remote_api_key: Optional[str] = None


@app.get("/api/settings/remote")
def get_remote_settings():
    """Saved LetsRunFaster push target (key prefix only, never the secret)."""
    return app_settings.public_config()


@app.post("/api/settings/remote")
def save_remote_settings(payload: RemoteSettingsRequest):
    """Persist the ingest URL + API key to ~/.garmin-ingestor/config.json."""
    if payload.remote_url is not None and payload.remote_url.strip() == "":
        raise HTTPException(status_code=400, detail="Remote URL cannot be empty.")
    if payload.remote_api_key is not None and payload.clear_key:
        raise HTTPException(status_code=400, detail="Pass either remote_api_key or clear_key, not both.")
    saved = app_settings.save_config(
        remote_url=payload.remote_url,
        remote_api_key=payload.remote_api_key,
        clear_key=payload.clear_key,
    )
    return {"success": True, **saved}


@app.post("/api/sync/latest")
def fetch_sync_latest(payload: SyncLatestRequest):
    """Ask the webapp for the owner's latest block activity day.

    Server-side proxy (avoids browser CORS): resolves the saved push
    settings when the request omits them, derives the sibling
    /activities/latest URL from the import URL, and forwards the call with
    the saved Bearer key. The UI defaults its start date to
    ``suggested_start`` (latest actual day + 1).
    """
    import requests as http_requests

    saved_url, saved_key = app_settings.get_remote_settings()
    remote_url = (payload.remote_url or "").strip() or saved_url
    api_key = (payload.remote_api_key or "").strip() or saved_key
    if not api_key:
        raise HTTPException(status_code=400, detail="No API key saved. Save your LetsRunFaster API key first.")
    latest_url = app_settings.resolve_latest_url(remote_url)
    try:
        resp = http_requests.get(
            latest_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach LetsRunFaster: {e}")
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="LetsRunFaster rejected the API key.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LetsRunFaster returned HTTP {resp.status_code}.")
    try:
        body = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="LetsRunFaster returned a non-JSON response.")
    if not isinstance(body, dict) or not body.get("ok"):
        raise HTTPException(status_code=502, detail="LetsRunFaster returned an unexpected response.")
    return body


@app.post("/api/exports/clear")
def clear_export_folder(payload: ClearExportFolderRequest):
    """Delete all files strictly inside the specified export directory."""
    raw_dir = payload.output_dir.strip() if payload.output_dir else ""
    if not raw_dir:
        raise HTTPException(status_code=400, detail="Export folder path cannot be empty.")

    target_dir = Path(raw_dir).resolve()

    # Safety checks: target directory must exist and be a directory
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail=f"Directory '{raw_dir}' does not exist.")
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Path '{raw_dir}' is not a directory.")

    # Restrict against dangerous root/system directories.
    # NOTE: Path("/") is NOT the filesystem root on Windows (it resolves to
    # the current drive root, e.g. "C:\\"), so compare against the resolved
    # path's own anchor to catch "/" on POSIX and "C:\\" etc. on Windows.
    repo_root = Path(__file__).resolve().parent.parent
    if (
        str(target_dir) == target_dir.anchor
        or target_dir == Path.home()
        or target_dir == repo_root
    ):
        raise HTTPException(status_code=403, detail="Deleting root, home, or project root directory is forbidden.")

    # Prevent deleting inside source code directories
    if str(target_dir).startswith(str(repo_root / "garmin_engine")) or str(target_dir).startswith(str(repo_root / ".git")):
        raise HTTPException(status_code=403, detail="Cannot delete files within system or repository code directories.")

    deleted_count = 0
    errors = []

    # Strictly iterate over files/symlinks inside the export directory without leaving it
    for entry in target_dir.iterdir():
        if entry.is_file() or entry.is_symlink():
            try:
                # Ensure entry resolves strictly within target_dir
                if entry.parent.resolve() == target_dir:
                    entry.unlink()
                    deleted_count += 1
            except Exception as e:
                errors.append(f"{entry.name}: {str(e)}")

    if errors:
        raise HTTPException(status_code=500, detail=f"Deleted {deleted_count} files, but encountered errors: {'; '.join(errors)}")

    return {
        "success": True,
        "deleted_count": deleted_count,
        "directory": str(target_dir),
        "message": f"Successfully deleted {deleted_count} file(s) from {target_dir.name}."
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)


# Mount static html frontend
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
