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


def _moving_average(vals: List[Optional[float]], half: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    for i in range(len(vals)):
        total, n = 0.0, 0
        for k in range(i - half, i + half + 1):
            if 0 <= k < len(vals):
                v = vals[k]
                if v is not None:
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    if f != f:  # NaN guard
                        continue
                    total += f
                    n += 1
        out[i] = (total / n) if n else None
    return out


def _build_canonical_time_series(
    samples: List[Dict[str, Any]], max_points: int = 350
) -> List[Dict[str, Any]]:
    """Legacy {distMi,timeSec,eleFt,hr,cad} -> frontend WorkoutTelemetryPoint.

    Mirrors the web client's buildSmoothedTimeSeries: pace from a ±15s
    rolling window (kills 1Hz GPS jitter), then centered smoothing. Output
    keys are exactly what ElevationChart reads: distanceMi, timeSec,
    paceMinPerMi, hrBpm, cadenceSpm, elevationFt.
    """
    n = len(samples)
    if n < 2:
        return []
    dist = [float(s.get("distMi") or 0.0) for s in samples]
    tsec = [float(s.get("timeSec") or 0.0) for s in samples]
    ele = [s.get("eleFt") for s in samples]
    hrs = [s.get("hr") for s in samples]
    cads = [s.get("cad") for s in samples]

    raw_pace: List[Optional[float]] = [None] * n
    for i in range(n):
        t0 = tsec[i]
        lo, hi = i, i
        while lo > 0 and t0 - tsec[lo - 1] <= 15:
            lo -= 1
        while hi < n - 1 and tsec[hi + 1] - t0 <= 15:
            hi += 1
        d_dist = dist[hi] - dist[lo]
        d_time = tsec[hi] - tsec[lo]
        if d_dist > 0.003 and d_time > 1:
            mph = d_dist / (d_time / 3600.0)
            if mph > 20 or mph < 0.3:
                raw_pace[i] = None
            else:
                raw_pace[i] = d_time / d_dist
        else:
            raw_pace[i] = None

    filled = list(raw_pace)
    for i in range(n):
        if filled[i] is None:
            prev = i - 1
            while prev >= 0 and filled[prev] is None:
                prev -= 1
            nxt = i + 1
            while nxt < n and raw_pace[nxt] is None:
                nxt += 1
            a = filled[prev] if prev >= 0 else None
            b = raw_pace[nxt] if nxt < n else None
            filled[i] = ((a + b) / 2) if (a is not None and b is not None) else (a if a is not None else b)

    clamped = [None if p is None else min(2400.0, max(180.0, p)) for p in filled]
    smooth_pace = _moving_average(clamped, 7)
    smooth_hr = _moving_average(
        [float(h) if isinstance(h, (int, float)) and 30 < h < 240 else None for h in hrs], 5
    )
    smooth_ele = _moving_average(
        [float(e) if isinstance(e, (int, float)) else None for e in ele], 4
    )
    smooth_cad = _moving_average(
        [float(c) if isinstance(c, (int, float)) and 30 <= c < 260 else None for c in cads], 5
    )

    defined_pace = sorted(p for p in smooth_pace if p is not None)
    median_pace = defined_pace[len(defined_pace) // 2] if defined_pace else 600.0
    defined_hr = [h for h in smooth_hr if h is not None]
    avg_hr_fb = (sum(defined_hr) / len(defined_hr)) if defined_hr else None

    full: List[Dict[str, Any]] = []
    for i, s in enumerate(samples):
        pace_sec = smooth_pace[i] if smooth_pace[i] is not None else median_pace
        hr_v = smooth_hr[i] if smooth_hr[i] is not None else avg_hr_fb
        cad_raw = cads[i]
        cad_fb = (
            int(cad_raw)
            if isinstance(cad_raw, (int, float)) and 30 <= cad_raw < 260
            else 0
        )
        full.append({
            "distanceMi": round(dist[i], 2),
            "timeSec": round(tsec[i]),
            "paceMinPerMi": round((pace_sec / 60.0) * 100) / 100,
            "hrBpm": int(round(hr_v)) if hr_v is not None else 0,
            "cadenceSpm": int(round(smooth_cad[i])) if smooth_cad[i] is not None else cad_fb,
            "elevationFt": int(round(smooth_ele[i])) if smooth_ele[i] is not None else int(ele[i] or 0),
        })
    if len(full) <= max_points:
        return full
    out: List[Dict[str, Any]] = []
    stride = (len(full) - 1) / (max_points - 1)
    for i in range(max_points):
        out.append(full[round(i * stride)])
    return out


def _value_at_distance(dist: List[float], ys: List[float], target: float) -> float:
    if target <= dist[0]:
        return ys[0]
    for i in range(1, len(dist)):
        if dist[i] >= target:
            d0, d1 = dist[i - 1], dist[i]
            if d1 <= d0:
                return ys[i]
            ratio = (target - d0) / (d1 - d0)
            return ys[i - 1] + ratio * (ys[i] - ys[i - 1])
    return ys[-1]


def _build_splits_from_laps(laps: List[Dict[str, Any]], baseline_hr: int = 162) -> Optional[List[Dict[str, Any]]]:
    """Mile splits straight from FIT lap messages (Garmin Connect numbers)."""
    usable = [
        lap for lap in laps
        if isinstance(lap.get("total_distance"), (int, float))
        and isinstance(lap.get("total_timer_time"), (int, float))
    ]
    if not usable:
        return None
    splits: List[Dict[str, Any]] = []
    cumulative = 0.0
    fastest_idx, fastest_pace = -1, float("inf")
    for lap in usable:
        dist_miles = float(lap["total_distance"]) * 0.000621371
        if dist_miles < 0.05:
            continue
        seg_time = max(1.0, float(lap["total_timer_time"]))
        pace_sec = seg_time / max(0.01, dist_miles)
        cumulative += seg_time
        hr_v = lap.get("avg_heart_rate")
        hr = int(round(hr_v)) if isinstance(hr_v, (int, float)) and 30 < hr_v < 240 else baseline_hr
        cad: Optional[int] = None
        raw_cad = lap.get("avg_cadence", lap.get("avg_running_cadence"))
        if isinstance(raw_cad, (int, float)) and 30 <= raw_cad < 260:
            cad = int(round(raw_cad * 2 if raw_cad < 120 else raw_cad))
        elev_delta = 0
        if isinstance(lap.get("total_ascent"), (int, float)) or isinstance(lap.get("total_descent"), (int, float)):
            asc = float(lap.get("total_ascent") or 0)
            desc = float(lap.get("total_descent") or 0)
            elev_delta = round((asc - desc) * 3.28084)
        split: Dict[str, Any] = {
            "mile": len(splits) + 1,
            "time": _fmt_duration(seg_time),
            "cumulativeTime": _fmt_duration(cumulative),
            "pace": _fmt_duration(pace_sec),
            "hr": hr,
            "elevDelta": elev_delta,
            "isFastest": False,
        }
        if cad is not None:
            split["cadence"] = cad
        splits.append(split)
        if pace_sec < fastest_pace:
            fastest_pace, fastest_idx = pace_sec, len(splits) - 1
    if not splits:
        return None
    if fastest_idx >= 0:
        splits[fastest_idx]["isFastest"] = True
    return splits


def _build_splits_from_telemetry(
    samples: List[Dict[str, Any]], baseline_hr: int = 162
) -> List[Dict[str, Any]]:
    """Real mile-by-mile splits by interpolating cumulative distance/time."""
    if len(samples) < 2:
        return []
    dist = [float(s.get("distMi") or 0.0) for s in samples]
    tsec = [float(s.get("timeSec") or 0.0) for s in samples]
    total = dist[-1]
    if total < 0.15:
        return []
    full_miles = int(total // 1)
    remainder = total - full_miles
    boundaries = [float(m) for m in range(1, full_miles + 1)]
    if remainder > 0.05:
        boundaries.append(total)
    ele_vals = [float(s.get("eleFt") or 0.0) for s in samples]
    splits: List[Dict[str, Any]] = []
    prev_time, prev_dist = tsec[0], 0.0
    prev_ele = _value_at_distance(dist, ele_vals, 0.0)
    fastest_idx, fastest_pace = -1, float("inf")
    for idx, b in enumerate(boundaries):
        t_at = _value_at_distance(dist, tsec, b)
        ele_at = _value_at_distance(dist, ele_vals, b)
        seg_dist = b - prev_dist
        seg_time = max(1.0, t_at - prev_time)
        pace_sec = seg_time / max(0.01, seg_dist)
        hr_sum, hr_n, cad_sum, cad_n = 0.0, 0, 0.0, 0
        for s in samples:
            d = float(s.get("distMi") or 0.0)
            if prev_dist - 1e-9 < d <= b + 1e-9:
                h = s.get("hr")
                if isinstance(h, (int, float)) and 30 < h < 240:
                    hr_sum += h
                    hr_n += 1
                c = s.get("cad")
                if isinstance(c, (int, float)) and 30 <= c < 260:
                    cad_sum += c
                    cad_n += 1
        cumulative = t_at - tsec[0]
        split: Dict[str, Any] = {
            "mile": idx + 1,
            "time": _fmt_duration(seg_time),
            "cumulativeTime": _fmt_duration(cumulative),
            "pace": _fmt_duration(pace_sec),
            "hr": int(round(hr_sum / hr_n)) if hr_n else baseline_hr,
            "elevDelta": round(ele_at - prev_ele),
            "isFastest": False,
        }
        if cad_n:
            split["cadence"] = int(round(cad_sum / cad_n))
        splits.append(split)
        if pace_sec < fastest_pace:
            fastest_pace, fastest_idx = pace_sec, idx
        prev_time, prev_dist, prev_ele = t_at, b, ele_at
    if fastest_idx >= 0:
        splits[fastest_idx]["isFastest"] = True
    return splits


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

    # Canonical frontend shape (WorkoutTelemetryPoint): the chart reads
    # distanceMi / elevationFt / paceMinPerMi / hrBpm — never the legacy
    # distMi / eleFt / hr shorthand. Pace comes from a ±15s rolling window
    # (mirrors the web client's buildSmoothedTimeSeries) and output is
    # capped at ~350 points so marathon FITs fit the 500KB server cap.
    time_series = _build_canonical_time_series(series)
    splits = _build_splits_from_laps(laps, avg_hr if hr_n else 162)
    if splits is None:
        splits = _build_splits_from_telemetry(series, avg_hr if hr_n else 162)

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
        "splits": splits,
        "timeSeries": time_series,
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
