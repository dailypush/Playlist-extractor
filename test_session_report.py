import csv
import json
from pathlib import Path
import tempfile
import unittest

from session_report import build_report
from test_scan_streams import track


class ReportTests(unittest.TestCase):
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
