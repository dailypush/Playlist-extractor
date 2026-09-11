from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from playlist_extractor import batch, scanner
from test_scan_streams import fake_sample, track


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        for name in ('a.mp4', 'b.mp4'):
            (self.media / name).touch()
        self.args = [str(self.media), '--output', str(self.root / 'out'), '--delay', '0', '--no-refine']
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scanner.shutil, 'which', return_value='/tool'))
        self.stack.enter_context(patch.object(scanner, 'duration', return_value=60))
        self.real_sample = scanner.sample
        self.stack.enter_context(patch.object(scanner, 'sample', side_effect=lambda p,o,l,**kwargs: fake_sample(p, o + (100 if p.name == 'b.mp4' else 0), l)))

    def queue(self):
        return json.loads(next((self.root / 'out/batches').glob('*/queue.json')).read_text())

    def test_global_budget_and_resume_skip_completed_files(self):
        with patch.object(scanner, 'recognize', return_value=[track()]) as api:
            self.assertEqual(batch.main(self.args + ['--max-requests', '3']), 0)
            self.assertEqual(api.call_count, 3)
            queue = self.queue()
            self.assertEqual(queue['last_run']['status'], 'paused')
            self.assertEqual([e['status'] for e in queue['files'].values()], ['complete', 'paused'])
            self.assertEqual(batch.main(self.args + ['--max-requests', '3']), 0)
            self.assertEqual(api.call_count, 4)
            self.assertEqual(self.queue()['last_run']['status'], 'complete')
            self.assertEqual(batch.main(self.args), 0)
            self.assertEqual(api.call_count, 4)

    def test_bad_file_continues_but_service_error_stops(self):
        with patch.object(scanner, 'duration', side_effect=[subprocess.CalledProcessError(1, 'ffprobe'), 60]), patch.object(scanner, 'recognize', return_value=[track()]):
            self.assertEqual(batch.main(self.args), 1)
        statuses = [e['status'] for e in self.queue()['files'].values()]
        self.assertEqual(statuses, ['failed', 'complete'])
        with patch.object(scanner, 'recognize', side_effect=RuntimeError('rate limited')):
            self.assertEqual(batch.main(self.args + ['--retry-failed']), 1)
        self.assertEqual(self.queue()['last_run']['status'], 'paused')

    def test_interruption_keeps_partial_checkpoint(self):
        with patch.object(scanner, 'recognize', side_effect=[[track()], KeyboardInterrupt()]):
            self.assertEqual(batch.main(self.args), 130)
        self.assertEqual(self.queue()['last_run']['status'], 'paused')
        self.assertEqual(len(list((self.root / 'out').glob('*/playlist.json'))), 1)
        with patch.object(scanner, 'recognize', return_value=[track()]) as api:
            self.assertEqual(batch.main(self.args), 0)
            self.assertEqual(api.call_count, 3)

    def test_output_lock_prevents_second_batch(self):
        with batch.batch_lock(self.root / 'out'):
            self.assertEqual(batch.main(self.args), 1)

    def test_scan_and_batch_share_output_lock(self):
        with batch.batch_lock(self.root / 'out'), patch.object(scanner, 'recognize') as api:
            self.assertEqual(scanner.main(self.args), 1)
            api.assert_not_called()
        attempted = []
        def competing_batch(event):
            if event['kind'] == 'progress' and not attempted:
                attempted.append(batch.main(self.args))
        with patch.object(scanner, 'recognize', return_value=[track()]):
            self.assertEqual(scanner.main(self.args, event_handler=competing_batch), 0)
        self.assertEqual(attempted, [1])

    def test_bad_completed_exports_are_rebuilt_without_requests(self):
        with patch.object(scanner, 'recognize', return_value=[track()]):
            self.assertEqual(batch.main(self.args), 0)
        folder = Path(next(iter(self.queue()['files'].values()))['output'])
        output = folder / 'playlist.json'
        checkpoint = folder / 'checkpoint.json'
        for damage in ('missing', 'invalid_json', 'wrong_source', 'incomplete', 'checkpoint_incomplete'):
            with self.subTest(damage=damage):
                document = json.loads(output.read_text())
                if damage == 'missing':
                    output.unlink()
                elif damage == 'invalid_json':
                    output.write_text('{')
                elif damage == 'checkpoint_incomplete':
                    state = json.loads(checkpoint.read_text())
                    state['sampling']['complete'] = False
                    checkpoint.write_text(json.dumps(state))
                else:
                    if damage == 'wrong_source':
                        document['source']['path'] = '/unrelated.mp4'
                    else:
                        document['recognition']['sampling_complete'] = False
                    output.write_text(json.dumps(document))
                with patch.object(scanner, 'recognize', side_effect=AssertionError('Must reuse samples')) as api:
                    self.assertEqual(batch.main(self.args), 0)
                    api.assert_not_called()
                repaired = json.loads(output.read_text())
                self.assertTrue(repaired['recognition']['sampling_complete'])
                self.assertEqual(repaired['source']['path'], str((self.media / 'a.mp4').resolve()))
                self.assertTrue(json.loads(checkpoint.read_text())['sampling']['complete'])

    def test_extraction_timeout_keeps_saved_samples_and_continues(self):
        original_sample = scanner.sample
        def samples(path, offset, length, **kwargs):
            if path.name == 'a.mp4' and offset == 45:
                # Exercise the real timeout wrapper, not just its error type.
                with patch.object(scanner.subprocess, 'run',
                        side_effect=subprocess.TimeoutExpired('ffmpeg', scanner.SAMPLE_TIMEOUT)):
                    return self.real_sample(path, offset, length)
            return original_sample(path, offset, length)
        with patch.object(scanner, 'sample', side_effect=samples), patch.object(scanner, 'recognize', return_value=[track()]):
            self.assertEqual(batch.main(self.args), 1)
        entries = list(self.queue()['files'].values())
        self.assertEqual([entry['status'] for entry in entries], ['failed', 'complete'])
        state = json.loads((Path(entries[0]['output']) / 'checkpoint.json').read_text())
        self.assertEqual(list(state['results']), ['0.0'])
        self.assertFalse(state['sampling']['complete'])

    def test_dry_run_has_no_queue_or_provider_requests(self):
        with patch.object(scanner, 'recognize') as api:
            self.assertEqual(batch.main(self.args + ['--dry-run']), 0)
            api.assert_not_called()
        self.assertFalse((self.root / 'out').exists())

    def test_service_failure_does_not_start_next_file(self):
        with patch.object(scanner, 'recognize', side_effect=RuntimeError('service unavailable')) as api:
            self.assertEqual(batch.main(self.args), 1)
            self.assertEqual(api.call_count, 1)
        self.assertEqual([e['status'] for e in self.queue()['files'].values()], ['paused', 'pending'])

    def test_time_limit_pauses_before_request(self):
        # Let one sample complete, then make the next extraction observe the deadline.
        ticks = iter([0, 0, 0, 0, 0, 120, 120, 120, 120])
        with patch.object(batch.time, 'monotonic', side_effect=lambda: next(ticks, 120)), patch.object(scanner, 'recognize', return_value=[track()]) as api:
            self.assertEqual(batch.main(self.args + ['--max-minutes', '1']), 0)
            self.assertLessEqual(api.call_count, 1)
        self.assertEqual(self.queue()['last_run']['status'], 'paused')
