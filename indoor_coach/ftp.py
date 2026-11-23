"""Resolve an indoor FTP setting as evidence, not as a fresh fitness estimate."""
from datetime import datetime, timedelta, timezone
import hashlib
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .icu import ICUError
from .store import number

FTP_CACHE_AGE = timedelta(hours=24)


def utc_now():
    return datetime.now(timezone.utc)


def concrete_account(value):
    if isinstance(value, bool):
        return None
    match = re.fullmatch(r"i?([1-9]\d*)", str(value))
    return match.group(1) if match else None


def credential_scope(key):
    # Only an ephemeral identity hint for the ambiguous API alias 0; never persisted/returned.
    return hashlib.sha256(key.encode()).digest() if key else None


def error_details(exc, stage):
    if isinstance(exc, ICUError):
        return exc.as_dict()
    return {'stage': stage, 'reason': 'operation_failed'}


class IndoorFTPResolver:
    def __init__(self, store):
        self.store = store
        self._alias = None
        self._last_error = None
        self._last_scope = None

    def refresh(self, factory, requested, key):
        account = concrete_account(requested)
        self._alias = None
        self._last_error = None
        self._last_scope = (requested, credential_scope(key))
        try:
            client = factory()
            identity = client.identity()
            verified = concrete_account(identity.get('id')) if isinstance(identity, dict) else None
            if not verified or account and verified != account:
                self._last_error = {'stage': 'settings', 'reason': 'account_not_verified'}
                if account:
                    self.store.mark_indoor_ftp_refresh_failed(account)
                return False
            account = verified
            if requested == '0':
                self._alias = (credential_scope(key), account)
            settings = client.sport_settings()
            if not isinstance(settings, dict):
                raise ValueError('unexpected settings shape')
            settings_owner = settings.get('athlete_id')
            if settings_owner is not None and concrete_account(settings_owner) != account:
                self._last_error = {'stage': 'settings', 'reason': 'account_not_verified'}
                self.store.mark_indoor_ftp_refresh_failed(account)
                return False
            watts = settings.get('indoor_ftp')
            state = 'available'
            if settings.get('indoor_ftp_status') == 'invalid':
                watts, state = None, 'indoor_ftp_invalid'
            elif watts is None or (type(watts) in (int, float) and watts == 0):
                # Unset is not automatically equivalent to the ordinary cycling FTP.
                watts, state = None, 'indoor_ftp_unset'
            else:
                try:
                    number(watts, 30, 700, 'indoor_ftp')
                except ValueError:
                    watts, state = None, 'indoor_ftp_invalid'
            now = utc_now()
            try:
                tz = ZoneInfo(identity.get('timezone') or 'UTC')
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                tz = timezone.utc
            self.store.remember_indoor_ftp(account, watts, now.isoformat(), now.astimezone(tz).date().isoformat(), state)
            return True
        except Exception as exc:
            self._last_error = error_details(exc, 'settings')
            if account:
                self.store.mark_indoor_ftp_refresh_failed(account)
            return False

    def basis(self, *, manual, as_of, requested, key, refreshed=False):
        if manual is not None:
            return {'watts': manual, 'source': 'explicit_input', 'status': 'available', 'account': None}
        account = concrete_account(requested)
        if requested == '0' and self._alias and self._alias[0] == credential_scope(key):
            account = self._alias[1]
        snapshot = self.store.indoor_ftp(account) if account else None
        error = self._last_error if not refreshed and self._last_scope == (requested, credential_scope(key)) else None
        if not snapshot:
            return {'watts': None, 'source': None, 'status': 'refresh_failed' if error else
                    ('account_unverified' if not account else 'not_cached'), 'account': account,
                    **({'refresh_error': error} if error else {})}
        fetched = datetime.fromisoformat(snapshot['observed_at'])
        now = utc_now()
        expires = fetched + FTP_CACHE_AGE
        status = snapshot['status']
        if as_of < snapshot['observed_day']:
            status = 'not_historical'
        elif status == 'available' and not fetched <= now < expires:
            status = 'stale'
        return {
            'watts': snapshot['watts'] if status == 'available' else None,
            'source': 'icu_indoor_ftp' if refreshed else 'icu_indoor_ftp_cache',
            'status': status, 'account': account, 'observed_at': snapshot['observed_at'],
            'observed_local_day': snapshot['observed_day'], 'valid_until': expires.isoformat(),
            **({'refresh_error': error} if error else {}),
        }
