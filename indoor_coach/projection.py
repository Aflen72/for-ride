"""Opt-in bounded tool response projections, never alternate calculation paths."""
from .provenance import classify_source


def unsafe_activity_context(value):
    """Inspect nested saved evidence, including pre-gate proposal payloads."""
    if isinstance(value, dict):
        if 'source' in value and 'id' in value and 'date' in value and classify_source(value) != 'independent':
            return True
        return any(unsafe_activity_context(v) for v in value.values())
    if isinstance(value, list):
        return any(unsafe_activity_context(v) for v in value)
    return False


def safe_proposal(proposal):
    plan=proposal.get('plan',{})
    legacy=plan.get('rationale',{}).get('recent_activity_context',[])
    if any(not isinstance(row,dict) or classify_source(row) != 'independent' for row in legacy) or unsafe_activity_context(plan):
        return {'id':proposal['id'],'status':proposal['status'],'payload_withheld':True,
                'reason':'Legacy proposal includes unverified/restricted source context; regenerate from current analysis.'}
    return proposal


def review_summary(review):
    evidence=review.get('evidence_workflow', {})
    comparison=evidence.get('comparison', {})
    access=evidence.get('data_access', {})
    return {
        **{k:review[k] for k in ('status','not_a_health_score','recommendation_permission','snapshot_id','scope') if k in review},
        'issues':[{'code':x.get('code'),'evidence':x.get('evidence')} for x in review.get('issues',[])],
        'usable_reference_metrics':review.get('usable_reference_metrics',[]),
        'evidence':{**{k:evidence[k] for k in ('window','target_selection','target','heart_rate_evidence','excluded_counts','missing_evidence','next_checks','unsupported_claims','capabilities') if k in evidence},
                    'data_access':{k:v for k,v in access.items() if k != 'sync_attempts'},
                    'comparison':{'criteria':comparison.get('criteria'),'count':comparison.get('count',0),
                                  'sample_records':comparison.get('records',[])[:5],
                                  'records_truncated':len(comparison.get('records',[]))>5}},
    }


def proposal_summary(proposal):
    if proposal.get('payload_withheld'): return proposal
    p=proposal.get('plan')
    result={k:v for k,v in proposal.items() if k not in ('plan','data_review')}
    if not p:
        result['plan']=p
        if 'data_review' in proposal: result['data_review']=review_summary(proposal['data_review'])
        return result
    result['plan']={k:p[k] for k in ('state','requires_confirmation','execution_supported','rule_version','ftp_basis','next_workout','limitations') if k in p}
    week=p.get('week_plan',[])
    result['plan']['week_overview']=[{k:w[k] for k in ('date','kind','duration_minutes') if k in w} for w in week]
    if 'data_review' in p: result['data_review']=review_summary(p['data_review'])
    result['detail_available']='get_proposal(detail="full")'
    return result


def project(name,result,detail):
    if detail=='full': return result
    if name in ('analyze','prepare_analysis'):
        keys=('schema_version','as_of','rule_version','ftp_w','ftp_updated','ftp_resolution','ftp_source','ftp_basis','load_ftp_basis','load','coverage','caution_flags','warnings','limitations')
        out={k:result[k] for k in keys if k in result}
        out['detail']='summary'
        out['dimensions']={k:{'current':v.get('current'),'status':v.get('status'),'delta':v.get('delta'),
                              'reference':{f:v.get('reference',{}).get(f) for f in ('mean','sample_count','source')}} for k,v in result.get('dimensions',{}).items()}
        out['data_review']=review_summary(result.get('data_review',{}))
        out['activity_count']=len(result.get('activities',[]))
        out['activity_details_available']='Repeat this tool with detail="full"; do not repeat refresh solely to expand output.'
        return out
    if name in ('propose_plan','get_proposal'): return proposal_summary(result)
    if name=='list_proposals':
        if isinstance(result,list): return [proposal_summary(p) for p in result]
        return {**result,'items':[proposal_summary(p) for p in result['items']]}
    return result
