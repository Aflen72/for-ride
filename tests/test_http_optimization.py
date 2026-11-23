"""Offline HTTP, identity and current-settings regression tests; no live accounts."""
import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock

from indoor_coach.icu import ICUClient, ICUError, USER_AGENT, _NoRedirect


class HTTPOptimizationTests(unittest.TestCase):
    def client(self, body=None, athlete='0'):
        c = ICUClient('fixture-secret', athlete)
        c._opener = MagicMock()
        c._opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        return c

    def failure(self, status, body):
        c = self.client()
        c._opener.open.side_effect = urllib.error.HTTPError('https://secret.invalid/key', status,
            'fixture-secret', {}, io.BytesIO(body))
        try:
            c.activities('2040-01-01', '2040-01-02')
        except ICUError as e:
            return c, e
        self.fail('expected failure')

    def test_truthful_stable_ua_on_read_and_shared_post(self):
        c = self.client([])
        c.activities('2040-01-01', '2040-01-02')
        req = c._opener.open.call_args.args[0]
        self.assertEqual(req.get_header('User-agent'), USER_AGENT)
        self.assertNotIn('Mozilla', USER_AGENT)
        c._opener.open.return_value = io.BytesIO(b'[]')
        c._request_json('POST', '/events/bulk', [], stage='create', endpoint='events_bulk', athlete_id='0')
        req = c._opener.open.call_args.args[0]
        self.assertEqual(req.get_header('User-agent'), USER_AGENT)
        self.assertEqual(req.method, 'POST')

    def test_forbidden_not_assumed_1010(self):
        c, e = self.failure(403, b'Forbidden fixture-secret')
        self.assertEqual(e.as_dict(), {'http_status':403, 'stage':'activities', 'endpoint':'activities',
            'reason':'forbidden', 'edge_code':None})
        self.assertNotIn('fixture-secret', str(e))
        self.assertEqual(c._opener.open.call_count, 1)

    def test_explicit_1010_only(self):
        for body in [b'error code: 1010', b'<h1>Error 1010</h1>', b'{"code":1010}']:
            _, e = self.failure(403, body)
            self.assertEqual(e.edge_code, 1010)
            self.assertEqual(e.reason, 'edge_1010')
        for body in [b'ID 1010', b'x' * 4096 + b' Error 1010', b'error code: 10101']:
            _, e = self.failure(403, body)
            self.assertIsNone(e.edge_code)
        _, e = self.failure(500, b'error code: 1010')
        self.assertIsNone(e.edge_code)

    def test_redirect_closed_no_retry(self):
        c, e = self.failure(302, b'Go somewhere else')
        self.assertEqual(e.status, 302)
        self.assertEqual(c._opener.open.call_count, 1)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, None, {}, 'https://evil.example'))

    def test_safe_transport_and_protocol_errors(self):
        c = self.client()
        c._opener.open.side_effect = TimeoutError('fixture-secret')
        with self.assertRaises(ICUError) as caught:
            c.identity()
        self.assertEqual(caught.exception.reason, 'transport_error')
        self.assertNotIn('fixture-secret', repr(caught.exception.as_dict()))
        c = self.client()
        c._opener.open.return_value = io.BytesIO(b'fixture-secret')
        with self.assertRaises(ICUError) as caught:
            c.identity()
        self.assertEqual(caught.exception.reason, 'invalid_json')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_identity_projects_secrets(self):
        c = self.client({'id':'i123', 'timezone':'Asia/Shanghai', 'api_key':'fixture-secret',
            'oauth': {'refresh_token':'other-secret'}, 'sportSettings':[]})
        self.assertEqual(c.identity(), {'id':'i123', 'timezone':'Asia/Shanghai'})
        self.assertEqual(c._opener.open.call_args.args[0].full_url, 'https://intervals.icu/api/v1/athlete/0')
        c = self.client({'id':'i124'}, athlete='i123')
        with self.assertRaises(ICUError) as caught:
            c.identity()
        self.assertEqual(caught.exception.reason, 'identity_mismatch')

    def test_current_ride_settings_projection(self):
        c = self.client({'id':12, 'athlete_id':'i123', 'types':['Ride','VirtualRide'],
            'ftp':200, 'indoor_ftp':210, 'lthr':170, 'max_hr':190,
            'power_zones':[55,75,90,105,120,150], 'hr_zones':[81,89,93,99,102],
            'private_token':'fixture-secret'})
        r = c.sport_settings()
        self.assertEqual(r['indoor_ftp'], 210)
        self.assertEqual(r['hr_zones'], [81,89,93,99,102])
        self.assertNotIn('private_token', r)
        self.assertTrue(c._opener.open.call_args.args[0].full_url.endswith('/sport-settings/Ride'))

    def test_settings_fail_closed_and_missing_not_guessed(self):
        for body in [[], {'athlete_id':'i123','types':['Run']}, {'athlete_id':'other','types':['Ride']}]:
            c = self.client(body)
            with self.assertRaises(ICUError):
                c.sport_settings()
        c = self.client({'athlete_id':'i123', 'types':['Ride'], 'ftp':200, 'hr_zones':[90,80]})
        r = c.sport_settings()
        self.assertIsNone(r['indoor_ftp'])
        self.assertIsNone(r['hr_zones'])

    def test_numeric_prefix_account_aliases_and_zero(self):
        c = self.client({'id':'123', 'timezone':'UTC'}, athlete='i123')
        self.assertEqual(c.identity()['id'], '123')
        c = self.client({'athlete_id':'i123', 'types':['Ride']}, athlete='123')
        self.assertEqual(c.sport_settings()['athlete_id'], 'i123')
        for ident in ('0', 'i0', '000'):
            c = self.client({'id':ident})
            with self.assertRaises(ICUError):
                c.identity()
        for value, status in [(None,'unset'), (0,'invalid'), (-1,'invalid'), ('200','invalid'), (200,'valid')]:
            c = self.client({'athlete_id':'i123', 'types':['Ride'], 'indoor_ftp':value})
            self.assertEqual(c.sport_settings()['indoor_ftp_status'], status)

    def test_diagnostic_metadata_whitelist(self):
        e = ICUError('safe', stage='fixture-secret', endpoint='https://private', reason='private reason')
        self.assertNotIn('fixture-secret', repr(e.as_dict()))
        self.assertEqual(e.stage, 'request')

if __name__ == '__main__':
    unittest.main()
