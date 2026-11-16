"""Synthetic only: no network, keys, or real athlete accounts."""
import copy
import datetime as dt
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

from indoor_coach.workout_export import preview, digest, validate_export
from indoor_coach.writer import Journal, RequestFailed, Terminal, WriterClient, run
from indoor_coach.service import Service


def fixture(week=False):
    today = dt.datetime.now(dt.timezone.utc).date()
    plan = {'week_plan':[{'kind':'easy','date':(today+dt.timedelta(days=i+1)).isoformat(),
             'steps':[{'duration_minutes':10,'ftp_fraction':[.5,.6],'power_w':[100,120]},
                      {'duration_minutes':20,'ftp_fraction':[.6,.7],'power_w':[120,140]}]} for i in range(2)]}
    ident = hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    return preview({'id':ident,'plan':plan}, '123456', 'UTC', 200, 'week' if week else 'next')


class Fake:
    def __init__(self):
        self.rows=[]; self.posts=0; self.gets=0; self.mode='ok'
        self.identity={'id':'123456','timezone':'UTC'}
    def account(self): return self.identity.copy()
    def events(self, day): return [copy.deepcopy(r) for r in self.rows if r['start_date_local'][:10]==day]
    def event(self, ident):
        self.gets+=1
        return copy.deepcopy(next(r for r in self.rows if r['id']==ident))
    def create(self, event):
        self.posts+=1
        if self.mode in ('401','403','429','500'): raise RequestFailed(int(self.mode))
        if self.mode=='timeout': raise RequestFailed()
        row=copy.deepcopy(event)
        row.update(id=self.posts,athlete_id='123456',moving_time=1800,joules=600*110+1200*130)
        row['workout_doc']={'duration':1800,'steps':[{'duration':600,'power':{'units':'w','value':110}},{'duration':1200,'power':{'units':'w','value':130}}]}
        if self.mode=='mismatch': row['description']='- 1800s 999w'
        self.rows.append(row)
        if self.mode=='landed_timeout': raise RequestFailed()
        if self.mode=='missing_id': return [{}]
        return [{'id':row['id']}]


