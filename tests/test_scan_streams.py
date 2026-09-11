import csv
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

import scan_streams as scanner


def track(key='a', score=95):
    return dict(id=key, title=key, artist='Artist', score=score, isrc='')


def fake_sample(path, offset, length):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        output.writeframes(int(offset * 2).to_bytes(2, 'little') * 100)
    return buffer.getvalue()


class ScannerTests(unittest.TestCase):
    def test_probe_rejects_missing_or_invalid_duration(self):
        for payload in ({}, {'format': {}}, {'format': {'duration': 'N/A'}},
                        {'format': {'duration': None}}, {'format': {'duration': 'NaN'}}):
            with self.subTest(payload=payload), patch.object(scanner.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))):
                with self.assertRaises(ValueError):
                    scanner.duration(Path('mix.mp4'))

    def test_media_processes_have_timeouts_and_use_recoverable_errors(self):
        for operation, args, timeout in (
            (scanner.duration, (Path('mix.mp4'),), scanner.PROBE_TIMEOUT),
            (scanner.sample, (Path('mix.mp4'), 45, 12), scanner.SAMPLE_TIMEOUT),
        ):
            with self.subTest(operation=operation.__name__), patch.object(scanner.subprocess, 'run',
                    side_effect=subprocess.TimeoutExpired('media tool', timeout)) as process:
                with self.assertRaisesRegex(ValueError, 'timed out'):
                    operation(*args)
                self.assertEqual(process.call_args.kwargs['timeout'], timeout)

    def test_removed_provider_is_rejected_and_default_uses_shazam(self):
        with patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                scanner.main(['unused.mp4', '--provider', 'acrcloud'])
            self.assertEqual(error.exception.code, 2)
        with patch.object(scanner, 'recognize_shazam', return_value=[]) as identify:
            self.assertEqual(scanner.recognize(b'audio', {'provider': 'shazam'}), [])
            identify.assert_called_once_with(b'audio')

    def test_merge_versions_without_losing_evidence(self):
        original = dict(track(), title='Breathe (feat. Jem Cooke)', isrc='original')
        remix = dict(track('remix'), title='Breathe (feat. Jem Cooke) [Eric Prydz Remix]', isrc='remix')
        results = {'0.0': [original, original], '45.0': [remix], '90.0': [original]}
        with tempfile.TemporaryDirectory() as temp:
            scanner.export({'results': results}, Path(temp), 80)
            with (Path(temp) / 'playlist.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['detections'], '3')
            self.assertEqual(rows[0]['confident_detections'], '3')
            self.assertEqual(rows[0]['title'], original['title'])
            self.assertEqual(rows[0]['isrc'], 'original')
            self.assertEqual(rows[0]['status'], 'review_versions')
            self.assertIn(remix['title'], rows[0]['versions_detected'])
        self.assertEqual(scanner.refinement_offsets([0.0, 45.0, 90.0], results, 80), [])

    def test_grouping_preserves_different_artists_and_meaningful_titles(self):
        a = dict(track(), title='Dreams')
        self.assertNotEqual(scanner.song_key(a), scanner.song_key(dict(a, artist='Another Artist')))
        self.assertNotEqual(scanner.song_key(a), scanner.song_key(dict(a, title='Dreams (Part Two)')))
        self.assertEqual(scanner.song_key(a), scanner.song_key(dict(a, title=' DREAMS - Radio Edit')))

    def test_export_only_uses_cached_results_without_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mix.mp4'
            source.touch()
            args = [str(source), '--output', str(Path(temp) / 'out'), '--provider', 'shazam', '--delay', '0']
            # No shazamio installation is required for rebuilding exports.
            with patch.object(scanner, 'duration', return_value=60), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/bin/tool'), patch.object(scanner, 'recognize', return_value=[track()]), patch.dict('sys.modules', {'shazamio': object()}):
                self.assertEqual(scanner.main(args), 0)
            with patch.object(scanner, 'duration', side_effect=AssertionError('No media probe needed')), patch.object(scanner.shutil, 'which', return_value=None), patch.object(scanner, 'recognize') as api, patch.object(scanner, 'sample') as extract, patch.dict('sys.modules', {'shazamio': None}):
                self.assertEqual(scanner.main(args + ['--export-only']), 0)
                api.assert_not_called()
                extract.assert_not_called()


    def test_seed_cache_reuses_saved_digests_without_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mix.mp4'
            source.touch()
            args = [str(source), '--output', str(Path(temp) / 'out'), '--delay', '0']
            with patch.object(scanner, 'duration', return_value=60), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/tool'), patch.object(scanner, 'recognize', return_value=[track()]):
                self.assertEqual(scanner.main(args), 0)
            with patch.object(scanner, 'sample') as extract, patch.object(scanner, 'recognize') as api, patch.object(scanner.shutil, 'which', return_value='/tool'):
                self.assertEqual(scanner.main(args + ['--seed-cache']), 0)
                extract.assert_not_called()
                api.assert_not_called()

    def test_failed_refinement_clears_old_completion_status(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mix.mp4'
            source.touch()
            args = [str(source), '--output', str(Path(temp) / 'out'), '--delay', '0']
            with patch.object(scanner, 'duration', return_value=60), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/tool'):
                with patch.object(scanner, 'recognize', return_value=[]):
                    self.assertEqual(scanner.main(args + ['--no-refine']), 0)
                checkpoint = next((Path(temp) / 'out').glob('*/checkpoint.json'))
                self.assertTrue(json.loads(checkpoint.read_text())['sampling']['complete'])
                with patch.object(scanner, 'recognize', side_effect=OSError('offline')):
                    self.assertEqual(scanner.main(args), 1)
                self.assertFalse(json.loads(checkpoint.read_text())['sampling']['complete'])


    def test_shazam_matches_have_no_invented_score(self):
        match = scanner.parse_shazam({'matches': [{'id': '123'}], 'track': {
            'key': '123', 'title': 'Song', 'subtitle': 'Artist', 'isrc': 'US123'}})[0]
        self.assertIsNone(match['score'])
        self.assertEqual(match['id'], 'shazam:123')
        with tempfile.TemporaryDirectory() as temp:
            scanner.export({'results': {'0.0': [match], '45.0': [match]}}, Path(temp), 80)
            with (Path(temp) / 'playlist.csv').open() as f:
                row = next(csv.DictReader(f))
            self.assertEqual(row['best_score'], '')
            self.assertEqual(row['confident_detections'], '0')
            self.assertEqual(row['status'], 'supported')
        self.assertEqual(scanner.parse_shazam({'matches': []}), [])
        with self.assertRaises(RuntimeError):
            scanner.parse_shazam({'error': 'rate limited'})
        with self.assertRaises(RuntimeError):
            scanner.parse_shazam({'matches': [{'id': '123'}]})

    def test_refine_changes_unmatched_and_low_confidence(self):
        offsets = [0.0, 45.0, 90.0, 135.0, 180.0]
        results = {'0.0': [track()], '45.0': [track()], '90.0': [track('b')],
                   '135.0': [], '180.0': [track('b', 40)]}
        self.assertEqual(scanner.refinement_offsets(offsets, results, 80), [67.5, 112.5, 157.5])

    def test_exports_keep_unmatched_and_repeat_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            scanner.export({'results': {'0.0': [track()], '45.0': [],
                           '90.0': [track('b', 40)], '135.0': [track()]}}, folder, 80)
            with (folder / 'playlist.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['status'], 'supported')
            self.assertEqual(rows[0]['last_detected'], '00:02:15')
            self.assertEqual(rows[1]['status'], 'review')
            with (folder / 'observations.csv').open() as f:
                self.assertIn('unmatched', f.read())

    def test_resume_skips_paid_samples_and_retries_failed_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mix.mp4'
            source.touch()
            args = [str(source), '--output', str(Path(temp) / 'out'), '--delay', '0']
            with patch.object(scanner, 'duration', return_value=100), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/bin/tool'), patch.object(scanner, 'recognize', side_effect=[[track()], OSError('network failure')]) as api:
                self.assertEqual(scanner.main(args), 1)
                self.assertEqual(api.call_count, 2)
            with patch.object(scanner, 'duration', return_value=100), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/bin/tool'), patch.object(scanner, 'recognize', return_value=[track()]) as api:
                self.assertEqual(scanner.main(args), 0)
                self.assertEqual(api.call_count, 1)

    def test_request_limit_and_refinement_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mix.mp4'
            source.touch()
            args = [str(source), '--output', str(Path(temp) / 'out'), '--delay', '0', '--max-requests', '2']
            with patch.object(scanner, 'duration', return_value=100), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/bin/tool'), patch.object(scanner, 'recognize', return_value=[]) as api:
                self.assertEqual(scanner.main(args), 0)
                self.assertEqual(api.call_count, 2)
                self.assertEqual(scanner.main(args), 0)
                self.assertEqual(api.call_count, 3)



if __name__ == '__main__':
    unittest.main()
