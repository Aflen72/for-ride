"""Synthetic fixtures: fixed dates in 2040 are fictional, not user records."""
import unittest
from indoor_coach.analysis import analyze, recommend, circular_bedtime

TODAY = "2040-11-15"

def records(**extra):
    return [dict(date=f"2040-11-{d:02d}", sleep_hours=8, bedtime="23:50", hrv_ms=50, hrv_type="RMSSD", device="device-a", resting_hr_bpm=50, **extra) for d in range(8,15)]

def ride(**extra):
    a = dict(id="r1", source="UPLOAD", date=TODAY, sport="Ride", indoor=True, duration_s=3600, normalized_power_w=130, avg_power_w=125)
    a.update(extra)
    return a

class AnalysisTests(unittest.TestCase):
    def test_midnight_clock_mean(self):
        self.assertEqual(circular_bedtime(["23:50", "00:10"]), "00:00")
        self.assertIsNone(circular_bedtime(["00:00", "12:00"]))
        self.assertIsNone(circular_bedtime([None, "24:10", "bad"]))

    def test_baseline_excludes_today_and_old(self):
        r = records() + [dict(date=TODAY, sleep_hours=4), dict(date="2040-11-01", sleep_hours=1)]
        a = analyze([], r, as_of=TODAY)
        self.assertEqual(a["dimensions"]["sleep_hours"]["reference"]["mean"], 8)
        self.assertEqual(a["dimensions"]["sleep_hours"]["reference"]["sample_count"], 7)
        self.assertEqual(a["dimensions"]["sleep_hours"]["status"], "caution")

    def test_missing_not_zero(self):
        a = analyze([], [], as_of=TODAY)
        self.assertIsNone(a["load"]["estimated_tss_known_only"])
        self.assertFalse(a["load"]["tss_complete"])
        self.assertIsNone(a["dimensions"]["sleep_hours"]["current"])
        self.assertEqual(a["caution_flags"], [])
        p = recommend(a)
        self.assertTrue(p["rationale"]["incomplete_data"])
        self.assertEqual(p["next_workout"]["kind"], "easy")
        self.assertIsNone(p["next_workout"]["steps"][0]["power_w"])

    def test_hrv_device_and_type_must_match(self):
        for patch in (dict(device="device-b", hrv_type="RMSSD"), dict(device="device-a", hrv_type="SDNN"), dict()):
            a = analyze([], records()+[dict(date=TODAY, hrv_ms=30, **patch)], as_of=TODAY)
            self.assertEqual(a["dimensions"]["hrv_ms"]["status"], "insufficient_data")
        a = analyze([], records()+[dict(date=TODAY, hrv_ms=30, device="device-a", hrv_type="RMSSD")], as_of=TODAY)
        self.assertIn("hrv_ms", a["caution_flags"])

    def test_stages_descriptive(self):
        a = analyze([], records(deep_sleep_pct=40, light_sleep_pct=40)+[dict(date=TODAY, deep_sleep_pct=5, light_sleep_pct=90)], as_of=TODAY)
        self.assertEqual(a["dimensions"]["deep_sleep_pct"]["status"], "descriptive_only")
        self.assertNotIn("deep_sleep_pct", a["caution_flags"])

    def test_load_no_average_power_substitution(self):
        a = analyze([ride(normalized_power_w=None)], [], as_of=TODAY, ftp=200)
        self.assertIsNone(a["load"]["estimated_tss_known_only"])
        self.assertFalse(a["load"]["tss_complete"])
        a = analyze([ride()], [], as_of=TODAY, ftp=200)
        self.assertEqual(a["load"]["estimated_tss_known_only"], 42.3)
        self.assertTrue(a["load"]["tss_complete"])
        self.assertFalse(a["ftp_updated"])

    def test_restricted_unknown_outdoor(self):
        a = analyze([ride(indoor=False), ride(id="r2", indoor=None), ride(id="r3", restricted=True, source="STRAVA")], [], as_of=TODAY, ftp=200)
        self.assertEqual(a["activities"], [])
        self.assertEqual(a["load"]["restricted_count"], 1)
        self.assertEqual(a["load"]["unknown_indoor_count"], 1)
        self.assertEqual(a["load"]["excluded_count"], 1)
        self.assertEqual(len(a["warnings"]), 2)

    def test_manual_mean_and_invalid_reference(self):
        baseline = dict(period_start="2040-11-08", period_end="2040-11-14", sample_count=5, sleep_hours=9, bedtime="00:10")
        a = analyze([], records()+[dict(date=TODAY, sleep_hours=8, bedtime="23:50")], baseline, TODAY)
        d = a["dimensions"]["sleep_hours"]
        self.assertEqual(d["reference"]["source"], "manual_summary")
        self.assertEqual(d["reference"]["mean"], 9)
        self.assertEqual(a["dimensions"]["bedtime"]["delta"], -20)
        baseline["period_end"] = TODAY
        a = analyze([], records(), baseline, TODAY)
        self.assertEqual(a["dimensions"]["sleep_hours"]["reference"]["source"], "daily_records")
        self.assertTrue(a["warnings"])

    def test_deduplicate_and_date_filter(self):
        a = analyze([ride(), ride(), ride(id="old", date="2040-10-01"), ride(id="future", date="2040-11-16")], [], as_of=TODAY, ftp=200)
        self.assertEqual(len(a["activities"]), 1)

    def test_plan_steps_and_dates(self):
        a = analyze([ride()], records()+[dict(date=TODAY, sleep_hours=8, resting_hr_bpm=50)], as_of=TODAY, ftp=200)
        p = recommend(a)
        self.assertEqual(p["next_workout"]["date"], "2040-11-16")
        self.assertEqual(len(p["week_plan"]), 7)
        self.assertTrue(p["requires_confirmation"])
        self.assertFalse(p["execution_supported"])
        for w in p["week_plan"]:
            self.assertEqual(w["duration_minutes"], sum(s["duration_minutes"] for s in w["steps"]))
        a["caution_flags"] = ["sleep_hours"]
        self.assertEqual(recommend(a)["next_workout"]["kind"], "rest")

    def test_high_subjective_fatigue_without_baseline(self):
        a = analyze([], [dict(date=TODAY, fatigue=8)], as_of=TODAY)
        self.assertIn("fatigue", a["caution_flags"])
        self.assertEqual(recommend(a)["next_workout"]["kind"], "rest")

    def test_invalid_input(self):
        for f in (-1, 0):
            with self.assertRaises(ValueError):
                analyze([], [], as_of=TODAY, ftp=f)
        with self.assertRaises(ValueError):
            analyze([], [], as_of="bad")

if __name__ == "__main__":
    unittest.main()
