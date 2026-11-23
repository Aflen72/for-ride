"""Synthetic fixtures: fixed dates in 2040 are fictional, not user records."""
import unittest
from indoor_coach.evidence import evidence_context, AGENT_WORKFLOW_PROMPT


def ride(**patch):
    r={'id':'target','date':'2040-11-15','source':'UPLOAD','sport':'VirtualRide','indoor':True,
       'duration_s':3600,'avg_power_w':140,'avg_hr_bpm':115,'hr_zone_upper_bpm':[110,130,150,170,190],
       'hr_zone_time_s':[0,3000,600,0,0]}
    r.update(patch)
    return r

class EvidenceTests(unittest.TestCase):
    def test_personal_zone_evidence_not_generic_or_current_settings(self):
        r=evidence_context([ride()], '2040-11-15')
        self.assertEqual(r['heart_rate_evidence']['mean_hr_band'],2)
        self.assertEqual(r['heart_rate_evidence']['zone_time_total_s'],3600)
        self.assertTrue(r['heart_rate_evidence']['zone_time_matches_duration'])
        self.assertFalse(r['capabilities']['current_athlete_settings'])
        self.assertNotIn('valid_target_hr_boundaries_missing',r['missing_evidence'])
        self.assertFalse(any('obtain_activity_hr_boundaries' in x['action'] for x in r['next_checks']))
    def test_missing_zones_no_guessed_hr_label(self):
        r=evidence_context([ride(hr_zone_upper_bpm=None)],'2040-11-15')
        self.assertIsNone(r['heart_rate_evidence']['mean_hr_band'])
        self.assertIn('valid_target_hr_boundaries_missing',r['missing_evidence'])
    def test_invalid_and_partial_zone_times(self):
        for times in ([1,2], [0,0,-1,0,0], [0,0,float('nan'),0,0]):
            r=evidence_context([ride(hr_zone_time_s=times)],'2040-11-15')
            self.assertIsNone(r['heart_rate_evidence']['zone_time_s'])
        r=evidence_context([ride(hr_zone_time_s=[0,30,0,0,0])],'2040-11-15')
        self.assertFalse(r['heart_rate_evidence']['zone_time_matches_duration'])
        self.assertIn('hr_zone_time_coverage_unverified',r['missing_evidence'])
    def test_explicit_target_no_silent_fallback(self):
        r=evidence_context([ride()], '2040-11-15',target_id='not-present')
        self.assertIsNone(r['target'])
        self.assertIn('requested_target_unavailable',r['missing_evidence'])
    def test_latest_selection_and_window(self):
        rows=[ride(id='old',date='2040-10-01'),ride(id='today-a'),ride(id='today-b'),ride(id='future',date='2040-11-16')]
        r=evidence_context(rows,'2040-11-15')
        self.assertEqual(r['target']['id'],'today-b')
        self.assertEqual(r['window']['oldest'],'2040-10-19')
    def test_comparable_history_requires_matching_duration_and_power(self):
        rows=[ride(),ride(id='match',date='2040-11-10'),ride(id='short',date='2040-11-10',duration_s=600),
              ride(id='hard',date='2040-11-10',avg_power_w=250),ride(id='no-power',date='2040-11-10',avg_power_w=None),
              ride(id='same-day'),ride(id='outdoor',date='2040-11-10',indoor=False)]
        r=evidence_context(rows,'2040-11-15',target_id='target')
        self.assertEqual([x['id'] for x in r['comparison']['records']],['match'])
        self.assertEqual(r['comparison']['records'][0]['source'],'UPLOAD')
    def test_restricted_and_unknown_records_not_exposed(self):
        r=evidence_context([ride(source='STRAVA',restricted=False),ride(id='secret',source='future-platform')],'2040-11-15')
        self.assertIsNone(r['target'])
        self.assertEqual(r['excluded_counts']['restricted'],1)
        self.assertEqual(r['excluded_counts']['unknown_source'],1)
        self.assertNotIn('secret',str(r))
    def test_full_refresh_coverage_not_assumed(self):
        for checks in (None,[{'status':'failed','oldest':'2040-10-19','newest':'2040-11-15'}],
                       [{'status':'success','oldest':'2040-11-01','newest':'2040-11-15'}]):
            self.assertFalse(evidence_context([ride()],'2040-11-15',sync_attempts=checks)['data_access']['successful_full_window_query_evidenced'])
        r=evidence_context([ride()],'2040-11-15',sync_attempts=[{'status':'success','oldest':'2040-10-19','newest':'2040-11-15','api_key':'SECRET','attempted_at':'2040-11-15T10:00:00+00:00'}])
        self.assertTrue(r['data_access']['successful_full_window_query_evidenced'])
        self.assertNotIn('SECRET',str(r))
        self.assertEqual(r['data_access']['sync_attempts'][0]['attempted_at'],'2040-11-15T10:00:00+00:00')
        self.assertFalse(any(x['action']=='check_authorized_icu_once' for x in r['next_checks']))
    def test_validation(self):
        for days in (True,6,91,28.1):
            with self.assertRaises(ValueError): evidence_context([],'2040-11-15',history_days=days)
        with self.assertRaises(ValueError): evidence_context([],'2040-11-15T00:00:00')
    def test_prompt_prevents_invented_access_or_repeated_questions(self):
        self.assertIn('before asking',AGENT_WORKFLOW_PROMPT)
        self.assertIn('does not establish historical FTP, current account HR settings, or per-second streams',AGENT_WORKFLOW_PROMPT)
        self.assertIn('Do not repeat questions',AGENT_WORKFLOW_PROMPT)

if __name__=='__main__': unittest.main()
