"""Agent callable JSON tools. Deliberately no external write tool."""
from .store import Store, valid_date, number, validate
from .analysis import analyze, recommend
from .icu import ICUClient, recoverable_activity_id
from .ftp import IndoorFTPResolver, error_details
from .evidence import evidence_context, AGENT_WORKFLOW_PROMPT
from .review import review_data
from .projection import project, safe_proposal
from datetime import date, timedelta
import hashlib
import json
import os

TOOLS = {
 'prepare_analysis':'Data-first review with optional authorized ICU refresh, including current indoor FTP unless explicitly supplied. {as_of, ftp?, target_id?, history_days?:28, refresh?:true, detail?:full|summary}',
 'import_activities':'Store normalized activity records locally; {records: [...]}',
 'import_wellness':'Store daily manual wellness records locally; {records: [...]}',
 'import_baseline':'Store manual 1–7 day baseline; {record: {...}}',
 'sync_icu':'Read authorized Intervals.icu activities/wellness into local DB; {oldest, newest}',
 'analyze':'Evidence and missing-data report; {as_of: YYYY-MM-DD, ftp?: watts, detail?:full|summary}',
 'propose_plan':'Save a local draft only, no calendar write; {as_of, ftp?: watts, detail?:full|summary}',
 'preview_icu':'Export minimal fixed-watt preview only; {proposal_id, account, timezone, ftp?: legacy or exact match, selection?:next|week}',
 'list_proposals':'Read local drafts. {} preserves legacy list; {limit?:20, after?:0, detail?:full|summary} enables bounded pagination.',
 'get_proposal':'Read one local draft by ID, filtered for provenance; {proposal_id, detail?:full|summary}',
}

def _validated_sync_rows(rows,kind):
    """Keep valid siblings; never log raw bad records or validator messages."""
    rejected=getattr(rows,'rejected_rows',[])
    skipped=len(rejected)
    ids={recoverable_activity_id(row.get('id')) for row in rejected} if kind=='activity' else set()
    accepted=[]
    for row in rows:
        try:
            accepted.append(validate(row,kind))
        except (ValueError,TypeError,ArithmeticError,RecursionError):
            skipped+=1
            if kind=='activity' and isinstance(row,dict):
                ids.add(recoverable_activity_id(row.get('id')))
    return accepted,skipped,sorted(i for i in ids if i is not None)

