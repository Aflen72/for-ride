"""Offline coverage for ICU mixed payloads and the absent-mean-HR sentinels."""
import unittest

from indoor_coach.icu import ICUClient, ICUError, normalize_activity


class ICUSyncNormalizationTests(unittest.TestCase):
    def client(self, rows):
        client = ICUClient("test-only")
        client._request_json = lambda *args, **kwargs: rows
        return client

    def activity(self, **updates):
        return {
            "id": "i123", "start_date_local": "2040-01-15T12:00:00",
            "type": "VirtualRide", "source": "UPLOAD",
            "elapsed_time": 1800, "icu_average_watts": 155,
            "average_heartrate": 120, **updates,
        }

    def test_only_numeric_zero_is_missing_sentinel(self):
        for hr in (0, 0.0, -0.0):
            row = normalize_activity(self.activity(average_heartrate=hr))
            self.assertIsNone(row["avg_hr_bpm"])
            self.assertEqual(row["heart_rate_quality"], "missing_zero")
            self.assertNotIn("avg_hr_bpm", row["available_fields"])
            self.assertEqual(row["avg_power_w"], 155)

    def test_missing_valid_and_invalid_stay_distinct(self):
        for hr in (False, True, "0", "125", -3, 24, 251, 10**500, float("nan"), float("inf"), []):
            with self.subTest(hr=hr):
                row = normalize_activity(self.activity(average_heartrate=hr))
                self.assertIsNone(row["avg_hr_bpm"])
                self.assertEqual(row["heart_rate_quality"], "invalid")
        for hr in (None, 25, 120.5, 250):
            row = normalize_activity(self.activity(average_heartrate=hr))
            self.assertEqual(row["avg_hr_bpm"], hr)
            self.assertNotIn("heart_rate_quality", row)

    def test_activities_isolate_normalization_failures(self):
        secret = "never surface this as an activity ID"
        rows = [None, self.activity(id="i2", start_date_local=None),
                self.activity(id=secret, start_date_local=None),
                self.activity(id={"token": secret}), self.activity(id=7812, elapsed_time=10**450),
                self.activity(average_heartrate=0)]
        res = self.client(rows).activities("2040-01-15", "2040-01-15")
        self.assertIsInstance(res, list)
        self.assertEqual([row["id"] for row in res], ["i123"])
        self.assertEqual(res.rejected_rows, [
            {"index": 0, "reason": "non_object"},
            {"index": 1, "id": "i2", "reason": "invalid_date"},
            {"index": 2, "reason": "invalid_id"},
            {"index": 3, "reason": "invalid_id"},
            {"index": 4, "id": "7812", "reason": "normalization_failed"},
        ])
        self.assertNotIn(secret, repr(res.rejected_rows))

    def test_wellness_isolates_bad_sibling_and_has_no_activity_ids(self):
        res = self.client([42, {"id": "i9"}, {"id": "2040-01-15", "sleepSecs": 28800}]).wellness("2040-01-15", "2040-01-15")
        self.assertEqual(res[0]["sleep_hours"], 8)
        self.assertEqual(res.rejected_rows, [
            {"index": 0, "reason": "non_object"},
            {"index": 1, "reason": "invalid_date"},
        ])

    def test_top_level_non_list_keeps_hard_failure(self):
        client = self.client({"error": "never disclose raw payload"})
        with self.assertRaises(ICUError) as error:
            client.activities("2040-01-15", "2040-01-15")
        self.assertEqual(error.exception.reason, "unexpected_shape")
        self.assertNotIn("raw payload", str(error.exception))


if __name__ == "__main__":
    unittest.main()
