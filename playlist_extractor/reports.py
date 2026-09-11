"""Offline song recurrence and recording-overlap reports from scan checkpoints."""
import argparse
from collections import defaultdict
from itertools import combinations
import json
import hashlib
from pathlib import Path

from .catalog import song_key, write_csv


def build_report(root, output):
    # Separate replaced files before choosing the most extensive settings scan.
    # These are provisional recording identities, not Twitch session IDs.
    selected = {}
    for path in sorted(root.rglob('checkpoint.json')):
        state = json.loads(path.read_text())
        identity = state['identity']
        if identity.get('provider') == 'shazam' and identity.get('version', 0) < 2:
            continue  # Discard the known-bad early decoder experiment.
        if not state['results']:
            continue
        source = identity['path']
        recording = (source, identity.get('size'), identity.get('mtime_ns'))
        previous = selected.get(recording)
        if previous is None or len(state['results']) > len(previous['results']):
            selected[recording] = state
    output.mkdir(parents=True, exist_ok=True)
    songs = defaultdict(dict)
    session_songs = {}
    session_rows = []
    for (source, size, mtime_ns), state in sorted(selected.items(), key=lambda item: repr(item[0])):
        # Preserve path-only IDs for legacy checkpoints without file metadata.
        session_key = source if size is None and mtime_ns is None else json.dumps([source, size, mtime_ns])
        session = 'recording-' + hashlib.sha256(session_key.encode()).hexdigest()[:16]
        keys = set()
        matched = 0
        for offset, matches in state['results'].items():
            matched += bool(matches)
            for match in matches:
                key = song_key(match)
                keys.add(key)
                entry = songs[key].setdefault(session, dict(title=match['title'], artist=match['artist'], offsets=set()))
                entry['offsets'].add(float(offset))
        session_songs[session] = keys
        session_rows.append(dict(recording_id=session, source=source, size_bytes=size, mtime_ns=mtime_ns,
                                 samples=len(state['results']),
                                 matched_samples=matched, candidate_songs=len(keys),
                                 last_sample_seconds=max(map(float, state['results'])),
                                 checkpoint_provider=state['identity'].get('provider', 'acrcloud')))
    song_rows = []
    for entries in songs.values():
        example = next(iter(entries.values()))
        song_rows.append(dict(title=example['title'], artist=example['artist'], recordings=len(entries),
                              detections=sum(len(e['offsets']) for e in entries.values()),
                              recording_ids=' | '.join(entries)))
    song_rows.sort(key=lambda r: (-r['recordings'], -r['detections'], r['artist'], r['title']))
    def overlaps():
        # Write comparisons as a stream instead of retaining O(n²) rows in memory.
        for a, b in combinations(session_songs, 2):
            common = session_songs[a] & session_songs[b]
            union = session_songs[a] | session_songs[b]
            yield dict(recording_a=a, recording_b=b, shared_songs=len(common),
                       union_songs=len(union), jaccard=round(len(common) / len(union), 4) if union else '')
    write_csv(output / 'recordings.csv', ['recording_id', 'source', 'size_bytes', 'mtime_ns', 'samples', 'matched_samples',
              'candidate_songs', 'last_sample_seconds', 'checkpoint_provider'], session_rows)
    write_csv(output / 'song_frequency.csv', ['title', 'artist', 'recordings', 'detections', 'recording_ids'], song_rows)
    write_csv(output / 'session_overlap.csv', ['recording_a', 'recording_b', 'shared_songs', 'union_songs', 'jaccard'], overlaps())
    (output / 'README.md').write_text(f'''# Recording patterns

{len(selected)} recording(s); {len(songs)} distinct candidate songs.

song_frequency.csv counts recordings containing a song, rather than treating
repeated samples as repeated plays. session_overlap.csv compares shared songs;
Jaccard is shared songs divided by all distinct songs across the two recordings.

These are detected candidates, including uncertain versions and partial scans,
not verified playlists or exact play counts. Missing matches do not prove that
a song was absent. Multiple files from one Twitch session are currently separate
recordings; split files and renamed copies need session IDs before drawing
session-level conclusions. Recordings are identified by source path, size and
modification time, preserving replaced files as separate recordings. For each
recording, only the checkpoint with the most sampled offsets is used. Provider
results are not double-counted. Renaming, copying or touching a file can still
split one recording into multiple identities until stable session IDs are added.

At least two recordings are needed for cross-recording comparisons.
''')
    return len(selected), len(songs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('scan_results'))
    parser.add_argument('--output', type=Path, default=Path('scan_results/reports'))
    args = parser.parse_args(argv)
    sessions, songs = build_report(args.input, args.output)
    print(f'Report: {sessions} recording(s), {songs} candidate songs -> {args.output}')

    return 0
