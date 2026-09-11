"""Song grouping, playlist exports, and refinement planning."""
import csv
import json
from pathlib import Path
import re
import unicodedata


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
    # Shazam has no numeric confidence score. Keep it blank and
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
    detection_times = {}
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
            detection_times.setdefault(key, set()).add(start)
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
    rows, json_rows = [], []
    for key, item in playlist.items():
        variants = item.pop('variants')
        title = max(variants, key=lambda t: variants[t]['count'])
        item['title'], item['isrc'] = title, variants[title]['isrc']
        item['versions_detected'] = ' | '.join(variants)
        evidence = item['detections'] if item['best_score'] is None else item['confident_detections']
        status = 'review_versions' if len(variants) > 1 else 'supported' if evidence >= 2 else 'review'
        rows.append(dict(item, status=status))
        times = sorted(detection_times[key])
        json_rows.append(dict(item, status=status, versions_detected=list(variants),
                              first_detected_seconds=times[0], last_detected_seconds=times[-1],
                              detection_seconds=times))
    write_csv(folder / 'observations.csv', ['timestamp', 'seconds', 'title', 'artist', 'score', 'status'], observations)
    write_csv(folder / 'playlist.csv', ['title', 'artist', 'first_detected', 'last_detected',
              'detections', 'confident_detections', 'best_score', 'isrc', 'versions_detected', 'status'], rows)
    identity = state.get('identity', {})
    sampling = state.get('sampling', {})
    source_path = identity.get('path')
    document = {
        'schema_version': 1,
        'source': {
            'path': source_path,
            'filename': Path(source_path).name if source_path else None,
            'size_bytes': identity.get('size'),
            'mtime_ns': identity.get('mtime_ns'),
            'duration_seconds': sampling.get('duration_seconds'),
        },
        'recognition': {
            'provider': identity.get('provider'),
            'sample_length_seconds': identity.get('sample_length'),
            'sample_interval_seconds': identity.get('interval'),
            'sampling_complete': sampling.get('complete'),
            'refinement_enabled': sampling.get('refinement_enabled'),
            'review_score_threshold': threshold,
        },
        'summary': {
            'candidate_songs': len(json_rows),
            'samples_processed': len(state['results']),
            'matched_samples': sum(bool(matches) for matches in state['results'].values()),
        },
        'playlist': json_rows,
    }
    temp = folder / 'playlist.json.tmp'
    temp.write_text(json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(folder / 'playlist.json')

