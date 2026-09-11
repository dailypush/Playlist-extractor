"""Resumable Shazam and ACRCloud scanning of local DJ recordings."""
import argparse
import asyncio
import base64
import configparser
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
import uuid
import wave
from .cache import RecognitionCache, audio_digest, namespace

from .catalog import song_key, write_csv, export, refinement_offsets, timestamp


MEDIA = {'.mp4', '.mkv', '.mov', '.flv', '.ts', '.webm', '.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac'}


def duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                             'format=duration', '-of', 'json', str(path)],
                            capture_output=True, check=True, text=True)
    seconds = float(json.loads(result.stdout)['format']['duration'])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Recording must have a finite, positive duration')
    return seconds


def sample(path, offset, length):
    return subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-ss', str(offset),
                           '-i', str(path), '-t', str(length), '-vn', '-ac', '1',
                           '-ar', '16000', '-f', 'wav', 'pipe:1'],
                          capture_output=True, check=True).stdout


def credentials(config_path, require_secret=True):
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    values = {}
    for name, default in [('host', 'identify-eu-west-1.acrcloud.com'),
                          ('access_key', ''), ('access_secret', '')]:
        values[name] = os.environ.get('ACRCLOUD_' + name.upper()) or config.get(
            'secrets', name, fallback=default)
    if require_secret and (not values['access_key'] or not values['access_secret']):
        raise ValueError('Set ACRCLOUD_ACCESS_KEY and ACRCLOUD_ACCESS_SECRET, or use config.ini')
    host = values['host']
    if not host.endswith('.acrcloud.com') or any(c in host for c in '/:@ '):
        raise ValueError('ACRCLOUD_HOST must be an ACRCloud hostname, without https://')
    return values


