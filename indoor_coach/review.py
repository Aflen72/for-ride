"""Evidence sufficiency gate, not a health/readiness score."""

def review_data(analysis, evidence, conflicts):
    issues=[]
    def issue(code, detail, action):
        issues.append({'code':code,'evidence':detail,'action':action})
    load=analysis.get('load',{})
    if not analysis.get('activities'):
        issue('no_recent_eligible_training','No usable indoor activity in the seven-day analysis window.','Check authorized recent records before issuing an individualized course.')
    for k in ('restricted_count','unknown_source_count','unknown_indoor_count'):
        if load.get(k): issue(k,load[k],'Do not treat excluded records as rest or zero load.')
    if analysis.get('ftp_w') is None: issue('ftp_missing',None,'Do not invent watt targets or infer FTP from low heart rate.')
    if not load.get('tss_complete'): issue('load_incomplete',load.get('estimated_tss_known_only'),'Describe known-only estimates, not complete fatigue load.')
    for gap in evidence.get('missing_evidence',[]):
        issue('evidence_gap',gap,'Narrow the conclusion; follow next_checks before asking the user to repeat available data.')
    access=evidence.get('data_access', {})
    latest=access.get('latest_relevant_attempt')
    target_rejected=evidence.get('target_selection', {}).get('rejected_by_partial_sync', False)
    if access.get('partial_coverage') or access.get('rejected_target_ids'):
        issue('latest_refresh_partial' if latest and latest['status']=='partial' else 'unrepaired_partial_refresh',
              {'unrepaired_partial_day_count':access.get('unrepaired_partial_day_count'),
               'load_window_partial':access.get('load_window_partial'),
               'diagnostics':{k:v for k,v in (access.get('latest_unrepaired_partial_attempt') or {}).get('diagnostics',{}).items() if k!='skipped_activity_ids'}},
              'Use only accepted evidence and disclose skipped data. A narrower later query does not repair unchecked days.')
    if target_rejected:
        issue('requested_target_rejected_by_partial_sync', None,
              'Do not substitute cached or different activity data for the requested rejected record.')
    if latest and latest['status'] in ('failed','error','not_configured'):
        issue('latest_refresh_not_successful',latest,'Use cached evidence transparently; do not claim latest records were verified.')
    usable=[]
    descriptive=[]
    for key,d in analysis.get('dimensions',{}).items():
        if d['status'] in ('insufficient_data','limited_reference'):
            issue(key+'_reference_unavailable',{'current_present':d['current'] is not None,'reference_count':d['reference']['sample_count']},'Do not claim a trend; check existing comparable personal reference first.')
        elif d['current'] is not None:
            (descriptive if key in ('deep_sleep_pct','light_sleep_pct') else usable).append(key)
    if conflicts: issue('source_conflicts',[{'date':r['date'],'fields':r['conflict_fields']} for r in conflicts],'Disclose conflicting sources and priority; do not call either independently verified.')
    if not analysis.get('activities') or target_rejected:
        status,permission='insufficient','needs_data'
    elif issues:
        status,permission='limited','provisional_template_only'
    else:
        status,permission='usable','template_only'
    return {'status':status,'not_a_health_score':True,'issues':issues,'usable_reference_metrics':usable,'descriptive_reference_metrics':descriptive,
            'recommendation_permission':permission,'evidence_workflow':evidence,
            'scope':'Usable means usable for this summary, never medical clearance or verified recovery.'}
