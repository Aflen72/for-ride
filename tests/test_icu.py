"""Synthetic fixtures: fixed dates in 2040 are fictional, not user records."""
import base64
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from indoor_coach.icu import ICUClient, ICUError, _NoRedirect, normalize_activity, normalize_wellness


class ICUTests(unittest.TestCase):
    def client(self, payload):
        client = ICUClient("test-secret")
        client._opener = MagicMock()
        client._opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        return client

    def test_read_only_request(self):
        c = self.client([{"id": "i1", "type": "VirtualRide", "start_date_local": "2040-11-14T10:00:00", "moving_time": 3600, "icu_average_watts": 120}])
        result = c.activities("2040-11-01", "2040-11-15")
        req = c._opener.open.call_args.args[0]
        self.assertEqual(req.method, "GET")
        self.assertEqual(req.full_url, "https://intervals.icu/api/v1/athlete/0/activities?oldest=2040-11-01&newest=2040-11-15")
        self.assertEqual(req.get_header("Authorization"), "Basic " + base64.b64encode(b"API_KEY:test-secret").decode())
        self.assertTrue(result[0]["indoor"])
        self.assertEqual(result[0]["avg_power_w"], 120)

    def test_missing_strava_metrics(self):
        r = normalize_activity({"id": "123", "source": "STRAVA", "start_date_local": "2040-11-14"})
        self.assertTrue(r["restricted"])
        self.assertIsNone(r["duration_s"])
        self.assertIsNone(r["avg_power_w"])
        self.assertIsNone(r["indoor"])
        self.assertIn("withheld_reason", r)
        m = normalize_activity({"id": "123", "start_date_local": "2040-11-14"})
        self.assertIn("Metadata-only", m["withheld_reason"])

    def test_wellness_units_and_types(self):
        r = normalize_wellness({"id": "2040-11-14", "sleepSecs": 28800, "hrv": 50, "restingHR": 45})
        self.assertEqual(r["sleep_hours"], 8)
        self.assertEqual(r["hrv_type"], "RMSSD")
        self.assertIsNone(r["device"])
        self.assertEqual(normalize_wellness({"hrvSDNN": 45})["hrv_type"], "SDNN")
        self.assertIsNone(normalize_wellness({})["sleep_hours"])

    def test_validation_before_network(self):
        c = self.client([])
        for start, end in [("2040-11-15", "2040-11-01"), ("2024-01-01", "2040-01-01"), ("2040-1-1", "2040-11-15")]:
            with self.assertRaises(ValueError):
                c.activities(start, end)
        c._opener.open.assert_not_called()
        for athlete in ["../secret", "0?x=1", "https://evil.example"]:
            with self.assertRaises(ValueError):
                ICUClient("secret", athlete)

    def test_safe_errors_and_redirects(self):
        c = self.client([])
        c._opener.open.side_effect = RuntimeError("test-secret URL query secret")
        with self.assertRaises(ICUError) as error:
            c.activities("2040-11-01", "2040-11-15")
        self.assertNotIn("test-secret", str(error.exception))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example"))
        c._opener.open.side_effect = urllib.error.HTTPError("https://intervals.icu", 401, "test-secret", {}, None)
        with self.assertRaisesRegex(ICUError, "HTTP 401"):
            c.wellness("2040-11-01", "2040-11-15")

    def test_limits_and_shape(self):
        c = self.client([])
        c.MAX_RESPONSE_BYTES = 2
        c._opener.open.return_value = io.BytesIO(b"[{}]")
        with self.assertRaisesRegex(ICUError, "too large"):
            c.activities("2040-11-01", "2040-11-15")
        c = self.client({"error": "secret"})
        with self.assertRaisesRegex(ICUError, "unexpected data shape"):
            c.activities("2040-11-01", "2040-11-15")

    def test_env_no_storage(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ICUError):
                ICUClient.from_env()
        with patch.dict(os.environ, {"INTERVALS_API_KEY": "secret", "INTERVALS_ATHLETE_ID": "i123"}, clear=True):
            c = ICUClient.from_env()
            self.assertEqual(c.athlete_id, "i123")
        self.assertNotIn("secret", repr(c))

    def test_invalid_metrics_not_zero(self):
        r = normalize_activity({"icu_average_watts": -1, "average_heartrate": float("nan"), "moving_time": True})
        self.assertIsNone(r["avg_power_w"])
        self.assertIsNone(r["avg_hr_bpm"])
        self.assertIsNone(r["duration_s"])

if __name__ == "__main__":
    unittest.main()
