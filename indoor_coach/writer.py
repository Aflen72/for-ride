"""User-only writer. Never import into Service/MCP. Not a same-OS security boundary."""
import argparse
import datetime as dt
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

from .icu import ICUClient, ICUError
from .workout_export import canonical, digest, validate_export


class RequestFailed(ICUError):
    def __init__(self, status=None):
        self.status = status
        super().__init__('ICU request failed' + (f' (HTTP {status})' if status else '; outcome may be unknown'))


class WriterClient(ICUClient):
    """Fixed HTTPS host, no redirects/retries, credentials provided only by user CLI."""
    def request(self, method, suffix='', payload=None):
        req = urllib.request.Request(self.BASE_URL + '/athlete/0' + suffix,
            data=canonical(payload).encode() if payload is not None else None,
            headers={'Authorization': self._authorization, 'Accept': 'application/json', 'Content-Type': 'application/json'}, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES: raise ValueError()
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise RequestFailed(status) from None
        except Exception:
            raise RequestFailed() from None

    def account(self):
        data = self.request('GET')
        if not isinstance(data, dict): raise RequestFailed()
        # Do not retain or print athlete profile (it can include secrets).
        return {'id': data.get('id'), 'timezone': data.get('timezone')}

    def events(self, day):
        rows = self.request('GET', '/events?' + urllib.parse.urlencode({'oldest': day, 'newest': day}))
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): raise RequestFailed()
        return rows

    def event(self, ident):
        if type(ident) is not int or ident <= 0: raise RequestFailed()
        result = self.request('GET', f'/events/{ident}')
        if not isinstance(result, dict): raise RequestFailed()
        return result

    def create(self, event):
        # Official OpenAPI: false creates, never updates matching external_id/uid.
        return self.request('POST', '/events/bulk?upsert=false&upsertOnUid=false&updatePlanApplied=false', [event])


