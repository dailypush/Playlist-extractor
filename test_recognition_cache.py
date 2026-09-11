import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recognition_cache import RecognitionCache, audio_digest
from test_scan_streams import fake_sample, track
import scan_streams as scanner


class CacheTests(unittest.TestCase):
    def test_persistence_provider_isolation_and_negative_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'cache.sqlite3'
            cache = RecognitionCache(path)
            cache.put('shazam', 'a', [track()])
            with patch('recognition_cache.time.time', return_value=0):
                cache.put('shazam', 'negative', [])
            cache.close()
            cache = RecognitionCache(path)
            self.assertEqual(cache.get('shazam', 'a'), [track()])
            self.assertIsNone(cache.get('acrcloud', 'a'))
            self.assertIsNone(cache.get('shazam', 'negative'))
            cache.put('shazam', 'fresh', [])
            self.assertEqual(cache.get('shazam', 'fresh'), [])
            cache.close()

    def test_duplicate_recording_makes_no_new_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one, two = root / 'one.mp4', root / 'two.mp4'
            one.touch()
            two.touch()
            settings = dict(host='test.acrcloud.com', access_key='key', access_secret='secret')
            with patch.object(scanner, 'duration', return_value=60), patch.object(scanner, 'sample', side_effect=fake_sample), patch.object(scanner, 'credentials', return_value=settings), patch.object(scanner.shutil, 'which', return_value='/tool'), patch.object(scanner, 'recognize', return_value=[track()]) as api:
                for source in (one, two):
                    self.assertEqual(scanner.main([str(source), '--output', str(root / 'out'), '--delay', '0']), 0)
                self.assertEqual(api.call_count, 2)
            self.assertNotEqual(audio_digest(fake_sample(one, 0, 12)), audio_digest(fake_sample(one, 45, 12)))