class Service:
    def __init__(self,db_path):
        self.store=Store(db_path)
        self.ftp_resolver=IndoorFTPResolver(self.store)
    def call(self,name,args):
        if not isinstance(args,dict): raise ValueError('arguments must be an object')
        args=dict(args)
        detail=args.pop('detail','full')
        supported=('analyze','prepare_analysis','propose_plan','list_proposals','get_proposal')
        if detail not in ('full','summary') or (detail!='full' and name not in supported): raise ValueError('invalid detail')
        return project(name,self._call(name,args),detail)
    def _call(self,name,args):
        if not isinstance(args,dict): raise ValueError('arguments must be an object')
        if name not in TOOLS: raise ValueError('unknown or disabled tool; external writes are not implemented')
        expected={'prepare_analysis':{'as_of','ftp','target_id','history_days','refresh'},'import_activities':{'records'},'import_wellness':{'records'},'import_baseline':{'record'},'sync_icu':{'oldest','newest'},'analyze':{'as_of','ftp'},'propose_plan':{'as_of','ftp'},'list_proposals':{'limit','after'},'get_proposal':{'proposal_id'},'preview_icu':{'proposal_id','account','timezone','ftp','selection'}}[name]
        if set(args)-expected: raise ValueError('unexpected arguments')
        if name=='preview_icu':
            from .workout_export import preview
            proposal=self.store.get_proposal(args['proposal_id'])
            if proposal is None: raise ValueError('proposal not found')
            basis=proposal['plan'].get('ftp_basis',{})
            if basis.get('account') is not None:
                current=self.store.indoor_ftp(basis['account'])
                if not current or current['status'] != 'available' or current['watts'] != basis['watts']:
                    raise ValueError('FTP basis changed or no longer usable; regenerate')
            if safe_proposal(proposal).get('payload_withheld'): raise ValueError('proposal evidence unavailable; regenerate')
            return preview(proposal,args['account'],args['timezone'],args.get('ftp'),args.get('selection','next'))
        if name in ('import_activities','import_wellness'):
            rows=args.get('records')
            if not isinstance(rows,list) or len(rows)>10000: raise ValueError('records must be a list with at most 10000 entries')
            return {'stored':self.store.put_many('activity',rows) if name=='import_activities' else self.store.save_wellness(rows,'manual')}
        if name=='import_baseline': return {'stored':self.store.put_many('baseline',[args['record']])}
        if name=='sync_icu':
            valid_date(args.get('oldest')); valid_date(args.get('newest'))
            span=(date.fromisoformat(args['newest'])-date.fromisoformat(args['oldest'])).days
            if not 0 <= span <= 366: raise ValueError('invalid date range')
            try:
                client=ICUClient.from_env()
                activities=client.activities(args['oldest'],args['newest'])
                wellness=client.wellness(args['oldest'],args['newest'])
                activities,n,skipped_ids=_validated_sync_rows(activities,'activity')
                wellness,m,_=_validated_sync_rows(wellness,'wellness')
                hr_flags=[r.get('heart_rate_quality') for r in activities if r.get('source_status')=='independent']
                diagnostics={'activities_skipped':n,'wellness_skipped':m,'skipped_activity_ids':skipped_ids,
                             'heart_rate_missing_zero':hr_flags.count('missing_zero'),'heart_rate_invalid':hr_flags.count('invalid')}
                status='partial' if n or m else 'success'
                activity_count,wellness_count=self.store.save_icu_sync(args['oldest'],args['newest'],activities,wellness,status,diagnostics)
            except Exception:
                self.store.log_sync(args['oldest'],args['newest'],'failed')
                raise
            conflicts=[r for r in self.store.wellness_provenance() if args['oldest'] <= r['date'] <= args['newest'] and r['conflict_fields']]
            return {'status':status,'activities_stored':activity_count,'wellness_stored':wellness_count,
                    **{k:v for k,v in diagnostics.items() if k!='skipped_activity_ids'},
                    'conflicts':conflicts,'merge_policy':'manual_non_null_priority; HRV group kept together','external_write_enabled':False}
        if name=='get_proposal':
            proposal=self.store.get_proposal(args.get('proposal_id'))
            if proposal is None: raise ValueError('proposal not found')
            return safe_proposal(proposal)
        if name=='list_proposals':
            if args:
                page=self.store.proposal_page(args.get('limit',20),args.get('after',0))
                return {**page,'items':[safe_proposal(p) for p in page['items']]}
            return [safe_proposal(p) for p in self.store.proposals()]
        valid_date(args.get('as_of'))
        ftp=args.get('ftp')
        if ftp is not None: number(ftp,30,700,'ftp')
        history_days=args.get('history_days',28)
        if type(history_days) is not int or not 7 <= history_days <= 90: raise ValueError('history_days must be 7..90')
        target_id=args.get('target_id')
        if target_id is not None and (not isinstance(target_id,str) or not 1 <= len(target_id) <= 200): raise ValueError('invalid target_id')
        requested=os.environ.get('INTERVALS_ATHLETE_ID','0')
        key=os.environ.get('INTERVALS_API_KEY')
        settings_refreshed=False
        sync_error=None
        if name=='prepare_analysis':
            refresh=args.get('refresh',True)
            if not isinstance(refresh,bool): raise ValueError('refresh must be boolean')
            if refresh:
                oldest=(date.fromisoformat(args['as_of'])-timedelta(days=history_days-1)).isoformat()
                if not key:
                    self.store.log_sync(oldest,args['as_of'],'not_configured')
                else:
                    try: self.call('sync_icu',{'oldest':oldest,'newest':args['as_of']})
                    except Exception as exc: sync_error=error_details(exc,'sync')
                    if ftp is None:
                        settings_refreshed=self.ftp_resolver.refresh(ICUClient.from_env,requested,key)
        ftp_basis=self.ftp_resolver.basis(manual=ftp,as_of=args['as_of'],requested=requested,key=key,refreshed=settings_refreshed)
        ftp=ftp_basis['watts']
        activities=self.store.records('activity')
        wellness=self.store.records('wellness')
        baselines=[b for b in self.store.records('baseline') if b['period_end']<args['as_of']]
        baseline=baselines[-1] if baselines else None
        start=(date.fromisoformat(args['as_of'])-timedelta(days=history_days-1)).isoformat()
        load_start=(date.fromisoformat(args['as_of'])-timedelta(days=6)).isoformat()
        sync_attempts=self.store.sync_history()
        day_coverage,quarantined=self.store.icu_sync_scope(start,args['as_of'])
        evidence=evidence_context(activities,args['as_of'],target_id,history_days,sync_attempts,
                                  day_coverage=day_coverage,quarantined_ids=quarantined)
        access=evidence['data_access']
        rejected=set(access.pop('rejected_target_ids',[])) | quarantined
        entries=[r for r in activities if r['id'] not in rejected]
        result=analyze(entries,wellness,baseline,args['as_of'],ftp)
        quarantined_load=any(r['id'] in rejected and load_start <= r['date'] <= args['as_of'] for r in activities)
        if access.get('load_window_partial') or quarantined_load:
            result['load']['tss_complete']=False
            result['load']['incomplete_due_to_partial_sync']=True
            result['warnings'].append('Partial ICU sync or quarantined cached activities make this load incomplete; known-only estimates are not total load.')
        result['ftp_basis']=ftp_basis
        provenance=[r for r in self.store.wellness_provenance() if result['reference_window']['start'] <= r['date'] <= args['as_of']]
        result['wellness_provenance']=provenance
        if sync_error:
            access['refresh_error']=sync_error
        result['data_review']=review_data(result,evidence,[r for r in provenance if r['conflict_fields']])
        snapshot={'as_of':args['as_of'],'ftp':{k:v for k,v in ftp_basis.items() if k not in ('source','refresh_error')},'activities':entries,'wellness':wellness,'baseline':baseline,'provenance':provenance,'sync':sync_attempts,'sync_days':day_coverage,'quarantined':sorted(rejected),'target_id':target_id,'history_days':history_days}
        result['data_review']['snapshot_id']=hashlib.sha256(json.dumps(snapshot,sort_keys=True,allow_nan=False).encode()).hexdigest()
        result['agent_workflow']=AGENT_WORKFLOW_PROMPT
        if name in ('analyze','prepare_analysis'): return result
        if result['data_review']['recommendation_permission']=='needs_data':
            return {'status':'needs_data','plan':None,'saved':False,'external_write_enabled':False,'data_review':result['data_review'],'ftp_basis':ftp_basis,
                    'next_step':'Check existing authorized activity records with prepare_analysis first; ask only for evidence still unavailable afterwards.'}
        plan=recommend(result,ftp,args['as_of'])
        plan['data_review']=result['data_review']
        plan['ftp_basis']=ftp_basis
        return self.store.proposal(plan)
