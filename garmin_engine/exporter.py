"""Garmin exporter for querying activities and downloading FIT/GPX files."""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import io
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Generator, List, Optional
import zipfile
import requests
from garminconnect import Garmin

from .fit_parser import parse_fit_to_payload
from . import settings as app_settings


class ExportFormat(str, Enum):
    FIT = "fit"
    GPX = "gpx"
    TCX = "tcx"
    TPX = "tpx"


@dataclass
class ActivityItem:
    activity_id: int
    activity_name: str
    activity_type: str
    start_time_local: str
    distance_meters: Optional[float] = None
    duration_seconds: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActivityItem":
        type_info = data.get("activityType", {})
        type_key = type_info.get("typeKey", "activity") if isinstance(type_info, dict) else "activity"
        return cls(
            activity_id=data.get("activityId", 0),
            activity_name=data.get("activityName", "activity"),
            activity_type=type_key,
            start_time_local=data.get("startTimeLocal", ""),
            distance_meters=data.get("distance"),
            duration_seconds=data.get("duration"),
        )


@dataclass
class ExportResult:
    activity_id: int
    filename: str
    format: ExportFormat
    success: bool
    size_bytes: int = 0
    error_message: Optional[str] = None
    saved_path: Optional[str] = None
    remote_synced: bool = False
    remote_matched: Optional[bool] = None
    remote_detail: Optional[str] = None
    remote_converted_plan: bool = False
    # Non-block awareness (server `linkage`, added with independent runs).
    # `remote_linkage` is "block" when an older server omits the key, so the
    # UI can keep its legacy mapping without guessing from `matched`.
    remote_linkage: Optional[str] = None
    remote_created: Optional[bool] = None
    remote_deleted: bool = False
    remote_social_activity_id: Optional[int] = None
    remote_activity_url: Optional[str] = None