class Journal:
    """One durable journal per isolated writer account/home; fail closed on lock contention."""
    def __init__(self, path): self.path = Path(path)
    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = open(str(self.path) + '.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.path)
            os.chmod(self.path, 0o600)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS executions (external_id TEXT PRIMARY KEY, binding TEXT NOT NULL, status TEXT NOT NULL, event_id INTEGER)')
            self.db.execute('CREATE TABLE IF NOT EXISTS transitions (at TEXT, external_id TEXT, status TEXT)')
            self.db.commit()
        except Exception:
            self.lock.close()
            raise ValueError('writer locked or journal unavailable') from None
        return self
    def __exit__(self, *args):
        self.db.close()
        self.lock.close()
    def row(self, key):
        return self.db.execute('SELECT binding,status,event_id FROM executions WHERE external_id=?', (key,)).fetchone()
    def set(self, key, binding, status, event_id=None):
        old = self.row(key)
        if old and old[0] != binding: raise ValueError('immutable course binding changed')
        with self.db:
            self.db.execute('INSERT INTO executions VALUES(?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET status=excluded.status,event_id=coalesce(excluded.event_id,executions.event_id)', (key,binding,status,event_id))
            self.db.execute('INSERT INTO transitions VALUES(?,?,?)', (dt.datetime.now(dt.timezone.utc).isoformat(),key,status))


def check_account(client, b):
    a = client.account()
    if a != {'id': b['account'], 'timezone': b['timezone']}: raise ValueError('account or timezone changed/mismatched')


def verify(remote, b, c):
    """Verify both native text and the independently returned parsed workout.

    Only flat, fixed-watt timed steps are supported. Unknown shapes fail closed.
    FTP is the reviewed derivation basis; remote targets must remain absolute w.
    """
    doc = remote.get('workout_doc')
    if not isinstance(doc, dict) or doc.get('duration') != c['total_seconds']:
        return False
    steps = doc.get('steps')
    if not isinstance(steps, list) or len(steps) != len(c['steps']): return False
    for actual, expected_step in zip(steps, c['steps']):
        if not isinstance(actual, dict) or set(actual)-{'duration','power','text','warmup','cooldown','intensity'}: return False
        if actual.get('duration') != expected_step['seconds']: return False
        if actual.get('power') != {'units':'w','value':expected_step['watts']}: return False
    expected = c['event']
    return (type(remote.get('id')) is int and remote['id'] > 0
            and remote.get('athlete_id') == b['account']
            and all(remote.get(k) == v for k,v in expected.items())
            and remote.get('moving_time') == c['total_seconds']
            and remote.get('joules') == sum(s['seconds'] * s['watts'] for s in c['steps']))


def binding(b, c):
    return digest({'account':b['account'],'timezone':b['timezone'],'ftp_w':b['ftp_w'],
                   'power_basis':b['power_basis'],'action':b['action'],'course':c})


def reconcile(client, journal, b, c):
    key, bound = c['event']['external_id'], binding(b,c)
    row = journal.row(key)
    try:
        matches = [r for r in client.events(c['date']) if r.get('external_id') == key]
        if len(matches) != 1:
            journal.set(key,bound,'unknown' if not matches else 'verification_failed')
            return
        ident = matches[0].get('id')
        if row and row[2] is not None and ident != row[2]:
            journal.set(key,bound,'verification_failed'); return
        remote = client.event(ident)  # independent GET; never trust POST or list payload
        check_account(client,b)
        journal.set(key,bound,'verified' if verify(remote,b,c) else 'verification_failed', ident)
    except Exception:
        journal.set(key,bound,'unknown')


class Terminal:
    def confirm(self, export):
        if not sys.stdin.isatty() or not sys.stdout.isatty(): raise ValueError('independent user terminal required')
        # JSON escapes all control characters; no untrusted text executes in terminal.
        print(json.dumps(export, indent=2, ensure_ascii=True))
        print('CREATE only; stop on any calendar item. Fixed watts; total_seconds and FTP basis above.')
        print('Only proceed in a user-controlled environment inaccessible to the agent.')
        challenge = 'CREATE ' + export['sha256'] + ' ' + secrets.token_hex(8)
        started = dt.datetime.now(dt.timezone.utc)
        answer = input('Type exactly ' + challenge + '\n> ')
        if answer != challenge or dt.datetime.now(dt.timezone.utc)-started > dt.timedelta(minutes=5):
            raise ValueError('confirmation absent, mismatched or expired')


def run(export, client, journal, terminal, reconcile_only=False):
    # Immutable copy prevents caller mutation between review and execution.
    export = json.loads(canonical(export))
    b = validate_export(export, allow_expired=reconcile_only)
    check_account(client,b)
    pending = []
    for c in b['courses']:
        key, bound = c['event']['external_id'], binding(b,c)
        row = journal.row(key)
        if row and row[0] != bound: raise ValueError('immutable proposal changed')
        if row:
            # verified records a past observation, never a cache of current remote state.
            # Re-read even on ordinary repeat runs; reconciliation never creates.
            reconcile(client,journal,b,c)
            # Any previously attempted course is NEVER automatically posted again.
            continue
        if reconcile_only: continue
        if client.events(c['date']): raise ValueError('calendar conflict; nothing submitted')
        pending.append(c)
    if any(journal.row(c['event']['external_id']) and journal.row(c['event']['external_id'])[1] != 'verified' for c in b['courses']):
        pending = []
    if pending:
        terminal.confirm(export)  # no bool, approval database flag, or agent tool
        validate_export(export)
        for c in pending:
            validate_export(export)
            check_account(client,b)
            if client.events(c['date']): raise ValueError('calendar changed; remaining courses stopped')
            key, bound = c['event']['external_id'], binding(b,c)
            journal.set(key,bound,'submitting')  # commit BEFORE network side effect
            try:
                response = client.create(c['event'])
                if (not isinstance(response,list) or len(response)!=1 or not isinstance(response[0],dict)
                        or type(response[0].get('id')) is not int or response[0]['id'] <= 0):
                    journal.set(key,bound,'unknown')
                    break
                ident = response[0]['id']
                journal.set(key,bound,'submitted',ident)
                remote = client.event(ident)
                check_account(client,b)
                journal.set(key,bound,'verified' if verify(remote,b,c) else 'verification_failed',ident)
            except RequestFailed as exc:
                journal.set(key,bound,'rejected' if exc.status in (401,403,429) else 'unknown')
                if exc.status not in (401,403,429): reconcile(client,journal,b,c)
            except Exception:
                journal.set(key,bound,'unknown')
            if journal.row(key)[1] != 'verified': break
    return [{'external_id':c['event']['external_id'], 'status':(journal.row(c['event']['external_id']) or (None,'not_submitted',None))[1]} for c in b['courses']]


def main():
    parser = argparse.ArgumentParser(description='User-only ICU writer; NEVER run from an agent shell')
    parser.add_argument('preview', type=Path)
    parser.add_argument('--reconcile-only', action='store_true', help='GET only, including expired previews')
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty(): raise ValueError('TTY required')
        export = json.loads(args.preview.read_text())
        validate_export(export, allow_expired=args.reconcile_only)
        # No environment fallback: read credentials only in the independent user process.
        key = getpass.getpass('Intervals API key (user-only process): ')
        client = WriterClient(key)
        del key
        with Journal(Path.home()/'.indoor-coach-writer'/'executions.sqlite3') as journal:
            result = run(export,client,journal,Terminal(),args.reconcile_only)
        print(json.dumps(result))
        return 0 if result and all(r['status']=='verified' for r in result) else 2
    except (Exception, KeyboardInterrupt):
        print('Writer stopped. No automatic retry. Check preview/account/calendar and durable journal; use --reconcile-only for uncertain outcomes.', file=sys.stderr)
        return 2


if __name__ == '__main__': sys.exit(main())
