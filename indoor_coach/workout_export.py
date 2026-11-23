"""Credential-free, minimal immutable workout previews. No network or approval API."""
import copy
import datetime as dt
import hashlib
import json
import re
from zoneinfo import ZoneInfo
from .ftp import concrete_account


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def external_id(account, proposal, index):
    return 'for-ride-v1-' + digest([account, proposal, index])


def event_for(bundle, index, course):
    return {'category': 'WORKOUT', 'type': 'Ride', 'name': 'for-ride indoor workout',
            'start_date_local': course['date'] + 'T00:00:00',
            'external_id': external_id(bundle['account'], bundle['proposal_id'], index),
            'description': '\n'.join(f"- {s['seconds']}s {s['watts']}w" for s in course['steps'])}


def preview(proposal, account, timezone, ftp, selection='next', now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if selection not in ('next', 'week'): raise ValueError('invalid selection')
    plan = proposal['plan']
    expires_at = now + dt.timedelta(hours=24)
    basis = plan.get('ftp_basis')
    if basis is not None:
        saved_ftp = basis['watts']
        if saved_ftp is None or basis['status'] != 'available':
            raise ValueError('no trusted FTP is bound to this draft; regenerate')
        if ftp is not None and (type(ftp) not in (int, float) or ftp != saved_ftp):
            raise ValueError('FTP differs from proposal; regenerate')
        ftp = saved_ftp
        if basis.get('account') is not None:
            if concrete_account(account) != basis['account']:
                raise ValueError('account differs from FTP basis; regenerate')
            expires_at = min(expires_at, dt.datetime.fromisoformat(basis['valid_until']))
            if expires_at <= now:
                raise ValueError('FTP basis expired; refresh settings and regenerate')
    elif type(ftp) not in (int, float) or not 30 <= ftp <= 700:
        raise ValueError('legacy proposal requires explicit FTP')
    # Store's original proposal identity uses its legacy JSON serialization.
    identity = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if identity != proposal['id']: raise ValueError('proposal changed')
    courses = []
    for index, workout in enumerate(plan['week_plan']):
        if selection == 'next' and index: break
        if workout['kind'] == 'rest': continue
        steps = []
        for s in workout['steps']:
            lo, hi = s['ftp_fraction']
            if s['power_w'] != [round(ftp * lo), round(ftp * hi)]:
                raise ValueError('FTP differs from proposal; regenerate')
            steps.append({'seconds': int(s['duration_minutes'] * 60),
                          'watts': round(ftp * (lo + hi) / 2)})
        courses.append({'index': index, 'date': workout['date'], 'steps': steps,
                        'total_seconds': sum(s['seconds'] for s in steps)})
    body = {'version': 1, 'proposal_id': identity, 'account': account, 'timezone': timezone,
            'ftp_w': ftp, 'power_basis': 'rounded midpoint of draft FTP ranges; fixed watts',
            'action': 'create', 'conflict_policy': 'stop_on_any_calendar_item',
            'created_at': now.isoformat(), 'expires_at': expires_at.isoformat(),
            'courses': courses}
    for course in courses: course['event'] = event_for(body, course['index'], course)
    result = {'payload': body, 'sha256': digest(body)}
    validate_export(result, now)
    return result


def validate_export(export, now=None, allow_expired=False):
    """Hash detects changes, not identity or permission; human review is mandatory."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if set(export) != {'payload', 'sha256'}: raise ValueError('invalid export')
    b = copy.deepcopy(export['payload'])
    if digest(b) != export['sha256']: raise ValueError('export hash mismatch')
    if set(b) != {'version','proposal_id','account','timezone','ftp_w','power_basis','action','conflict_policy','created_at','expires_at','courses'}:
        raise ValueError('invalid payload')
    if b['version'] != 1 or b['action'] != 'create' or b['conflict_policy'] != 'stop_on_any_calendar_item': raise ValueError('unsupported action')
    if b['power_basis'] != 'rounded midpoint of draft FTP ranges; fixed watts': raise ValueError('invalid basis')
    if not isinstance(b['account'], str) or not re.fullmatch(r'i?[1-9]\d*', b['account']): raise ValueError('concrete account required')
    if not re.fullmatch('[a-f0-9]{64}', b['proposal_id']): raise ValueError('invalid proposal')
    ZoneInfo(b['timezone'])
    if isinstance(b['ftp_w'], bool) or not isinstance(b['ftp_w'], (int, float)) or not 30 <= b['ftp_w'] <= 700: raise ValueError('invalid FTP')
    created, expires = (dt.datetime.fromisoformat(b[k]) for k in ('created_at','expires_at'))
    if created.tzinfo is None or expires.tzinfo is None or not dt.timedelta(0) < expires-created <= dt.timedelta(hours=24) or created > now: raise ValueError('invalid expiry')
    if not allow_expired and now >= expires: raise ValueError('expired export')
    if not isinstance(b['courses'], list) or not 1 <= len(b['courses']) <= 7: raise ValueError('no executable courses')
    indices, dates = set(), set()
    for c in b['courses']:
        if set(c) != {'index','date','steps','total_seconds','event'}: raise ValueError('invalid course')
        if type(c['index']) is not int or not 0 <= c['index'] < 7 or c['index'] in indices: raise ValueError('invalid index')
        indices.add(c['index'])
        day = dt.date.fromisoformat(c['date'])
        if day.isoformat() != c['date'] or c['date'] in dates: raise ValueError('invalid date')
        dates.add(c['date'])
        if not allow_expired and day < now.astimezone(ZoneInfo(b['timezone'])).date(): raise ValueError('past workout')
        if not isinstance(c['steps'], list) or not 1 <= len(c['steps']) <= 100: raise ValueError('invalid steps')
        for s in c['steps']:
            if set(s) != {'seconds','watts'} or any(type(v) is not int for v in s.values()) or not 1 <= s['seconds'] <= 7200 or not 1 <= s['watts'] <= 1500: raise ValueError('invalid step')
        if c['total_seconds'] != sum(s['seconds'] for s in c['steps']) or c['total_seconds'] > 21600: raise ValueError('invalid duration')
        if c['event'] != event_for(b, c['index'], c): raise ValueError('event differs from course')
    return b
