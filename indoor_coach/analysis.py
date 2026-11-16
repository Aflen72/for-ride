"""Transparent, non-diagnostic indoor-training summaries and draft suggestions.

Thresholds are conservative product heuristics, NOT validated fatigue cutoffs.
There is deliberately no composite readiness score or automatic FTP estimation.
"""
from datetime import date, timedelta
import math
from statistics import mean
from .provenance import classify_source

VERSION = "heuristics-0.1"
EVIDENCE = [
    {"topic": "HRV individual trends", "url": "https://doi.org/10.1249/MSS.0000000000000910", "limit": "Runner study; does not validate this app's thresholds or a seven-day multi-metric score."},
    {"topic": "Consumer sleep stages", "url": "https://pubmed.ncbi.nlm.nih.gov/33378539/", "limit": "Device-dependent accuracy; sleep stages are descriptive only."},
    {"topic": "Readiness scores", "url": "https://doi.org/10.1515/teb-2025-0001", "limit": "No universal validated weighting assumed."},
]
NUMERIC = ("sleep_hours", "hrv_ms", "resting_hr_bpm", "deep_sleep_pct", "light_sleep_pct", "fatigue")


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _bedtime(value):
    if not isinstance(value, str):
        return None
    try:
        h, m = map(int, value.split(":"))
        return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, TypeError):
        return None


def circular_bedtime(values):
    """Clock mean, never arithmetic average across midnight; None if undefined."""
    minutes = [v for v in (_bedtime(x) for x in values) if v is not None]
    if not minutes:
        return None
    s = mean(math.sin(v * math.tau / 1440) for v in minutes)
    c = mean(math.cos(v * math.tau / 1440) for v in minutes)
    if math.hypot(s, c) < 0.1:
        return None
    minute = round(math.atan2(s, c) % math.tau * 1440 / math.tau) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _reference(wellness, manual, as_of, today):
    days = {}
    for row in wellness:
        d = _day(row.get("date"))
        if d and as_of - timedelta(days=7) <= d < as_of:
            days[d] = row  # One record per day, last supplied version wins.
    refs, warnings = {}, []
    if manual:
        start, end = _day(manual.get("period_start")), _day(manual.get("period_end"))
        n = _number(manual.get("sample_count"))
        valid = start is not None and end is not None and as_of - timedelta(days=7) <= start <= end < as_of and n is not None and n.is_integer() and 1 <= n <= (end-start).days+1
        if not valid:
            warnings.append("Manual baseline ignored: require a valid prior-seven-day period and sample_count within that period.")
            manual = None
    for key in NUMERIC:
        comparable = list(days.values())
        if key == "hrv_ms":
            comparable = [r for r in comparable if today.get("device") and today.get("hrv_type") and r.get("device") == today.get("device") and r.get("hrv_type") == today.get("hrv_type")]
        vals = [n for n in (_number(r.get(key)) for r in comparable) if n is not None]
        refs[key] = {"mean": mean(vals) if vals else None, "sample_count": len(vals), "source": "daily_records"}
        if manual and _number(manual.get(key)) is not None:
            compatible = key != "hrv_ms" or (today.get("device") and today.get("hrv_type") and manual.get("device") == today.get("device") and manual.get("hrv_type") == today.get("hrv_type"))
            if compatible:
                refs[key] = {"mean": _number(manual[key]), "sample_count": int(manual["sample_count"]), "source": "manual_summary", "period_start": manual["period_start"], "period_end": manual["period_end"]}
    beds = [r.get("bedtime") for r in days.values()]
    refs["bedtime"] = {"mean": circular_bedtime(beds), "sample_count": sum(_bedtime(v) is not None for v in beds), "source": "daily_records"}
    if manual and _bedtime(manual.get("bedtime")) is not None:
        refs["bedtime"] = {"mean": manual["bedtime"], "sample_count": int(manual["sample_count"]), "source": "manual_summary"}
    return refs, warnings


