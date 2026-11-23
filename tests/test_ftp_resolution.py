"""Synthetic end-to-end service tests; no real account/network use."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from indoor_coach.icu import ICUError
from indoor_coach.service import Service

TODAY = '2040-11-15'
NOW = datetime(2040, 11, 15, 10, 0, tzinfo=timezone.utc)
RIDE = {'id':'synthetic-ftp-test','date':TODAY,'source':'UPLOAD','sport':'VirtualRide',
        'indoor':True,'duration_s':1800,'normalized_power_w':140}


class FTPResolutionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'INTERVALS_API_KEY':'synthetic-only','INTERVALS_ATHLETE_ID':'0'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch('indoor_coach.ftp.utc_now', return_value=NOW)
        self.mock_clock = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.factory_patch = patch('indoor_coach.service.ICUClient.from_env')
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.client = self.factory.return_value
        self.client.activities.return_value = [RIDE]
        self.client.wellness.return_value = []
        self.client.identity.return_value = {'id':'i2345','timezone':'UTC'}
        self.client.sport_settings.return_value = {'athlete_id':'i2345','types':['Ride'],'indoor_ftp':215,'ftp':245}
        self.s = Service(':memory:')
        self.addCleanup(self.s.store.close)

    def test_read_once_then_draft_from_cache_and_exact_bound_preview(self):
        report=self.s.call('prepare_analysis',{'as_of':TODAY})
        self.client.activities.assert_called_once()
        self.client.wellness.assert_called_once()
        self.client.identity.assert_called_once()
        self.client.sport_settings.assert_called_once()
        self.assertEqual(report['ftp_basis']['source'],'icu_indoor_ftp')
        self.assertEqual(report['ftp_w'],215)
        draft=self.s.call('propose_plan',{'as_of':TODAY})
        self.client.sport_settings.assert_called_once()
        self.assertEqual(draft['plan']['data_review']['snapshot_id'],report['data_review']['snapshot_id'])
        self.assertEqual(draft['plan']['ftp_basis']['source'],'icu_indoor_ftp_cache')
        args={'proposal_id':draft['id'],'account':'i2345','timezone':'UTC'}
        with patch('indoor_coach.workout_export.dt.datetime', wraps=datetime) as clock:
            clock.now.return_value=NOW+timedelta(hours=1)
            result=self.s.call('preview_icu',args)
            self.assertEqual(result['payload']['ftp_w'],215)
            self.assertEqual(result['payload']['expires_at'],(NOW+timedelta(hours=24)).isoformat())
            with self.assertRaisesRegex(ValueError,'FTP differs'):
                self.s.call('preview_icu',{**args,'ftp':215.01})
            with self.assertRaisesRegex(ValueError,'account differs'):
                self.s.call('preview_icu',{**args,'account':'i9999'})
            clock.now.return_value=NOW+timedelta(hours=25)
            with self.assertRaisesRegex(ValueError,'expired'):
                self.s.call('preview_icu',args)

    def test_cache_age_and_historical_date_do_not_invent_ftp(self):
        self.s.call('prepare_analysis',{'as_of':TODAY})
        old=self.s.call('analyze',{'as_of':'2040-11-14'})
        self.assertIsNone(old['ftp_w'])
        self.assertEqual(old['ftp_basis']['status'],'not_historical')
        self.mock_clock.return_value=NOW+timedelta(hours=24)
        draft=self.s.call('propose_plan',{'as_of':'2040-11-16'})
        self.assertEqual(draft['plan']['ftp_basis']['status'],'stale')
        self.assertTrue(all(s['power_w'] is None for w in draft['plan']['week_plan'] for s in w['steps']))
        self.client.sport_settings.assert_called_once()

    def test_no_generic_fallback_when_indoor_unset_and_manual_still_works(self):
        self.client.sport_settings.return_value['indoor_ftp']=None
        p=self.s.call('prepare_analysis',{'as_of':TODAY})
        self.assertIsNone(p['ftp_w'])
        self.assertEqual(p['ftp_basis']['status'],'indoor_ftp_unset')
        manual=self.s.call('prepare_analysis',{'as_of':TODAY,'ftp':227})
        self.assertEqual(manual['ftp_w'],227)
        self.assertEqual(manual['ftp_basis']['source'],'explicit_input')
        self.client.sport_settings.assert_called_once()

    def test_newly_observed_ftp_change_requires_fresh_draft(self):
        self.s.call('prepare_analysis',{'as_of':TODAY})
        draft=self.s.call('propose_plan',{'as_of':TODAY})
        self.client.sport_settings.return_value['indoor_ftp']=230
        self.s.call('prepare_analysis',{'as_of':TODAY})
        with self.assertRaisesRegex(ValueError,'basis changed'):
            self.s.call('preview_icu',{'proposal_id':draft['id'],'account':'i2345','timezone':'UTC'})
        self.assertEqual(self.s.call('get_proposal',{'proposal_id':draft['id']})['plan']['ftp_basis']['watts'],215)

    def test_account_switch_and_settings_refresh_failure_do_not_reuse_value(self):
        self.s.call('prepare_analysis',{'as_of':TODAY})
        with patch.dict(os.environ,{'INTERVALS_API_KEY':'different-synthetic-account'}):
            p=self.s.call('analyze',{'as_of':TODAY})
            self.assertEqual(p['ftp_basis']['status'],'account_unverified')
            self.assertIsNone(p['ftp_w'])
        with patch.dict(os.environ,{'INTERVALS_ATHLETE_ID':'9999'}):
            wrong=self.s.call('prepare_analysis',{'as_of':TODAY})
            self.assertEqual(wrong['ftp_basis']['refresh_error']['reason'],'account_not_verified')
            self.assertIsNone(wrong['ftp_w'])
        self.client.sport_settings.side_effect=ICUError('private detail', status=403, stage='sport_settings', reason='forbidden')
        p=self.s.call('prepare_analysis',{'as_of':TODAY})
        self.assertEqual(p['ftp_basis']['status'],'refresh_failed')
        self.assertEqual(p['ftp_basis']['refresh_error']['http_status'],403)
        self.assertIsNone(p['ftp_w'])
        self.assertNotIn('private detail',str(p))

    def test_activity_failure_status_and_independent_settings_refresh(self):
        self.client.activities.side_effect=ICUError('private detail',status=403,stage='activities',reason='edge_1010',edge_code=1010)
        p=self.s.call('prepare_analysis',{'as_of':TODAY})
        access=p['data_review']['evidence_workflow']['data_access']
        self.assertEqual(access['freshness'],'latest_attempt_failed')
        self.assertEqual(access['refresh_error']['edge_code'],1010)
        self.assertEqual(p['ftp_w'],215)
        self.assertNotIn('private detail',str(p))

    def test_offline_restart_needs_concrete_matching_account(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'synthetic.db'
            with patch.dict(os.environ,{'INTERVALS_ATHLETE_ID':'i2345'}):
                first=Service(path)
                first.call('prepare_analysis',{'as_of':TODAY})
                first.store.close()
            with patch.dict(os.environ,{},clear=True):
                second=Service(path)
                try:
                    self.assertIsNone(second.call('analyze',{'as_of':TODAY})['ftp_w'])
                    with patch.dict(os.environ,{'INTERVALS_ATHLETE_ID':'2345'}):
                        report=second.call('prepare_analysis',{'as_of':TODAY,'refresh':False})
                        self.assertEqual(report['ftp_w'],215)
                    with patch.dict(os.environ,{'INTERVALS_ATHLETE_ID':'9999'}):
                        self.assertIsNone(second.call('analyze',{'as_of':TODAY})['ftp_w'])
                finally:
                    second.store.close()
        self.client.sport_settings.assert_called_once()
