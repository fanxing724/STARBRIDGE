#!/usr/bin/env python3
"""Create an owner-only config without printing credentials or overwriting files."""
import json
import os
from pathlib import Path
import secrets
import sys

domains = ['github.com', 'githubusercontent.com', 'githubassets.com',
           'microsoft.com', 'windowsupdate.com', 'download.windowsupdate.com',
           'nvidia.com', 'download.nvidia.com', 'amd.com', 'intel.com',
           'dell.com', 'dellcdn.com', 'hp.com', 'hpcloud.hp.com', 'lenovo.com']
path = Path(sys.argv[1])
config = dict(bind='127.0.0.1', port=18080, concurrency=4, link_ttl=21600,
              public=True, quota_db=str(path.resolve().with_suffix('.usage.sqlite3')),
              proxy_secret=secrets.token_urlsafe(48),
              limits=dict(ip_concurrency=2, bytes_per_second=2*1024**2,
                          ip_daily_bytes=5*1024**3, daily_bytes=30*1024**3,
                          requests_per_minute=60),
              max_file_bytes=8 * 1024**3, allowed_domains=domains,
              password=secrets.token_urlsafe(32), secret=secrets.token_urlsafe(48))
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
print('Created protected configuration; credentials were not printed.')
