#!/usr/bin/env python3
"""StarBridge: bounded, authenticated HTTPS download / Docker Hub relay."""
import argparse
import base64
import collections
import concurrent.futures
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import re
import secrets
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from limits import Limits, Limited, Reservation

ROOT = Path(__file__).resolve().parent
TLS = ssl.create_default_context()
LOG = logging.getLogger('starbridge')
VERSION = '0.2.0'


class Rejected(Exception):
    pass


def encode(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip('=')


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode()


class PublicResolver:
    """Resolve once, reject nonpublic results, pin the connection to that result."""
    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.slots = threading.BoundedSemaphore(4)

    def resolve(self, host):
        if not self.slots.acquire(blocking=False):
            raise Rejected('DNS busy; retry later')
        try:
            future = self.pool.submit(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
        except Exception:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.slots.release())
        try:
            results = future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            raise Rejected('DNS timeout') from None
        addresses = list(dict.fromkeys(row[4][0] for row in results))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise Rejected('Nonpublic destination rejected')
        return addresses


RESOLVER = PublicResolver()


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=30, context=TLS)
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def validate_url(url, allowed=None):
    if len(url) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in url) or '\\' in url:
        raise Rejected('Use a percent-encoded HTTPS URL')
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise Rejected('Invalid URL') from None
    if (parsed.scheme != 'https' or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443) or parsed.fragment):
        raise Rejected('Only HTTPS port 443 without credentials or fragments is supported')
    if host.endswith('.') or not re.fullmatch(r'[a-zA-Z0-9.-]+', host):
        raise Rejected('Invalid hostname')
    if allowed is not None and not any(host == d or host.endswith('.' + d) for d in allowed):
        raise Rejected('Source domain is not enabled')
    return parsed


def fetch(url, method='GET', headers=None, allowed=None):
    """Follow redirects server-side; validate and pin every hop; strip auth on hops."""
    headers = dict(headers or {})
    for hop in range(9):
        parsed = validate_url(url, allowed)
        addresses = RESOLVER.resolve(parsed.hostname)
        conn = PinnedHTTPS(parsed.hostname, addresses[0])
        request_headers = {'User-Agent': 'StarBridge/' + VERSION, 'Accept-Encoding': 'identity'}
        request_headers.update(headers)
        try:
            target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
            conn.request(method, target, headers=request_headers)
            response = conn.getresponse()
            if response.status not in (301, 302, 303, 307, 308):
                return conn, response
            location = response.getheader('Location')
            if not location:
                return conn, response
            next_url = urljoin(url, location)
            if urlsplit(next_url).hostname != parsed.hostname:
                headers.pop('Authorization', None)
            url = next_url
        except Exception:
            conn.close()
            raise
        response.close()
        conn.close()
    raise Rejected('Too many redirects')


