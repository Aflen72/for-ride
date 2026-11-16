"""Only synthetic fixtures; no accounts, credentials, or live calls."""
import unittest
from unittest.mock import patch
from indoor_coach.icu import normalize_activity
from indoor_coach.analysis import analyze, recommend
from indoor_coach.service import Service
from indoor_coach.store import validate


def fixture(**overrides):
    row={'id':'synthetic-merged','source':'GARMIN_CONNECT','strava_id':'synthetic-link',
         'type':'VirtualRide','start_date_local':'2040-11-15T10:00:00',
         'moving_time':3600,'icu_average_watts':135,'icu_weighted_avg_watts':140}
    row.update(overrides)
    return row


def report(*rows):
    return analyze(list(rows),[],None,'2040-11-15',200)


class SourceClassificationTests(unittest.TestCase):
    def test_independent_link_does_not_restrict(self):
        for source in ('GARMIN_CONNECT','SUUNTO','UPLOAD','COROS','WAHOO'):
            with self.subTest(source=source):
                row=normalize_activity(fixture(source=source))
                self.assertIs(row['restricted'],False)
                self.assertEqual(row['source_status'],'independent')
                validate(row,'activity')
                r=report(row)
                self.assertEqual(r['load']['known_minutes'],60)
                self.assertEqual(r['load']['estimated_tss_known_only'],49)
                self.assertNotIn('strava_id',row)
    def test_source_case_whitespace(self):
        row=normalize_activity(fixture(source='  garmin_connect '))
        self.assertEqual(row['source'],'GARMIN_CONNECT')
        self.assertFalse(row['restricted'])
    def test_missing_np_stays_unknown(self):
        row=normalize_activity(fixture(icu_weighted_avg_watts=None))
        r=report(row)
        self.assertEqual(r['load']['known_minutes'],60)
        self.assertIsNone(r['load']['estimated_tss_known_only'])
        self.assertFalse(r['load']['tss_complete'])
    def test_strava_without_or_with_link_remains_restricted(self):
        for link in (None,'synthetic-link'):
            row=normalize_activity(fixture(source=' strava ',strava_id=link))
            self.assertTrue(row['restricted'])
            self.assertEqual(report(row)['activities'],[])
    def test_explicit_restriction_dominates_independent_source(self):
        row=normalize_activity(fixture(restricted=True))
        self.assertTrue(row['restricted'])
        self.assertEqual(report(row)['load']['restricted_count'],1)
    def test_unknown_source_does_not_claim_strava_or_unrestricted(self):
        for source in (None,'','FUTURE_SOURCE','STRAVA_ARCHIVE','NOT_STRAVA'):
            with self.subTest(source=source):
                row=normalize_activity(fixture(source=source))
                self.assertIsNone(row['restricted'])
                self.assertEqual(row['source_status'],'unknown')
                r=report(row)
                self.assertEqual(r['activities'],[])
                self.assertEqual(r['load']['unknown_source_count'],1)
                self.assertEqual(r['load']['restricted_count'],0)
                self.assertFalse(r['load']['tss_complete'])
                self.assertTrue(recommend(r)['rationale']['incomplete_data'])
    def test_malformed_restriction_marker_is_not_permission(self):
        for marker in ('false',0,1,[],{}):
            row=normalize_activity(fixture(restricted=marker))
            self.assertEqual(row['source_status'],'unknown')
    def test_metadata_not_zero_load(self):
        row=normalize_activity({'id':'synthetic-stub','start_date_local':'2040-11-15'})
        self.assertIsNone(row['duration_s'])
        self.assertIsNone(report(row)['load']['estimated_tss_known_only'])
        self.assertIn('Metadata-only',row['withheld_reason'])
    def test_unknown_source_keeps_aggregate_incomplete(self):
        known=normalize_activity(fixture())
        unknown=normalize_activity(fixture(id='unknown',source=None))
        r=report(known,unknown)
        self.assertEqual(r['load']['estimated_tss_known_only'],49)
        self.assertFalse(r['load']['tss_complete'])
    def test_resync_repairs_old_false_positive_without_unblocking_strava(self):
        s=Service(':memory:')
        try:
            old=normalize_activity(fixture())
            old.pop('source_status')
            old.update(restricted=True,withheld_reason='legacy false positive')
            blocked=normalize_activity(fixture(id='strava-stub',source='STRAVA'))
            s.call('import_activities',{'records':[old,blocked]})
            self.assertEqual(s.call('analyze',{'as_of':'2040-11-15','ftp':200})['activities'],[])
            with patch('indoor_coach.service.ICUClient.from_env') as factory:
                factory.return_value.activities.return_value=[normalize_activity(fixture()),blocked]
                factory.return_value.wellness.return_value=[]
                for _ in range(2):
                    s.call('sync_icu',{'oldest':'2040-11-15','newest':'2040-11-15'})
            records=s.store.records('activity')
            self.assertEqual(len(records),2)
            fixed=next(r for r in records if r['id']=='synthetic-merged')
            self.assertFalse(fixed['restricted'])
            self.assertNotIn('withheld_reason',fixed)
            r=s.call('analyze',{'as_of':'2040-11-15','ftp':200})
            self.assertEqual(r['load']['known_minutes'],60)
            self.assertEqual(r['load']['restricted_count'],1)
            self.assertFalse(r['load']['tss_complete'])
        finally: s.store.close()

