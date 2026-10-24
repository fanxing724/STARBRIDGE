import base64
import hashlib
import http.client
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import server as s


def config():
    return dict(password='p' * 32, secret='s' * 48, concurrency=2,
                allowed_domains=['github.com', 'githubusercontent.com'])


class Response:
    def __init__(self, status=200, headers=None, body=b'hello'):
        self.status = status
        self.headers = headers or {'Content-Length': str(len(body)), 'Content-Type': 'application/octet-stream'}
        self.body = io.BytesIO(body)

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, n=-1):
        return self.body.read(n)

    def close(self):
        pass


class Connection:
    def close(self):
        pass


class SecurityTests(unittest.TestCase):
    def test_reject_unsafe_urls(self):
        for url in ('http://github.com/a', 'https://user:pass@github.com/a',
                    'https://github.com:22/a', 'https://github.com/a#x',
                    'https://github.com.evil.test/a', 'https://github.com./a',
                    'https://github.com/\r\nHeader:x', 'https://github.com\\@evil.test/a'):
            with self.subTest(url=url), self.assertRaises(s.Rejected):
                s.validate_url(url, ['github.com'])

    def test_domain_boundary(self):
        self.assertEqual(s.validate_url('https://raw.githubusercontent.com/x', ['githubusercontent.com']).hostname,
                         'raw.githubusercontent.com')

    def test_nonpublic_dns_rejected(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1', '::ffff:127.0.0.1'):
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
            with self.subTest(ip=ip), patch.object(socket, 'getaddrinfo', return_value=rows), self.assertRaises(s.Rejected):
                s.RESOLVER.resolve('github.com')

    def test_signatures_tamper_and_expiry(self):
        app = s.App(config())
        path = app.sign('https://github.com/owner/repo/file.zip')
        self.assertEqual(app.verify(path)[0], 'https://github.com/owner/repo/file.zip')
        with self.assertRaises(s.Rejected):
            app.verify(path.replace('/d/', '/d/0'))
        with patch.object(s.time, 'time', return_value=time.time() + 30000), self.assertRaises(s.Rejected):
            app.verify(path)

    def test_signatures_reuse_cache_key_within_bucket(self):
        app = s.App(config())
        with patch.object(s.time, 'time', return_value=1000):
            first = app.sign('https://github.com/a.zip')
        with patch.object(s.time, 'time', return_value=1001):
            self.assertEqual(first, app.sign('https://github.com/a.zip'))

    def test_redirect_pinned_and_auth_stripped(self):
        calls = []
        replies = [Response(302, {'Location': 'https://objects.githubusercontent.com/file'}), Response()]
        class Fake(s.PinnedHTTPS):
            def __init__(self, host, address):
                self.host, self.address = host, address
            def request(self, method, path, headers):
                calls.append((self.host, self.address, dict(headers)))
            def getresponse(self):
                return replies.pop(0)
            def close(self):
                pass
        with patch.object(s.RESOLVER, 'resolve', return_value=['1.1.1.1']) as resolve, patch.object(s, 'PinnedHTTPS', Fake):
            s.fetch('https://github.com/file', headers={'Authorization': 'Bearer secret'},
                    allowed=['github.com', 'githubusercontent.com'])
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(calls[0][1], '1.1.1.1')
        self.assertNotIn('Authorization', calls[1][2])

    def test_redirect_cannot_escape_allowlist(self):
        class Fake:
            def __init__(self, *args): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response(302, {'Location': 'https://evil.test/file'})
            def close(self): pass
        with patch.object(s.RESOLVER, 'resolve', return_value=['1.1.1.1']), patch.object(s, 'PinnedHTTPS', Fake), self.assertRaises(s.Rejected):
            s.fetch('https://github.com/file', allowed=['github.com'])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = s.App(config())
        cls.server = s.Server(('127.0.0.1', 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.auth = 'Basic ' + base64.b64encode(b'bridge:' + b'p' * 32).decode()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method='GET', body=None, auth=True, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = dict(headers or {})
        if auth:
            headers['Authorization'] = self.auth
        conn.request(method, path, body, headers)
        res = conn.getresponse()
        result = (res.status, dict(res.getheaders()), res.read())
        conn.close()
        return result

    def test_health_and_panel_auth(self):
        self.assertEqual(self.request('/healthz', auth=False)[0], 200)
        status, headers, _ = self.request('/', auth=False)
        self.assertEqual(status, 401)
        self.assertIn('Basic', headers['WWW-Authenticate'])
        self.assertEqual(self.request('/')[0], 200)

    def test_create_link_and_cross_site_rejected(self):
        data = json.dumps({'url': 'https://github.com/a.zip'})
        status, _, body = self.request('/api/link', 'POST', data, headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['path'].startswith('/d/'))
        self.assertEqual(self.request('/api/link', 'POST', data, headers={
            'Content-Type': 'application/json', 'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_range_and_integrity(self):
        path = self.app.sign('https://github.com/a.zip')
        reply = Response(206, {'Content-Length': '3', 'Content-Range': 'bytes 1-3/5',
                               'Accept-Ranges': 'bytes', 'Content-Type': 'application/octet-stream'}, b'ell')
        with patch.object(s, 'fetch', return_value=(Connection(), reply)) as fetch:
            status, headers, data = self.request(path, auth=False, headers={'Range': 'bytes=1-3'})
        self.assertEqual((status, data), (206, b'ell'))
        self.assertEqual(headers['Content-Range'], 'bytes 1-3/5')
        self.assertEqual(fetch.call_args[0][2]['Range'], 'bytes=1-3')
        self.assertIn('public', headers['Cache-Control'])

    def test_multi_range_rejected(self):
        path = self.app.sign('https://github.com/a.zip')
        with patch.object(s, 'fetch') as fetch:
            self.assertEqual(self.request(path, headers={'Range': 'bytes=0-1,3-4'})[0], 400)
            fetch.assert_not_called()

    def test_download_concurrency_limit(self):
        path = self.app.sign('https://github.com/a.zip')
        self.app.transfers.acquire()
        self.app.transfers.acquire()
        try:
            self.assertEqual(self.request(path)[0], 503)
        finally:
            self.app.transfers.release()
            self.app.transfers.release()

    def test_docker_read_only_and_auth(self):
        self.assertEqual(self.request('/v2/', auth=False)[0], 401)
        self.assertEqual(self.request('/v2/')[0], 200)
        self.assertEqual(self.request('/v2/library/alpine/blobs/uploads/', 'POST')[0], 404)

    def test_docker_token_and_manifest(self):
        data = b'{"schemaVersion":2}'
        with patch.object(self.app, 'hub_token', return_value='upstream-token'), patch.object(
                s, 'fetch', return_value=(Connection(), Response(body=data))) as fetch:
            status, headers, body = self.request('/v2/library/alpine/manifests/latest')
        self.assertEqual((status, body), (200, data))
        self.assertEqual(fetch.call_args[0][2]['Authorization'], 'Bearer upstream-token')
        self.assertIn('private', headers['Cache-Control'])

    def test_upstream_oversize_and_errors(self):
        path = self.app.sign('https://github.com/a.zip')
        with patch.object(s, 'fetch', return_value=(Connection(), Response(headers={'Content-Length': str(9 * 1024**3)}))):
            self.assertEqual(self.request(path)[0], 413)
        with patch.object(s, 'fetch', return_value=(Connection(), Response(403, body=b'secret upstream message'))):
            status, _, data = self.request(path)
        self.assertEqual(status, 502)
        self.assertNotIn(b'secret', data)

    def test_partial_failure_does_not_append_json(self):
        class Broken(Response):
            def read(self, n=-1):
                raise OSError('network failed')
        path = self.app.sign('https://github.com/a.zip')
        with patch.object(s, 'fetch', return_value=(Connection(), Broken())), self.assertRaises(http.client.IncompleteRead) as caught:
            self.request(path)
        self.assertEqual(caught.exception.partial, b'')


if __name__ == '__main__':
    unittest.main()
