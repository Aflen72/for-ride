"""Agent callable JSON tools. Deliberately no external write tool."""
from .store import Store, valid_date, number, validate
from .analysis import analyze, recommend
from .icu import ICUClient
from .provenance import classify_source
from .evidence import evidence_context, AGENT_WORKFLOW_PROMPT
from .review import review_data
from datetime import date, timedelta
import hashlib
import json
import os

TOOLS = {
 'prepare_analysis':'Data-first review; optionally refresh authorized ICU once, then inspect local zones/history. {as_of, ftp?, target_id?, history_days?:28, refresh?:true}',
 'import_activities':'Store normalized activity records locally; {records: [...]}',
 'import_wellness':'Store daily manual wellness records locally; {records: [...]}',
 'import_baseline':'Store manual 1–7 day baseline; {record: {...}}',
 'sync_icu':'Read authorized Intervals.icu activities/wellness into local DB; {oldest, newest}',
 'analyze':'Evidence and missing-data report; {as_of: YYYY-MM-DD, ftp?: watts}',
 'propose_plan':'Save a local draft only, no calendar write; {as_of, ftp?: watts}',
 'preview_icu':'Export minimal fixed-watt preview only; {proposal_id, account, timezone, ftp, selection?:next|week}',
 'list_proposals':'Read local proposed plans; {}',
}

class Service:
    def __init__(self,db_path): self.store=Store(db_path)
    def call(self,name,args):
        if not isinstance(args,dict): raise ValueError('arguments must be an object')
        if name not in TOOLS: raise ValueError('unknown or disabled tool; external writes are not implemented')
        expected={'prepare_analysis':{'as_of','ftp','target_id','history_days','refresh'},'import_activities':{'records'},'import_wellness':{'records'},'import_baseline':{'record'},'sync_icu':{'oldest','newest'},'analyze':{'as_of','ftp'},'propose_plan':{'as_of','ftp'},'list_proposals':set(),'preview_icu':{'proposal_id','account','timezone','ftp','selection'}}[name]
        if set(args)-expected: raise ValueError('unexpected arguments')
        if name=='preview_icu':
            from .workout_export import preview
            proposals=self.store.proposals()
            proposal=next((p for p in proposals if p['id']==args['proposal_id']),None)
            if proposal is None: raise ValueError('proposal not found')
            return preview(proposal,args['account'],args['timezone'],args['ftp'],args.get('selection','next'))
        if name in ('import_activities','import_wellness'):
            rows=args.get('records')
            if not isinstance(rows,list) or len(rows)>10000: raise ValueError('records must be a list with at most 10000 entries')
            return {'stored':self.store.put_many('activity',rows) if name=='import_activities' else self.store.save_wellness(rows,'manual')}
        if name=='import_baseline': return {'stored':self.store.put_many('baseline',[args['record']])}
        if name=='sync_icu':
            valid_date(args.get('oldest')); valid_date(args.get('newest'))
            try:
                client=ICUClient.from_env()
                activities=client.activities(args['oldest'],args['newest'])
                wellness=client.wellness(args['oldest'],args['newest'])
                for row in activities: validate(row,'activity')
                for row in wellness: validate(row,'wellness')
                activity_count=self.store.put_many('activity',activities)
                wellness_count=self.store.save_wellness(wellness,'icu')
            except Exception:
                self.store.log_sync(args['oldest'],args['newest'],'failed')
                raise
            self.store.log_sync(args['oldest'],args['newest'],'success')
            conflicts=[r for r in self.store.wellness_provenance() if args['oldest'] <= r['date'] <= args['newest'] and r['conflict_fields']]
            return {'activities_stored':activity_count,'wellness_stored':wellness_count,'conflicts':conflicts,'merge_policy':'manual_non_null_priority; HRV group kept together','external_write_enabled':False}
        if name=='list_proposals':
            proposals=self.store.proposals()
            # Old saved plans may contain source-derived context from pre-fix code.
            # Keep local data intact, but do not re-expose those payloads over tools.
            return [({'id':p['id'],'status':p['status'],'payload_withheld':True,
                      'reason':'Legacy proposal includes unverified/restricted source context; regenerate from current analysis.'}
                     if any(classify_source(r) != 'independent' for r in p['plan'].get('rationale',{}).get('recent_activity_context',[]))
                     else p) for p in proposals]
        valid_date(args.get('as_of'))
        ftp=args.get('ftp')
        if ftp is not None: number(ftp,30,700,'ftp')
        history_days=args.get('history_days',28)
        if type(history_days) is not int or not 7 <= history_days <= 90: raise ValueError('history_days must be 7..90')
        target_id=args.get('target_id')
        if target_id is not None and (not isinstance(target_id,str) or not 1 <= len(target_id) <= 200): raise ValueError('invalid target_id')
        if name=='prepare_analysis':
            refresh=args.get('refresh',True)
            if not isinstance(refresh,bool): raise ValueError('refresh must be boolean')
            if refresh:
                oldest=(date.fromisoformat(args['as_of'])-timedelta(days=history_days-1)).isoformat()
                if not os.environ.get('INTERVALS_API_KEY'):
                    self.store.log_sync(oldest,args['as_of'],'not_configured')
                else:
                    try: self.call('sync_icu',{'oldest':oldest,'newest':args['as_of']})
                    except Exception: pass  # Retain available local evidence, expose failed check below.
        activities=self.store.records('activity')
        wellness=self.store.records('wellness')
        baselines=[b for b in self.store.records('baseline') if b['period_end']<args['as_of']]
        baseline=baselines[-1] if baselines else None
        result=analyze(activities,wellness,baseline,args['as_of'],ftp)
        provenance=[r for r in self.store.wellness_provenance() if result['reference_window']['start'] <= r['date'] <= args['as_of']]
        result['wellness_provenance']=provenance
        evidence=evidence_context(activities,args['as_of'],target_id,history_days,self.store.sync_history())
        result['data_review']=review_data(result,evidence,[r for r in provenance if r['conflict_fields']])
        snapshot={'as_of':args['as_of'],'ftp':ftp,'activities':activities,'wellness':wellness,'baseline':baseline,'provenance':provenance,'sync':self.store.sync_history(),'target_id':target_id,'history_days':history_days}
        result['data_review']['snapshot_id']=hashlib.sha256(json.dumps(snapshot,sort_keys=True,allow_nan=False).encode()).hexdigest()
        result['agent_workflow']=AGENT_WORKFLOW_PROMPT
        if name in ('analyze','prepare_analysis'): return result
        if result['data_review']['recommendation_permission']=='needs_data':
            return {'status':'needs_data','plan':None,'saved':False,'external_write_enabled':False,'data_review':result['data_review'],
                    'next_step':'Check existing authorized activity records with prepare_analysis first; ask only for evidence still unavailable afterwards.'}
        plan=recommend(result,ftp,args['as_of'])
        plan['data_review']=result['data_review']
        return self.store.proposal(plan)