class App:
    def __init__(self, config):
        self.config = config
        self.secret = config['secret'].encode()
        self.password = config['password']
        self.transfers = threading.BoundedSemaphore(config.get('concurrency', 4))
        self.clients = threading.BoundedSemaphore(24)
        self.lock = threading.Lock()
        self.tokens = {}
        self.failures = collections.OrderedDict()
        self.public = config.get('public', False)
        self.limits = Limits(config, self.secret) if self.public else None

    def client_ip(self, peer, headers):
        expected = self.config.get('proxy_secret', '')
        supplied = headers.get('X-StarBridge-Origin-Key', '')
        if len(expected) >= 32 and hmac.compare_digest(supplied.encode(), expected.encode()):
            value = headers.get('EO-Connecting-IP', '')
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                raise Rejected('Invalid trusted client IP header') from None
        # Forwarded headers alone are untrusted, including on direct origin requests.
        return str(ipaddress.ip_address(peer))

    def signature(self, expiry, target):
        return hmac.new(self.secret, f'{expiry}/{target}'.encode(), hashlib.sha256).hexdigest()

    def sign(self, url):
        validate_url(url, self.config.get('allowed_domains'))
        target = encode(url)
        ttl = self.config.get('link_ttl', 21600)
        bucket = min(300, ttl)
        # Reuse a canonical signed URL for five minutes to improve CDN hit rate.
        expiry = str(int(time.time()) // bucket * bucket + ttl)
        name = unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or 'download'
        name = re.sub(r'[^\w.() -]', '_', name)[:120]
        return f'/d/{expiry}/{self.signature(expiry, target)}/{target}/{quote(name)}'

    def verify(self, path):
        parts = path.split('/')
        if len(parts) != 6:
            raise Rejected('Invalid download link')
        _, _, expiry, signature, target, name = parts
        try:
            remaining = int(expiry) - int(time.time())
            url = decode(target)
        except (ValueError, UnicodeError):
            raise Rejected('Invalid download link') from None
        if remaining <= 0 or not hmac.compare_digest(self.signature(expiry, target), signature):
            raise Rejected('Link expired or invalid')
        return url, min(3600, remaining)

    def hub_token(self, repository):
        with self.lock:
            cached = self.tokens.get(repository)
            if cached and cached[0] > time.time():
                return cached[1]
        query = urlencode({'service': 'registry.docker.io', 'scope': f'repository:{repository}:pull'})
        conn, res = fetch('https://auth.docker.io/token?' + query, allowed=['auth.docker.io'])
        try:
            if res.status != 200:
                raise Rejected('Docker Hub authentication unavailable')
            data = json.loads(res.read(131073))
            token = data['token']
            if not isinstance(token, str) or len(token) > 32768:
                raise Rejected('Invalid upstream token')
            ttl = min(240, max(1, int(data.get('expires_in', 300)) - 30))
            with self.lock:
                if len(self.tokens) >= 128:
                    self.tokens.clear()
                self.tokens[repository] = (time.time() + ttl, token)
            return token
        finally:
            res.close()
            conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, app):
        self.app = app
        self.clients = app.clients
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.clients.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.clients.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.clients.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'StarBridge/' + VERSION
    sys_version = ''

    @property
    def app(self):
        return self.server.app

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # Do not log URLs, Basic credentials, signed links, or upstream error bodies.
        pass

    def reply(self, status, data, content_type='application/json; charset=utf-8', extra=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def authorized(self):
        ip = self.client_address[0]  # Never trust a user-provided X-Forwarded-For.
        now = time.monotonic()
        with self.app.lock:
            count, until = self.app.failures.get(ip, (0, now + 60))
            if until <= now:
                count, until = 0, now + 60
        if count >= 20:
            self.reply(429, {'error': 'Too many attempts; retry later'})
            return False
        try:
            scheme, credentials = self.headers.get('Authorization', '').split(' ', 1)
            value = base64.b64decode(credentials, validate=True).decode()
            valid = scheme.lower() == 'basic' and hmac.compare_digest(
                value.encode(), ('bridge:' + self.app.password).encode())
        except (ValueError, UnicodeError):
            valid = False
        if valid:
            return True
        with self.app.lock:
            if len(self.app.failures) >= 1024:
                self.app.failures.popitem(last=False)
            self.app.failures[ip] = (count + 1, until)
        self.reply(401, {'error': 'Authentication required'}, extra={
            'WWW-Authenticate': 'Basic realm="StarBridge", charset="UTF-8"'})
        return False

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            if len(self.path) > 16384:
                return self.reply(414, {'error': 'URL too long'})
            if self.path == '/healthz' and self.command in ('GET', 'HEAD'):
                return self.reply(200, {'ok': True, 'version': VERSION})
            self.ip = self.app.client_ip(self.client_address[0], self.headers)
            if self.app.public:
                self.app.limits.request(self.ip)
            if self.path.startswith('/d/') and self.command in ('GET', 'HEAD'):
                url, ttl = self.app.verify(urlsplit(self.path).path)
                return self.transfer(url, ttl=ttl)
            if not self.app.public and not self.authorized():
                return
            if self.path == '/api/limits' and self.command in ('GET', 'HEAD'):
                data = self.app.limits.snapshot(self.ip) if self.app.public else {}
                return self.reply(200, dict(data, public=self.app.public))
            if self.path == '/' and self.command in ('GET', 'HEAD'):
                return self.reply(200, (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8', {
                    'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})
            if self.path == '/api/link' and self.command == 'POST':
                if self.headers.get('Sec-Fetch-Site', '') == 'cross-site':
                    return self.reply(403, {'error': 'Cross-site request rejected'})
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.reply(415, {'error': 'Expected application/json'})
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 12288 or self.headers.get('Transfer-Encoding'):
                    return self.reply(413, {'error': 'Invalid body size'})
                body = json.loads(self.rfile.read(length))
                path = self.app.sign(body['url'])
                return self.reply(200, {'path': path, 'expires_in': int(path.split('/')[2]) - int(time.time())})
            if self.command in ('GET', 'HEAD') and self.path.startswith('/v2/'):
                return self.docker()
            return self.reply(404, {'error': 'Not found'})
        except Limited as exc:
            self.reply(429, {'error': str(exc)}, extra={'Retry-After': '60'})
        except Rejected as exc:
            self.reply(400, {'error': str(exc)})
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error': 'Invalid request'})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as exc:
            LOG.warning('Request failed: %s', type(exc).__name__)
            self.reply(502, {'error': 'Upstream unavailable; retry later'})

    def docker(self):
        if self.path == '/v2/':
            return self.reply(200, {}, extra={'Docker-Distribution-Api-Version': 'registry/2.0'})
        # Read-only public Docker Hub repositories. No pushes or arbitrary token realms.
        match = re.fullmatch(r'/v2/([a-z0-9._/-]+)/(manifests|blobs)/([a-zA-Z0-9_.:+-]+)', self.path)
        if not match:
            return self.reply(404, {'error': 'Use /v2/<namespace>/<image>/{manifests,blobs}/<reference>'})
        repository, kind, ref = match.groups()
        if '/' not in repository:
            repository = 'library/' + repository
        if any(part in ('', '.', '..') for part in repository.split('/')):
            raise Rejected('Invalid repository')
        if kind == 'blobs' and not re.fullmatch(r'sha256:[0-9a-f]{64}', ref):
            raise Rejected('Invalid blob digest')
        token = self.app.hub_token(repository)
        return self.transfer(f'https://registry-1.docker.io/v2/{repository}/{kind}/{ref}',
                             docker=True, token=token)

    def transfer(self, url, ttl=0, docker=False, token=None):
        if not self.app.transfers.acquire(blocking=False):
            return self.reply(503, {'error': 'All download slots busy'}, extra={'Retry-After': '5'})
        conn = res = None
        started = False
        reservation = None
        ip_slot = False
        try:
            if self.app.public:
                self.app.limits.acquire(self.ip)
                ip_slot = True
            headers = {}
            for name in ('Range', 'If-Range', 'If-None-Match', 'If-Modified-Since', 'Accept'):
                if name in self.headers:
                    headers[name] = self.headers[name]
            if 'Range' in headers and not re.fullmatch(r'bytes=(?:\d+-\d*|-\d+)', headers['Range']):
                raise Rejected('Only a single byte range is supported')
            if token:
                headers['Authorization'] = 'Bearer ' + token
            allowed = self.app.config.get('allowed_domains')
            if docker:
                allowed = ['docker.io', 'docker.com', 'cloudflare.docker.com', 'cloudflarestorage.com']
            conn, res = fetch(url, self.command, headers, allowed)
            if res.status not in (200, 206, 304, 404, 416, 429):
                return self.reply(502, {'error': f'Upstream HTTP {res.status}'})
            size = res.getheader('Content-Length')
            limit = self.app.config.get('max_file_bytes', 8589934592)
            if size is not None and int(size) > limit:
                return self.reply(413, {'error': 'File exceeds configured limit'})
            if res.status in (404, 429):
                return self.reply(res.status, {'error': 'Upstream not found or rate limited'},
                                  extra={'Retry-After': '60'} if res.status == 429 else None)
            if self.app.public and self.command != 'HEAD' and res.status in (200, 206):
                reservation = Reservation(self.app.limits, self.ip, int(size) if size is not None else None)
            self.send_response(res.status)
            started = True
            forwarded = ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges',
                         'ETag', 'Last-Modified', 'Content-Encoding', 'Docker-Content-Digest',
                         'Docker-Distribution-Api-Version', 'RateLimit-Limit', 'RateLimit-Remaining')
            for name in forwarded:
                value = res.getheader(name)
                if value is not None and '\r' not in value and '\n' not in value:
                    self.send_header(name, value)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "sandbox; default-src 'none'")
            if not docker:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" +
                                 quote(unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or 'download', safe=''))
            self.send_header('Cache-Control', f'public, max-age={ttl}, no-transform' if not self.app.public and ttl and res.status in (200, 206, 304)
                             else 'private, no-store')
            self.end_headers()
            if self.command == 'HEAD' or res.status == 304:
                return
            total = 0
            deadline = time.monotonic() + 7200
            while True:
                chunk = res.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    break
                if reservation:
                    reservation.consume(len(chunk))
                    self.app.limits.pace(self.ip, len(chunk))
                self.wfile.write(chunk)
        except Exception:
            if started:
                # A partial stream must terminate; never append a JSON error to a file.
                self.close_connection = True
            else:
                raise
        finally:
            if reservation:
                try:
                    reservation.close()
                except Exception:
                    LOG.error('Quota refund unavailable; reserved bytes retained')
            if ip_slot:
                self.app.limits.release(self.ip)
            if res:
                res.close()
            if conn:
                conn.close()
            self.app.transfers.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if config.get('public') is True and (not config.get('allowed_domains') or not config.get('quota_db')):
        raise SystemExit('Public mode requires allowed_domains and a persistent quota_db')
    if type(config.get('public', False)) is not bool:
        raise SystemExit('public must be a JSON boolean')
    if len(config.get('secret', '')) < 32 or len(config.get('password', '')) < 24:
        raise SystemExit('Configure independent random password and secret (24/32+ characters)')
    logging.basicConfig(level=logging.INFO)
    app = App(config)
    server = Server((config.get('bind', '127.0.0.1'), config.get('port', 18080)), app)
    if config.get('tls_cert') and config.get('tls_key'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config['tls_cert'], config['tls_key'])
        secure = Server((config.get('tls_bind', '0.0.0.0'), config.get('tls_port', 8443)), app)
        secure.socket = context.wrap_socket(secure.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=secure.serve_forever, daemon=True).start()
    LOG.info('StarBridge %s ready', VERSION)
    server.serve_forever()


if __name__ == '__main__':
    main()
