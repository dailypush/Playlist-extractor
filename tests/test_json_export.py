import json
from pathlib import Path
import tempfile
import unittest

from playlist_extractor.catalog import export


class JsonExportTests(unittest.TestCase):
    def test_source_versions_and_fractional_timestamps_are_preserved(self):
        match = dict(id='one', title='Écho | Night', artist='Artist', score=None, isrc='TEST')
        remix = dict(match, id='two', title='Écho | Night [Club Remix]')
        state = dict(identity=dict(path='/recordings/Pyka — Session.mp4', size=1234,
                     mtime_ns=987, provider='shazam', interval=45, sample_length=12),
                     sampling=dict(duration_seconds=120, complete=False, refinement_enabled=True),
                     results={'22.5': [match, match], '45.0': [remix], '90.0': []})
        with tempfile.TemporaryDirectory() as temp:
            export(state, Path(temp), 80)
            result = json.loads((Path(temp) / 'playlist.json').read_text())
        self.assertEqual(result['source']['path'], state['identity']['path'])
        self.assertEqual(result['source']['filename'], 'Pyka — Session.mp4')
        self.assertFalse(result['recognition']['sampling_complete'])
        self.assertEqual(result['summary'], dict(candidate_songs=1, samples_processed=3, matched_samples=2))
        song = result['playlist'][0]
        self.assertEqual(song['versions_detected'], [match['title'], remix['title']])
        self.assertEqual(song['detection_seconds'], [22.5, 45.0])
        self.assertEqual(song['first_detected_seconds'], 22.5)
        self.assertEqual(song['detections'], 2)
        self.assertIsNone(song['best_score'])
        self.assertEqual(song['status'], 'review_versions')

    def test_empty_results_do_not_claim_scan_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            export({'results': {}}, Path(temp), 80)
            result = json.loads((Path(temp) / 'playlist.json').read_text())
        self.assertEqual(result['playlist'], [])
        self.assertIsNone(result['recognition']['sampling_complete'])
