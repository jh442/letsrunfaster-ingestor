"""
JSON-RPC / Stdout protocol server for Tauri desktop sidecar communication.
Communicates via newline-delimited JSON over stdin/stdout.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict

from .auth import GarminAuthManager, AuthStatus
from .exporter import GarminExporter, ExportFormat
from . import settings as app_settings


def send_response(req_id: Any, result: Any = None, error: Any = None):
    msg = {
        "jsonrpc": "2.0",
        "id": req_id,
    }
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_event(event_name: str, payload: Dict[str, Any]):
    msg = {
        "type": "event",
        "event": event_name,
        "payload": payload,
    }
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def run_rpc_server():
    auth_mgr = GarminAuthManager()
    active_client = None

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            send_response(None, error=f"Invalid JSON: {str(e)}")
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        try:
            if method == "check_auth":
                res = auth_mgr.login_with_tokens()
                send_response(req_id, {
                    "status": res.status.value,
                    "displayName": res.display_name,
                    "message": res.message,
                })

            elif method == "login":
                email = params.get("email")
                password = params.get("password")
                res, client = auth_mgr.login_with_credentials(email, password)
                active_client = client
                send_response(req_id, {
                    "status": res.status.value,
                    "message": res.message,
                    "displayName": res.display_name,
                    "mfaRequired": res.status == AuthStatus.MFA_REQUIRED,
                })

            elif method == "submit_mfa":
                mfa_code = params.get("code")
                client = active_client or auth_mgr.get_client()
                res = auth_mgr.submit_mfa(client, None, mfa_code)
                send_response(req_id, {
                    "status": res.status.value,
                    "message": res.message,
                    "displayName": res.display_name,
                })

            elif method == "logout":
                success = auth_mgr.clear_tokens()
                active_client = None
                send_response(req_id, {"success": success})

            elif method == "list_activities":
                start_date = params.get("startDate")
                end_date = params.get("endDate")
                activity_type = params.get("activityType")
                client = active_client or auth_mgr.get_client()
                client.login(tokenstore=auth_mgr.token_store)
                exporter = GarminExporter(client)
                activities = exporter.get_activities_in_range(start_date, end_date, activity_type)
                send_response(req_id, {
                    "activities": [
                        {
                            "id": a.activity_id,
                            "name": a.activity_name,
                            "type": a.activity_type,
                            "startTime": a.start_time_local,
                            "distanceMeters": a.distance_meters,
                            "durationSeconds": a.duration_seconds,
                        }
                        for a in activities
                    ]
                })

            elif method == "start_export":
                start_date = params.get("startDate")
                end_date = params.get("endDate")
                fmt_str = params.get("format", "fit").lower()
                fmt_map = {
                    "fit": ExportFormat.FIT,
                    "gpx": ExportFormat.GPX,
                    "tcx": ExportFormat.TCX,
                    "tpx": ExportFormat.TPX,
                }
                export_fmt = fmt_map.get(fmt_str, ExportFormat.FIT)
                output_dir = params.get("outputDir")
                remote_url = params.get("remoteUrl")
                remote_api_key = params.get("remoteApiKey")
                # Omitted push fields fall back to saved settings; explicit ""
                # disables pushing (same contract as the web UI endpoint).
                saved_url, saved_key = app_settings.get_remote_settings()
                if remote_url is None:
                    remote_url = saved_url
                elif not str(remote_url).strip():
                    remote_url = None
                if remote_api_key is None:
                    remote_api_key = saved_key
                elif not str(remote_api_key).strip():
                    remote_api_key = None
                activity_type = params.get("activityType")
                delay = float(params.get("delay", 0.5))

                client = active_client or auth_mgr.get_client()
                client.login(tokenstore=auth_mgr.token_store)
                exporter = GarminExporter(client)
                activities = exporter.get_activities_in_range(start_date, end_date, activity_type)

                total = len(activities)
                send_event("export_started", {"total": total})

                def on_progress(cur: int, tot: int, res):
                    send_event("export_progress", {
                        "current": cur,
                        "total": tot,
                        "activityId": res.activity_id,
                        "filename": res.filename,
                        "success": res.success,
                        "sizeBytes": res.size_bytes,
                        "savedPath": res.saved_path,
                        "remoteSynced": res.remote_synced,
                        "error": res.error_message,
                    })

                results = exporter.bulk_export(
                    activities=activities,
                    export_format=export_fmt,
                    output_dir=Path(output_dir) if output_dir else None,
                    remote_url=remote_url,
                    remote_api_key=remote_api_key,
                    delay_seconds=delay,
                    progress_callback=on_progress,
                )

                send_response(req_id, {
                    "total": total,
                    "succeeded": sum(1 for r in results if r.success),
                    "failed": sum(1 for r in results if not r.success),
                })

            elif method == "get_remote_settings":
                send_response(req_id, app_settings.public_config())

            elif method == "save_remote_settings":
                saved = app_settings.save_config(
                    remote_url=params.get("remoteUrl"),
                    remote_api_key=params.get("remoteApiKey"),
                    clear_key=bool(params.get("clearKey", False)),
                )
                send_response(req_id, {"success": True, **saved})

            else:
                send_response(req_id, error=f"Unknown method: {method}")

        except Exception as e:
            send_response(req_id, error=str(e))


if __name__ == "__main__":
    run_rpc_server()
