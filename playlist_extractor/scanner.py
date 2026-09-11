"""Resumable Shazam scanning of local DJ recordings."""
import argparse
import asyncio
from concurrent.futures import CancelledError
from contextlib import nullcontext
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import wave
from .cache import RecognitionCache, audio_digest, namespace
from .locking import output_lock
from .prefetch import SamplePreparation

from .catalog import song_key, write_csv, export, refinement_offsets, timestamp


MEDIA = {'.mp4', '.mkv', '.mov', '.flv', '.ts', '.webm', '.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac'}
PROBE_TIMEOUT = 60
SAMPLE_TIMEOUT = 120


def duration(path):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                 'format=duration', '-of', 'json', str(path)],
                                capture_output=True, check=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f'{path.name}: media probe timed out after {PROBE_TIMEOUT}s') from error
    try:
        seconds = float(json.loads(result.stdout)['format']['duration'])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f'{path.name}: media duration is missing or invalid') from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Recording must have a finite, positive duration')
    return seconds


def sample(path, offset, length, cancel_event=None):
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-ss', str(offset),
               '-i', str(path), '-t', str(length), '-vn', '-ac', '1',
               '-ar', '16000', '-f', 'wav', 'pipe:1']
    try:
        if cancel_event is None:
            return subprocess.run(command, capture_output=True, check=True, timeout=SAMPLE_TIMEOUT).stdout
        if cancel_event.is_set():
            raise CancelledError()
        # Poll communicate so cancellation drains pipes and reaps FFmpeg promptly.
        # The sequential path retains subprocess.run's own timeout cleanup.
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            deadline = time.monotonic() + SAMPLE_TIMEOUT
            try:
                while True:
                    if cancel_event.is_set():
                        raise CancelledError()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, SAMPLE_TIMEOUT)
                    try:
                        audio, errors = process.communicate(timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, audio, errors)
                return audio
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
    except subprocess.TimeoutExpired as error:
        raise ValueError(f'{path.name}: audio extraction at {offset}s timed out after {SAMPLE_TIMEOUT}s') from error


def recognize(audio, settings):
    """Provider boundary retained for future recognition backends."""
    return recognize_shazam(audio)


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
    try:
        from shazamio import Shazam
        from shazamio.client import HTTPClient
        from aiohttp_retry import ExponentialRetry
    except ImportError as error:
        raise RuntimeError('Install requirements-shazam.txt with Python 3.11+ first') from error

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


