import base64
import datetime
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server as s
from admin import Admin
from limits import DEFAULTS
from test_server import config, Connection, Response


class AdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(config(), public=True, quota_db=str(Path(self.tmp.name) / 'usage.sqlite3'), proxy_secret='q' * 48, admin_password='a' * 32)
        self.app = s.App(self.cfg)
        self.server = s.Server(('127.0.0.1', 0), self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.limits.db.close()
        self.tmp.cleanup()

    def request(self, path, body=None, cookie=None, csrf=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        fields = dict(headers or {})
        if body is not None:
            fields['Content-Type'] = 'application/json'
        if cookie:
            fields['Cookie'] = cookie
        if csrf:
            fields['X-CSRF-Token'] = csrf
        conn.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, fields)
        r = conn.getresponse()
        result = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return result

    def login(self, password=None):
        status, headers, body = self.request('/admin/api/login', {'username': 'bridge', 'password': password or self.cfg['admin_password']})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0], json.loads(body)['csrf']

    def test_login_cookie_and_no_secrets_in_status(self):
        self.assertEqual(self.request('/admin/api/status')[0], 401)
        basic = 'Basic ' + base64.b64encode(('bridge:' + self.cfg['admin_password']).encode()).decode()
        self.assertEqual(self.request('/admin/api/status', headers={'Authorization': basic})[0], 401)
        status, headers, _ = self.request('/admin/api/login', {'username': 'bridge', 'password': self.cfg['admin_password']})
        self.assertEqual(status, 200)
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/admin'):
            self.assertIn(flag, headers['Set-Cookie'])
        status, headers, body = self.request('/admin/api/status', cookie=headers['Set-Cookie'].split(';')[0])
        self.assertEqual(status, 200)
        self.assertIn('no-store', headers['Cache-Control'])
        for name in ('password', 'admin_password', 'secret', 'proxy_secret'):
            self.assertNotIn(self.cfg[name].encode(), body)

    def test_private_download_password_does_not_grant_owner_access(self):
        self.assertEqual(self.request('/admin/api/login', {'username': 'bridge', 'password': self.cfg['password']})[0], 401)
        self.login()

    def test_custom_owner_username_survives_password_change(self):
        self.app.admin.credentials['username'] = 'owner-test'
        self.assertIsNone(self.app.admin.login('127.0.0.1', 'bridge', self.cfg['admin_password']))
        self.assertIsNotNone(self.app.admin.login('127.0.0.1', 'owner-test', self.cfg['admin_password']))
        self.app.admin.change_password(self.cfg['admin_password'], 'new-custom-owner-password')
        restarted = s.App(self.cfg)
        try:
            self.assertEqual(restarted.admin.credentials['username'], 'owner-test')
            self.assertIsNotNone(restarted.admin.login('127.0.0.1', 'owner-test', 'new-custom-owner-password'))
        finally:
            restarted.limits.db.close()

    def test_public_admin_shell_and_strict_csp(self):
        status, headers, body = self.request('/admin')
        self.assertEqual(status, 200)
        self.assertIn('script-src \'self\'', headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        self.assertEqual(self.request('/admin/assets/admin.js')[0], 200)
        self.assertEqual(self.request('/admin/assets/admin.css')[0], 200)
        self.assertNotIn(self.cfg['admin_password'].encode(), body)

    def test_csrf_and_cross_origin_rejected(self):
        cookie, csrf = self.login()
        body = dict(settings=self.app.admin.settings(), revision=0)
        self.assertEqual(self.request('/admin/api/settings', body, cookie)[0], 403)
        self.assertEqual(self.request('/admin/api/settings', body, cookie, csrf, {'Origin': 'https://evil.test'})[0], 400)
        self.assertEqual(self.request('/admin/api/logout', {}, cookie, csrf, {'Sec-Fetch-Site': 'cross-site'})[0], 400)
        self.assertEqual(self.app.admin.revision, 0)
        self.assertEqual(self.request('/admin/api/status', cookie=cookie)[0], 200)

    def test_settings_persist_without_resetting_usage(self):
        cookie, csrf = self.login()
        limits = self.app.limits
        limits.reserve(limits.day(), '1.1.1.1', 123)
        settings = self.app.admin.settings()
        settings['limits']['bytes_per_second'] = 1024**2
        settings['allowed_domains'] = ['github.com', 'nvidia.com']
        status, _, _ = self.request('/admin/api/settings', dict(settings=settings, revision=0), cookie, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(limits.settings['bytes_per_second'], 1024**2)
        restarted = s.App(self.cfg)
        try:
            self.assertEqual(restarted.limits.settings['bytes_per_second'], 1024**2)
            self.assertEqual(restarted.config['allowed_domains'], ['github.com', 'nvidia.com'])
            self.assertEqual(restarted.admin.revision, 1)
            self.assertEqual(restarted.limits.snapshot('1.1.1.1')['ip_used'], 123)
            self.assertEqual(len(restarted.admin.status()['audit']), 1)
        finally:
            restarted.limits.db.close()

    def test_stale_revision_conflicts(self):
        cookie, csrf = self.login()
        body = dict(settings=self.app.admin.settings(), revision=0)
        self.assertEqual(self.request('/admin/api/settings', body, cookie, csrf)[0], 200)
        self.assertEqual(self.request('/admin/api/settings', body, cookie, csrf)[0], 409)
        self.assertEqual(self.app.admin.revision, 1)

    def test_history_is_last_seven_calendar_days(self):
        day = datetime.date.fromisoformat(self.app.limits.day())
        rows = [((day - datetime.timedelta(days=n)).isoformat(), '*', 10) for n in (0, 6, 7, 30)]
        with self.app.limits.db:
            self.app.limits.db.executemany('INSERT INTO usage VALUES(?,?,?)', rows)
        days = [item['day'] for item in self.app.admin.status()['history']]
        self.assertEqual(days, [rows[0][0], rows[1][0]])

    def test_invalid_setting_cannot_update(self):
        cookie, csrf = self.login()
        settings = self.app.admin.settings()
        settings['limits']['bytes_per_second'] = 0
        self.assertEqual(self.request('/admin/api/settings', dict(settings=settings, revision=0), cookie, csrf)[0], 400)
        settings = self.app.admin.settings()
        settings['secret'] = 'attempted overwrite'
        self.assertEqual(self.request('/admin/api/settings', dict(settings=settings, revision=0), cookie, csrf)[0], 400)
        self.assertEqual(self.app.admin.revision, 0)

    def test_pause_preserves_admin_and_public_page(self):
        cookie, csrf = self.login()
        path = self.app.sign('https://github.com/a.zip')
        settings = self.app.admin.settings()
        settings['paused'] = True
        self.assertEqual(self.request('/admin/api/settings', dict(settings=settings, revision=0), cookie, csrf)[0], 200)
        with patch.object(s, 'fetch') as fetch:
            self.assertEqual(self.request(path)[0], 503)
            fetch.assert_not_called()
        self.assertEqual(self.request('/api/link', {'url': 'https://github.com/a.zip'})[0], 503)
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/admin/api/status', cookie=cookie)[0], 200)

    def test_private_switch_protects_existing_links_and_can_return_public(self):
        cookie, csrf = self.login()
        path = self.app.sign('https://github.com/a.zip')
        settings = self.app.admin.settings()
        settings['public'] = False
        self.assertEqual(self.request('/admin/api/settings', dict(settings=settings, revision=0), cookie, csrf)[0], 200)
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request('/')[0], 401)
        self.assertEqual(self.request('/admin/api/status', cookie=cookie)[0], 200)
        settings['public'] = True
        self.assertEqual(self.request('/admin/api/settings', dict(settings=settings, revision=1), cookie, csrf)[0], 200)
        self.assertEqual(self.request('/')[0], 200)

    def test_live_mode_switch_does_not_leak_active_slots(self):
        def fetch(*args):
            self.app.public = False
            return Connection(), Response()
        with patch.object(s, 'fetch', side_effect=fetch):
            status, headers, body = self.request(self.app.sign('https://github.com/a.zip'))
        self.assertEqual((status, body), (200, b'hello'))
        self.assertIn('no-store', headers['Cache-Control'])
        # HTTP Content-Length can be received before the server's finally block runs.
        deadline = time.monotonic() + 2
        while self.app.active_transfers and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.app.limits.active, {})
        self.assertEqual(self.app.active_transfers, 0)
        self.assertEqual(self.app.limits.snapshot('127.0.0.1')['ip_used'], 5)

    def test_password_change_persists_and_revokes_sessions(self):
        cookie, csrf = self.login()
        other_cookie, _ = self.login()
        password = 'new-owner-password-1234'
        status, headers, body = self.request('/admin/api/password',
            dict(old_password=self.cfg['admin_password'], new_password=password), cookie, csrf)
        self.assertEqual(status, 200)
        self.assertNotIn(password.encode(), body)
        self.assertEqual(self.request('/admin/api/status', cookie=other_cookie)[0], 401)
        self.assertEqual(self.request('/admin/api/status', cookie=cookie)[0], 401)
        self.assertEqual(self.request('/admin/api/status', cookie=headers['Set-Cookie'].split(';')[0])[0], 200)
        self.assertEqual(self.request('/admin/api/login', {'username': 'bridge', 'password': self.cfg['admin_password']})[0], 401)
        self.login(password)
        restarted = s.App(self.cfg)
        try:
            self.assertTrue(restarted.admin.password_valid(password))
            self.assertFalse(restarted.admin.password_valid(self.cfg['admin_password']))
            value = restarted.limits.db.execute("SELECT value FROM admin_state WHERE name='credentials'").fetchone()[0]
            self.assertNotIn(password, value)
        finally:
            restarted.limits.db.close()

    def test_logout_and_expiry(self):
        cookie, csrf = self.login()
        status, headers, _ = self.request('/admin/api/logout', {}, cookie, csrf)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.request('/admin/api/status', cookie=cookie)[0], 401)
        cookie, _ = self.login()
        token = cookie.split('=', 1)[1]
        with patch('admin.time.monotonic', return_value=time.monotonic() + 9 * 3600):
            self.assertIsNone(self.app.admin.session(token))

    def test_login_rate_limit_and_download_limits_independent(self):
        for i in range(5):
            self.assertEqual(self.request('/admin/api/login', {'username': 'bridge', 'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('/admin/api/login', {'username': 'bridge', 'password': 'wrong'})[0], 429)
        self.assertEqual(self.request('/')[0], 200)

    def test_owner_can_manage_when_public_request_limit_exhausted(self):
        cookie, _ = self.login()
        self.app.limits.requests['127.0.0.1'] = (time.monotonic(), 60)
        self.assertEqual(self.request('/')[0], 429)
        self.assertEqual(self.request('/admin/api/status', cookie=cookie)[0], 200)


class SettingValidationTests(unittest.TestCase):
    def test_invalid_domains(self):
        for domain in ['localhost', '*.github.com', 'https://github.com', 'github.com/path', '127.0.0.1', 'evil..test']:
            settings = dict(public=True, paused=False, limits=dict(DEFAULTS), allowed_domains=[domain])
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                Admin.validate(settings)


if __name__ == '__main__':
    unittest.main()
