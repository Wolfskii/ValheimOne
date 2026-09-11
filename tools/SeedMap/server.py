#!/usr/bin/env python3
"""Loopback-only native map API; publish through an authenticated private relay."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse
from worker import ROOT, key_for, validate_seed

pool = ThreadPoolExecutor(max_workers=1)
jobs = {}
guard = threading.Lock()


def generate(seed, key):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('worker.py')), seed],
                                capture_output=True, timeout=400)
        if result.returncode:
            raise RuntimeError('Generation failed')
        with guard:
            jobs[key] = 'complete'
    except Exception:
        folder = ROOT / 'cache' / key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'failed').touch()
        with guard:
            jobs[key] = 'failed'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, code, data, kind='application/json'):
        payload = json.dumps(data).encode() if kind == 'application/json' else data
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.connection.settimeout(10)
        try:
            if self.path != '/generate':
                return self.send(404, {'error': 'Not found'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(415, {'error': 'JSON required'})
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 256:
                return self.send(400, {'error': 'Invalid request size'})
            data = json.loads(self.rfile.read(length))
            if set(data) != {'seed'} or not isinstance(data['seed'], str):
                raise ValueError('A seed is required')
            seed = validate_seed(data['seed'])
            key = key_for(seed, 1024)
            cached = (ROOT / 'cache' / key / 'result.json').exists()
            with guard:
                if len(jobs) > 512:
                    for old in list(jobs):
                        if jobs[old] != 'running':
                            del jobs[old]
                if not cached and jobs.get(key) != 'running':
                    if sum(x == 'running' for x in jobs.values()) >= 2:
                        return self.send(429, {'error': 'Two worlds are already queued. Try again shortly.'})
                    jobs[key] = 'running'
                    (ROOT / 'cache' / key / 'failed').unlink(missing_ok=True)
                    pool.submit(generate, seed, key)
            self.send(200 if cached else 202, {'key': key, 'cached': cached, 'seed': seed})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.send(400, {'error': 'Enter 1–10 letters or numbers.'})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/catalog':
            rows = []
            # Only deliberate examples belong in a discoverable catalogue.
            # Never enumerate seeds submitted by visitors or staff.
            for seed in ('SmokeWorld', 'DeepNorth', 'VikingHome'):
                p = ROOT / 'cache' / key_for(seed, 1024) / 'result.json'
                if not p.is_file():
                    continue
                data = json.loads(p.read_text())
                rows.append({k: data[k] for k in ['seed', 'key', 'gameVersion']})
            return self.send(200, {'seeds': rows[:50]})
        if url.path == '/status':
            key = parse_qs(url.query).get('key', [''])[0]
            if not re.fullmatch('[a-f0-9]{64}', key):
                return self.send(400, {'error': 'Invalid map request'})
            folder = ROOT / 'cache' / key
            if (folder / 'result.json').exists():
                data = json.loads((folder / 'result.json').read_text())
                return self.send(200, {'state': 'complete', 'percent': 100, 'map': data})
            if jobs.get(key) != 'running':
                return self.send(200 if folder.is_dir() else 404, {'state': 'failed', 'error': 'This map is no longer available. Generate the seed again.'})
            if (folder / 'preview-result.json').exists():
                data = json.loads((folder / 'preview-result.json').read_text())
                return self.send(200, {'state': 'preview', 'map': data})
            try:
                progress = json.loads((folder / 'progress.json').read_text())
                return self.send(200, progress)
            except (FileNotFoundError, json.JSONDecodeError):
                return self.send(200, {'state': 'queued', 'percent': 0})
        match = re.fullmatch(r'/maps/([a-f0-9]{64})/(terrain\.png|biomes\.png|locations\.json)', url.path)
        if match:
            folder = ROOT / 'cache' / match[1]
            p = folder / match[2]
            if p.is_file() and ((folder / 'result.json').exists() or
                    (p.suffix == '.png' and (folder / 'preview-result.json').exists())):
                return self.send(200, p.read_bytes(), 'image/png' if p.suffix == '.png' else 'application/json; charset=utf-8')
        self.send(404, {'error': 'Not found'})


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('VALHEIM_SEED_PORT', '8797'))), Handler).serve_forever()