class Human:
    """Test-only stand-in for independent interactive user."""
    def __init__(self, action=None): self.calls=0; self.action=action
    def confirm(self, export):
        self.calls+=1
        if self.action: self.action(export)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'journal.sqlite3'
        self.client=Fake(); self.export=fixture(); self.human=Human()
    def tearDown(self): self.tmp.cleanup()
    def execute(self, export=None, human=None, reconcile=False):
        with Journal(self.path) as j: return run(export or self.export,self.client,j,human or self.human,reconcile)
    def test_success_independent_get_and_repeat(self):
        self.assertEqual(self.execute()[0]['status'],'verified')
        self.assertEqual(self.client.gets,1)
        self.execute(); self.assertEqual(self.client.posts,1); self.assertEqual(self.human.calls,1)
    def test_no_confirmation(self):
        def deny(e): raise ValueError('no')
        with self.assertRaises(ValueError): self.execute(human=Human(deny))
        self.assertEqual(self.client.posts,0)
    def test_boolean_is_not_confirmation(self):
        with self.assertRaises(AttributeError): self.execute(human=True)
        self.assertEqual(self.client.posts,0)
    def test_agent_surface_rejects_approval_and_write(self):
        s=Service(':memory:')
        try:
            for name,args in [('apply_plan',{'confirmed':True}),('preview_icu',{'confirmed':True})]:
                with self.assertRaises(ValueError): s.call(name,args)
        finally: s.store.close()
    def test_tamper_blocks(self):
        self.export['payload']['ftp_w']=250
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(self.client.posts,0)
    def test_rehashed_invalid_event_blocks(self):
        self.export['payload']['courses'][0]['event']['description']='- 1s 1500w'
        self.export['sha256']=digest(self.export['payload'])
        with self.assertRaises(ValueError): self.execute()
    def test_wrong_account(self):
        self.client.identity['id']='999'
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(self.client.posts,0)
    def test_account_changes_during_confirmation(self):
        def change(e): self.client.identity['id']='999'
        with self.assertRaises(ValueError): self.execute(human=Human(change))
        self.assertEqual(self.client.posts,0)
    def test_timezone_mismatch(self):
        self.client.identity['timezone']='Europe/London'
        with self.assertRaises(ValueError): self.execute()
    def test_expired(self):
        b=self.export['payload']; b['created_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=2)).isoformat(); b['expires_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=1)).isoformat()
        self.export['sha256']=digest(b)
        with self.assertRaises(ValueError): self.execute()
    def test_conflicting_calendar_item(self):
        self.client.rows=[dict(self.export['payload']['courses'][0]['event'],description='user edited',id=1)]
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(self.client.posts,0)
    def test_calendar_changes_after_confirmation(self):
        def change(e): self.client.rows=[dict(e['payload']['courses'][0]['event'],id=9)]
        with self.assertRaises(ValueError): self.execute(human=Human(change))
        self.assertEqual(self.client.posts,0)
    def test_timeout_landed_reconciles_without_repost(self):
        self.client.mode='landed_timeout'
        self.assertEqual(self.execute()[0]['status'],'verified')
        self.execute(); self.assertEqual(self.client.posts,1)
    def test_timeout_unknown_never_reposts(self):
        self.client.mode='timeout'
        self.assertEqual(self.execute()[0]['status'],'unknown')
        self.execute(); self.assertEqual(self.client.posts,1)
    def test_missing_id_not_success(self):
        self.client.mode='missing_id'
        self.assertEqual(self.execute()[0]['status'],'unknown')
        self.assertEqual(self.execute(reconcile=True)[0]['status'],'verified')
        self.assertEqual(self.client.posts,1)
    def test_readback_mismatch(self):
        self.client.mode='mismatch'
        self.assertEqual(self.execute()[0]['status'],'verification_failed')
        self.execute(); self.assertEqual(self.client.posts,1)
    def test_readback_checks_all_fields(self):
        from indoor_coach.writer import verify
        self.execute(); row=self.client.rows[0]; b=self.export['payload']; c=b['courses'][0]
        for key in ('athlete_id','start_date_local','type','category','description','moving_time','joules','external_id','id'):
            changed=copy.deepcopy(row); changed[key]=None
            self.assertFalse(verify(changed,b,c),key)
    def test_http_errors_no_retries(self):
        for status in ('401','403','429','500'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                self.path=Path(td)/'j'; self.client=Fake(); self.client.mode=status
                self.assertEqual(self.execute()[0]['status'],'unknown' if status=='500' else 'rejected')
                self.execute(); self.assertEqual(self.client.posts,1)
    def test_partial_week(self):
        self.export=fixture(True)
        original=self.client.create
        def create(event):
            if self.client.posts: self.client.mode='timeout'
            return original(event)
        self.client.create=create
        self.assertEqual([r['status'] for r in self.execute()],['verified','unknown'])
        self.execute(); self.assertEqual(self.client.posts,2)
    def test_lock_prevents_concurrent_writer(self):
        with Journal(self.path):
            with self.assertRaises(ValueError):
                with Journal(self.path): pass
        self.assertEqual(self.client.posts,0)
    def test_changed_binding_after_success_blocks(self):
        self.execute(); self.export['payload']['ftp_w']=201; self.export['sha256']=digest(self.export['payload'])
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(self.client.posts,1)
    def test_crash_submitting_reconciles_only(self):
        from indoor_coach.writer import binding
        b=self.export['payload']; c=b['courses'][0]
        with Journal(self.path) as j: j.set(c['event']['external_id'],binding(b,c),'submitting')
        self.assertEqual(self.execute()[0]['status'],'unknown'); self.assertEqual(self.client.posts,0)
    def test_reconcile_only_cannot_create(self):
        self.assertEqual(self.execute(reconcile=True)[0]['status'],'not_submitted')
        self.assertEqual(self.client.posts,0); self.assertEqual(self.human.calls,0)
    def test_export_minimal(self):
        text=json.dumps(self.export)
        for forbidden in ('sleep','hrv','rationale','icu_workout','fatigue'): self.assertNotIn(forbidden,text)
    def test_external_identity_not_date_only(self):
        e=fixture(); b=e['payload']; b['proposal_id']='a'*64
        from indoor_coach.workout_export import event_for
        self.assertNotEqual(event_for(b,0,b['courses'][0])['external_id'],b['courses'][0]['event']['external_id'])
    def test_terminal_rejects_non_tty(self):
        with patch('sys.stdin.isatty',return_value=False), self.assertRaises(ValueError): Terminal().confirm(self.export)
    def test_terminal_mismatch_and_expired(self):
        with patch('sys.stdin.isatty',return_value=True), patch('sys.stdout.isatty',return_value=True), patch('builtins.print'), patch('builtins.input',return_value='confirmed=true'):
            with self.assertRaises(ValueError): Terminal().confirm(self.export)
    def test_terminal_exact_challenge(self):
        challenge='CREATE '+self.export['sha256']+' nonce'
        with patch('sys.stdin.isatty',return_value=True),patch('sys.stdout.isatty',return_value=True),patch('builtins.print'),patch('secrets.token_hex',return_value='nonce'),patch('builtins.input',return_value=challenge):
            Terminal().confirm(self.export)
    def test_transport_safe_error_and_create_url(self):
        client=WriterClient('synthetic-test-secret')
        with patch.object(client._opener,'open',side_effect=RuntimeError('synthetic-test-secret')) as op:
            with self.assertRaises(RequestFailed) as caught: client.create({'type':'Ride'})
            self.assertNotIn('synthetic-test-secret',str(caught.exception))
            request=op.call_args.args[0]
            self.assertEqual(request.method,'POST'); self.assertIn('upsert=false',request.full_url)
            self.assertNotIn('synthetic-test-secret',request.full_url)

    def test_missing_or_ambiguous_structure_fails(self):
        from indoor_coach.writer import verify
        self.execute(); b=self.export['payload']; c=b['courses'][0]
        for doc in (None, {}, {'duration':1800,'steps':[]}, {'duration':1800,'steps':[{'duration':600,'power':{'units':'%ftp','value':55}}, {'duration':1200,'power':{'units':'w','value':130}}]}):
            row=copy.deepcopy(self.client.rows[0]); row['workout_doc']=doc
            self.assertFalse(verify(row,b,c))
    def test_parsed_steps_swapped_or_rounded_fail(self):
        from indoor_coach.writer import verify
        self.execute(); b=self.export['payload']; c=b['courses'][0]
        row=copy.deepcopy(self.client.rows[0]); row['workout_doc']['steps'].reverse()
        self.assertFalse(verify(row,b,c))
        row=copy.deepcopy(self.client.rows[0]); row['workout_doc']['steps'][0]['power']['value']=110.1
        self.assertFalse(verify(row,b,c))
    def test_unknown_first_course_stops_unsubmitted_rest(self):
        self.export=fixture(True); self.client.mode='timeout'
        self.assertEqual([r['status'] for r in self.execute()],['unknown','not_submitted'])
        self.execute(); self.assertEqual(self.client.posts,1)
    def test_confirmation_expiry(self):
        start=dt.datetime.now(dt.timezone.utc)
        with patch('sys.stdin.isatty',return_value=True),patch('sys.stdout.isatty',return_value=True),patch('builtins.print'),patch('secrets.token_hex',return_value='nonce'),patch('builtins.input',return_value='CREATE '+self.export['sha256']+' nonce'),patch('indoor_coach.writer.dt.datetime') as clock:
            clock.now.side_effect=[start,start+dt.timedelta(minutes=6)]
            with self.assertRaises(ValueError): Terminal().confirm(self.export)
    def test_export_ftp_mismatch(self):
        # Explicit FTP basis is mandatory and cannot silently reinterpret an old draft.
        e=fixture(); e['payload']['ftp_w']=True; e['sha256']=digest(e['payload'])
        with self.assertRaises(ValueError): validate_export(e)

    def test_service_preview_and_no_network(self):
        service=Service(':memory:')
        try:
            today=dt.datetime.now(dt.timezone.utc).date().isoformat()
            service.call('import_activities',{'records':[{'id':'synthetic','date':today,'source':'UPLOAD','sport':'VirtualRide','indoor':True,'duration_s':1800,'normalized_power_w':125}]})
            proposal=service.call('propose_plan',{'as_of':today,'ftp':200})
            with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network forbidden')):
                result=service.call('preview_icu',{'proposal_id':proposal['id'],'account':'123456','timezone':'UTC','ftp':200})
            validate_export(result)
            self.assertEqual(result['payload']['proposal_id'],proposal['id'])
            with self.assertRaises(ValueError):
                service.call('preview_icu',{'proposal_id':proposal['id'],'account':'123456','timezone':'UTC','ftp':250})
        finally: service.store.close()
    def test_review_mutation_blocks(self):
        def change(e): e['payload']['courses'][0]['steps'][0]['watts']=999
        with self.assertRaises(ValueError): self.execute(human=Human(change))
        self.assertEqual(self.client.posts,0)
    def test_duplicate_remote_identity_is_not_verified(self):
        self.client.mode='missing_id'; self.execute()
        self.client.rows.append(dict(self.client.rows[0],id=2))
        self.assertEqual(self.execute(reconcile=True)[0]['status'],'verification_failed')
        self.assertEqual(self.client.posts,1)
    def test_transport_http_sanitized(self):
        client=WriterClient('synthetic-secret')
        for status in (401,403,429,500):
            err=urllib.error.HTTPError('https://example.invalid/secret',status,'synthetic-secret',{},None)
            with patch.object(client._opener,'open',side_effect=err):
                with self.assertRaises(RequestFailed) as caught: client.create({})
                self.assertEqual(caught.exception.status,status)
                self.assertNotIn('synthetic-secret',str(caught.exception))

    def test_verified_reconcile_refreshes_unchanged_course(self):
        self.assertEqual(self.execute()[0]['status'],'verified')
        gets=self.client.gets
        self.assertEqual(self.execute(reconcile=True)[0]['status'],'verified')
        self.assertEqual(self.client.gets,gets+1)
        self.assertEqual(self.client.posts,1)
        self.assertEqual(self.human.calls,1)

    def test_verified_remote_edit_fails_fresh_reconciliation(self):
        for reconcile_only in (False,True):
            with self.subTest(reconcile_only=reconcile_only), tempfile.TemporaryDirectory() as td:
                self.path=Path(td)/'journal'; self.client=Fake()
                self.assertEqual(self.execute()[0]['status'],'verified')
                self.client.rows[0]['description']='- 1800s 999w'
                gets=self.client.gets
                self.assertEqual(self.execute(reconcile=reconcile_only)[0]['status'],'verification_failed')
                self.assertEqual(self.client.gets,gets+1)
                self.execute(reconcile=reconcile_only)
                self.assertEqual(self.client.posts,1)

    def test_verified_remote_deletion_is_unknown_without_repost(self):
        self.execute(); self.client.rows.clear()
        with patch.object(self.client,'events',wraps=self.client.events) as listing:
            self.assertEqual(self.execute(reconcile=True)[0]['status'],'unknown')
            listing.assert_called_once_with(self.export['payload']['courses'][0]['date'])
        # Missing from the date list may mean deletion or movement: never infer safe retry.
        self.assertEqual(self.execute()[0]['status'],'unknown')
        self.assertEqual(self.client.posts,1)

    def test_verified_remote_read_failure_does_not_preserve_verified(self):
        self.execute()
        with patch.object(self.client,'event',side_effect=RequestFailed()):
            self.assertEqual(self.execute(reconcile=True)[0]['status'],'unknown')
        self.assertEqual(self.client.posts,1)
