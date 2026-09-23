import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from playlist_extractor import browser
from playlist_extractor.catalog import export
from test_scan_streams import track


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.folder = self.output / 'DJ recording'
        self.folder.mkdir()

    def test_live_checkpoint_wins_over_export_without_writes(self):
        state = dict(identity={'path': '/media/mix.mp4'}, sampling={'complete': False},
                     results={'0.0': [track()], '45.0': [track()]})
        path = self.folder / 'checkpoint.json'
        path.write_text(json.dumps(state))
        (self.folder / 'playlist.json').write_text('{"playlist": []}')
        before = path.read_bytes()
        name, complete, songs = browser.read_playlist(self.folder)
        self.assertEqual(name, 'mix.mp4')
        self.assertFalse(complete)
        self.assertEqual(songs[0]['detections'], 2)
        self.assertEqual(songs[0]['first_detected'], '00:00:00')
        self.assertEqual(songs[0]['last_detected'], '00:00:45')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads((self.folder / 'playlist.json').read_text())['playlist'], [])
        self.assertEqual(browser.recordings(self.output, 'dj'), [self.folder])
        self.assertEqual(browser.read_playlist(self.folder, 'never matches')[2], [])

    def test_completed_archive_has_same_songs_as_live_view(self):
        state = dict(identity={'path': '/media/mix.mp4'}, sampling={'complete': True}, results={'0.0': [track()]})
        export(state, self.folder, 80)
        name, complete, songs = browser.read_playlist(self.folder)
        self.assertTrue(complete)
        self.assertEqual(songs, json.loads((self.folder / 'playlist.json').read_text())['playlist'])

    def test_missing_database_is_not_created(self):
        path = self.output / 'missing.sqlite3'
        self.assertEqual(browser.cache_page(path)['total'], 0)
        self.assertFalse(path.exists())

    def test_cache_pagination_search_and_writer_can_continue(self):
        path = self.output / 'cache.sqlite3'
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute('CREATE TABLE results (digest TEXT, matches TEXT, created REAL)')
        for i in range(55):
            matches = [{'artist': 'Beyoncé', 'title': '100%_mix'}] if i % 2 else []
            db.execute('INSERT INTO results VALUES (?, ?, ?)', (str(i), json.dumps(matches), 100 + i))
        db.commit()
        before = path.read_bytes()
        first = browser.cache_page(path)
        self.assertEqual(first['total'], 55)
        self.assertEqual(first['matched'], 27)
        self.assertEqual(len(first['rows']), 50)
        self.assertEqual(first['rows'][0]['digest'], '54')
        self.assertEqual(len(browser.cache_page(path, page=1)['rows']), 5)
        self.assertEqual(browser.cache_page(path, 'Beyoncé')['filtered'], 27)
        self.assertEqual(browser.cache_page(path, '%_')['filtered'], 27)
        self.assertEqual(browser.cache_page(path, "' OR 1=1 --")['filtered'], 0)
        self.assertEqual(path.read_bytes(), before)
        db.execute('INSERT INTO results VALUES (?, ?, ?)', ('new', '[]', 200))
        db.commit()
        self.assertEqual(browser.cache_page(path)['total'], 56)

    def test_corrupt_cached_json_does_not_crash_listing(self):
        path = self.output / 'cache.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE results (digest TEXT, matches TEXT, created REAL)')
            db.execute("INSERT INTO results VALUES ('bad', '{', 1)")
        self.assertEqual(browser.cache_page(path)['rows'][0]['label'], 'Unreadable cached result')
