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


USER_AGENT = "for-ride/0.1.0 (indoor cycling analysis; Python urllib)"


class ICUError(RuntimeError):
    """Safe structured diagnostics; never attach upstream body, URL, or credentials."""
    def __init__(self, message="Intervals.icu request failed", *, status=None,
                 stage="request", endpoint="unknown", reason="request_failed", edge_code=None):
        super().__init__(message)
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.stage = stage if stage in {"request", "activities", "wellness", "identity", "sport_settings", "account", "events", "event", "create", "readback"} else "request"
        self.endpoint = endpoint if endpoint in {"unknown", "athlete", "activities", "wellness", "sport_settings", "events", "event", "events_bulk"} else "unknown"
        self.reason = reason if reason in {"request_failed", "http_error", "forbidden", "edge_1010", "transport_error", "response_too_large", "invalid_json", "unexpected_shape", "invalid_identity", "identity_mismatch", "wrong_sport", "not_configured"} else "request_failed"
        self.edge_code = 1010 if edge_code == 1010 else None

    def as_dict(self):
        return {"http_status": self.status, "stage": self.stage,
                "endpoint": self.endpoint, "reason": self.reason, "edge_code": self.edge_code}



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
            raise ICUError("Set INTERVALS_API_KEY locally before requesting live data", reason="not_configured")
        return cls(key, os.environ.get("INTERVALS_ATHLETE_ID", "0"))

    def _request_json(self, method, suffix="", payload=None, *, stage="request",
                      endpoint="unknown", athlete_id=None):
        """Shared reader/writer transport. No retry, redirect, UA rotation or impersonation.

        Callers supply only fixed endpoint suffixes. Athlete profiles are projected by
        identity() immediately; never log or persist this low-level raw response.
        """
        athlete = self.athlete_id if athlete_id is None else str(athlete_id)
        if not re.fullmatch(r"(?:0|i?\d+)", athlete):
            raise ValueError("Invalid athlete ID")
        if not isinstance(suffix, str) or (suffix and not suffix.startswith(("/", "?"))) or any(c in suffix for c in "\r\n#"):
            raise ValueError("Invalid endpoint suffix")
        if ".." in suffix or suffix.startswith("//"):
            raise ValueError("Invalid endpoint suffix")
        url = f"{self.BASE_URL}/athlete/{athlete}{suffix}"
        request = urllib.request.Request(url,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
                            separators=(",", ":")).encode() if payload is not None else None,
            headers={"Authorization": self._authorization, "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": USER_AGENT}, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data = response.read(self.MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            # Only inspect a bounded prefix to distinguish an explicit edge error.
            # Never retain or expose the response body, headers, URL or exception.
            try:
                body = exc.read(4096).decode("utf-8", errors="replace")
                identified = status == 403 and bool(re.search(
                    r"(?:error(?:\s+code)?\s*[:#]?\s*1010\b|[\"'](?:code|error_code)[\"']\s*:\s*[\"']?1010\b)", body, re.I))
            except Exception:
                identified = False
            finally:
                exc.close()
            reason = "edge_1010" if identified else "forbidden" if status == 403 else "http_error"
            extra = "; explicit edge error 1010" if identified else ""
            raise ICUError(f"Intervals.icu HTTP {status}{extra}", status=status, stage=stage,
                           endpoint=endpoint, reason=reason, edge_code=1010 if identified else None) from None
        except Exception:
            raise ICUError("Intervals.icu request failed; check connectivity and retry manually",
                           stage=stage, endpoint=endpoint, reason="transport_error") from None
        if len(data) > self.MAX_RESPONSE_BYTES:
            raise ICUError("Intervals.icu response too large; use a smaller date range",
                           stage=stage, endpoint=endpoint, reason="response_too_large")
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise ICUError("Intervals.icu returned invalid JSON", stage=stage,
                           endpoint=endpoint, reason="invalid_json") from None

    def _get(self, endpoint: str, oldest: str, newest: str):
        start, end = _date(oldest), _date(newest)
        if end < start or (end - start).days > self.MAX_RANGE_DAYS:
            raise ValueError("Date range must be ordered and no longer than 366 days")
        if endpoint not in ("activities", "wellness"):
            raise ValueError("Unsupported endpoint")
        query = urllib.parse.urlencode({"oldest": oldest, "newest": newest})
        parsed = self._request_json("GET", f"/{endpoint}?{query}", stage=endpoint, endpoint=endpoint)
        if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
            raise ICUError("Intervals.icu returned an unexpected data shape", stage=endpoint,
                           endpoint=endpoint, reason="unexpected_shape")
        return parsed

    def identity(self):
        """Resolve configured account (0 means authenticated self), whitelist id/timezone.

        Official OpenAPI documents no fields filter for GET athlete. We therefore
        do not claim server-side projection: all other fields are discarded here.
        """
        data = self._request_json("GET", stage="identity", endpoint="athlete")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not re.fullmatch(r"i?\d+", data["id"]) or int(data["id"].removeprefix("i")) == 0:
            raise ICUError("Intervals.icu returned invalid account identity", stage="identity",
                           endpoint="athlete", reason="invalid_identity")
        if self.athlete_id != "0" and data["id"].removeprefix("i") != self.athlete_id.removeprefix("i"):
            raise ICUError("Intervals.icu account identity mismatch", stage="identity",
                           endpoint="athlete", reason="identity_mismatch")
        timezone = data.get("timezone")
        if not isinstance(timezone, str) or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,80}", timezone):
            timezone = None
        return {"id": data["id"], "timezone": timezone}

    def sport_settings(self):
        """Current Ride settings only, not the settings used for historical activities.

        power_zones are percentages of FTP; hr_zones are returned as API-native
        values without guessing their unit or applying a max-HR conversion.
        """
        data = self._request_json("GET", "/sport-settings/Ride", stage="sport_settings", endpoint="sport_settings")
        if not isinstance(data, dict):
            raise ICUError("Intervals.icu returned an unexpected data shape", stage="sport_settings",
                           endpoint="sport_settings", reason="unexpected_shape")
        types = data.get("types")
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types) or "Ride" not in types:
            raise ICUError("Intervals.icu returned settings for the wrong sport", stage="sport_settings",
                           endpoint="sport_settings", reason="wrong_sport")
        account = data.get("athlete_id")
        if not isinstance(account, str) or not re.fullmatch(r"i?\d+", account) or int(account.removeprefix("i")) == 0 or (self.athlete_id != "0" and account.removeprefix("i") != self.athlete_id.removeprefix("i")):
            raise ICUError("Intervals.icu settings account mismatch", stage="sport_settings",
                           endpoint="sport_settings", reason="identity_mismatch")
        def positive(value, maximum):
            value = _number(value)
            return value if value is not None and 0 < value <= maximum else None
        return {"id": data.get("id") if type(data.get("id")) is int else None,
                "athlete_id": account, "types": [t for t in types if len(t) < 80],
                "ftp": positive(data.get("ftp"), 1000),
                "indoor_ftp": positive(data.get("indoor_ftp"), 1000),
                "indoor_ftp_status": ("unset" if data.get("indoor_ftp") is None else "valid" if positive(data.get("indoor_ftp"), 1000) is not None else "invalid"),
                "lthr": positive(data.get("lthr"), 250),
                "max_hr": positive(data.get("max_hr"), 250),
                "power_zones": _zone_list(data.get("power_zones"), boundaries=True),
                "hr_zones": _zone_list(data.get("hr_zones"), boundaries=True)}

    def activities(self, oldest: str, newest: str):
        return [normalize_activity(row) for row in self._get("activities", oldest, newest)]

    def wellness(self, oldest: str, newest: str):
        return [normalize_wellness(row) for row in self._get("wellness", oldest, newest)]
