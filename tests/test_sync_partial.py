"""Public tool entry-point tests using synthetic ICU transport, never real data."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from indoor_coach.cli import main

DAY='2040-11-15'
START='2040-10-19'
ARGS={'as_of':DAY,'ftp':200}
SYNC={'oldest':START,'newest':DAY}


def ride(ident, hr=143, **changes):
    return {'id':ident, 'start_date_local':DAY, 'source':'UPLOAD', 'type':'VirtualRide',
            'trainer':True, 'moving_time':1800, 'average_heartrate':hr,
            'icu_average_watts':140, 'icu_weighted_avg_watts':150, **changes}


def command(tool, **arguments):
    return {'id':tool,'tool':tool,'arguments':arguments}


class Response(io.BytesIO):
    pass


class PartialSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=str(Path(self.temp.name)/'synthetic.sqlite')

    def isolated_env(self, **extra):
        # Both entry points evaluate a default based on Path.home() even when a
        # DB path is supplied. Windows requires USERPROFILE when env is cleared.
        return patch.dict(os.environ, {
            'HOME':self.temp.name,
            'USERPROFILE':self.temp.name,
            'INTERVALS_API_KEY':'synthetic-only',
            **extra,
        },clear=True)

    @contextmanager
    def transport(self, activities, wellness):
        def request(req,timeout):
            key='activities' if '/activities?' in req.full_url else 'wellness' if '/wellness?' in req.full_url else None
            self.assertIsNotNone(key, 'unexpected API endpoint')
            result=activities if key=='activities' else wellness
            if isinstance(result,int):
                raise urllib.error.HTTPError(req.full_url,result,'synthetic server failure',{},Response(b'PRIVATE_UPSTREAM_ERROR'))
            return Response(json.dumps(result).encode())
        with patch('urllib.request.OpenerDirector.open', side_effect=request):
            yield

    def cli(self,*cmds):
        buf=io.StringIO()
        stdin=io.StringIO(''.join(json.dumps(c)+'\n' for c in cmds))
        with patch('sys.argv',['indoor-coach','--db',self.db]), patch('sys.stdin',stdin), patch('sys.stdout',buf), self.isolated_env():
            main()
        return [json.loads(row) for row in buf.getvalue().splitlines()]

    def test_normalize_zero_and_nonzero_invalid_preserve_power_time_and_mark_quality(self):
        with self.transport([ride('i501',0),ride('i502',251)], [{'id':DAY,'sleepSecs':28800}]):
            sync, report=self.cli(command('sync_icu',**SYNC),command('analyze',**ARGS))
        s,r=sync['result'],report['result']
        self.assertEqual((s['status'],s['activities_stored'],s['wellness_stored']),('success',2,1))
        self.assertEqual((s['heart_rate_missing_zero'],s['heart_rate_invalid']),(1,1))
        self.assertTrue(r['load']['tss_complete'])
        self.assertEqual(r['dimensions']['sleep_hours']['current'],8)
        for rec,flag in zip(r['activities'],('missing_zero','invalid')):
            self.assertIsNone(rec['avg_hr_bpm'])
            self.assertEqual(rec['heart_rate_quality'],flag)
            self.assertEqual(rec['duration_minutes'],30)
            self.assertEqual(rec['normalized_power_w'],150)

    def test_normalization_and_validation_failures_isolated_both_sources_and_persisted(self):
        import sqlite3
        rows=[ride('i511',0), 'PRIVATE_ACTIVITIES_PAYLOAD', ride('i512',moving_time=0),
              ride('i513',start_date_local='PRIVATE_BAD_DATE')]
        health=[{'id':DAY,'sleepSecs':28800},None,{'id':'2040-11-14','hrv':5000}]
        with self.transport(rows,health):
            result=self.cli(command('sync_icu',**SYNC))[0]['result']
        self.assertEqual((result['status'],result['activities_stored'],result['wellness_stored']),('partial',1,1))
        self.assertEqual((result['activities_skipped'],result['wellness_skipped']),(3,2))
        for mode in ('full','summary'):
            rep=self.cli(command('analyze',**ARGS,detail=mode))[0]['result']
            self.assertFalse(rep['load']['tss_complete'])
            self.assertGreater(rep['load']['estimated_tss_known_only'],0)
            self.assertEqual(rep['data_review']['status'],'limited')
            self.assertNotIn('PRIVATE_',str(rep))
        self.assertNotIn('PRIVATE_',Path(self.db).read_bytes().decode(errors='ignore'))
        with sqlite3.connect(self.db) as db:
            persisted=json.loads(db.execute('SELECT payload FROM sync_diagnostics').fetchone()[0])
            self.assertEqual(persisted['skipped_activity_ids'],['i512','i513'])

    def test_request_level_failure_keeps_old_cache_and_doesnt_commit_unpaired_response(self):
        with self.transport([ride('i521')],[{'id':DAY,'sleepSecs':27000}]):
            self.cli(command('sync_icu',**SYNC))
        for bad_health in (403, {'unexpected':'PRIVATE_NON_LIST_PAYLOAD'}):
            with self.subTest(bad_health=type(bad_health).__name__), self.transport([ride('i522')],bad_health):
                err, rep=self.cli(command('sync_icu',**SYNC),command('analyze',**ARGS))
            self.assertIn('error',err)
            self.assertNotIn('PRIVATE_',str(err))
            self.assertEqual([r['id'] for r in rep['result']['activities']],['i521'])
            self.assertEqual(rep['result']['dimensions']['sleep_hours']['current'],7.5)
            self.assertTrue(any(i['code']=='latest_refresh_not_successful' for i in rep['result']['data_review']['issues']))

    def test_rejected_explicit_cached_target_not_replaced_and_cache_recovers(self):
        with self.transport([ride('i531')],[]):
            self.cli(command('sync_icu',**SYNC))
        with self.transport([ride('i531', moving_time=0),ride('i532')],[]):
            rev=self.cli(command('prepare_analysis',**ARGS,target_id='i531'))[0]['result']
        evidence=rev['data_review']['evidence_workflow']
        self.assertIsNone(evidence['target'])
        self.assertTrue(evidence['target_selection']['rejected_by_partial_sync'])
        self.assertFalse(rev['load']['tss_complete'])
        with self.transport([ride('i531', 148),ride('i532')],[]):
            fresh=self.cli(command('prepare_analysis',**ARGS,target_id='i531'))[0]['result']
        ev=fresh['data_review']['evidence_workflow']
        self.assertEqual(ev['target']['avg_hr_bpm'],148)
        self.assertTrue(fresh['load']['tss_complete'])

    def test_narrow_success_does_not_erase_gap_or_unhide_cached_row(self):
        bad_day='2040-11-14'
        historical=ride('i541',start_date_local=bad_day)
        with self.transport([historical,ride('i542')],[]):
            self.cli(command('sync_icu',**SYNC))
        with self.transport([{**historical,'moving_time':0},ride('i542')],[]):
            self.cli(command('sync_icu',**SYNC))
        # Exercise persistent per-day status independently from the last 20 audit attempts.
        with self.transport([ride('i542')],[]):
            self.cli(*(command('sync_icu',oldest=DAY,newest=DAY) for _ in range(21)))
        rep=self.cli(command('analyze',**ARGS,detail='summary'))[0]['result']
        self.assertTrue(rep['data_review']['evidence']['data_access']['partial_coverage'])
        self.assertFalse(rep['load']['tss_complete'])
        with self.transport([ride('i542')],[]):
            rep=self.cli(command('prepare_analysis',**ARGS,target_id='i541',detail='summary'))[0]['result']
        ev=rep['data_review']['evidence']
        self.assertIsNone(ev['target'])
        self.assertTrue(ev['target_selection']['rejected_by_partial_sync'])
        self.assertFalse(rep['load']['tss_complete'])
        self.assertEqual(rep['activity_count'],1)

    def test_mcp_compact_refresh_contract(self):
        import asyncio
        import importlib.util
        if not importlib.util.find_spec('mcp'): self.skipTest('optional MCP dependency absent')
        import indoor_coach.mcp_server as server
        with self.transport([ride('i551'),None],[]), self.isolated_env(INDOOR_COACH_DB=self.db), patch.object(server,'_service',None):
            try:
                output=asyncio.run(server.mcp.call_tool('prepare_analysis',{**ARGS,'detail':'summary'}))
                text=output[0] if isinstance(output,tuple) else output
                report=json.loads(text[0].text)
            finally:
                if server._service: server._service.store.close()
        self.assertFalse(report['load']['tss_complete'])
        self.assertEqual(report['data_review']['evidence']['data_access']['latest_relevant_attempt']['status'],'partial')
