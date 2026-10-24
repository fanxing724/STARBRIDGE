#!/usr/bin/env python3
"""Real upstream smoke test. Credentials and signed links are never printed."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import sys

import server

config = json.loads(Path(sys.argv[1]).read_text())
auth = 'Basic ' + base64.b64encode(('bridge:' + config['password']).encode()).decode()
app = server.App(config)


def request(path, method='GET', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', config['port'], timeout=60)
    fields = {'Authorization': auth}
    fields.update(headers or {})
    conn.request(method, path, headers=fields)
    res = conn.getresponse()
    body = res.read(4 * 1024 * 1024)
    code, response_headers = res.status, dict(res.getheaders())
    conn.close()
    return code, response_headers, body


url = 'https://github.com/jqlang/jq/releases/download/jq-1.7.1/sha256sum.txt'
conn, res = server.fetch(url, allowed=config['allowed_domains'])
assert res.status == 200, ('github upstream', res.status)
reference = res.read(65536)
res.close()
conn.close()
path = app.sign(url)
code, headers, data = request(path)
assert code == 200 and data == reference, ('github relay', code)
print('PASS GitHub Release: byte-for-byte equality,', len(data), 'bytes')
code, headers, data = request(path, headers={'Range': 'bytes=0-63'})
assert code == 206 and data == reference[:64], ('github range', code)
print('PASS GitHub Range: 206 and exact first 64 bytes')
code, headers, data = request(path, method='HEAD')
assert code == 200 and not data and int(headers['Content-Length']) == len(reference)
print('PASS GitHub HEAD: correct file length')

accept = {'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'}
code, headers, data = request('/v2/library/hello-world/manifests/latest', headers=accept)
assert code == 200, ('docker index', code)
index = json.loads(data)
digest = next(m['digest'] for m in index['manifests'] if m['platform'].get('os') == 'linux'
              and m['platform'].get('architecture') == 'amd64')
code, headers, data = request('/v2/library/hello-world/manifests/' + digest, headers=accept)
assert code == 200 and 'sha256:' + hashlib.sha256(data).hexdigest() == digest
manifest = json.loads(data)
layer = manifest['layers'][0]['digest']
code, headers, data = request('/v2/library/hello-world/blobs/' + layer)
assert code == 200 and 'sha256:' + hashlib.sha256(data).hexdigest() == layer, ('docker blob', code)
print('PASS Docker Hub: multi-platform index, manifest digest, layer SHA256,', len(data), 'bytes')

driver_url = 'https://download.nvidia.com/XFree86/Linux-x86_64/latest.txt'
code, headers, data = request(app.sign(driver_url))
assert code == 200 and 0 < len(data) < 65536, ('nvidia metadata', code)
print('PASS NVIDIA public driver metadata:', len(data), 'bytes (installer not downloaded)')
