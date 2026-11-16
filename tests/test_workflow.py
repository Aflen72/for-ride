"""Synthetic fixtures: fixed dates in 2040 are fictional, not user records."""
import os
import unittest
from unittest.mock import patch
from indoor_coach.service import Service
from indoor_coach.icu import normalize_activity
from indoor_coach.store import validate

class WorkflowTests(unittest.TestCase):
    def setUp(self): self.s=Service(':memory:')
    def tearDown(self): self.s.store.close()
    def seed(self):
        self.s.call('import_activities',{'records':[{'id':'synthetic','date':'2040-11-15','source':'UPLOAD','sport':'VirtualRide','indoor':True,'duration_s':3600,'avg_power_w':120,'normalized_power_w':125,'avg_hr_bpm':120,'hr_zone_upper_bpm':[110,130,150,170,190]}]})
    def test_empty_no_full_course_saved(self):
        r=self.s.call('propose_plan',{'as_of':'2040-11-15','ftp':200})
        self.assertEqual(r['status'],'needs_data')
        self.assertIsNone(r['plan'])
        self.assertEqual(self.s.call('list_proposals',{}),[])
    def test_no_key_does_not_claim_refresh(self):
        self.seed()
        with patch.dict(os.environ,{},clear=True):
            r=self.s.call('prepare_analysis',{'as_of':'2040-11-15','ftp':200})
        self.assertEqual(self.s.store.sync_history()[0]['status'],'not_configured')
        self.assertEqual(len(r['activities']),1)
    def test_refresh_failure_keeps_local_evidence_and_logs_failure(self):
        self.seed()
        with patch.dict(os.environ,{'INTERVALS_API_KEY':'synthetic-key'}), patch('indoor_coach.service.ICUClient.from_env') as factory:
            factory.return_value.activities.side_effect=RuntimeError('secret detail')
            r=self.s.call('prepare_analysis',{'as_of':'2040-11-15','ftp':200})
            factory.return_value.activities.assert_called_once()
        self.assertEqual(self.s.store.sync_history()[0]['status'],'failed')
        self.assertEqual(len(r['activities']),1)
        self.assertNotIn('secret detail',str(r))
        self.assertNotIn('synthetic-key',str(r))
    def test_refresh_false_has_no_network_call(self):
        self.seed()
        with patch('indoor_coach.service.ICUClient.from_env') as factory:
            self.s.call('prepare_analysis',{'as_of':'2040-11-15','refresh':False})
            factory.assert_not_called()
        self.assertEqual(self.s.store.sync_history(),[])
    def test_no_ftp_no_invented_watt_target(self):
        self.seed()
        p=self.s.call('propose_plan',{'as_of':'2040-11-15'})
        self.assertEqual(p['plan']['data_review']['status'],'limited')
        for workout in p['plan']['week_plan']:
            for step in workout['steps']: self.assertIsNone(step['power_w'])
    def test_snapshot_recomputed_before_proposal(self):
        self.seed()
        a=self.s.call('analyze',{'as_of':'2040-11-15','ftp':200})
        p=self.s.call('propose_plan',{'as_of':'2040-11-15','ftp':200})
        self.assertEqual(a['data_review']['snapshot_id'],p['plan']['data_review']['snapshot_id'])
        self.s.call('import_wellness',{'records':[{'date':'2040-11-15','fatigue':9}]})
        new=self.s.call('propose_plan',{'as_of':'2040-11-15','ftp':200})
        self.assertNotEqual(a['data_review']['snapshot_id'],new['plan']['data_review']['snapshot_id'])
        self.assertEqual(new['plan']['next_workout']['kind'],'rest')
    def test_health_conflict_and_hrv_uncomparable_disclosed(self):
        self.seed()
        self.s.store.save_wellness([{'date':'2040-11-15','sleep_hours':8,'hrv_ms':50}],'manual')
        self.s.store.save_wellness([{'date':'2040-11-15','sleep_hours':6}],'icu')
        r=self.s.call('analyze',{'as_of':'2040-11-15','ftp':200})
        codes={i['code'] for i in r['data_review']['issues']}
        self.assertIn('source_conflicts',codes)
        self.assertIn('hrv_ms_reference_unavailable',codes)
        self.assertNotIn('hrv_ms',r['data_review']['usable_reference_metrics'])
    def test_activity_zone_mapping_and_validation(self):
        a=normalize_activity({'id':'z','source':'UPLOAD','start_date_local':'2040-11-15','icu_hr_zones':[110,130,150],'icu_hr_zone_times':[300,400,500]})
        self.assertEqual(a['hr_zone_upper_bpm'],[110,130,150])
        self.assertEqual(a['hr_zone_time_s'],[300,400,500])
        validate(a,'activity')
        a['hr_zone_time_s']=[300]
        with self.assertRaises(ValueError): validate(a,'activity')
    def test_malformed_zones_not_medical_alarm(self):
        a=normalize_activity({'icu_hr_zones':[130,110],'icu_hr_zone_times':[float('nan')]})
        self.assertIsNone(a['hr_zone_upper_bpm'])
        self.assertIsNone(a['hr_zone_time_s'])
    def test_argument_validation_before_fetch(self):
        with patch('indoor_coach.service.ICUClient.from_env') as factory:
            for args in ({'as_of':'2040-11-15','history_days':1},{'as_of':'2040-11-15','refresh':'yes'}):
                with self.assertRaises(ValueError): self.s.call('prepare_analysis',args)
            factory.assert_not_called()
    def test_prior_success_does_not_hide_latest_refresh_failure(self):
        self.seed()
        self.s.store.log_sync('2040-10-19','2040-11-15','success')
        self.s.store.log_sync('2040-10-19','2040-11-15','failed')
        r=self.s.call('analyze',{'as_of':'2040-11-15','ftp':200})
        access=r['data_review']['evidence_workflow']['data_access']
        self.assertEqual(access['freshness'],'latest_attempt_failed')
        self.assertIn('latest_refresh_not_successful',{i['code'] for i in r['data_review']['issues']})
