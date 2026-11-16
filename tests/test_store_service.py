"""Synthetic fixtures: fixed dates in 2040 are fictional, not user records."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from indoor_coach.store import Store, validate

class StoreTests(unittest.TestCase):
    def setUp(self): self.store=Store(':memory:')
    def tearDown(self): self.store.close()
    def test_idempotent_import(self):
        r={'id':'a','date':'2040-11-15','indoor':True,'duration_s':3600}
        self.store.put_many('activity',[r,r]); self.assertEqual(len(self.store.records('activity')),1)
    def test_invalid_batch_atomic(self):
        with self.assertRaises(ValueError): self.store.put_many('wellness',[{'date':'2040-11-15','sleep_hours':7},{'date':'2040-11-16','sleep_hours':-1}])
        self.assertEqual(self.store.records('wellness'),[])
    def test_reject_secret_fields(self):
        with self.assertRaises(ValueError): validate({'date':'2040-11-15','api_key':'secret'},'wellness')
    def test_baseline_range(self):
        with self.assertRaises(ValueError): validate({'period_start':'2040-11-01','period_end':'2040-11-14','sample_count':7},'baseline')
    def test_nan_rejected(self):
        with self.assertRaises(ValueError): validate({'date':'2040-11-15','sleep_hours':float('nan')},'wellness')
    def test_stages(self):
        with self.assertRaises(ValueError): validate({'date':'2040-11-15','deep_sleep_pct':50,'light_sleep_pct':80},'wellness')
    def test_draft_readback_idempotent(self):
        p={'state':'proposed','next_workout':{'duration_min':30}}
        a=self.store.proposal(p); b=self.store.proposal(p)
        self.assertEqual(a,b); self.assertEqual(a['plan'],p)
        self.assertFalse(a['external_write_enabled'])
    def test_db_private_on_creation(self):
        with tempfile.TemporaryDirectory() as td:
            s=Store(td+'/data.db'); s.close()
            if os.name!='nt': self.assertEqual(os.stat(td+'/data.db').st_mode & 0o777,0o600)

class ServiceTests(unittest.TestCase):
    def test_no_write_tool_or_confirmation_escape(self):
        from indoor_coach.service import Service
        s=Service(':memory:')
        for method in ('apply_plan','approve_plan','upload_workout'):
            with self.assertRaises(ValueError): s.call(method,{'confirmed':True})
        with self.assertRaises(ValueError): s.call('propose_plan',{'as_of':'2040-11-15','confirmed':True})
        s.store.close()
    def test_jsonl_contract(self):
        p=subprocess.run([sys.executable,'-m','indoor_coach','--db',':memory:'],input='not json\n'+json.dumps({'id':2,'tool':'list_proposals'})+'\n',text=True,capture_output=True)
        self.assertEqual(p.returncode,0); lines=p.stdout.splitlines()
        self.assertEqual(len(lines),2); self.assertIn('error',json.loads(lines[0])); self.assertEqual(json.loads(lines[1]),{'id':2,'result':[]})
        self.assertEqual(p.stderr,'')
    def test_no_confirmation_is_approval(self):
        from indoor_coach.service import Service
        s=Service(':memory:')
        p=s.call('propose_plan',{'as_of':'2040-11-15','ftp':240})
        self.assertEqual(p['status'],'needs_data'); self.assertFalse(p['external_write_enabled'])
        self.assertEqual(s.call('list_proposals',{}),[]); s.store.close()

if __name__=='__main__': unittest.main()

class SyncIntegrationTests(unittest.TestCase):
    def test_normalized_icu_schema_stores_and_preserves_manual_context(self):
        from unittest.mock import patch
        from indoor_coach.service import Service
        from indoor_coach.icu import normalize_activity, normalize_wellness
        s=Service(':memory:')
        s.call('import_wellness',{'records':[{'date':'2040-11-15','bedtime':'23:50','fatigue':5}]})
        with patch('indoor_coach.service.ICUClient.from_env') as factory:
            factory.return_value.activities.return_value=[normalize_activity({'id':'i1','start_date_local':'2040-11-15T10:00:00','type':'VirtualRide','moving_time':3600})]
            factory.return_value.wellness.return_value=[normalize_wellness({'id':'2040-11-15','sleepSecs':25200})]
            r=s.call('sync_icu',{'oldest':'2040-11-15','newest':'2040-11-15'})
        self.assertEqual(r['activities_stored'],1)
        self.assertEqual(s.store.records('wellness')[0]['bedtime'],'23:50')
        self.assertEqual(s.store.records('wellness')[0]['sleep_hours'],7)
        s.store.close()

class WellnessMergeTests(unittest.TestCase):
    def setUp(self):
        from indoor_coach.service import Service
        self.s=Service(':memory:')
        self.manual={'date':'2040-11-15','sleep_hours':8,'hrv_ms':50,'hrv_type':'RMSSD','device':'watch','resting_hr_bpm':55}
        self.s.call('import_wellness',{'records':[self.manual]})
    def tearDown(self): self.s.store.close()
    def sync(self,row):
        from unittest.mock import patch
        with patch('indoor_coach.service.ICUClient.from_env') as factory:
            factory.return_value.activities.return_value=[]
            factory.return_value.wellness.return_value=[row]
            return self.s.call('sync_icu',{'oldest':'2040-11-15','newest':'2040-11-15'})
    def test_null_sync_does_not_erase_manual(self):
        self.sync({k:('2040-11-15' if k=='date' else None) for k in self.manual})
        actual=self.s.store.records('wellness')[0]
        for k,v in self.manual.items(): self.assertEqual(actual[k],v)
    def test_conflicts_retained_and_reported(self):
        result=self.sync({'date':'2040-11-15','sleep_hours':6,'hrv_ms':35,'hrv_type':'SDNN','device':None,'resting_hr_bpm':65})
        self.assertIn('sleep_hours',result['conflicts'][0]['conflict_fields'])
        sources=self.s.store.wellness_provenance()[0]['sources']
        self.assertEqual(sources['icu']['hrv_ms'],35)
        self.assertEqual(sources['manual']['hrv_ms'],50)
        actual=self.s.store.records('wellness')[0]
        self.assertEqual((actual['hrv_ms'],actual['hrv_type'],actual['device']),(50,'RMSSD','watch'))
    def test_hrv_group_not_mixed_on_manual_clear(self):
        self.sync({'date':'2040-11-15','hrv_ms':35,'hrv_type':'SDNN','device':None})
        self.s.call('import_wellness',{'records':[{'date':'2040-11-15','hrv_ms':None}]})
        actual=self.s.store.records('wellness')[0]
        self.assertEqual((actual['hrv_ms'],actual['hrv_type'],actual['device']),(35,'SDNN',None))
        self.assertEqual(actual['sleep_hours'],8)
    def test_refresh_updates_icu_without_changing_manual(self):
        self.sync({'date':'2040-11-15','sleep_hours':6})
        self.sync({'date':'2040-11-15','sleep_hours':7})
        self.assertEqual(self.s.store.records('wellness')[0]['sleep_hours'],8)
        self.assertEqual(self.s.store.wellness_provenance()[0]['sources']['icu']['sleep_hours'],7)
