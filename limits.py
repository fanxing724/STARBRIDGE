"""Persistent payload budgets and bounded per-client request/concurrency/rate limits."""
import collections
import datetime
import hashlib
import hmac
import ipaddress
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo


DEFAULTS = dict(ip_concurrency=2, bytes_per_second=2 * 1024**2,
                ip_daily_bytes=5 * 1024**3, daily_bytes=30 * 1024**3,
                requests_per_minute=60)


class Limited(Exception):
    pass


class Limits:
    def __init__(self, config, secret):
        self.settings = dict(DEFAULTS, **config.get('limits', {}))
        if any(type(n) is not int or n <= 0 for n in self.settings.values()):
            raise ValueError('Limits must be positive integers')
        self.secret = secret
        self.tz = ZoneInfo(config.get('quota_timezone', 'Asia/Shanghai'))
        self.db = sqlite3.connect(config.get('quota_db', ':memory:'), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS usage(day TEXT, client TEXT, bytes INTEGER NOT NULL, PRIMARY KEY(day,client))')
        self.db.commit()
        self.lock = threading.RLock()
        self.active = {}
        self.pacers = {}
        self.requests = collections.OrderedDict()
        self.cleaned_day = None

    def day(self):
        return datetime.datetime.now(self.tz).date().isoformat()

    def identity(self, ip):
        address = ipaddress.ip_address(ip)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        # Treat one IPv6 /64 as one client to prevent trivial interface-ID rotation.
        return str(ipaddress.ip_network(str(address) + '/64', strict=False)) if address.version == 6 else str(address)

    def key(self, day, ip):
        return hmac.new(self.secret, (day + '/' + self.identity(ip)).encode(), hashlib.sha256).hexdigest()

    def used(self, day, key):
        row = self.db.execute('SELECT bytes FROM usage WHERE day=? AND client=?', (day, key)).fetchone()
        return row[0] if row else 0

    def reserve(self, day, ip, amount):
        if amount < 0:
            raise ValueError('Negative reservation')
        key = self.key(day, ip)
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.cleaned_day != day:
                cutoff = (datetime.date.fromisoformat(day) - datetime.timedelta(days=7)).isoformat()
                self.db.execute('DELETE FROM usage WHERE day < ?', (cutoff,))
                self.cleaned_day = day
            local = self.used(day, key)
            total = self.used(day, '*')
            if local + amount > self.settings['ip_daily_bytes'] or local >= self.settings['ip_daily_bytes']:
                raise Limited('今日当前 IP 的下载额度不足，请明天再试或使用较小的 Range 分段')
            if total + amount > self.settings['daily_bytes'] or total >= self.settings['daily_bytes']:
                raise Limited('今日全站下载额度不足，请明天再试')
            if not self.db.execute('SELECT 1 FROM usage WHERE day=? AND client=?', (day, key)).fetchone():
                count = self.db.execute('SELECT COUNT(*) FROM usage WHERE day=?', (day,)).fetchone()[0]
                if count >= 10001:
                    raise Limited('今日访问人数已达服务容量上限')
            for client in (key, '*'):
                self.db.execute('INSERT INTO usage VALUES(?,?,?) ON CONFLICT(day,client) DO UPDATE SET bytes=bytes+excluded.bytes',
                                (day, client, amount))

    def refund(self, day, ip, amount):
        if not amount:
            return
        with self.lock, self.db:
            for key in (self.key(day, ip), '*'):
                self.db.execute('UPDATE usage SET bytes=MAX(0,bytes-?) WHERE day=? AND client=?', (amount, day, key))

    def snapshot(self, ip):
        day = self.day()
        with self.lock:
            return dict(self.settings, ip_used=self.used(day, self.key(day, ip)),
                        total_used=self.used(day, '*'), day=day, timezone=str(self.tz))

    def request(self, ip):
        key = self.identity(ip)
        now = time.monotonic()
        with self.lock:
            while self.requests and next(iter(self.requests.values()))[0] + 60 <= now:
                self.requests.popitem(last=False)
            since, count = self.requests.get(key, (now, 0))
            if since + 60 <= now:
                since, count = now, 0
            if count >= self.settings['requests_per_minute']:
                raise Limited('请求过于频繁，请一分钟后重试')
            if key not in self.requests and len(self.requests) >= 4096:
                raise Limited('请求繁忙，请稍后重试')
            self.requests[key] = (since, count + 1)

    def acquire(self, ip):
        key = self.identity(ip)
        with self.lock:
            if self.active.get(key, 0) >= self.settings['ip_concurrency']:
                raise Limited('当前 IP 已达到同时下载数量上限')
            self.active[key] = self.active.get(key, 0) + 1

    def release(self, ip):
        key = self.identity(ip)
        with self.lock:
            self.active[key] -= 1
            if not self.active[key]:
                del self.active[key]
                self.pacers.pop(key, None)

    def pace(self, ip, size):
        key = self.identity(ip)
        with self.lock:
            now = time.monotonic()
            end = max(now, self.pacers.get(key, now)) + size / self.settings['bytes_per_second']
            self.pacers[key] = end
        time.sleep(max(0, end - time.monotonic()))


class Reservation:
    """Reserve known lengths before headers; refund unsent tail on disconnect."""
    def __init__(self, limits, ip, size):
        self.limits, self.ip, self.day = limits, ip, limits.day()
        self.held = 0
        self.sent = 0
        if size is not None:
            limits.reserve(self.day, ip, size)
            self.held = size
        else:
            limits.reserve(self.day, ip, 0)

    def consume(self, size):
        missing = max(0, self.sent + size - self.held)
        if missing:
            self.limits.reserve(self.day, self.ip, missing)
            self.held += missing
        # Count attempted writes conservatively if the socket fails partway through.
        self.sent += size

    def close(self):
        self.limits.refund(self.day, self.ip, self.held - self.sent)
