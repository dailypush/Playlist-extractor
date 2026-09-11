import csv
import json
from pathlib import Path
import tempfile
import unittest

from session_report import build_report
from test_scan_streams import track


class ReportTests(unittest.TestCase):
    def test_replaced_source_preserves_both_recordings_and_deduplicates_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, mtime, results in (
                ('old', 1, {'0': [track('old')], '45': [track('old')]}),
                ('new', 2, {'0': [track('new')]}),
                ('old-other-settings', 1, {'0': [track('old')]}),
            ):
                folder = root / name
                folder.mkdir()
                state = dict(identity=dict(path='/mix.mp4', size=123, mtime_ns=mtime), results=results)
                (folder / 'checkpoint.json').write_text(json.dumps(state))
            self.assertEqual(build_report(root, root / 'reports'), (2, 2))
            with (root / 'reports/recordings.csv').open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len({row['recording_id'] for row in rows}), 2)
            self.assertEqual({row['mtime_ns']: row['samples'] for row in rows}, {'1': '2', '2': '1'})

    def test_recording_id_stays_stable_when_another_recording_is_added(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'z'
            folder.mkdir()
            state = dict(identity=dict(path='/z.mp4'), results={'0': [track()]})
            (folder / 'checkpoint.json').write_text(json.dumps(state))
            build_report(root, root / 'reports')
            with (root / 'reports/recordings.csv').open() as f:
                before = next(csv.DictReader(f))['recording_id']
            folder = root / 'a'
            folder.mkdir()
            state['identity']['path'] = '/a.mp4'
            (folder / 'checkpoint.json').write_text(json.dumps(state))
            build_report(root, root / 'reports')
            with (root / 'reports/recordings.csv').open() as f:
                after = next(r['recording_id'] for r in csv.DictReader(f) if r['source'] == '/z.mp4')
            self.assertEqual(before, after)

    def test_recurrence_does_not_count_samples_or_checkpoints_as_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, source, results in [('a', '/one.mp4', {'0': [track()], '45': [track()]}),
                                          ('a2', '/one.mp4', {'0': [track()]}),
                                          ('b', '/two.mp4', {'0': [track()], '45': [track('b')]})]:
                folder = root / name
                folder.mkdir()
                (folder / 'checkpoint.json').write_text(json.dumps(dict(identity=dict(path=source), results=results)))
            self.assertEqual(build_report(root, root / 'reports'), (2, 2))
            with (root / 'reports/song_frequency.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]['recordings'], '2')
            self.assertEqual(rows[0]['detections'], '3')
            with (root / 'reports/session_overlap.csv').open() as f:
                row = next(csv.DictReader(f))
            self.assertEqual(row['jaccard'], '0.5')
