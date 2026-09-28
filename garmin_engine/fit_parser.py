"""Parse Garmin FIT bytes into the payload the LetsRunFaster site expects.

Mirrors the web client's garminParser.ts essentials (distance, pace, HR,
cadence, elevation, device, coordinates, elevation profile, time series)
so POST /api/activities/import can append the activity to a training-block
day without server-side FIT decoding. All failures return None fields —
callers send thin metadata only.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional


def _semi_to_deg(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    if abs(v) > 180:
        deg = v * (180.0 / 2147483648.0)
        return deg if -180.0 <= deg <= 180.0 else None
    return v


def _fmt_duration(total_seconds: float) -> str:
    s = max(0, int(total_seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def parse_fit_to_payload(data: bytes, activity_name: str = "") -> Dict[str, Any]:
    """Parse FIT binary -> flat payload dict. Never raises; {} on failure."""
    try:
        from fitparse import FitFile
    except ImportError:
        return {}
    try:
        fit = FitFile(io.BytesIO(data))
    except Exception:
        return {}

    records: List[Dict[str, Any]] = []
    laps: List[Dict[str, Any]] = []
    session: Dict[str, Any] = {}
    file_ids: List[Dict[str, Any]] = []
    device = "Garmin Watch"
    try:
        for msg in fit.get_messages():
            name = msg.name
            vals = {f.name: f.value for f in msg.fields if f.value is not None}
            if name == "record":
                records.append(vals)
            elif name == "lap":
                laps.append(vals)
            elif name == "session" and not session:
                session = vals
            elif name == "file_id":
                file_ids.append(vals)
            elif name == "device_info" and device == "Garmin Watch":
                prod = vals.get("product_name") or vals.get("garmin_product")
                if prod and not str(prod).isdigit():
                    device = str(prod)
        if device == "Garmin Watch":
            for fid in file_ids:
                manu = str(fid.get("manufacturer", "")).lower()
                if manu in ("1", "garmin"):
                    device = "Garmin Forerunner"
                    break
    except Exception:
        return {}

    if not records and not session:
        return {}

    dist_mi = 0.0
    hr_sum, hr_n, hr_max = 0, 0, 0
    cad_sum, cad_n = 0, 0
    elev_gain_ft = 0.0
    last_ele: Optional[float] = None
    coords: List[List[float]] = []
    ele_profile: List[Dict[str, float]] = []
    series: List[Dict[str, Any]] = []
    t0: Optional[float] = None

    for i, rec in enumerate(records):
        lat = _semi_to_deg(rec.get("position_lat"))
        lng = _semi_to_deg(rec.get("position_long"))
        alt = rec.get("enhanced_altitude", rec.get("altitude"))
        ele_ft = float(alt) * 3.28084 if isinstance(alt, (int, float)) else 0.0
        hr = rec.get("heart_rate")
        if isinstance(hr, (int, float)) and 35 < hr < 240:
            hr_sum += hr
            hr_n += 1
            hr_max = max(hr_max, hr)
        cad = rec.get("cadence")
        if isinstance(cad, (int, float)) and cad and cad > 30:
            c = cad * 2 if cad < 120 else cad
            cad_sum += c
            cad_n += 1
        d = rec.get("distance")
        if isinstance(d, (int, float)) and d > 0:
            dist_mi = float(d) * 0.000621371
        if ele_ft:
            if last_ele is not None:
                diff = ele_ft - last_ele
                if 1 < diff < 150:
                    elev_gain_ft += diff
            last_ele = ele_ft
        ts = rec.get("timestamp")
        t_sec: Optional[float] = None
        try:
            if ts is not None:
                t_sec = float(ts.timestamp()) if hasattr(ts, "timestamp") else float(ts)
        except Exception:
            t_sec = None
        if t_sec is not None and t0 is None:
            t0 = t_sec
        timer = rec.get("timer_time")
        rel = (float(timer) if isinstance(timer, (int, float)) else (t_sec - t0 if t_sec is not None and t0 is not None else 0.0))
        if lat is not None and lng is not None:
            coords.append([round(lat, 5), round(lng, 5)])
        series.append({
            "distMi": round(dist_mi, 3),
            "timeSec": round(rel, 1),
            "eleFt": round(ele_ft),
            **({"hr": int(hr)} if isinstance(hr, (int, float)) and 35 < hr < 240 else {}),
            **({"cad": int(cad * 2 if cad < 120 else cad)} if isinstance(cad, (int, float)) and cad and cad > 30 else {}),
        })
        if i % 15 == 0 or i == len(records) - 1:
            ele_profile.append({"distance": round(dist_mi, 2), "elevation": round(ele_ft)})

    total_timer = session.get("total_timer_time") or session.get("total_elapsed_time") or 0
    if isinstance(session.get("total_distance"), (int, float)) and session["total_distance"] > 0:
        dist_mi = float(session["total_distance"]) * 0.000621371
    if isinstance(session.get("total_ascent"), (int, float)) and session["total_ascent"] > 0:
        elev_gain_ft = float(session["total_ascent"]) * 3.28084
    if isinstance(session.get("avg_heart_rate"), (int, float)) and session["avg_heart_rate"] > 35:
        hr_sum, hr_n = float(session["avg_heart_rate"]), 1
    if isinstance(session.get("max_heart_rate"), (int, float)) and session["max_heart_rate"] > 35:
        hr_max = int(session["max_heart_rate"])
    for key in ("avg_cadence", "avg_running_cadence"):
        c = session.get(key)
        if isinstance(c, (int, float)) and c > 35:
            c = c * 2 if c < 120 else c
            cad_sum, cad_n = c, 1
            break

    dist_mi = round(dist_mi, 2)
    total_sec = float(total_timer) if total_timer else (series[-1]["timeSec"] if series else 0.0)
    avg_hr = round(hr_sum / hr_n) if hr_n else 0
    avg_cad = round(cad_sum / cad_n) if cad_n else 0
    avg_pace_sec = (total_sec / dist_mi) if dist_mi > 0 and total_sec > 0 else 0.0

    import json

    # Server caps telemetry_json at 500KB (same as PUT /days): downsample the
    # per-second series to <=600 points so even marathon FITs fit with room.
    if len(series) > 600:
        step = -(-len(series) // 600)
        series = series[::step]

    telemetry = {
        "device": device,
        "duration": _fmt_duration(total_sec),
        "movingTime": _fmt_duration(total_sec),
        "elapsedTime": _fmt_duration(total_sec),
        "elevGainFt": round(elev_gain_ft),
        "maxHr": int(hr_max) if hr_max else avg_hr,
        "avgHr": avg_hr,
        "avgCadence": avg_cad,
        "calories": round(dist_mi * 102),
        "routeCoordinates": coords,
        "elevationProfile": ele_profile,
        "timeSeries": series,
    }

    return {
        "distance_miles": dist_mi,
        "avg_pace": _fmt_duration(avg_pace_sec) if avg_pace_sec else "",
        "avg_hr": avg_hr,
        "max_hr": int(hr_max) if hr_max else avg_hr,
        "avg_cadence": avg_cad,
        "elev_gain_ft": round(elev_gain_ft),
        "finish_time": _fmt_duration(total_sec),
        "device": device,
        "telemetry_json": json.dumps(telemetry),
    }
