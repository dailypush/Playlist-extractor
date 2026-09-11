from contextlib import ExitStack
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from playlist_extractor import batch, reports, scanner
from playlist_extractor.storage import retire_checkpoint
from test_scan_streams import fake_sample, track


class ArchiveTests(unittest.TestCase):
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
        self.stack.enter_context(patch.object(scanner, 'duration', return_value=60))
        self.stack.enter_context(patch.object(scanner, 'sample', side_effect=fake_sample))

    def completed_scan(self):
        with patch.object(scanner, 'recognize', side_effect=[[track()], []]):
            self.assertEqual(scanner.main(self.args), 0)
        return next(self.output.glob('*/playlist.json')).parent

    def test_json_alone_can_resume_export_seed_cache_and_report(self):
        folder = self.completed_scan()
        document = json.loads((folder / 'playlist.json').read_text())
        state = document['scan_state']
        self.assertFalse((folder / 'checkpoint.json').exists())
        self.assertEqual(state['results']['45.0'], [])
        self.assertEqual(state['results']['0.0'][0]['id'], 'a')
        self.assertEqual(len(state['provenance']['0.0']['audio_digest']), 64)
        for path in folder.glob('*.csv'):
            path.unlink()
        (self.output / 'recognition-cache.sqlite3').unlink()
        with patch.object(scanner, 'sample', side_effect=AssertionError('No extraction needed')), patch.object(scanner, 'recognize', side_effect=AssertionError('No request needed')):
            self.assertEqual(scanner.main(self.args + ['--export-only']), 0)
            self.assertEqual(scanner.main(self.args + ['--seed-cache']), 0)
            self.assertEqual(scanner.main(self.args), 0)
            self.assertEqual(batch.main(self.args), 0)
            self.assertEqual(batch.main(self.args), 0)
        self.assertEqual(scanner.load_state(folder), state)
        self.assertFalse((folder / 'checkpoint.json').exists())
        self.source.unlink()
        # The exported file can also be moved and renamed for offline reports.
        archive = self.root / 'saved-session.json'
        shutil.copy2(folder / 'playlist.json', archive)
        self.assertEqual(reports.build_report(archive, self.root / 'reports'), (1, 1))

    def test_legacy_complete_checkpoint_migrates_without_requests(self):
        folder = self.completed_scan()
        document = json.loads((folder / 'playlist.json').read_text())
        state = document.pop('scan_state')
        document['schema_version'] = 1
        (folder / 'checkpoint.json').write_text(json.dumps(state))
        (folder / 'playlist.json').write_text(json.dumps(document))
        with patch.object(scanner, 'recognize', side_effect=AssertionError('No request needed')), patch.object(scanner.shutil, 'which', return_value=None):
            self.assertEqual(scanner.main(self.args + ['--export-only']), 0)
        self.assertFalse((folder / 'checkpoint.json').exists())
        self.assertEqual(json.loads((folder / 'playlist.json').read_text())['scan_state'], state)

    def test_export_failure_retains_completed_checkpoint_for_recovery(self):
        with patch.object(scanner, 'recognize', return_value=[track()]), patch.object(scanner, 'export', side_effect=OSError('disk full')):
            self.assertEqual(scanner.main(self.args), 1)
        checkpoint = next(self.output.glob('*/checkpoint.json'))
        state = json.loads(checkpoint.read_text())
        self.assertTrue(state['sampling']['complete'])
        with patch.object(scanner, 'recognize', side_effect=AssertionError('No request needed')):
            self.assertEqual(scanner.main(self.args + ['--export-only']), 0)
        self.assertFalse(checkpoint.exists())
        self.assertEqual(scanner.load_state(checkpoint.parent), state)

    def test_mismatched_archive_never_deletes_checkpoint(self):
        folder = self.completed_scan()
        state = scanner.load_state(folder)
        checkpoint = folder / 'checkpoint.json'
        checkpoint.write_text(json.dumps(state))
        document = json.loads((folder / 'playlist.json').read_text())
        document['scan_state']['results'] = {}
        (folder / 'playlist.json').write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, 'differs'):
            retire_checkpoint(state, folder)
        self.assertTrue(checkpoint.exists())

    def test_report_deduplicates_checkpoint_and_archive(self):
        folder = self.completed_scan()
        state = scanner.load_state(folder)
        (folder / 'checkpoint.json').write_text(json.dumps(state))
        copy = self.output / 'snapshot'
        copy.mkdir()
        shutil.copy2(folder / 'playlist.json', copy / 'playlist.json')
        self.assertEqual(reports.build_report(self.output, self.root / 'reports'), (1, 1))
        with (self.root / 'reports/song_frequency.csv').open() as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row['detections'], '1')

    def test_damaged_or_missing_only_archive_stops_without_requests(self):
        folder = self.completed_scan()
        archive = folder / 'playlist.json'
        saved = archive.read_text()
        for path in folder.glob('*.csv'):
            path.unlink()
        for damage in ('invalid', 'missing', 'legacy_without_checkpoint'):
            with self.subTest(damage=damage):
                if damage == 'missing':
                    archive.unlink()
                elif damage == 'legacy_without_checkpoint':
                    document = json.loads(saved)
                    document['schema_version'] = 1
                    document.pop('scan_state')
                    archive.write_text(json.dumps(document))
                else:
                    archive.write_text('{')
                with patch.object(scanner, 'recognize') as api:
                    self.assertEqual(scanner.main(self.args), 1)
                    api.assert_not_called()
                archive.write_text(saved)
