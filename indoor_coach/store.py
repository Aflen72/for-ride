"""Local, parameterized SQLite persistence. No credentials or raw API payloads."""
import json
import math
import sqlite3
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from .provenance import enforce_provenance

ACTIVITY_FIELDS = {'id','date','sport','indoor','duration_s','avg_power_w','normalized_power_w','avg_hr_bpm','source','restricted','available_fields','withheld_reason','source_status','hr_zone_upper_bpm','hr_zone_time_s','heart_rate_quality'}
WELLNESS_FIELDS = {'date','sleep_hours','bedtime','hrv_ms','hrv_type','device','resting_hr_bpm','deep_sleep_pct','light_sleep_pct','fatigue'}
BASELINE_FIELDS = WELLNESS_FIELDS - {'date','fatigue'} | {'sample_count','period_start','period_end','source'}

def valid_date(value):
    if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:
        raise ValueError('date must be YYYY-MM-DD')
    return value

def number(value, low, high, name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} outside supported range [{low}, {high}]')

def validate(data, kind):
    if not isinstance(data,dict): raise ValueError('record must be an object')
    allowed = {'activity':ACTIVITY_FIELDS,'wellness':WELLNESS_FIELDS,'baseline':BASELINE_FIELDS}[kind]
    if set(data)-allowed: raise ValueError('unsupported fields: '+', '.join(sorted(set(data)-allowed)))
    d = dict(data)
    if kind != 'baseline': valid_date(d.get('date'))
    if kind=='activity':
        if not isinstance(d.get('id'),str) or not 1<=len(d['id'])<=200: raise ValueError('activity id required')
        if d.get('indoor') not in (True,False,None) or (d.get('indoor') is not None and not isinstance(d['indoor'],bool)): raise ValueError('indoor must be boolean or null')
        if d.get('restricted') not in (True,False,None) or (d.get('restricted') is not None and not isinstance(d['restricted'],bool)): raise ValueError('restricted must be boolean or null')
        quality=d.get('heart_rate_quality')
        if quality not in (None,'missing_zero','invalid') or (quality is not None and d.get('avg_hr_bpm') is not None): raise ValueError('invalid average heart-rate quality')
    if d.get('source_status') not in (None,'independent','restricted','unknown'): raise ValueError('invalid source_status')
    if d.get('available_fields') is not None and (not isinstance(d['available_fields'],list) or any(x not in ACTIVITY_FIELDS for x in d['available_fields'])): raise ValueError('invalid available_fields')
    if d.get('withheld_reason') is not None and (not isinstance(d['withheld_reason'],str) or len(d['withheld_reason'])>1000): raise ValueError('invalid withheld_reason')
    for field in ('hr_zone_upper_bpm','hr_zone_time_s'):
        values=d.get(field)
        if values is not None:
            if not isinstance(values,list) or not 1 <= len(values) <= 12: raise ValueError('invalid zone list')
            for value in values: number(value,1 if field=='hr_zone_upper_bpm' else 0,250 if field=='hr_zone_upper_bpm' else 86400,field)
            if field=='hr_zone_upper_bpm' and any(a>=b for a,b in zip(values,values[1:])): raise ValueError('HR zone boundaries must increase')
    if d.get('hr_zone_time_s') is not None and d.get('hr_zone_upper_bpm') is not None and len(d['hr_zone_time_s'])!=len(d['hr_zone_upper_bpm']): raise ValueError('zone lengths differ')
    ranges = {'duration_s':(1,86400),'avg_power_w':(0,2500),'normalized_power_w':(0,2500),'avg_hr_bpm':(25,250),'sleep_hours':(0,24),'hrv_ms':(0,1000),'resting_hr_bpm':(25,250),'deep_sleep_pct':(0,100),'light_sleep_pct':(0,100),'fatigue':(1,10),'sample_count':(1,7)}
    for key,(lo,hi) in ranges.items():
        if d.get(key) is not None: number(d[key],lo,hi,key)
    if d.get('bedtime') is not None:
        value=d['bedtime']
        if not isinstance(value,str) or datetime.strptime(value,'%H:%M').strftime('%H:%M')!=value: raise ValueError('bedtime must be HH:MM')
    if d.get('deep_sleep_pct') is not None and d.get('light_sleep_pct') is not None and d['deep_sleep_pct']+d['light_sleep_pct']>100: raise ValueError('sleep stage percentages exceed 100')
    for key in ('sport','source','hrv_type','device'):
        if d.get(key) is not None and (not isinstance(d[key],str) or len(d[key])>200): raise ValueError(f'invalid {key}')
    if kind=='baseline':
        valid_date(d.get('period_start')); valid_date(d.get('period_end'))
        span=(date.fromisoformat(d['period_end'])-date.fromisoformat(d['period_start'])).days+1
        if not 1<=span<=7: raise ValueError('baseline must cover 1 to 7 days')
        if d.get('sample_count') is None or int(d['sample_count'])!=d['sample_count'] or d['sample_count']>span: raise ValueError('valid baseline sample_count required')
    return enforce_provenance(d) if kind == 'activity' else d

