"""Bounded-memory, resumable ACRCloud scanning of local DJ recordings."""
import argparse
import asyncio
import base64
import configparser
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
import uuid
import unicodedata
import wave
from recognition_cache import RecognitionCache, audio_digest, namespace


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


def credentials(config_path):
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    values = {}
    for name, default in [('host', 'identify-eu-west-1.acrcloud.com'),
                          ('access_key', ''), ('access_secret', '')]:
        values[name] = os.environ.get('ACRCLOUD_' + name.upper()) or config.get(
            'secrets', name, fallback=default)
    if not values['access_key'] or not values['access_secret']:
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
                            isrc=','.join(track.get('external_ids', {}).get('isrc', []))))
    return matches


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


def song_key(match):
    """Group versions of the same song, while keeping different artists apart."""
    title = unicodedata.normalize('NFKC', match['title']).casefold()
    # Preserve featured artists and meaningful parentheticals. Only strip
    # explicit version labels, never arbitrary words from the song title.
    version = r'\b(remix|mix|edit|version|remaster(?:ed)?|instrumental|acapella)\b'
    title = re.sub(r'\([^()]*\)|\[[^\[\]]*\]',
                   lambda m: '' if re.search(version, m[0]) else m[0], title)
    parts = re.split(r'\s+[-–—]\s+', title)
    if len(parts) > 1 and re.search(version, parts[-1]):
        title = ' - '.join(parts[:-1])
    def normalize(value):
        value = unicodedata.normalize('NFKC', value).casefold()
        return ' '.join(re.findall(r'\w+', value))
    return normalize(match['artist']), normalize(title)


def confident_ids(matches, threshold):
    return {song_key(m) for m in matches if usable_match(m, threshold)}


def usable_match(match, threshold):
    # Shazam has no equivalent numeric confidence score. Keep it blank and
    # use repeat detections as evidence instead of inventing a score.
    return match['score'] is None or match['score'] >= threshold


def refinement_offsets(offsets, results, threshold):
    extra = set()
    for left, right in zip(offsets, offsets[1:]):
        a = confident_ids(results[str(left)], threshold)
        b = confident_ids(results[str(right)], threshold)
        if not a or not b or a != b:
            extra.add((left + right) / 2)
    return sorted(extra)


def timestamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


def write_csv(path, fields, rows):
    temp = path.with_suffix('.csv.tmp')
    with temp.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def export(state, folder, threshold):
    observations, playlist = [], {}
    for offset, matches in sorted(state['results'].items(), key=lambda item: float(item[0])):
        start = float(offset)
        common = dict(timestamp=timestamp(start), seconds=start)
        if not matches:
            observations.append(dict(common, title='', artist='', score='', status='unmatched'))
        seen = set()
        for match in matches:
            observations.append(dict(common, title=match['title'], artist=match['artist'],
                                     score=match['score'], status='candidate' if usable_match(match, threshold) else 'review'))
            key = song_key(match)
            item = playlist.setdefault(key, dict(title=match['title'], artist=match['artist'],
                first_detected=timestamp(start), last_detected=timestamp(start), detections=0,
                confident_detections=0, best_score=None, isrc=match['isrc'], variants={}))
            variant = item['variants'].setdefault(match['title'], dict(count=0, isrc=match['isrc']))
            variant['count'] += int((key, match['title']) not in seen)
            item['last_detected'] = timestamp(start)
            item['detections'] += int(key not in seen)
            scored = match['score'] is not None and match['score'] >= threshold
            item['confident_detections'] += int(scored and (key, 'scored') not in seen)
            if scored:
                seen.add((key, 'scored'))
            seen.add(key)
            seen.add((key, match['title']))
            if match['score'] is not None:
                item['best_score'] = max(item['best_score'] or 0, match['score'])
    rows = []
    for item in playlist.values():
        variants = item.pop('variants')
        title = max(variants, key=lambda t: variants[t]['count'])
        item['title'], item['isrc'] = title, variants[title]['isrc']
        item['versions_detected'] = ' | '.join(variants)
        evidence = item['detections'] if item['best_score'] is None else item['confident_detections']
        status = 'review_versions' if len(variants) > 1 else 'supported' if evidence >= 2 else 'review'
        rows.append(dict(item, status=status))
    write_csv(folder / 'observations.csv', ['timestamp', 'seconds', 'title', 'artist', 'score', 'status'], observations)
    write_csv(folder / 'playlist.csv', ['title', 'artist', 'first_detected', 'last_detected',
              'detections', 'confident_detections', 'best_score', 'isrc', 'versions_detected', 'status'], rows)


def scan(path, args, settings):
    seconds = duration(path)
    offsets = [float(i) for i in range(0, math.ceil(seconds), args.interval)]
    offsets = [offset for offset in offsets if seconds - offset >= args.sample_length]
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
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = folder / 'checkpoint.json'
    if args.export_only and not checkpoint.exists():
        raise ValueError('No checkpoint for these source/settings; run a scan first')
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
            run_offsets(refinement_offsets(offsets, state['results'], args.min_score))
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
    parser.add_argument('--seed-cache', action='store_true', help='Index existing matched samples locally without provider requests')
    parser.add_argument('--config', type=Path, default=Path('config.ini'))
    parser.add_argument('--provider', choices=['acrcloud', 'shazam'], default='acrcloud')
    parser.add_argument('--interval', type=int, default=45, help='Seconds between baseline samples')
    parser.add_argument('--sample-length', type=int, default=12, choices=range(5, 16), metavar='5..15')
    parser.add_argument('--min-score', type=int, default=80, help='Review threshold, not a probability')
    parser.add_argument('--delay', type=float, default=1, help='Seconds between API requests')
    parser.add_argument('--max-requests', type=int, help='Limit new requests per recording in this run')
    parser.add_argument('--no-refine', dest='refine', action='store_false')
    parser.add_argument('--dry-run', action='store_true', help='Estimate samples without API requests or credentials')
    parser.add_argument('--export-only', action='store_true', help='Rebuild CSVs from the checkpoint without recognition requests')
    args = parser.parse_args(argv)
    if args.interval < args.sample_length or not math.isfinite(args.delay) or args.delay < 0:
        parser.error('Interval must be at least sample length; delay must be finite and nonnegative')
    if not 0 <= args.min_score <= 100 or (args.max_requests is not None and args.max_requests < 1):
        parser.error('Score must be 0..100 and max requests must be positive')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
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
            settings = credentials(args.config)
        for path in files:
            scan(path, args, settings)
    except subprocess.CalledProcessError as error:
        detail = error.stderr or ''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        print(f'Media could not be read: {detail.strip() or error}. '
              'Completed samples are retained.', file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError) as error:
        print(f'Stopped: {error}. Completed samples are retained; rerun to resume.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Rerun the same command to resume.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
