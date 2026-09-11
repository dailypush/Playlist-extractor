from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from playlist_extractor import scanner
import test_batch
from test_scan_streams import fake_sample, track


class ThreadedBatchTests(test_batch.BatchTests):
    """Run the batch budget, interruption, error and recovery contract in both modes."""
    def setUp(self):
        super().setUp()
        self.args.append('--threaded')


class ThreadedScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'mix.mp4'
        self.source.touch()
        self.args = [str(self.source), '--output', str(self.root / 'out'), '--delay', '0', '--no-refine']
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(scanner.shutil, 'which', return_value='/tool'))
        self.stack.enter_context(patch.object(scanner, 'duration', return_value=150))

    def test_prefetch_overlaps_recognition_with_one_sample_ahead_and_cancels(self):
        preparing_next = threading.Event()
        cancelled = threading.Event()
        extracted = []
        main = threading.get_ident()

        def extract(path, offset, length, cancel_event=None):
            self.assertNotEqual(threading.get_ident(), main)
            extracted.append(offset)
            if offset == 45:
                preparing_next.set()
                self.assertTrue(cancel_event.wait(5), 'Prefetch was not cancelled')
                cancelled.set()
                raise CancelledError()
            return fake_sample(path, offset, length)

        def identify(audio, settings):
            self.assertEqual(threading.get_ident(), main)
            self.assertTrue(preparing_next.wait(5), 'Extraction did not overlap recognition')
            self.assertEqual(extracted, [0.0, 45.0])
            raise RuntimeError('service unavailable')

        def event_handler(event):
            self.assertEqual(threading.get_ident(), main)

        with patch.object(scanner, 'sample', side_effect=extract), patch.object(scanner, 'recognize', side_effect=identify) as api:
            self.assertEqual(scanner.main(self.args + ['--threaded'], event_handler=event_handler), 1)
            self.assertEqual(api.call_count, 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(t.name.startswith('audio-prefetch') for t in threading.enumerate()))

    def test_threaded_exports_match_sequential_and_resume_keeps_identity(self):
        def extract(path, offset, length, **kwargs):
            return fake_sample(path, offset, length)
        with patch.object(scanner, 'sample', side_effect=extract), patch.object(scanner, 'recognize', return_value=[track()]) as api:
            self.assertEqual(scanner.main(self.args), 0)
            original = next((self.root / 'out').glob('*/playlist.json')).parent
            threaded_args = [str(self.source), '--output', str(self.root / 'threaded'), '--delay', '0', '--no-refine', '--threaded']
            self.assertEqual(scanner.main(threaded_args), 0)
            threaded = next((self.root / 'threaded').glob('*/playlist.json')).parent
            for filename in ('playlist.json', 'playlist.csv', 'observations.csv', 'checkpoint.json'):
                self.assertEqual((original / filename).read_bytes(), (threaded / filename).read_bytes())
            api.reset_mock()
            with patch.object(scanner, 'sample', side_effect=AssertionError('Must reuse checkpoint')):
                self.assertEqual(scanner.main(self.args + ['--threaded']), 0)
            api.assert_not_called()

    def test_threaded_refinement_cache_reuse_and_pacing(self):
        # Identical excerpts should produce one request even with speculative work.
        with patch.object(scanner, 'sample', side_effect=lambda p,o,l,**kw: fake_sample(p, 0, l)), patch.object(scanner, 'recognize', return_value=[]) as api:
            args = [arg for arg in self.args if arg != '--no-refine'] + ['--threaded']
            self.assertEqual(scanner.main(args), 0)
            self.assertEqual(api.call_count, 1)
            checkpoint = next((self.root / 'out').glob('*/checkpoint.json'))
            self.assertEqual(len(json.loads(checkpoint.read_text())['results']), 7)
        with patch.object(scanner, 'sample', side_effect=lambda p,o,l,**kw: fake_sample(p, o, l)), patch.object(scanner, 'recognize', return_value=[track()]) as api, patch.object(scanner.time, 'sleep') as pause:
            args = [str(self.source), '--output', str(self.root / 'paced'), '--delay', '3', '--no-refine', '--threaded']
            self.assertEqual(scanner.main(args), 0)
            self.assertEqual(api.call_count, 4)
            self.assertEqual(pause.call_count, 3)
            self.assertTrue(all(call.args == (3,) for call in pause.call_args_list))

    def test_cancelling_extraction_reaps_child_process(self):
        launched = threading.Event()
        cancel = threading.Event()
        children = []
        popen = subprocess.Popen

        def launch(command, **kwargs):
            child = popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            launched.set()
            return child

        with patch.object(scanner.subprocess, 'Popen', side_effect=launch), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(scanner.sample, self.source, 0, 12, cancel)
            self.assertTrue(launched.wait(5))
            cancel.set()
            with self.assertRaises(CancelledError):
                future.result(timeout=5)
        self.assertIsNotNone(children[0].poll())
