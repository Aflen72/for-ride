"""Small read-only Intervals.icu adapter. No calendar writes or credential storage."""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .provenance import canonical_source, classify_source


class ICUError(RuntimeError):
    """Safe-to-display connector error (never includes response body or credentials)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward authorization, even to another HTTPS destination.
        return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _date(value):
    if not isinstance(value, str):
        raise ValueError("Date must use YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Date must use YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def _record_date(record, *keys):
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            try:
                return _date(value[:10]).isoformat()
            except ValueError:
                pass
    return None


def _zone_list(value, boundaries=False):
    if not isinstance(value,list) or not 1 <= len(value) <= 12:
        return None
    clean=[_number(v) for v in value]
    if any(v is None or v > (250 if boundaries else 86400) or (boundaries and v < 1) for v in clean):
        return None
    if boundaries and any(a>=b for a,b in zip(clean,clean[1:])):
        return None
    return clean


def normalize_activity(record):
    """Whitelist fields; missing upstream values remain null, never inferred as zero."""
    source = record.get("source")
    source = canonical_source(source)
    kind = record.get("type") or record.get("sport")
    kind = kind if isinstance(kind, str) else None
    trainer = record.get("trainer")
    indoor = trainer if isinstance(trainer, bool) else None
    if kind in ("VirtualRide", "Virtual Cycling", "Indoor Cycling"):
        indoor = True
    elif kind is not None and kind not in ("Ride", "Cycling", "EBikeRide"):
        indoor = False
    # strava_id can link a replacement GARMIN_CONNECT/SUUNTO/etc. activity;
    # it is NOT evidence that its training metrics originated at Strava.
    status = classify_source(record)
    restricted = {"restricted": True, "independent": False, "unknown": None}[status]
    duration = _number(record.get("moving_time"))
    if duration is None:
        duration = _number(record.get("elapsed_time"))
    result = {
        "id": str(record["id"]) if record.get("id") is not None else None,
        "date": _record_date(record, "start_date_local", "start_date", "date"),
        "sport": kind,
        "indoor": indoor,
        "duration_s": duration,
        "avg_power_w": _number(record.get("icu_average_watts", record.get("average_watts"))),
        "normalized_power_w": _number(record.get("icu_weighted_avg_watts")),
        "avg_hr_bpm": _number(record.get("average_heartrate")),
        "source": source,
        "restricted": restricted,
        "source_status": status,
        "hr_zone_upper_bpm": _zone_list(record.get("icu_hr_zones"), boundaries=True),
        "hr_zone_time_s": _zone_list(record.get("icu_hr_zone_times")),
    }
    if result['hr_zone_upper_bpm'] is not None and result['hr_zone_time_s'] is not None and len(result['hr_zone_upper_bpm']) != len(result['hr_zone_time_s']):
        result['hr_zone_time_s'] = None
    if restricted:
        result["withheld_reason"] = "Strava-origin/restricted data may be unavailable through Intervals.icu; no bypass attempted."
    elif status == "unknown":
        result["withheld_reason"] = "Source unverified; training metrics excluded pending source confirmation. Not evidence of zero training."
        if kind is None and duration is None:
            result["withheld_reason"] += " Metadata-only activity."
    elif kind is None and duration is None:
        result["withheld_reason"] = "Metadata-only activity: source and training metrics unavailable; not evidence of no training."
    result["available_fields"] = [key for key, value in result.items() if value is not None and key not in ("available_fields", "withheld_reason")]
    return result


def normalize_wellness(record):
    sleep_seconds = _number(record.get("sleepSecs"))
    hrv = _number(record.get("hrv"))
    hrv_type = "RMSSD" if hrv is not None else None
    if hrv is None:
        hrv = _number(record.get("hrvSDNN"))
        hrv_type = "SDNN" if hrv is not None else None
    return {
        "date": _record_date(record, "id", "date"),
        "sleep_hours": sleep_seconds / 3600 if sleep_seconds is not None else None,
        "hrv_ms": hrv,
        "hrv_type": hrv_type,
        # The general API does not identify the HRV measurement device reliably.
        "device": None,
        "resting_hr_bpm": _number(record.get("restingHR")),
    }


class ICUClient:
    BASE_URL = "https://intervals.icu/api/v1"
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    MAX_RANGE_DAYS = 366

    def __init__(self, api_key: str, athlete_id: str = "0", timeout: float = 20):
        if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
            raise ValueError("A nonempty API key is required")
        athlete_id = str(athlete_id)
        if not re.fullmatch(r"(?:0|i?\d+)", athlete_id):
            raise ValueError("Invalid athlete ID")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("Timeout must be between 0 and 120 seconds")
        self._authorization = "Basic " + base64.b64encode(("API_KEY:" + api_key).encode()).decode()
        self.athlete_id = athlete_id
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_env(cls):
        key = os.environ.get("INTERVALS_API_KEY")
        if not key:
            raise ICUError("Set INTERVALS_API_KEY locally before requesting live data")
        return cls(key, os.environ.get("INTERVALS_ATHLETE_ID", "0"))

    def _get(self, endpoint: str, oldest: str, newest: str):
        start, end = _date(oldest), _date(newest)
        if end < start or (end - start).days > self.MAX_RANGE_DAYS:
            raise ValueError("Date range must be ordered and no longer than 366 days")
        # endpoint is exclusively a literal selected by the two public methods.
        query = urllib.parse.urlencode({"oldest": oldest, "newest": newest})
        url = f"{self.BASE_URL}/athlete/{self.athlete_id}/{endpoint}?{query}"
        request = urllib.request.Request(url, headers={"Authorization": self._authorization, "Accept": "application/json"}, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read(self.MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ICUError(f"Intervals.icu HTTP {status}; check permission, credentials, or service availability") from None
        except Exception:
            # URL/transport errors may contain sensitive details in their repr.
            raise ICUError("Intervals.icu request failed; check connectivity and retry manually") from None
        if len(data) > self.MAX_RESPONSE_BYTES:
            raise ICUError("Intervals.icu response too large; use a smaller date range")
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise ICUError("Intervals.icu returned invalid JSON") from None
        if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
            raise ICUError("Intervals.icu returned an unexpected data shape")
        return parsed

    def activities(self, oldest: str, newest: str):
        return [normalize_activity(row) for row in self._get("activities", oldest, newest)]

    def wellness(self, oldest: str, newest: str):
        return [normalize_wellness(row) for row in self._get("wellness", oldest, newest)]
