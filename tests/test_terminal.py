import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playlist_extractor import scanner, terminal
from test_scan_streams import fake_sample, track


class TerminalTests(unittest.TestCase):
    def test_scan_events_include_phases_resume_counts_and_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mix.mp4'
            path.touch()
            args = [str(path), '--output', str(Path(temp) / 'out'), '--delay', '0']
            events = []
            with patch.object(scanner, 'duration', return_value=60), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/tool'), patch.object(scanner, 'recognize', return_value=[track()]):
                self.assertEqual(scanner.main(args, event_handler=events.append), 0)
                progress = [e for e in events if e['kind'] == 'progress']
                self.assertEqual({e['phase'] for e in progress}, {'Baseline', 'Refinement'})
                self.assertTrue(any(e.get('done') == 2 and e['requests'] == 2 for e in progress))
                self.assertTrue(any(e['kind'] == 'exported' for e in events))
                events.clear()
                self.assertEqual(scanner.main(args, event_handler=events.append), 0)
                finished = next(e for e in events if e['kind'] == 'finished')
                self.assertTrue(finished['complete'])
                self.assertEqual(finished['resumed'], 2)
                self.assertEqual(finished['requests'], 0)

    def test_interrupt_exports_partial_results(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mix.mp4'
            path.touch()
            events = []
            with patch.object(scanner, 'duration', return_value=60), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner.shutil, 'which', return_value='/tool'), patch.object(scanner, 'recognize', side_effect=[[track()], KeyboardInterrupt()]):
                self.assertEqual(scanner.main([str(path), '--output', str(Path(temp) / 'out'), '--delay', '0'], event_handler=events.append), 130)
            folder = Path(next(e for e in events if e['kind'] == 'exported')['folder'])
            self.assertIn('Artist', (folder / 'playlist.csv').read_text())

    def test_plain_display_has_no_escape_codes(self):
        output = io.StringIO()
        with terminal.ProgressDisplay(output) as display:
            display(dict(kind='progress', phase='Baseline', total=2, done=1, requests=1,
                         cache_hits=0, resumed=0, songs=1, offset=0, matches=[track()], activity='Sample saved'))
        self.assertIn('Artist', output.getvalue())
        self.assertIn('1/2', output.getvalue())
        self.assertNotIn('\033', output.getvalue())

    def test_menu_estimate_then_quit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mix.mp4'
            path.touch()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['2', '1', 'q']), patch.object(scanner, 'main', return_value=0) as run, patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(terminal.main(['--source', str(path)]), 0)
            argv = run.call_args.args[0]
            self.assertIn('--dry-run', argv)
            self.assertIn('shazam', argv)

    def test_noninteractive_menu_explains_docker_tty_requirement(self):
        with patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', new_callable=io.StringIO) as errors:
            self.assertEqual(terminal.main([]), 2)
            self.assertIn('docker run -it', errors.getvalue())

    def test_threaded_option_reaches_scan_and_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'mix.mp4'
            path.touch()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['1', '1', '8', '10', 'q']), patch.object(scanner, 'main', return_value=0) as scan, patch.object(terminal.batch, 'main', return_value=0) as batch, patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(terminal.main(['--source', str(path), '--threaded']), 0)
            self.assertIn('--threaded', scan.call_args.args[0])
            self.assertIn('--threaded', batch.call_args.args[0])