class SharedProvenanceBoundaryTests(unittest.TestCase):
    def row(self, **overrides):
        row={'id':'declared-source','date':'2040-11-15','sport':'VirtualRide',
             'indoor':True,'duration_s':3600,'normalized_power_w':140,
             'source':'STRAVA','restricted':False,'source_status':'independent'}
        row.update(overrides)
        return row
    def test_import_cannot_override_known_restricted_source(self):
        s=Service(':memory:')
        try:
            s.call('import_activities',{'records':[self.row(source=' strava ')]})
            stored=s.store.records('activity')[0]
            self.assertEqual(stored['source_status'],'restricted')
            self.assertTrue(stored['restricted'])
            self.assertEqual(s.call('analyze',{'as_of':'2040-11-15','ftp':200})['activities'],[])
            p=s.call('propose_plan',{'as_of':'2040-11-15','ftp':200})
            self.assertEqual(p['status'],'needs_data')
            self.assertIsNone(p['plan'])
        finally: s.store.close()
    def test_legacy_rows_rechecked_at_analysis_boundary(self):
        import json
        s=Service(':memory:')
        try:
            # Simulate a pre-fix persisted payload without passing today's validator.
            s.store.db.execute('INSERT INTO records VALUES(?,?,?)',('activity','legacy',json.dumps(self.row())))
            r=s.call('analyze',{'as_of':'2040-11-15','ftp':200})
            self.assertEqual(r['activities'],[])
            self.assertEqual(r['load']['restricted_count'],1)
            self.assertIsNone(r['load']['estimated_tss_known_only'])
        finally: s.store.close()
    def test_direct_analysis_rejects_forged_independent_hint(self):
        self.assertEqual(report(self.row())['load']['restricted_count'],1)
    def test_unknown_hint_cannot_authorize_unknown_source(self):
        for source in (None,'UNVERIFIED','STRAVA_ARCHIVE'):
            row=validate(self.row(source=source),'activity')
            self.assertEqual(row['source_status'],'unknown')
            self.assertIsNone(row['restricted'])
            self.assertEqual(report(self.row(source=source))['load']['unknown_source_count'],1)
    def test_original_user_files_remain_usable(self):
        for source in ('UPLOAD','MANUAL',' upload '):
            row=validate(self.row(source=source),'activity')
            self.assertEqual(row['source_status'],'independent')
            self.assertEqual(report(row)['load']['known_minutes'],60)
    def test_explicit_restriction_hint_cannot_be_downgraded(self):
        for kwargs in ({'restricted':True},{'source_status':'restricted'}):
            row=self.row(source='UPLOAD',**kwargs)
            self.assertEqual(validate(row,'activity')['source_status'],'restricted')
            self.assertEqual(report(row)['load']['restricted_count'],1)
    def test_legacy_proposal_context_not_reexposed(self):
        s=Service(':memory:')
        try:
            saved=s.store.proposal({'rationale':{'recent_activity_context':[self.row()]}})
            result=s.call('list_proposals',{})[0]
            self.assertEqual(result['id'],saved['id'])
            self.assertTrue(result['payload_withheld'])
            self.assertNotIn('plan',result)
            self.assertIn('plan',s.store.proposals()[0])  # Preserve local original.
        finally: s.store.close()
