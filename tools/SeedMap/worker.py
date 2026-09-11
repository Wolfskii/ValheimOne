#!/usr/bin/env python3
"""Serial native seed-generation worker. Game files are supplied separately."""
import argparse
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import time
import zlib

ROOT = Path(os.environ.get('VALHEIM_SEED_DATA', str(Path(__file__).resolve().parent / 'data'))).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
SERVER = Path(os.environ.get('VALHEIM_SEED_SERVER', str(ROOT / 'server'))).resolve()
MAX_CACHE_BYTES = 4 * 1024**3
MIN_FREE_BYTES = 5 * 1024**3
KEEP_RAW = os.environ.get('VALHEIM_SEED_KEEP_RAW') == '1'


def validate_seed(seed):
    if not re.fullmatch(r'[A-Za-z0-9]{1,10}', seed):
        raise ValueError('Enter 1–10 letters or numbers; capitalization matters.')
    return seed


@lru_cache(maxsize=1)
def engine_identity():
    files = [SERVER / 'valheim_server_Data/Managed/assembly_valheim.dll',
             SERVER / 'BepInEx/plugins/SeedPreviewExporter.dll']
    return tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in files)


def key_for(seed, size):
    validate_seed(seed)
    if size not in (256, 512, 1024):
        raise ValueError('Unsupported preview resolution')
    identity = [seed, str(size), *engine_identity()]
    if os.environ.get('VALHEIM_SEED_DEFER_TERRAIN') == '1':
        identity.append('deferred-terrain-control-v1')
    return hashlib.sha256('\n'.join(identity).encode()).hexdigest()


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def png_from_rgba(path, size):
    rgba = path.read_bytes()
    if len(rgba) != size * size * 4:
        raise ValueError('Incomplete rendered pixels')
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\0' + rgba[y*size*4:(y+1)*size*4] for y in range(size))
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b'')
    destination = path.with_suffix('.png')
    temporary = destination.with_suffix('.png.tmp')
    temporary.write_bytes(data)
    temporary.replace(destination)


def reserve_cache_space(current):
    candidates = []
    used = 0
    for folder in (ROOT / 'cache').iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        size = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
        used += size
        if folder != current:
            candidates.append((folder.stat().st_mtime, folder, size))
    for _, folder, size in sorted(candidates):
        if used < MAX_CACHE_BYTES - 32 * 1024**2 and shutil.disk_usage(ROOT).free >= MIN_FREE_BYTES:
            break
        shutil.rmtree(folder)
        used -= size
    if used >= MAX_CACHE_BYTES - 32 * 1024**2 or shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
        raise RuntimeError('Map storage is temporarily at capacity')