def analyze(activities, wellness, baseline=None, as_of=None, ftp=None):
    as_date = _day(as_of)
    if as_date is None:
        raise ValueError("as_of must be an ISO date")
    ftp = _number(ftp)
    if ftp is not None and ftp <= 0:
        raise ValueError("ftp must be positive or null")
    today = next((r for r in reversed(wellness) if _day(r.get("date")) == as_date), {})
    refs, warnings = _reference(wellness, baseline, as_date, today)
    dimensions = {}
    for key in NUMERIC + ("bedtime",):
        current = today.get(key) if key == "bedtime" else _number(today.get(key))
        if key == "bedtime" and _bedtime(current) is None:
            current = None
        ref = refs[key]
        status, delta = "insufficient_data", None
        if current is not None and ref["mean"] is not None:
            delta = ((_bedtime(current) - _bedtime(ref["mean"]) + 720) % 1440 - 720) if key == "bedtime" else current - ref["mean"]
            status = "descriptive_only" if key in ("deep_sleep_pct", "light_sleep_pct") else "no_heuristic_flag"
            if ref["sample_count"] < 3:
                status = "limited_reference"
            elif ((key == "sleep_hours" and delta <= -1) or (key == "resting_hr_bpm" and delta >= 7) or (key == "bedtime" and abs(delta) >= 90) or (key == "fatigue" and current >= 7) or (key == "hrv_ms" and ref["mean"] > 0 and current / ref["mean"] <= 0.8)):
                status = "caution"
        if key == "fatigue" and current is not None and current >= 7:
            status = "caution"  # High self-reported fatigue does not need a baseline.
        dimensions[key] = {"current": current, "reference": ref, "delta": delta, "status": status}
    summaries, ignored, restricted, unknown, unknown_source = [], 0, 0, 0, 0
    seen = set()
    for i, a in enumerate(activities):
        d = _day(a.get("date"))
        if not d or not as_date - timedelta(days=6) <= d <= as_date:
            continue
        ident = str(a.get("id", f"row-{i}"))
        if ident in seen:
            continue
        seen.add(ident)
        source_status = classify_source(a)  # Recheck legacy/direct rows, not only connector output.
        if source_status == "restricted":
            restricted += 1
            continue
        if source_status == "unknown":
            unknown_source += 1
            continue
        if a.get("indoor") is not True:
            if a.get("indoor") is False:
                ignored += 1
            else:
                unknown += 1
            continue
        if str(a.get("sport", "")).lower() not in ("ride", "virtualride", "cycling", "indoorcycling", "indoor_cycling", "virtual_ride", "virtual cycling", "indoor cycling"):
            ignored += 1
            continue
        duration = _number(a.get("duration_s"))
        np = _number(a.get("normalized_power_w"))
        valid_load = duration is not None and duration > 0 and np is not None and np > 0 and ftp is not None
        intensity = np / ftp if valid_load else None
        summaries.append({"id": a.get("id"), "date": d.isoformat(), "duration_minutes": duration / 60 if duration is not None and duration >= 0 else None, "avg_power_w": _number(a.get("avg_power_w")), "normalized_power_w": np, "avg_hr_bpm": _number(a.get("avg_hr_bpm")), "intensity_factor": round(intensity, 3) if intensity is not None else None, "estimated_tss": round(duration / 3600 * intensity ** 2 * 100, 1) if valid_load else None, "source": a.get("source")})
    if restricted:
        warnings.append("Restricted activities exist: missing metrics are not zero load. Obtain an authorized original export if needed; do not bypass source restrictions.")
    if unknown_source:
        warnings.append("Activities with unverified source were excluded; missing load is unknown, not zero or confirmed Strava restriction.")
    if unknown:
        warnings.append("Some activities have unknown indoor status and were not counted.")
    flags = [k for k, v in dimensions.items() if v["status"] == "caution"]
    total = sum(a["estimated_tss"] for a in summaries if a["estimated_tss"] is not None)
    return {"schema_version": "0.1", "as_of": as_date.isoformat(), "rule_version": VERSION, "ftp_w": ftp, "ftp_updated": False, "load_ftp_basis": "All estimated TSS uses the caller-supplied current FTP; not historical FTP or original platform TSS.", "reference_window": {"start": (as_date-timedelta(days=7)).isoformat(), "end": (as_date-timedelta(days=1)).isoformat()}, "dimensions": dimensions, "caution_flags": flags, "activities": summaries, "load": {"known_minutes": round(sum(a["duration_minutes"] or 0 for a in summaries), 1), "estimated_tss_known_only": round(total, 1) if any(a["estimated_tss"] is not None for a in summaries) else None, "tss_complete": bool(summaries) and all(a["estimated_tss"] is not None for a in summaries) and not restricted and not unknown and not unknown_source, "restricted_count": restricted, "unknown_indoor_count": unknown, "unknown_source_count": unknown_source, "excluded_count": ignored}, "coverage": {"current_health_fields": sum(v["current"] is not None for v in dimensions.values()), "health_fields_total": len(dimensions), "indoor_activity_count": len(summaries)}, "warnings": warnings, "limitations": ["No composite readiness score; absence of a flag does not demonstrate recovery.", "Seven days is an initial personal reference, not a clinical baseline. Thresholds are unvalidated product heuristics.", "TSS is an estimate from normalized power, duration and configured FTP; missing power is not inferred from speed or heart rate.", "Bedtime is local clock time; use a consistent timezone. Stage percentages are not used to prescribe intensity.", "If unwell, dizzy, experiencing chest pain or unusual breathlessness, stop training and seek appropriate medical care."], "evidence": EVIDENCE}


