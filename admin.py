"""Owner sessions, durable settings, and aggregate administration data."""
import base64
import collections
import datetime
import hashlib
import hmac
import ipaddress
import json
import re
from pathlib import Path
import secrets
import threading
import time

from limits import DEFAULTS, Limited


class Conflict(Exception):
    pass


class Admin:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.password_lock = threading.Lock()
        self.sessions = collections.OrderedDict()
        self.attempts = collections.OrderedDict()
        self.global_attempts = (0, 0)
        self.revision = 0
        limits = app.limits
        with limits.lock, limits.db:
            limits.db.execute('CREATE TABLE IF NOT EXISTS admin_state(name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            limits.db.execute('CREATE TABLE IF NOT EXISTS admin_audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, action TEXT NOT NULL)')
            row = limits.db.execute("SELECT value FROM admin_state WHERE name='credentials'").fetchone()
            if row:
                self.credentials = json.loads(row[0])
            else:
                self.credentials = self.hash_password(app.config.get('admin_password', app.password))
                limits.db.execute('INSERT INTO admin_state VALUES(?,?)', ('credentials', json.dumps(self.credentials)))
            row = limits.db.execute("SELECT value FROM admin_state WHERE name='settings'").fetchone()
            if row:
                saved = json.loads(row[0])
                self.revision = saved['revision']
                self.apply(self.validate(saved['settings']))

    @staticmethod
    def hash_password(password):
        salt = secrets.token_bytes(24)
        iterations = 310000
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
        return dict(salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode(), iterations=iterations)

    def password_valid(self, password):
        credentials = self.credentials
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(credentials['salt']),
                                     credentials['iterations'])
        return hmac.compare_digest(digest, base64.b64decode(credentials['digest']))

    def login(self, ip, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            raise ValueError('Invalid login')
        now = time.monotonic()
        with self.lock:
            while self.attempts and next(iter(self.attempts.values()))[0] + 300 <= now:
                self.attempts.popitem(last=False)
            since, count = self.attempts.get(ip, (now, 0))
            global_since, global_count = self.global_attempts
            if global_since + 60 <= now:
                global_since, global_count = now, 0
            if count >= 5 or global_count >= 20:
                raise Limited('登录尝试过多，请稍后再试')
            if ip not in self.attempts and len(self.attempts) >= 1024:
                raise Limited('登录请求繁忙，请稍后再试')
            self.attempts[ip] = (since, count + 1)
            self.global_attempts = (global_since, global_count + 1)
        # Always hash even if the username is wrong; username is not an oracle.
        expected_credentials = self.credentials
        password_ok = self.password_valid(password)
        if not password_ok or username != 'bridge':
            return None
        with self.lock:
            self.attempts.pop(ip, None)
        return self.new_session(expected_credentials)

    def new_session(self, expected_credentials=None):
        token = secrets.token_urlsafe(32)
        session = dict(csrf=secrets.token_urlsafe(32), expires=time.monotonic() + 8 * 3600)
        with self.lock:
            if expected_credentials is not None and expected_credentials is not self.credentials:
                return None
            self.prune_sessions()
            if len(self.sessions) >= 64:
                self.sessions.popitem(last=False)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = session
        return token, session

    def prune_sessions(self):
        now = time.monotonic()
        for key in [k for k, v in self.sessions.items() if v['expires'] <= now]:
            del self.sessions[key]

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.lock:
            self.prune_sessions()
            return self.sessions.get(hashlib.sha256(token.encode()).hexdigest())

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    @staticmethod
    def validate(settings):
        if not isinstance(settings, dict) or set(settings) != {'public', 'paused', 'limits', 'allowed_domains'}:
            raise ValueError('设置字段不完整或包含未知字段')
        if type(settings['public']) is not bool or type(settings['paused']) is not bool:
            raise ValueError('公开模式和暂停必须为布尔值')
        limits = settings['limits']
        if not isinstance(limits, dict) or set(limits) != set(DEFAULTS):
            raise ValueError('限额字段不完整')
        bounds = dict(ip_concurrency=(1, 4), bytes_per_second=(65536, 100 * 1024**2),
                      ip_daily_bytes=(1024**2, 10 * 1024**4), daily_bytes=(1024**2, 10 * 1024**4),
                      requests_per_minute=(5, 600))
        for key, (low, high) in bounds.items():
            value = limits[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError('限额超出允许范围：' + key)
        if limits['daily_bytes'] < limits['ip_daily_bytes']:
            raise ValueError('全站日额度不能小于每 IP 日额度')
        domains = settings['allowed_domains']
        if not isinstance(domains, list) or not 1 <= len(domains) <= 100:
            raise ValueError('来源白名单需要包含 1 至 100 个域名')
        cleaned = []
        for domain in domains:
            if not isinstance(domain, str):
                raise ValueError('域名格式不正确')
            domain = domain.strip().lower()
            if (len(domain) > 253 or '.' not in domain or not all(
                re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in domain.split('.'))):
                raise ValueError('请填写裸域名，不包含协议、路径或通配符')
            try:
                ipaddress.ip_address(domain)
            except ValueError:
                pass
            else:
                raise ValueError('来源白名单仅接受域名')
            if domain not in cleaned:
                cleaned.append(domain)
        return dict(public=settings['public'], paused=settings['paused'], limits=dict(limits), allowed_domains=cleaned)

    def settings(self):
        return dict(public=self.app.public, paused=self.app.paused,
                    limits=dict(self.app.limits.settings), allowed_domains=list(self.app.config['allowed_domains']))

    def apply(self, settings):
        self.app.public = settings['public']
        self.app.paused = settings['paused']
        self.app.limits.settings = dict(settings['limits'])
        self.app.config = dict(self.app.config, allowed_domains=list(settings['allowed_domains']),
                               require_download_login=not settings['public'])

    def save(self, settings, revision):
        settings = self.validate(settings)
        limits = self.app.limits
        with limits.lock:
            if type(revision) is not int or revision != self.revision:
                raise Conflict('设置已在其他页面修改，请刷新后重试')
            next_revision = self.revision + 1
            value = json.dumps(dict(revision=next_revision, settings=settings))
            with limits.db:
                limits.db.execute("INSERT INTO admin_state VALUES('settings',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (value,))
                self.audit('下载规则已更新，版本 ' + str(next_revision))
            self.apply(settings)
            self.revision = next_revision

    def change_password(self, old, new):
        with self.password_lock:
            return self._change_password(old, new)

    def _change_password(self, old, new):
        if not isinstance(old, str) or not isinstance(new, str) or len(old) > 256 or not 12 <= len(new) <= 256:
            raise ValueError('新密码长度需要在 12 至 256 字符之间')
        if not self.password_valid(old):
            raise ValueError('原管理密码不正确')
        if hmac.compare_digest(old.encode(), new.encode()):
            raise ValueError('新密码不能与原密码相同')
        credentials = self.hash_password(new)
        limits = self.app.limits
        with limits.lock, limits.db:
            limits.db.execute("UPDATE admin_state SET value=? WHERE name='credentials'", (json.dumps(credentials),))
            self.audit('管理密码已更新，旧会话已撤销')
        with self.lock:
            self.credentials = credentials
            self.sessions.clear()
        return self.new_session()

    def audit(self, action):
        self.app.limits.db.execute('INSERT INTO admin_audit(action) VALUES(?)', (action,))
        self.app.limits.db.execute('DELETE FROM admin_audit WHERE id NOT IN (SELECT id FROM admin_audit ORDER BY id DESC LIMIT 100)')

    def status(self):
        limits = self.app.limits
        day = limits.day()
        cutoff = (datetime.date.fromisoformat(day) - datetime.timedelta(days=6)).isoformat()
        with limits.lock:
            history = [dict(day=d, bytes=b) for d, b in limits.db.execute(
                "SELECT day,bytes FROM usage WHERE client='*' AND day>=? AND day<=? ORDER BY day DESC LIMIT 7", (cutoff, day))]
            visitors = limits.db.execute("SELECT COUNT(*) FROM usage WHERE day=? AND client!='*' AND bytes>0", (day,)).fetchone()[0]
            clients = [dict(visitor=k[:12], bytes=b) for k, b in limits.db.execute(
                "SELECT client,bytes FROM usage WHERE day=? AND client!='*' AND bytes>0 ORDER BY bytes DESC LIMIT 10", (day,))]
            data = dict(settings=self.settings(), revision=self.revision, day=day, timezone=str(limits.tz),
                        total_used=limits.used(day, '*'), visitors=visitors, history=history, clients=clients,
                        audit=[dict(at=at, action=action) for at, action in limits.db.execute(
                            'SELECT at,action FROM admin_audit ORDER BY id DESC LIMIT 10')])
        with self.app.lock:
            data.update(active_downloads=self.app.active_transfers, verified_proxy_requests=self.app.verified_proxy_calls)
        try:
            rss = next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))
            memory = int(rss) * 1024
        except (OSError, ValueError, StopIteration):
            memory = None
        data.update(version=self.app.version, uptime_seconds=int(time.monotonic() - self.app.started), memory_bytes=memory)
        return data
