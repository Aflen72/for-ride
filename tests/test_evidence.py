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

class PartialSyncEvidenceTests(unittest.TestCase):
    @staticmethod
    def attempt(status='partial', oldest='2040-10-19', newest='2040-11-15', hour='12', ids=('target',)):
        return {'status':status,'oldest':oldest,'newest':newest,'attempted_at':f'2040-11-15T{hour}:00:00Z',
                'diagnostics':{'activities_skipped':len(ids),'wellness_skipped':1,'heart_rate_missing_zero':2,
                               'skipped_activity_ids':list(ids), 'raw_values':'private', 'api_key':'secret'}}
    def test_exact_target_is_not_substituted_with_cache_or_other_ride(self):
        rows=[ride(),ride(id='different',date='2040-11-14')]
        result=evidence_context(rows,'2040-11-15',target_id='target',sync_attempts=[self.attempt()])
        self.assertIsNone(result['target'])
        self.assertTrue(result['target_selection']['rejected_by_partial_sync'])
        self.assertIn('requested_target_rejected_by_partial_sync',result['missing_evidence'])
        self.assertEqual(result['data_access']['rejected_target_ids'],['target'])
        self.assertNotIn('private',str(result))
        self.assertNotIn('secret',str(result))
    def test_latest_partial_does_not_allow_stale_full_success_claim(self):
        result=evidence_context([ride()],'2040-11-15',sync_attempts=[
            self.attempt(),self.attempt('success',hour='11',ids=())])
        access=result['data_access']
        self.assertFalse(access['successful_full_window_query_evidenced'])
        self.assertTrue(access['partial_coverage'])
        self.assertTrue(access['load_window_partial'])
        self.assertEqual(access['latest_relevant_attempt']['status'],'partial')
    def test_later_narrow_success_does_not_repair_other_days(self):
        rows=[ride(date='2040-11-13'),ride(id='other')]
        result=evidence_context(rows,'2040-11-15',target_id='target',sync_attempts=[
            self.attempt('success','2040-11-15','2040-11-15','13',()),self.attempt()])
        access=result['data_access']
        self.assertEqual(access['latest_relevant_attempt']['status'],'success')
        self.assertTrue(access['load_window_partial'])
        self.assertEqual(access['unrepaired_partial_day_count'],27)
        self.assertEqual(access['latest_unrepaired_partial_attempt']['diagnostics']['activities_skipped'],1)
        self.assertIsNone(result['target'])
    def test_later_covering_success_repairs_same_id(self):
        result=evidence_context([ride()],'2040-11-15',target_id='target',sync_attempts=[
            self.attempt('success',hour='13',ids=()),self.attempt()])
        self.assertEqual(result['target']['id'],'target')
        self.assertFalse(result['target_selection']['rejected_by_partial_sync'])
        self.assertFalse(result['data_access']['partial_coverage'])
    def test_old_partial_outside_load_window_does_not_mark_load_incomplete(self):
        result=evidence_context([ride()],'2040-11-15',sync_attempts=[
            self.attempt(oldest='2040-11-01',newest='2040-11-02',ids=())])
        self.assertTrue(result['data_access']['partial_coverage'])
        self.assertFalse(result['data_access']['load_window_partial'])
    def test_review_and_summary_preserve_partial_and_gate_exact_target(self):
        from indoor_coach.review import review_data
        from indoor_coach.projection import review_summary
        data=evidence_context([ride()],'2040-11-15',target_id='target',sync_attempts=[self.attempt()])
        review=review_data({'activities':[{'id':'different'}],'load':{'tss_complete':False},'ftp_w':220},data,[])
        self.assertEqual(review['recommendation_permission'],'needs_data')
        self.assertIn('latest_refresh_partial',[i['code'] for i in review['issues']])
        small=review_summary(review)
        access=small['evidence']['data_access']
        self.assertEqual(access['latest_relevant_attempt']['diagnostics']['activities_skipped'],1)
        self.assertTrue(access['load_window_partial'])
        self.assertNotIn('skipped_activity_ids',str(small))
        self.assertNotIn('rejected_target_ids',access)

    def test_persisted_quarantine_survives_empty_success_and_bounded_history(self):
        result=evidence_context([ride()],'2040-11-15',target_id='target',
                                sync_attempts=[self.attempt('success',ids=())],
                                quarantined_ids={'target'},day_coverage={'2040-11-15':'success'})
        self.assertIsNone(result['target'])
        self.assertTrue(result['data_access']['load_window_partial'])
        self.assertFalse(result['data_access']['partial_coverage'])
        self.assertFalse(result['data_access']['successful_full_window_query_evidenced'])
        self.assertTrue(result['target_selection']['rejected_by_partial_sync'])

    def test_persisted_coverage_reports_partial_after_history_pruned(self):
        result=evidence_context([ride()],'2040-11-15',day_coverage={'2040-11-13':'partial'})
        self.assertTrue(result['data_access']['partial_coverage'])
        self.assertTrue(result['data_access']['load_window_partial'])
        self.assertIn('icu_refresh_partial_unrepaired',result['missing_evidence'])
