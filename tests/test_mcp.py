"""Synthetic fixtures: fixed dates in 2040 are fictional, not user records."""
import importlib.util
import os
import sys
import tempfile
import unittest

@unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK not installed')
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_tools_and_call(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as td:
            params=StdioServerParameters(command=sys.executable,args=['-m','indoor_coach.mcp_server'],env={**os.environ,'INDOOR_COACH_DB':td+'/mcp.db'})
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    names={t.name for t in tools.tools}
                    self.assertEqual(len(names),10)
                    self.assertNotIn('apply_plan',names)
                    result=await session.call_tool('propose_plan',{'as_of':'2040-11-15','ftp':240})
                    self.assertFalse(result.isError)
                    import json
                    payload=json.loads(result.content[0].text)
                    self.assertEqual(payload['status'],'needs_data')
                    self.assertFalse(payload['external_write_enabled'])
                    prompts=await session.list_prompts()
                    self.assertIn('evidence_first_review',{p.name for p in prompts.prompts})
                    result=await session.call_tool('list_proposals',{})
                    self.assertFalse(result.isError)
                    imported=await session.call_tool('import_activities',{'records':[{'id':'synthetic-restricted','date':'2040-11-15','source':'STRAVA','restricted':False,'source_status':'independent','sport':'VirtualRide','indoor':True,'duration_s':3600,'normalized_power_w':140}]})
                    self.assertFalse(imported.isError)
                    checked=await session.call_tool('analyze',{'as_of':'2040-11-15','ftp':240})
                    checked_data=json.loads(checked.content[0].text)
                    self.assertEqual(checked_data['activities'],[])
                    self.assertEqual(checked_data['load']['restricted_count'],1)
                    self.assertIsNone(checked_data['load']['estimated_tss_known_only'])