def generate(seed, size=1024):
    key = key_for(seed, size)
    folder = ROOT / 'cache' / key
    folder.mkdir(parents=True, exist_ok=True)
    with open(ROOT / 'generation.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder / 'result.json').exists():
            return json.loads((folder / 'result.json').read_text())
        reserve_cache_space(folder)
        for name in ['error.txt', 'map.json', 'progress.json', 'preview.json', 'preview-result.json']:
            (folder / name).unlink(missing_ok=True)
        work = ROOT / 'work' / key
        work.mkdir(parents=True, exist_ok=True)
        write_json(folder / 'progress.json', {'state': 'starting', 'percent': 0})
        env = dict(os.environ)
        env.update({'VO_PREVIEW_DEFER_TERRAIN': os.environ.get('VALHEIM_SEED_DEFER_TERRAIN', '0'),
                    'VO_PREVIEW_SEED': seed, 'VO_PREVIEW_SIZE': str(size), 'VO_PREVIEW_OUTPUT': str(folder),
                    'DOORSTOP_ENABLED': '1', 'DOORSTOP_TARGET_ASSEMBLY': str(SERVER / 'BepInEx/core/BepInEx.Preloader.dll'),
                    'LD_LIBRARY_PATH': str(SERVER / 'doorstop_libs') + ':' + str(SERVER / 'linux64'),
                    'LD_PRELOAD': str(SERVER / 'doorstop_libs/libdoorstop_x64.so'), 'SteamAppId': '892970'})
        cmd = [str(SERVER / 'valheim_server.x86_64'), '-nographics', '-batchmode',
               '-name', 'Seed Preview', '-port', '32456', '-world', 'SeedPreview',
               '-password', 'PreviewFixtureOnly', '-public', '0', '-savedir', str(work)]
        start = time.monotonic()
        peak_rss = 0
        first_map_seconds = None
        with open(folder / 'engine.log', 'wb') as log:
            proc = subprocess.Popen(cmd, cwd=SERVER, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, preexec_fn=lambda: os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:2])))
            write_json(folder / 'process.json', {'pid': proc.pid, 'seed': seed, 'size': size})
            try:
                while proc.poll() is None:
                    if time.monotonic() - start > 360:
                        raise TimeoutError('Map generation exceeded six minutes')
                    try:
                        status = Path(f'/proc/{proc.pid}/status').read_text()
                        match = re.search(r'^VmRSS:\s+(\d+)', status, re.M)
                        rss = int(match[1]) * 1024 if match else 0
                        peak_rss = max(peak_rss, rss)
                        if rss > 3 * 1024**3:
                            raise MemoryError('Map generation exceeded its memory budget')
                    except FileNotFoundError:
                        pass
                    if first_map_seconds is None and (folder / 'preview.json').is_file():
                        preview = json.loads((folder / 'preview.json').read_text())
                        if preview['seed'] != seed or preview['size'] != size:
                            raise ValueError('Preview identity mismatch')
                        for name in ['terrain.rgba', 'biomes.rgba']:
                            png_from_rgba(folder / name, size)
                        first_map_seconds = round(time.monotonic() - start, 2)
                        preview.update({'key': key, 'gameVersion': preview['gameVersion'].removeprefix('l-'),
                                        'locationsReady': False, 'firstMapSeconds': first_map_seconds})
                        write_json(folder / 'preview-result.json', preview)
                    time.sleep(0.5)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=10)
                shutil.rmtree(work, ignore_errors=True)
        if proc.returncode != 0 or (folder / 'error.txt').exists() or not (folder / 'map.json').exists():
            raise RuntimeError('The isolated game export did not complete; inspect its private engine log')
        meta = json.loads((folder / 'map.json').read_text())
        write_json(folder / 'run_metrics.json', {'elapsedSeconds': round(time.monotonic() - start, 2), 'peakMemoryMiB': round(peak_rss / 1024**2, 1)})
        if meta['seed'] != seed or meta['size'] != size or not re.fullmatch(r'(?:l-)?1\.0\.[0-9]+', str(meta['gameVersion'])):
            raise ValueError('Export identity or supported game version mismatch')
        if (folder / 'height.f32').stat().st_size != size * size * 4 or (folder / 'biomes.u16').stat().st_size != size * size * 2:
            raise ValueError('Incomplete sampled terrain')
        for name in ['terrain.rgba', 'biomes.rgba']:
            png_from_rgba(folder / name, size)
        meta['rawGameVersion'] = meta['gameVersion']
        meta['gameVersion'] = meta['gameVersion'].removeprefix('l-')
        meta.update({'key': key, 'elapsedSeconds': round(time.monotonic() - start, 2),
                     'peakMemoryMiB': round(peak_rss / 1024**2, 1), 'locationsReady': True,
                     'firstMapSeconds': first_map_seconds,
                     'locationCount': len(json.loads((folder / 'locations.json').read_text()))})
        write_json(folder / 'result.json', meta)
        write_json(folder / 'progress.json', {'state': 'complete', 'percent': 100})
        if not KEEP_RAW:
            for name in ['height.f32', 'biomes.u16', 'terrain.rgba', 'biomes.rgba', 'engine.log', 'process.json']:
                (folder / name).unlink(missing_ok=True)
        return meta


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('seed')
    parser.add_argument('--size', type=int, default=1024)
    args = parser.parse_args()
    print(json.dumps(generate(args.seed, args.size)))
