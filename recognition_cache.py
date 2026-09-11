"""Persistent exact-PCM result cache. This is not approximate song matching."""
import hashlib
import io
import json
import sqlite3
import time
import wave
from pathlib import Path


def audio_digest(audio):
    # Ignore container headers/metadata; include the decoded audio format.
    with wave.open(io.BytesIO(audio), 'rb') as source:
        header = (source.getnchannels(), source.getsampwidth(), source.getframerate())
        pcm = source.readframes(source.getnframes())
    return hashlib.sha256(repr(header).encode() + pcm).hexdigest()


def namespace(identity):
    return json.dumps({k: identity.get(k) for k in ('provider', 'host', 'version')}, sort_keys=True)


class RecognitionCache:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute('CREATE TABLE IF NOT EXISTS results (provider TEXT, digest TEXT, '
                        'matches TEXT NOT NULL, created REAL NOT NULL, '
                        'PRIMARY KEY(provider, digest))')
        self.db.commit()

    def get(self, provider, digest):
        row = self.db.execute('SELECT matches, created FROM results WHERE provider=? AND digest=?',
                              (provider, digest)).fetchone()
        if row is None:
            return None
        matches = json.loads(row[0])
        # Missing songs can enter a catalog later; retry negative results after a week.
        if not matches and time.time() - row[1] > 7 * 86400:
            return None
        return matches

    def put(self, provider, digest, matches):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)',
                            (provider, digest, json.dumps(matches), time.time()))

    def close(self):
        self.db.close()