def _int_or_none(value: Any) -> Optional[int]:
    """Tolerant int coercion for JSON ids (old servers, string ids)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_remote_response(body: Any, remote_url: Optional[str]) -> Dict[str, Any]:
    """Map an import response body onto the remote_* result fields.

    Tolerant by design: the ingestor must keep working against an older site
    that has no ``linkage`` and returns ``no_block_found``. A missing key means
    legacy/block, never "assume the caller wanted a run" — only the server's
    explicit ``linkage:"nonblock"`` enables the run status.
    """
    mapped: Dict[str, Any] = {
        "remote_matched": None,
        "remote_detail": None,
        "remote_converted_plan": False,
        "remote_linkage": None,
        "remote_created": None,
        "remote_deleted": False,
        "remote_social_activity_id": None,
        "remote_activity_url": None,
    }
    if not isinstance(body, dict):
        return mapped

    linkage = body.get("linkage")
    linkage = linkage.strip().lower() if isinstance(linkage, str) else ""
    mapped["remote_linkage"] = linkage if linkage in ("block", "nonblock") else "block"
    if "matched" in body:
        mapped["remote_matched"] = bool(body["matched"])
    if "created" in body:
        mapped["remote_created"] = bool(body["created"])
    mapped["remote_deleted"] = bool(body.get("deleted"))
    mapped["remote_converted_plan"] = bool(body.get("converted_plan"))
    mapped["remote_social_activity_id"] = _int_or_none(body.get("social_activity_id"))
    url = body.get("url")
    if isinstance(url, str) and url.strip():
        mapped["remote_activity_url"] = app_settings.site_url_for(remote_url, url)

    if mapped["remote_deleted"]:
        # The owner deleted this run on the site. The unique key survives, so
        # a re-push is a deliberate no-op rather than a resurrection.
        mapped["remote_detail"] = "deleted"
    elif mapped["remote_linkage"] == "nonblock":
        if mapped["remote_created"]:
            mapped["remote_detail"] = "run"
        elif body.get("deduped"):
            mapped["remote_detail"] = "duplicate"
        else:
            mapped["remote_detail"] = str(body.get("code") or "")
    elif body.get("deduped"):
        mapped["remote_detail"] = "duplicate"
    elif body.get("replaced"):
        mapped["remote_detail"] = "updated:" + str(body.get("slot") or "")
    else:
        mapped["remote_detail"] = str(body.get("code") or body.get("appended_to") or "")
    return mapped


def sort_activities_oldest_first(activities: List[ActivityItem]) -> List[ActivityItem]:
    """Order activities chronologically (oldest first) for export.

    Garmin returns newest-first, which would land the day's *later* run in
    the block's primary slot and the earlier run as secondary. Exporting
    oldest-first keeps first-run → primary, second-run → secondary.
    ``start_time_local`` sorts lexicographically (``YYYY-MM-DD HH:MM:SS``);
    missing timestamps sink last, ties break by activity id. Pure (copy).
    """
    return sorted(
        activities,
        key=lambda a: ((a.start_time_local or "9999"), a.activity_id),
    )


def sanitize_filename(name: str) -> str:
    """Sanitize activity names for safe filesystem storage."""
    s = re.sub(r"[^\w\s-]", "", name).strip().lower()
    return re.sub(r"[-\s]+", "_", s)


class GarminExporter:
    """Handles fetching activities and downloading files."""

    def __init__(self, client: Garmin):
        self.client = client

    def get_activities_in_range(
        self,
        start_date: date | str,
        end_date: date | str,
        activity_type: Optional[str] = None,
    ) -> List[ActivityItem]:
        """Fetch all activities within the specified date range."""
        start_str = start_date.strftime("%Y-%m-%d") if isinstance(start_date, (date, datetime)) else str(start_date)
        end_str = end_date.strftime("%Y-%m-%d") if isinstance(end_date, (date, datetime)) else str(end_date)

        raw_activities = self.client.get_activities_by_date(
            startdate=start_str,
            enddate=end_str,
            activitytype=activity_type,
        )

        return [ActivityItem.from_dict(item) for item in raw_activities]

    def download_activity_data(
        self,
        activity_id: int,
        export_format: ExportFormat,
    ) -> tuple[bytes, str]:
        """
        Download activity in requested format.
        Garmin downloads 'ORIGINAL' format as a ZIP containing the .fit file.
        We extract the .fit file directly if it is a ZIP.
        """
        if export_format == ExportFormat.FIT:
            dl_fmt = self.client.ActivityDownloadFormat.ORIGINAL
            raw_bytes = self.client.download_activity(str(activity_id), dl_fmt=dl_fmt)
            
            # Check if raw_bytes is a zip file (which Garmin returns for ORIGINAL)
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    for filename in zf.namelist():
                        if filename.lower().endswith(".fit"):
                            return zf.read(filename), "fit"
            except zipfile.BadZipFile:
                # Not a zip, return as raw .fit
                pass
            return raw_bytes, "fit"

        elif export_format == ExportFormat.GPX:
            dl_fmt = self.client.ActivityDownloadFormat.GPX
            raw_bytes = self.client.download_activity(str(activity_id), dl_fmt=dl_fmt)
            return raw_bytes, "gpx"
        elif export_format in (ExportFormat.TCX, ExportFormat.TPX):
            # Garmin provides fitness XML (heart rate, cadence, laps) via TCX
            dl_fmt = self.client.ActivityDownloadFormat.TCX
            raw_bytes = self.client.download_activity(str(activity_id), dl_fmt=dl_fmt)
            ext = "tcx" if export_format == ExportFormat.TCX else "tpx"
            return raw_bytes, ext
        else:
            raise ValueError(f"Unsupported export format: {export_format}")

    def export_activity(
        self,
        activity: ActivityItem,
        export_format: ExportFormat,
        output_dir: Optional[Path] = None,
        remote_url: Optional[str] = None,
        remote_api_key: Optional[str] = None,
        remote_mode: Optional[str] = None,
        import_private: bool = False,
    ) -> ExportResult:
        """Download single activity, save locally and/or push to remote API.

        ``remote_mode`` is sent as a **form field** (never a query parameter):
        the saved target is suffix-matched to derive ``/activities/latest``, so
        appending ``?mode=`` would break sync-new. Older servers ignore it.
        ``import_private`` sends ``is_private=1`` only when enabled, leaving the
        server's public default untouched otherwise.
        """
        try:
            data, ext = self.download_activity_data(activity.activity_id, export_format)
            
            # Construct friendly filename: YYYY-MM-DD_type_id_name.ext
            date_prefix = activity.start_time_local[:10] if activity.start_time_local else "activity"
            clean_name = sanitize_filename(activity.activity_name)
            filename = f"{date_prefix}_{activity.activity_type}_{activity.activity_id}_{clean_name}.{ext}"

            saved_path_str: Optional[str] = None
            if output_dir:
                output_path = Path(output_dir)
                output_path.mkdir(parents=True, exist_ok=True)
                file_dest = output_path / filename
                file_dest.write_bytes(data)
                saved_path_str = str(file_dest)

            remote_synced = False
            remote_fields: Dict[str, Any] = parse_remote_response(None, remote_url)
            if remote_url:
                headers = {}
                if remote_api_key:
                    headers["Authorization"] = f"Bearer {remote_api_key}"

                files = {
                    "file": (filename, data, "application/octet-stream")
                }
                form_data = {
                    "activity_id": str(activity.activity_id),
                    "activity_name": activity.activity_name,
                    "activity_type": activity.activity_type,
                    "start_time": activity.start_time_local,
                    "format": ext,
                }
                # Additive contract: absent `mode` is the legacy block path,
                # so an old server simply ignores these fields.
                if remote_mode:
                    form_data["mode"] = str(remote_mode)
                if import_private:
                    form_data["is_private"] = "1"
                # Parsed payload (mirrors the site's ParsedGpxResult) so the
                # import endpoint can append to the matching block day.
                # FIT only — GPX/TCX send thin metadata + file.
                if ext == "fit":
                    parsed = parse_fit_to_payload(data, activity.activity_name)
                    for key in (
                        "distance_miles", "avg_pace", "avg_hr", "max_hr",
                        "avg_cadence", "elev_gain_ft", "finish_time",
                        "device", "telemetry_json",
                    ):
                        if parsed.get(key) not in (None, ""):
                            form_data[key] = str(parsed[key])
                else:
                    if activity.distance_meters:
                        form_data["distance_miles"] = str(round(activity.distance_meters * 0.000621371, 2))
                resp = requests.post(remote_url, headers=headers, data=form_data, files=files, timeout=30)
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    # 4xx/5xx bodies carry the actionable message ("mode must
                    # be...", "Unauthorized..."). Surface it on that activity's
                    # result instead of aborting the whole export with a bare
                    # "422 Client Error".
                    detail = ""
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            detail = str(body.get("error") or body.get("message") or "")
                    except Exception:
                        detail = str(getattr(resp, "text", "") or "")[:200]
                    raise RuntimeError(f"{exc}" + (f" — {detail}" if detail else "")) from exc
                remote_synced = True
                try:
                    remote_fields = parse_remote_response(resp.json(), remote_url)
                except Exception:
                    # A 200 with a non-JSON body (proxy page) still means the
                    # file landed; keep the legacy empty mapping.
                    pass

            return ExportResult(
                activity_id=activity.activity_id,
                filename=filename,
                format=export_format,
                success=True,
                size_bytes=len(data),
                saved_path=saved_path_str,
                remote_synced=remote_synced,
                **remote_fields,
            )

        except Exception as e:
            date_prefix = activity.start_time_local[:10] if activity.start_time_local else "activity"
            filename = f"{date_prefix}_{activity.activity_id}.{export_format.value}"
            return ExportResult(
                activity_id=activity.activity_id,
                filename=filename,
                format=export_format,
                success=False,
                error_message=str(e),
            )

    def bulk_export(
        self,
        activities: List[ActivityItem],
        export_format: ExportFormat,
        output_dir: Optional[Path] = None,
        remote_url: Optional[str] = None,
        remote_api_key: Optional[str] = None,
        delay_seconds: float = 0.5,
        progress_callback: Optional[Callable[[int, int, ExportResult], None]] = None,
        remote_mode: Optional[str] = None,
        import_private: bool = False,
    ) -> List[ExportResult]:
        """Bulk export multiple activities with rate-limiting backoff."""
        results: List[ExportResult] = []
        activities = sort_activities_oldest_first(activities)
        total = len(activities)

        for idx, act in enumerate(activities, start=1):
            res = self.export_activity(
                activity=act,
                export_format=export_format,
                output_dir=output_dir,
                remote_url=remote_url,
                remote_api_key=remote_api_key,
                remote_mode=remote_mode,
                import_private=import_private,
            )
            results.append(res)
            if progress_callback:
                progress_callback(idx, total, res)
            
            # Rate-limiting backoff between requests
            if idx < total and delay_seconds > 0:
                time.sleep(delay_seconds)

        return results
