"""Synthetic data only. Projection must not change recommendations or permissions."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from indoor_coach.service import Service


def plan(n=0, unsafe=False):
    return {'state':'proposed','requires_confirmation':True,'execution_supported':False,
            'ftp_basis':{'ftp_w':240,'source':'explicit'},
            'next_workout':{'date':'2040-11-16','kind':'easy','duration_minutes':40,'steps':[]},
            'week_plan':[{'date':'2040-11-16','kind':'easy','duration_minutes':40,'steps':[]}],
            'rationale':{'n':n,'recent_activity_context':([{'id':'restricted','date':'2040-11-15','source':'STRAVA'}] if unsafe else [])},
            'limitations':['Synthetic fixture, not a training prescription.']}


class ProjectionTests(unittest.TestCase):
    def setUp(self): self.s=Service(':memory:')
    def tearDown(self): self.s.store.close()
    def test_default_full_backward_compatible(self):
        a=self.s.call('analyze',{'as_of':'2040-11-15','ftp':240})
        b=self.s.call('analyze',{'as_of':'2040-11-15','ftp':240,'detail':'full'})
        self.assertEqual(a,b)
        self.assertIn('agent_workflow',a)
    def test_summary_preserves_permission_evidence_limits(self):
        full=self.s.call('analyze',{'as_of':'2040-11-15','ftp':240})
        small=self.s.call('analyze',{'as_of':'2040-11-15','ftp':240,'detail':'summary'})
        self.assertEqual(full['load'],small['load'])
        self.assertEqual(full['data_review']['recommendation_permission'],small['data_review']['recommendation_permission'])
        self.assertEqual(full['data_review']['snapshot_id'],small['data_review']['snapshot_id'])
        self.assertIn('missing_evidence',small['data_review']['evidence'])
        self.assertIn('unsupported_claims',small['data_review']['evidence'])
        self.assertIn('limitations',small)
        self.assertNotIn('agent_workflow',small)
        self.assertNotIn('agent_instructions',json.dumps(small))
        self.assertLess(len(json.dumps(small)),len(json.dumps(full)))
    def test_legacy_list_shape_and_keyset_pages(self):
        saved=[self.s.store.proposal(plan(i)) for i in range(5)]
        self.assertIsInstance(self.s.call('list_proposals',{}),list)
        with patch.object(self.s.store,'proposals',side_effect=AssertionError('full table fetch')):
            page=self.s.call('list_proposals',{'limit':2,'detail':'summary'})
            self.assertEqual(len(page['items']),2)
            self.assertEqual(page['scanned'],3)
            self.assertTrue(page['has_more'])
            second=self.s.call('list_proposals',{'limit':2,'after':page['next_cursor'],'detail':'summary'})
            final=self.s.call('list_proposals',{'limit':2,'after':second['next_cursor']})
        self.assertEqual([p['id'] for p in page['items']+second['items']+final['items']],[p['id'] for p in saved])
        self.assertFalse(final['has_more']); self.assertIsNone(final['next_cursor'])
    def test_unsafe_rows_count_toward_page_bound(self):
        for i in range(8): self.s.store.proposal(plan(i,True))
        page=self.s.call('list_proposals',{'limit':2,'detail':'summary'})
        self.assertEqual(page['scanned'],3)
        self.assertEqual(len(page['items']),2)
        self.assertTrue(all(p.get('payload_withheld') for p in page['items']))
    def test_get_does_not_scan_all_and_keeps_full_details(self):
        saved=self.s.store.proposal(plan())
        with patch.object(self.s.store,'proposals',side_effect=AssertionError('full table fetch')):
            full=self.s.call('get_proposal',{'proposal_id':saved['id']})
            small=self.s.call('get_proposal',{'proposal_id':saved['id'],'detail':'summary'})
        self.assertEqual(full['plan'],saved['plan'])
        self.assertEqual(small['plan']['ftp_basis'],saved['plan']['ftp_basis'])
        self.assertNotIn('rationale',small['plan'])
        with self.assertRaises(ValueError): self.s.call('get_proposal',{'proposal_id':'missing'})
    def test_preview_no_full_scan_and_provenance_gate(self):
        saved=self.s.store.proposal(plan())
        args={'proposal_id':saved['id'],'account':'i123','timezone':'UTC','ftp':240}
        with patch.object(self.s.store,'proposals',side_effect=AssertionError('full table fetch')), patch('indoor_coach.workout_export.preview',return_value={'preview':True}) as p:
            self.assertTrue(self.s.call('preview_icu',args)['preview'])
            self.assertEqual(p.call_args.args[0]['id'],saved['id'])
        unsafe=self.s.store.proposal(plan(1,True))
        with self.assertRaises(ValueError): self.s.call('preview_icu',{**args,'proposal_id':unsafe['id']})
        self.assertTrue(self.s.call('get_proposal',{'proposal_id':unsafe['id']})['payload_withheld'])
    def test_invalid_page_and_detail(self):
        for args in ({'limit':0},{'limit':101},{'limit':True},{'after':-1},{'after':'1'},{'after':2**65}):
            with self.assertRaises(ValueError): self.s.call('list_proposals',args)
        with self.assertRaises(ValueError): self.s.call('analyze',{'as_of':'2040-11-15','detail':'tiny'})
    def test_jsonl_smoke_legacy_and_summary(self):
        rows=[{'id':1,'tool':'analyze','arguments':{'as_of':'2040-11-15','ftp':240,'detail':'summary'}},
              {'id':2,'tool':'list_proposals','arguments':{}},
              {'id':3,'tool':'list_proposals','arguments':{'limit':2,'detail':'summary'}}]
        p=subprocess.run([sys.executable,'-m','indoor_coach','--db',':memory:'],input='\n'.join(json.dumps(x) for x in rows)+'\n',text=True,capture_output=True)
        self.assertEqual(p.returncode,0)
        output=[json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual(output[0]['result']['detail'],'summary')
        self.assertEqual(output[1]['result'],[])
        self.assertEqual(output[2]['result']['items'],[])
        self.assertEqual(output[2]['result']['scanned'],0)


@unittest.skipUnless(importlib.util.find_spec('mcp'),'optional SDK unavailable')
class CompactMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_summary_page_and_detail(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as td:
            path=td+'/db.sqlite3'
            s=Service(path); saved=s.store.proposal(plan()); s.store.close()
            params=StdioServerParameters(command=sys.executable,args=['-m','indoor_coach.mcp_server'],env={**os.environ,'INDOOR_COACH_DB':path})
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tool_list=await session.list_tools()
                    self.assertIn('get_proposal',{t.name for t in tool_list.tools})
                    for name,args in [('analyze',{'as_of':'2040-11-15','ftp':240,'detail':'summary'}),('list_proposals',{'limit':1,'detail':'summary'}),('get_proposal',{'proposal_id':saved['id']})]:
                        r=await session.call_tool(name,args)
                        self.assertFalse(r.isError)
                        payload=json.loads(r.content[0].text)
                        self.assertNotIn('error',payload)
                        if name=='analyze': self.assertEqual(payload['detail'],'summary')
                        if name=='list_proposals': self.assertEqual(payload['items'][0]['id'],saved['id'])
                        if name=='get_proposal': self.assertEqual(payload['plan'],saved['plan'])