def _workout(day, kind, ftp):
    if kind == "rest":
        return {"date": day.isoformat(), "kind": "rest", "duration_minutes": 0, "steps": [], "purpose": "Rest and reassess daily feedback"}
    blocks = [(10, .45, .55), (25, .55, .65), (5, .4, .5)] if kind == "easy" else [(10, .45, .55), (40, .6, .7), (5, .4, .5)]
    return {"date": day.isoformat(), "kind": kind, "duration_minutes": sum(b[0] for b in blocks), "steps": [{"label": ["warmup", "steady", "cooldown"][i], "duration_minutes": minutes, "ftp_fraction": [lo, hi], "power_w": [round(ftp*lo), round(ftp*hi)] if ftp else None, "effort": "comfortable conversational pace; reduce if this feels hard"} for i, (minutes, lo, hi) in enumerate(blocks)], "purpose": "Stable indoor endurance, not a maximal test"}


def recommend(analysis, ftp=None, as_of=None):
    day = _day(as_of or analysis.get("as_of"))
    if day is None:
        raise ValueError("as_of must be an ISO date")
    ftp = _number(ftp if ftp is not None else analysis.get("ftp_w"))
    if ftp is not None and ftp <= 0:
        raise ValueError("ftp must be positive or null")
    flags = analysis.get("caution_flags", [])
    load = analysis.get("load", {})
    coverage = analysis.get("coverage", {})
    current_relevant = sum(analysis.get("dimensions", {}).get(k, {}).get("current") is not None for k in ("sleep_hours", "hrv_ms", "resting_hr_bpm", "fatigue"))
    uncertain = load.get("unknown_source_count", 0) > 0 or load.get("restricted_count", 0) > 0 or load.get("unknown_indoor_count", 0) > 0 or current_relevant < 2 or coverage.get("indoor_activity_count", 0) == 0 or ftp is None or not load.get("tss_complete", False)
    today_load = sum(a.get("estimated_tss") or 0 for a in analysis.get("activities", []) if a.get("date") == day.isoformat())
    # Scheduling rules are transparent defaults, not an optimized/adaptive performance model.
    caution = bool(flags) or today_load >= 100
    kinds = ["rest" if caution else "easy" if uncertain else "endurance", "easy", "rest", "easy" if uncertain or caution else "endurance", "rest", "easy" if uncertain or caution else "endurance", "rest"]
    week = [_workout(day+timedelta(days=i+1), kind, ftp) for i, kind in enumerate(kinds)]
    return {"state": "proposed", "requires_confirmation": True, "execution_supported": False, "rule_version": VERSION, "next_workout": week[0], "week_plan": week, "rationale": {"caution_flags": flags, "incomplete_data": uncertain, "today_tss_known_only": today_load, "recent_seven_day_load_context": load, "recent_activity_context": analysis.get("activities", []), "approach": "Conservative static template; prior-day load and user goals are not used to optimize this schedule; daily feedback and explicit user confirmation are required before scheduling or changing intensity."}, "questions": ["Any unusual symptoms or soreness?", "How hard did the last ride feel (RPE 1–10)?", "Does this duration fit your available time?"], "limitations": ["No external calendar write is performed by this function.", "No automatic FTP change, high-intensity prescription, or diagnosis.", "This is a static schedule template, not a personalized or periodized model. Recent history is provided as context but only same-day load and health flags influence this version. User goals/preferences are not yet modeled.", "This seven-day draft is not a promise of recovery or an optimized training program."]}
