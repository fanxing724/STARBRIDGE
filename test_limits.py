import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import server as s
from limits import Limits, Limited, Reservation
from test_server import config, Connection, Response


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(quota_db=str(Path(self.tmp.name) / 'usage.sqlite3'),
                        limits=dict(ip_daily_bytes=10, daily_bytes=15))
        self.limits = Limits(self.cfg, b'secret')

    def tearDown(self):
        self.limits.db.close()
        self.tmp.cleanup()

    def test_persistent_limits_survive_restart(self):
        self.limits.reserve(self.limits.day(), '1.1.1.1', 8)
        restarted = Limits(self.cfg, b'secret')
        try:
            self.assertEqual(restarted.snapshot('1.1.1.1')['ip_used'], 8)
            with self.assertRaises(Limited):
                restarted.reserve(restarted.day(), '1.1.1.1', 3)
        finally:
            restarted.db.close()

    def test_global_budget_and_atomic_reservations(self):
        def attempt(ip):
            try:
                self.limits.reserve(self.limits.day(), ip, 10)
                return True
            except Limited:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['1.1.1.1', '8.8.8.8']))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.limits.snapshot('1.1.1.1')['total_used'], 10)

    def test_refund_unused_tail(self):
        reservation = Reservation(self.limits, '1.1.1.1', 10)
        reservation.consume(4)
        reservation.close()
        self.assertEqual(self.limits.snapshot('1.1.1.1')['ip_used'], 4)

    def test_unknown_length_stops_at_budget(self):
        reservation = Reservation(self.limits, '1.1.1.1', None)
        reservation.consume(8)
        with self.assertRaises(Limited):
            reservation.consume(3)
        reservation.close()
        self.assertEqual(self.limits.snapshot('1.1.1.1')['ip_used'], 8)

    def test_midnight_resets_but_refund_keeps_original_day(self):
        with patch.object(self.limits, 'day', return_value='2026-10-24'):
            reservation = Reservation(self.limits, '1.1.1.1', 10)
            reservation.consume(3)
        with patch.object(self.limits, 'day', return_value='2026-10-25'):
            reservation.close()
            self.assertEqual(self.limits.snapshot('1.1.1.1')['ip_used'], 0)
            self.limits.reserve('2026-10-25', '1.1.1.1', 10)
        with patch.object(self.limits, 'day', return_value='2026-10-24'):
            self.assertEqual(self.limits.snapshot('1.1.1.1')['ip_used'], 3)

    def test_shared_ipv6_subnet_and_concurrency(self):
        self.limits.acquire('2001:4860:1:2::1')
        self.limits.acquire('2001:4860:1:2::2')
        with self.assertRaises(Limited):
            self.limits.acquire('2001:4860:1:2::3')
        self.limits.release('2001:4860:1:2::1')
        self.limits.acquire('2001:4860:1:2::4')
        self.limits.release('2001:4860:1:2::2')
        self.limits.release('2001:4860:1:2::4')
        self.assertFalse(self.limits.active)

    def test_rate_is_aggregate_per_ip(self):
        self.limits.settings['bytes_per_second'] = 10
        with patch('limits.time.monotonic', return_value=100), patch('limits.time.sleep') as sleep:
            self.limits.pace('1.1.1.1', 10)
            self.limits.pace('1.1.1.1', 10)
            self.limits.pace('8.8.8.8', 10)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 1])

    def test_request_frequency(self):
        self.limits.settings['requests_per_minute'] = 2
        with patch('limits.time.monotonic', return_value=1):
            self.limits.request('1.1.1.1')
            self.limits.request('1.1.1.1')
            with self.assertRaises(Limited):
                self.limits.request('1.1.1.1')
        with patch('limits.time.monotonic', return_value=62):
            self.limits.request('1.1.1.1')


class PublicHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = s.App(dict(config(), public=True, limits={'ip_daily_bytes': 7, 'daily_bytes': 10}))
        self.server = s.Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.limits.db.close()

    def request(self, path, method='GET', headers=None, body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request(method, path, headers=headers or {}, body=body)
        res = conn.getresponse()
        result = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return result

    def test_public_page_link_and_registry_without_login(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/v2/')[0], 200)
        status, _, body = self.request('/api/link', 'POST', {'Content-Type': 'application/json'},
                                       json.dumps({'url': 'https://github.com/file.zip'}))
        self.assertEqual(status, 200)
        self.assertIn('/d/', json.loads(body)['path'])
        self.assertTrue(json.loads(self.request('/api/limits')[2])['public'])

    def test_quota_exhaustion_returns_429_and_cannot_forge_ip(self):
        path = self.app.sign('https://github.com/file.zip')
        with patch.object(s, 'fetch', side_effect=lambda *a: (Connection(), Response())):
            status, headers, _ = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn('no-store', headers['Cache-Control'])
            self.assertEqual(self.request(path, headers={'EO-Connecting-IP': '8.8.8.8',
                                                        'X-Forwarded-For': '8.8.8.8'})[0], 429)
        data = json.loads(self.request('/api/limits')[2])
        self.assertEqual(data['ip_used'], 5)

    def test_trusted_cdn_identity_requires_secret(self):
        self.app.config['proxy_secret'] = 'k' * 48
        headers = {'EO-Connecting-IP': '8.8.8.8', 'X-StarBridge-Origin-Key': 'k' * 48}
        self.assertEqual(self.app.client_ip('1.1.1.1', headers), '8.8.8.8')
        headers['X-StarBridge-Origin-Key'] = 'wrong'
        self.assertEqual(self.app.client_ip('1.1.1.1', headers), '1.1.1.1')
        headers.update({'X-StarBridge-Origin-Key': 'k' * 48, 'EO-Connecting-IP': 'bad'})
        with self.assertRaises(s.Rejected):
            self.app.client_ip('1.1.1.1', headers)

    def test_head_does_not_spend_bytes(self):
        path = self.app.sign('https://github.com/file.zip')
        with patch.object(s, 'fetch', return_value=(Connection(), Response())):
            self.assertEqual(self.request(path, 'HEAD')[0], 200)
        self.assertEqual(self.app.limits.snapshot('127.0.0.1')['ip_used'], 0)

    def test_ip_concurrency_rejection_releases_global_slot(self):
        self.app.limits.acquire('127.0.0.1')
        self.app.limits.acquire('127.0.0.1')
        try:
            self.assertEqual(self.request(self.app.sign('https://github.com/file.zip'))[0], 429)
            self.assertTrue(self.app.transfers.acquire(blocking=False))
            self.assertTrue(self.app.transfers.acquire(blocking=False))
            self.app.transfers.release()
            self.app.transfers.release()
        finally:
            self.app.limits.release('127.0.0.1')
            self.app.limits.release('127.0.0.1')


if __name__ == '__main__':
    unittest.main()