def recognize(audio, settings):
    if settings.get('provider') == 'shazam':
        return recognize_shazam(audio)
    timestamp = str(int(time.time()))
    signed = '\n'.join(['POST', '/v1/identify', settings['access_key'], 'audio', '1', timestamp])
    signature = base64.b64encode(hmac.new(settings['access_secret'].encode(),
                                         signed.encode(), hashlib.sha1).digest()).decode()
    fields = dict(access_key=settings['access_key'], sample_bytes=str(len(audio)),
                  timestamp=timestamp, signature=signature, data_type='audio', signature_version='1')
    boundary = uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="sample"; filename="sample.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    body.extend(audio)
    body.extend(f'\r\n--{boundary}--\r\n'.encode())
    request = urllib.request.Request('https://' + settings['host'] + '/v1/identify',
                                     data=bytes(body), headers={
                                         'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    code = result.get('status', {}).get('code')
    if code == 1001:
        return []
    if code != 0:
        reasons = {
            3001: 'Wrong Access Key; check the project access key and matching regional host',
            3003: 'Request count limit exceeded; check the account quota',
            3014: 'Invalid signature; check the project access secret',
            3015: 'Requests per second limit exceeded; increase --delay',
        }
        reason = reasons.get(code, 'check credentials, project and quota')
        raise RuntimeError(f'ACRCloud returned status {code}: {reason}')
    matches = []
    for track in result.get('metadata', {}).get('music', []):
        artists = ', '.join(a['name'] for a in track.get('artists', []))
        title = track.get('title', '')
        matches.append(dict(id=track.get('acrid') or f'{artists.casefold()}|{title.casefold()}',
                            title=title, artist=artists, score=track.get('score', 0),
                            isrc=normalize_isrc(track.get('external_ids', {}).get('isrc'))))
    return matches


def normalize_isrc(value):
    return value if isinstance(value, str) else ','.join(value or [])


def parse_shazam(result):
    if not isinstance(result, dict) or 'matches' not in result:
        raise RuntimeError('Unexpected Shazam response; sample has not been cached')
    if not result['matches']:
        return []
    track = result.get('track')
    if not track or not track.get('key') or not track.get('title'):
        raise RuntimeError('Shazam match lacks track metadata; sample has not been cached')
    return [dict(id='shazam:' + str(track['key']), title=track['title'],
                 artist=track.get('subtitle', ''), score=None, isrc=track.get('isrc', ''),
                 url=track.get('url', ''))]


def recognize_shazam(audio):
    from shazamio import Shazam
    from shazamio.client import HTTPClient
    from aiohttp_retry import ExponentialRetry

    async def identify():
        # A named WAV gives the decoder a format hint. Rewrite FFmpeg's pipe
        # header with the actual frame count before passing it to Shazam.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sample.wav'
            with wave.open(io.BytesIO(audio), 'rb') as source:
                with wave.open(str(path), 'wb') as target:
                    target.setparams(source.getparams())
                    target.setnframes(0)
                    target.writeframes(source.readframes(source.getnframes()))
            client = HTTPClient(retry_options=ExponentialRetry(attempts=1))
            return await asyncio.wait_for(Shazam(http_client=client).recognize(str(path)), timeout=45)

    try:
        return parse_shazam(asyncio.run(identify()))
    except Exception as error:
        raise RuntimeError(f'Shazam recognition failed ({type(error).__name__}): {error}') from error


def save(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temp.replace(path)


def scan(path, args, settings):
    if not args.export_only and not args.seed_cache:
        seconds = duration(path)
        offsets = [float(i) for i in range(0, math.ceil(seconds), args.interval)
                   if seconds - i >= args.sample_length]
        if not offsets:
            raise ValueError(f'{path.name}: shorter than the sample length')
        print(f'{path.name}: {timestamp(seconds)}, {len(offsets)} baseline checks, '
              f'up to {len(offsets) - 1 if args.refine else 0} extra checks', flush=True)
    if args.dry_run:
        return
    stat = path.stat()
    identity = dict(path=str(path.resolve()), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    interval=args.interval, sample_length=args.sample_length, host=settings['host'], version=1)
    if args.provider == 'shazam':
        identity['provider'] = 'shazam'
        identity['version'] = 2
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    folder = args.output / f'{path.stem}-{key}'
    checkpoint = folder / 'checkpoint.json'
    if (args.export_only or args.seed_cache) and not checkpoint.exists():
        raise ValueError('No checkpoint for these source/settings; run a scan first')
    folder.mkdir(parents=True, exist_ok=True)
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(identity=identity, results={})
    if state['identity'] != identity:
        raise ValueError('Checkpoint does not match source/settings')
    if args.export_only:
        export(state, folder, args.min_score)
        print(f'Rebuilt review files without recognition requests: {folder}', flush=True)
        return
    calls = 0
    hits = 0
    cache = RecognitionCache(args.cache or args.output / 'recognition-cache.sqlite3')
    provider_key = namespace(identity)
    state.setdefault('provenance', {})

    def run_offsets(positions):
        nonlocal calls, hits
        for offset in positions:
            if str(offset) in state['results']:
                continue
            audio = sample(path, offset, args.sample_length)
            digest = audio_digest(audio)
            matches = cache.get(provider_key, digest)
            if matches is None:
                if args.max_requests is not None and calls >= args.max_requests:
                    print('Request limit reached; rerun to resume.', flush=True)
                    return False
                if calls:
                    time.sleep(args.delay)
                calls += 1
                matches = recognize(audio, settings)
                cache.put(provider_key, digest, matches)
                origin = 'provider'
            else:
                hits += 1
                origin = 'exact_cache'
            state['results'][str(offset)] = matches
            state['provenance'][str(offset)] = dict(origin=origin, audio_digest=digest)
            save(checkpoint, state)
            print(f'  {timestamp(offset)}: {len(state["results"][str(offset)])} candidate(s)', flush=True)
        return True

    try:
        if args.seed_cache:
            for offset, matches in state['results'].items():
                # Seed positive identifications only: old no-match timestamps
                # are unknown and must not be made artificially fresh.
                if not matches:
                    continue
                digest = audio_digest(sample(path, float(offset), args.sample_length))
                cache.put(provider_key, digest, matches)
            print('Seeded shared cache from existing matched samples.', flush=True)
            return
        complete = run_offsets(offsets)
        if complete and args.refine:
            complete = run_offsets(refinement_offsets(offsets, state['results'], args.min_score))
        state['sampling'] = dict(duration_seconds=seconds, baseline_samples=len(offsets),
                                 refinement_enabled=args.refine, complete=complete)
        save(checkpoint, state)
    finally:
        cache.close()
        export(state, folder, args.min_score)
        print(f'This run: {calls} provider requests, {hits} exact-audio cache hits.', flush=True)
        print(f'Review files: {folder}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Local media file or folder (searched recursively)')
    parser.add_argument('--output', type=Path, default=Path('scan_results'))
    parser.add_argument('--cache', type=Path, help='Shared SQLite cache (default: OUTPUT/recognition-cache.sqlite3)')
    parser.add_argument('--config', type=Path, default=Path('config.ini'))
    parser.add_argument('--provider', choices=['acrcloud', 'shazam'], default='acrcloud')
    parser.add_argument('--interval', type=int, default=45, help='Seconds between baseline samples')
    parser.add_argument('--sample-length', type=int, default=12, choices=range(5, 16), metavar='5..15')
    parser.add_argument('--min-score', type=int, default=80, help='Review threshold, not a probability')
    parser.add_argument('--delay', type=float, default=3, help='Seconds between API requests (default: 3)')
    parser.add_argument('--max-requests', type=int, help='Limit new requests per recording in this run')
    parser.add_argument('--no-refine', dest='refine', action='store_false')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Estimate samples without API requests or credentials')
    mode.add_argument('--export-only', action='store_true', help='Rebuild CSVs from the checkpoint without recognition requests')
    mode.add_argument('--seed-cache', action='store_true', help='Index existing matched samples locally without provider requests')
    args = parser.parse_args(argv)
    if args.interval < args.sample_length or not math.isfinite(args.delay) or args.delay < 0:
        parser.error('Interval must be at least sample length; delay must be finite and nonnegative')
    if not 0 <= args.min_score <= 100 or (args.max_requests is not None and args.max_requests < 1):
        parser.error('Score must be 0..100 and max requests must be positive')
    if not args.export_only and (not shutil.which('ffmpeg') or not shutil.which('ffprobe')):
        parser.error('Install FFmpeg and ffprobe first')
    files = sorted(p for p in args.source.rglob('*') if p.is_file() and p.suffix.lower() in MEDIA) if args.source.is_dir() else [args.source]
    if not files or any(not p.is_file() for p in files):
        parser.error('No local media files found')
    try:
        if args.dry_run:
            settings = None
        elif args.provider == 'shazam':
            try:
                if not args.export_only and not args.seed_cache:
                    import shazamio
            except ImportError:
                raise ValueError('Install requirements-shazam.txt with Python 3.10+ first')
            settings = dict(provider='shazam', host='shazam')
        else:
            settings = credentials(args.config, require_secret=False) if args.export_only or args.seed_cache else credentials(args.config)
        for path in files:
            scan(path, args, settings)
    except subprocess.CalledProcessError as error:
        detail = error.stderr or ''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        print(f'Media could not be read: {detail.strip() or error}. '
              'Completed samples are retained.', file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, sqlite3.Error, wave.Error) as error:
        print(f'Stopped: {error}. Completed samples are retained; rerun to resume.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Rerun the same command to resume.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
