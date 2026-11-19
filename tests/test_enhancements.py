from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playlist_extractor import batch, scanner
from playlist_extractor.metrics import ScanMetrics
from test_scan_streams import fake_sample, track


class EnhancementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'mix.mp4'
        self.source.touch()
        self.output = self.root / 'out'
        self.args = [str(self.source), '--output', str(self.output), '--delay', '0', '--no-refine']
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scanner.shutil, 'which', return_value='/tool'))
        self.stack.enter_context(patch.object(scanner, 'duration', return_value=150))
        self.stack.enter_context(patch.object(scanner, 'sample', side_effect=lambda p, o, l, **kw: fake_sample(p, o, l)))
        self.clock = self.stack.enter_context(patch.object(scanner.time, 'time', return_value=1_800_000_000))
        self.stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))

    def folder(self):
        return next(self.output.glob('*/playlist.json')).parent

    def state(self):
        return scanner.load_state(self.folder())

    def initial(self, command=scanner.main):
        with patch.object(scanner, 'recognize', side_effect=[[track()], [], [], []]):
            self.assertEqual(command(self.args), 0)

    def test_default_resume_and_fresh_negatives_do_not_make_requests(self):
        self.initial()
        with patch.object(scanner, 'recognize') as api, patch.object(scanner, 'sample') as extract:
            self.assertEqual(scanner.main(self.args + ['--retry-unmatched']), 0)
            self.clock.return_value += 8 * 86400
            self.assertEqual(scanner.main(self.args), 0)
        api.assert_not_called()
        extract.assert_not_called()

    def test_aged_retry_budget_and_resume_keep_positive_evidence(self):
        self.initial()
        positive = self.state()['provenance']['0.0']
        self.clock.return_value += 8 * 86400
        retry_args = self.args + ['--retry-unmatched', '--max-requests', '1']
        with patch.object(scanner, 'recognize', return_value=[]) as api:
            self.assertEqual(scanner.main(retry_args), 0)
            self.assertEqual(api.call_count, 1)
            self.assertFalse(self.state()['sampling']['complete'])
            self.clock.return_value += 1
            self.assertEqual(scanner.main(retry_args), 0)
            self.assertEqual(api.call_count, 2)
        with patch.object(scanner, 'recognize', return_value=[track('new')]) as api:
            self.clock.return_value += 1
            self.assertEqual(scanner.main(retry_args), 0)
            self.assertEqual(api.call_count, 1)
        state = self.state()
        self.assertTrue(state['sampling']['complete'])
        self.assertEqual(state['results']['135.0'], [track('new')])
        self.assertEqual(state['provenance']['0.0'], positive)

    def test_retry_error_preserves_old_evidence_and_resumes(self):
        self.initial()
        old = self.state()
        self.clock.return_value += 8 * 86400
        with patch.object(scanner, 'recognize', side_effect=RuntimeError('offline')):
            self.assertEqual(scanner.main(self.args + ['--retry-unmatched']), 1)
        self.assertEqual(self.state()['results'], old['results'])
        self.assertEqual(self.state()['provenance'], old['provenance'])
        self.assertFalse(self.state()['sampling']['complete'])
        self.assertEqual(json.loads((self.folder() / 'metrics.json').read_text())['requests'], 1)
        self.clock.return_value += 1
        with patch.object(scanner, 'recognize', return_value=[track('new')]) as api:
            self.assertEqual(scanner.main(self.args + ['--retry-unmatched', '--threaded']), 0)
        self.assertEqual(api.call_count, 3)

    def test_legacy_negatives_use_recent_cache_without_resetting_its_age(self):
        self.initial()
        state = self.state()
        for provenance in state['provenance'].values():
            provenance.pop('recognized_at')
        scanner.save(self.folder() / 'checkpoint.json', state)
        self.clock.return_value += 86400
        with patch.object(scanner, 'recognize') as api:
            self.assertEqual(scanner.main(self.args + ['--retry-unmatched']), 0)
        api.assert_not_called()
        self.assertEqual(self.state()['provenance']['45.0']['recognized_at'], 1_800_000_000)

    def test_force_retry_campaign_resumes_across_batch_request_caps(self):
        self.initial(batch.main)
        self.clock.return_value += 1
        args = self.args + ['--retry-unmatched', '--unmatched-min-age-days', '0', '--max-requests', '1']
        def negative(audio, settings):
            self.clock.return_value += 1
            return []
        with patch.object(scanner, 'recognize', side_effect=negative) as api:
            for expected in (1, 2, 3):
                self.assertEqual(batch.main(args), 0)
                self.assertEqual(api.call_count, expected)
                self.clock.return_value += 1
        queue = json.loads(next(self.output.glob('batches/*/queue.json')).read_text())
        self.assertEqual(queue['last_run']['status'], 'complete')
        self.assertTrue(queue['unmatched_retry']['complete'])
        self.assertTrue(self.state()['sampling']['complete'])

    def test_force_retry_scanner_retains_cutoff_until_complete(self):
        self.initial()
        self.clock.return_value += 1
        args = self.args + ['--retry-unmatched', '--unmatched-min-age-days', '0', '--max-requests', '1']
        def negative(audio, settings):
            self.clock.return_value += 1
            return []
        with patch.object(scanner, 'recognize', side_effect=negative) as api:
            for expected in (1, 2, 3):
                self.assertEqual(scanner.main(args), 0)
                self.assertEqual(api.call_count, expected)
                self.clock.return_value += 1
        self.assertTrue(self.state()['sampling']['complete'])

    def test_queue_coalesces_cosmetic_updates_and_persists_saved_samples(self):
        writes = []
        real_save = scanner.save
        def save(path, data):
            if path.name == 'queue.json':
                writes.append(json.loads(json.dumps(data)))
            return real_save(path, data)
        with patch.object(scanner, 'save', side_effect=save), patch.object(scanner, 'recognize', return_value=[track()]), patch.object(batch, 'PROGRESS_SAVE_SECONDS', 3600):
            self.assertEqual(batch.main(self.args), 0)
        progress = [q['files'][str(self.source.resolve())].get('progress') for q in writes]
        activities = [p['activity'] for p in progress if p]
        self.assertNotIn('Recognizing audio', activities)
        saved_counts = {p['done'] for p in progress if p and p['activity'] == 'Sample saved'}
        self.assertEqual(saved_counts, {1, 2, 3, 4})
        self.assertIn('metrics', writes[-1]['files'][str(self.source.resolve())])
        # Four requests previously generated 16 activity writes plus four lifecycle writes.
        self.assertLess(len(writes), 20)

    def test_metrics_are_separate_and_failures_do_not_discard_results(self):
        events = []
        with patch.object(scanner, 'recognize', return_value=[track()]):
            self.assertEqual(scanner.main(self.args, event_handler=events.append), 0)
        metrics = json.loads((self.folder() / 'metrics.json').read_text())
        self.assertEqual(metrics['requests'], 4)
        self.assertEqual(metrics['prepared_samples'], 4)
        self.assertEqual(metrics['cache_hits'], 0)
        self.assertGreater(metrics['process_peak_rss_mib'], 0)
        self.assertTrue(all(v >= 0 for v in metrics['seconds'].values()))
        self.assertNotIn('metrics', self.state())
        self.assertEqual(next(e['metrics'] for e in events if e['kind'] == 'metrics'), metrics)
        real_save = scanner.save
        def save(path, data):
            if path.name == 'metrics.json':
                raise OSError('disk full')
            return real_save(path, data)
        with patch.object(scanner, 'save', side_effect=save), patch.object(scanner, 'recognize') as api:
            self.assertEqual(scanner.main(self.args), 0)
        api.assert_not_called()
        self.assertTrue(self.state()['sampling']['complete'])

    def test_stage_timing_includes_failed_work(self):
        with patch('playlist_extractor.metrics.time.perf_counter', side_effect=[10, 11, 14, 15]):
            metrics = ScanMetrics()
            with self.assertRaises(RuntimeError), metrics.measure('recognition'):
                raise RuntimeError('offline')
            snapshot = metrics.snapshot(requests=1, cache_hits=0, resumed=0)
        self.assertEqual(snapshot['seconds']['recognition'], 3)
        self.assertEqual(snapshot['elapsed_seconds'], 5)

    def test_invalid_retry_arguments_and_readonly_modes(self):
        for extra in (['--unmatched-min-age-days', '-1'], ['--unmatched-min-age-days', 'nan'],
                      ['--unmatched-min-age-days', 'inf'], ['--retry-unmatched', '--export-only'],
                      ['--retry-unmatched', '--seed-cache']):
            with self.subTest(extra=extra), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                scanner.main(self.args + extra)
        with patch.object(scanner, 'recognize') as api:
            self.assertEqual(scanner.main(self.args + ['--retry-unmatched', '--dry-run']), 0)
        api.assert_not_called()
        self.assertFalse(self.output.exists())
