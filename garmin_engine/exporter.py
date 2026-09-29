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
    ) -> ExportResult:
        """Download single activity, save locally and/or push to remote API."""
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
            remote_matched: Optional[bool] = None
            remote_detail: Optional[str] = None
            remote_converted_plan = False
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
                resp.raise_for_status()
                remote_synced = True
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        if "matched" in body:
                            remote_matched = bool(body["matched"])
                        if body.get("converted_plan"):
                            remote_converted_plan = True
                        if body.get("deduped"):
                            remote_detail = "duplicate"
                        elif body.get("replaced"):
                            remote_detail = "updated:" + str(body.get("slot") or "")
                        else:
                            remote_detail = str(body.get("code") or body.get("appended_to") or "")
                except Exception:
                    pass

            return ExportResult(
                activity_id=activity.activity_id,
                filename=filename,
                format=export_format,
                success=True,
                size_bytes=len(data),
                saved_path=saved_path_str,
                remote_synced=remote_synced,
                remote_matched=remote_matched,
                remote_detail=remote_detail,
                remote_converted_plan=remote_converted_plan,
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
            )
            results.append(res)
            if progress_callback:
                progress_callback(idx, total, res)
            
            # Rate-limiting backoff between requests
            if idx < total and delay_seconds > 0:
                time.sleep(delay_seconds)

        return results