def scan(path, args, settings, event_handler=None):
    def emit(kind, **data):
        if event_handler:
            event_handler(dict(kind=kind, source=str(path), **data))
        elif kind == 'message':
            print(data['text'], flush=True)
    if not args.export_only and not args.seed_cache:
        seconds = duration(path)
        offsets = [float(i) for i in range(0, math.ceil(seconds), args.interval)
                   if seconds - i >= args.sample_length]
        if not offsets:
            raise ValueError(f'{path.name}: shorter than the sample length')
        emit('message', text=f'{path.name}: {timestamp(seconds)}, {len(offsets)} baseline checks, '
             f'up to {len(offsets) - 1 if args.refine else 0} extra checks')
    if args.dry_run:
        return
    stat = path.stat()
    identity = dict(path=str(path.resolve()), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    interval=args.interval, sample_length=args.sample_length, host='shazam', version=2, provider='shazam')
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
        emit('message', text=f'Rebuilt review files without recognition requests: {folder}')
        emit('exported', folder=str(folder))
        return
    calls = 0
    hits = 0
    cache = RecognitionCache(args.cache or args.output / 'recognition-cache.sqlite3')
    provider_key = namespace(identity)
    state.setdefault('provenance', {})
    known_songs = {song_key(m) for matches in state['results'].values() for m in matches}
    resumed = len(state['results'])

    def prepare(offset, cancel_event):
        if cancel_event is None:
            audio = sample(path, offset, args.sample_length)
        else:
            audio = sample(path, offset, args.sample_length, cancel_event=cancel_event)
        return audio, audio_digest(audio)

    def run_offsets(positions, phase):
        nonlocal calls, hits
        done = sum(str(offset) in state['results'] for offset in positions)
        def progress(**extra):
            emit('progress', phase=phase, done=done, total=len(positions),
                 requests=calls, cache_hits=hits, resumed=resumed,
                 songs=len(known_songs), **extra)
        progress(activity='Starting phase')
        pending = [offset for offset in positions if str(offset) not in state['results']]
        with SamplePreparation(pending, prepare, threaded=getattr(args, 'threaded', False)) as prepared:
            for offset in pending:
                progress(activity='Extracting audio', offset=offset)
                audio, digest = next(prepared)
                matches = cache.get(provider_key, digest)
                if matches is None:
                    if args.max_requests is not None and calls >= args.max_requests:
                        emit('message', text='Request limit reached; rerun to resume.')
                        return False
                    if calls:
                        progress(activity='Pacing requests', offset=offset)
                        time.sleep(args.delay)
                    calls += 1
                    progress(activity='Recognizing audio', offset=offset)
                    matches = recognize(audio, settings)
                    cache.put(provider_key, digest, matches)
                    origin = 'provider'
                else:
                    hits += 1
                    origin = 'exact_cache'
                state['results'][str(offset)] = matches
                state['provenance'][str(offset)] = dict(origin=origin, audio_digest=digest)
                save(checkpoint, state)
                known_songs.update(song_key(m) for m in matches)
                done += 1
                progress(activity='Sample saved', offset=offset, matches=matches, origin=origin)
                if not event_handler:
                    print(f'  {timestamp(offset)}: {len(matches)} candidate(s)', flush=True)
                del audio
        return True

    try:
        if args.seed_cache:
            for offset, matches in state['results'].items():
                # Seed positive identifications only: old no-match timestamps
                # are unknown and must not be made artificially fresh.
                if not matches:
                    continue
                provenance = state['provenance'].get(offset, {})
                digest = provenance.get('audio_digest')
                if digest is None:
                    digest = audio_digest(sample(path, float(offset), args.sample_length))
                    state['provenance'][offset] = dict(origin='checkpoint', audio_digest=digest)
                if cache.get(provider_key, digest) is None:
                    cache.put(provider_key, digest, matches)
            save(checkpoint, state)
            emit('message', text='Seeded shared cache from existing matched samples.')
            return
        # An earlier baseline-only run may be complete while this run's new
        # refinement settings are not. Persist that distinction before requests.
        state['sampling'] = dict(duration_seconds=seconds, baseline_samples=len(offsets),
                                 refinement_enabled=args.refine, min_score=args.min_score,
                                 complete=False)
        save(checkpoint, state)
        if getattr(args, 'threaded', False):
            emit('message', text='Threaded audio preparation: one worker, one sample ahead; Shazam requests remain sequential.')
        complete = run_offsets(offsets, 'Baseline')
        if complete and args.refine:
            complete = run_offsets(refinement_offsets(offsets, state['results'], args.min_score), 'Refinement')
        state['sampling']['complete'] = complete
        save(checkpoint, state)
        emit('finished', complete=complete, requests=calls, cache_hits=hits,
             resumed=resumed, songs=len(known_songs))
    finally:
        cache.close()
        export(state, folder, args.min_score)
        emit('message', text=f'This run: {calls} provider requests, {hits} exact-audio cache hits.')
        emit('message', text=f'Review files: {folder}')
        emit('exported', folder=str(folder))


def main(argv=None, event_handler=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Local media file or folder (searched recursively)')
    parser.add_argument('--output', type=Path, default=Path('scan_results'))
    parser.add_argument('--cache', type=Path, help='Shared SQLite cache (default: OUTPUT/recognition-cache.sqlite3)')
    parser.add_argument('--provider', choices=['shazam'], default='shazam', help=argparse.SUPPRESS)
    parser.add_argument('--interval', type=int, default=45, help='Seconds between baseline samples')
    parser.add_argument('--sample-length', type=int, default=12, choices=range(5, 16), metavar='5..15')
    parser.add_argument('--min-score', type=int, default=80, help='Review threshold, not a probability')
    parser.add_argument('--delay', type=float, default=3, help='Seconds between API requests (default: 3)')
    parser.add_argument('--max-requests', type=int, help='Limit new requests per recording in this run')
    parser.add_argument('--no-refine', dest='refine', action='store_false')
    parser.add_argument('--threaded', action='store_true', help='Prepare one sample ahead in a background thread; recognition remains sequential')
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
        settings = dict(provider='shazam', host='shazam')
        with nullcontext() if args.dry_run else output_lock(args.output):
            for path in files:
                scan(path, args, settings, event_handler=event_handler)
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