class Store:
    def __init__(self,path):
        self.path=str(path)
        if self.path!=':memory:':
            p=Path(path); p.parent.mkdir(parents=True,exist_ok=True)
            if not p.exists(): p.touch(mode=0o600)
        self.db=sqlite3.connect(self.path)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,key));
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL CHECK(status='proposed'));
        CREATE TABLE IF NOT EXISTS wellness_sources(day TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(day,source));
        CREATE TABLE IF NOT EXISTS sync_attempts(id INTEGER PRIMARY KEY, oldest TEXT, newest TEXT, attempted_at TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS sync_diagnostics(attempt_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS icu_sync_day(day TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS icu_rejected_activity(id TEXT PRIMARY KEY, oldest TEXT NOT NULL, newest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS icu_indoor_ftp(account TEXT PRIMARY KEY, watts REAL, observed_at TEXT, observed_day TEXT, status TEXT NOT NULL);
        PRAGMA user_version=5;''')
    def remember_indoor_ftp(self, account, watts, observed_at, observed_day, status):
        with self.db:
            self.db.execute('''INSERT INTO icu_indoor_ftp VALUES(?,?,?,?,?)
                ON CONFLICT(account) DO UPDATE SET watts=excluded.watts, observed_at=excluded.observed_at,
                observed_day=excluded.observed_day, status=excluded.status''',
                (account,watts,observed_at,observed_day,status))
    def mark_indoor_ftp_refresh_failed(self, account):
        with self.db:
            self.db.execute('UPDATE icu_indoor_ftp SET status=? WHERE account=?',('refresh_failed',account))
    def indoor_ftp(self, account):
        row=self.db.execute('SELECT watts,observed_at,observed_day,status FROM icu_indoor_ftp WHERE account=?',(account,)).fetchone()
        return dict(zip(('watts','observed_at','observed_day','status'),row)) if row else None
    def log_sync(self, oldest, newest, status, diagnostics=None, *, _commit=True):
        from datetime import timezone
        with (self.db if _commit else nullcontext()):
            cursor=self.db.execute('INSERT INTO sync_attempts(oldest,newest,attempted_at,status) VALUES(?,?,?,?)',(oldest,newest,datetime.now(timezone.utc).isoformat(),status))
            if diagnostics:
                self.db.execute('INSERT INTO sync_diagnostics VALUES(?,?)',(cursor.lastrowid,json.dumps(diagnostics,allow_nan=False)))
    def sync_history(self):
        rows=self.db.execute('SELECT oldest,newest,attempted_at,status,payload FROM sync_attempts LEFT JOIN sync_diagnostics ON attempt_id=sync_attempts.id ORDER BY sync_attempts.id DESC LIMIT 20')
        return [dict(zip(('oldest','newest','attempted_at','status'),r[:4]),**({'diagnostics':json.loads(r[4])} if r[4] else {})) for r in rows]
    def save_icu_sync(self, oldest, newest, activities, wellness, status, diagnostics):
        # Publish accepted data and its completeness record in one transaction.
        # Network-level failure is handled before calling this method.
        with self.db:
            n=self.put_many('activity',activities,_commit=False)
            m=self.save_wellness(wellness,'icu',_commit=False)
            self.db.executemany('DELETE FROM icu_rejected_activity WHERE id=?',((row['id'],) for row in activities))
            self.db.executemany('''INSERT INTO icu_rejected_activity VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET oldest=excluded.oldest,newest=excluded.newest''',
                ((ident,oldest,newest) for ident in diagnostics['skipped_activity_ids']))
            start,end=date.fromisoformat(oldest),date.fromisoformat(newest)
            self.db.executemany('INSERT INTO icu_sync_day VALUES(?,?) ON CONFLICT(day) DO UPDATE SET status=excluded.status',
                (((start+timedelta(days=i)).isoformat(),status) for i in range((end-start).days+1)))
            self.log_sync(oldest,newest,status,diagnostics,_commit=False)
        return n,m
    def icu_sync_scope(self, oldest, newest):
        by_day=dict(self.db.execute('SELECT day,status FROM icu_sync_day WHERE day BETWEEN ? AND ?',(oldest,newest)))
        rejected={r[0] for r in self.db.execute('SELECT id FROM icu_rejected_activity WHERE oldest<=? AND newest>=?',(newest,oldest))}
        return by_day,rejected
    def put_many(self,kind,records,*,_commit=True):
        checked=[validate(r,kind) for r in records]
        with (self.db if _commit else nullcontext()):
            for r in checked:
                key=r['id'] if kind=='activity' else r['date'] if kind=='wellness' else r['period_end']
                self.db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload',(kind,key,json.dumps(r,allow_nan=False)))
        return len(checked)
    def records(self,kind):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM records WHERE kind=? ORDER BY key',(kind,))]
    def save_wellness(self, records, source, *, _commit=True):
        """Keep normalized source snapshots; manual non-null values win conflicts.

        HRV value/type/device are selected together, never mixed across sources.
        Manual updates are patches; explicit null clears that manual override.
        """
        if source not in ('manual','icu'): raise ValueError('unsupported source')
        checked=[validate(r,'wellness') for r in records]
        with (self.db if _commit else nullcontext()):
            for row in checked:
                day=row['date']
                snapshots={r[0]:json.loads(r[1]) for r in self.db.execute('SELECT source,payload FROM wellness_sources WHERE day=?',(day,))}
                # Preserve pre-v2 local records conservatively as legacy manual input.
                if not snapshots:
                    old=self.db.execute("SELECT payload FROM records WHERE kind='wellness' AND key=?",(day,)).fetchone()
                    if old:
                        snapshots['manual']=json.loads(old[0])
                        self.db.execute('INSERT INTO wellness_sources VALUES(?,?,?)',(day,'manual',old[0]))
                incoming=dict(snapshots.get(source,{})) if source=='manual' else {}
                incoming.update(row)
                snapshots[source]=incoming
                self.db.execute('INSERT INTO wellness_sources VALUES(?,?,?) ON CONFLICT(day,source) DO UPDATE SET payload=excluded.payload',(day,source,json.dumps(incoming,allow_nan=False)))
                manual=snapshots.get('manual',{})
                remote=snapshots.get('icu',{})
                merged={'date':day}
                for field in WELLNESS_FIELDS-{'date','hrv_ms','hrv_type','device'}:
                    merged[field]=manual.get(field) if manual.get(field) is not None else remote.get(field)
                hrv_source=manual if manual.get('hrv_ms') is not None else remote
                for field in ('hrv_ms','hrv_type','device'): merged[field]=hrv_source.get(field)
                validate(merged,'wellness')
                self.db.execute("INSERT INTO records VALUES('wellness',?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload",(day,json.dumps(merged,allow_nan=False)))
        return len(checked)
    def wellness_provenance(self):
        days={}
        for day,source,payload in self.db.execute('SELECT day,source,payload FROM wellness_sources ORDER BY day'):
            days.setdefault(day,{})[source]=json.loads(payload)
        results=[]
        for day,sources in days.items():
            manual,icu=sources.get('manual',{}),sources.get('icu',{})
            conflicts=[field for field in WELLNESS_FIELDS-{'date'} if manual.get(field) is not None and icu.get(field) is not None and manual[field]!=icu[field]]
            selected={field:('manual' if manual.get(field) is not None else 'icu') for field in WELLNESS_FIELDS-{'date'} if manual.get(field) is not None or icu.get(field) is not None}
            hrv_origin='manual' if manual.get('hrv_ms') is not None else 'icu'
            if sources.get(hrv_origin,{}).get('hrv_ms') is not None:
                for field in ('hrv_ms','hrv_type','device'): selected[field]=hrv_origin
            results.append({'date':day,'sources':sources,'selected_sources':selected,'conflict_fields':sorted(conflicts),'policy':'manual_non_null_priority; HRV value/type/device selected as one group'})
        return results
    def proposal(self,plan):
        import hashlib
        payload=json.dumps(plan,sort_keys=True,ensure_ascii=False,allow_nan=False)
        ident=hashlib.sha256(payload.encode()).hexdigest()
        with self.db: self.db.execute('INSERT OR IGNORE INTO proposals VALUES(?,?,?)',(ident,payload,'proposed'))
        row=self.db.execute('SELECT payload,status FROM proposals WHERE id=?',(ident,)).fetchone()
        if row[0]!=payload: raise RuntimeError('proposal readback failed')
        return {'id':ident,'status':row[1],'plan':json.loads(row[0]),'external_write_enabled':False}
    def proposals(self):
        return [{'id':r[0],'plan':json.loads(r[1]),'status':r[2]} for r in self.db.execute('SELECT id,payload,status FROM proposals ORDER BY rowid')]
    def close(self): self.db.close()

    def get_proposal(self, proposal_id):
        if not isinstance(proposal_id,str) or not proposal_id or len(proposal_id)>200: raise ValueError('invalid proposal_id')
        row=self.db.execute('SELECT id,payload,status FROM proposals WHERE id=?',(proposal_id,)).fetchone()
        return {'id':row[0],'plan':json.loads(row[1]),'status':row[2]} if row else None
    def proposal_page(self, limit=20, after=0):
        # Keyset cursor bounds rows decoded and avoids OFFSET work on large tables.
        if type(limit) is not int or not 1<=limit<=100: raise ValueError('limit must be 1..100')
        if type(after) is not int or after<0 or after>9223372036854775807: raise ValueError('invalid after cursor')
        rows=self.db.execute('SELECT rowid,id,payload,status FROM proposals WHERE rowid>? ORDER BY rowid LIMIT ?',(after,limit+1)).fetchall()
        page=rows[:limit]
        return {'items':[{'id':r[1],'plan':json.loads(r[2]),'status':r[3]} for r in page],
                'next_cursor':page[-1][0] if len(rows)>limit else None,'has_more':len(rows)>limit,
                'scanned':len(rows),'limit':limit}
