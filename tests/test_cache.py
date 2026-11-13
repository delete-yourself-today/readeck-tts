from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from readeck_tts.cache import cleanup_completed
from readeck_tts.state import State


class CacheTests(unittest.TestCase):
    def test_upgrade_cleans_legacy_completed_audio_but_preserves_unfinished_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "state.sqlite3")) as db:
                db.executescript("""
                    CREATE TABLE jobs (key TEXT PRIMARY KEY, state TEXT, error TEXT);
                    INSERT INTO jobs VALUES ('delivered', 'complete', NULL);
                    INSERT INTO jobs VALUES ('unfinished', 'awaiting_index', NULL);
                """)
            for key in ("delivered", "unfinished"):
                path = root / "articles" / key
                path.mkdir(parents=True)
                (path / "audio.mp3").write_bytes(b"audio")
            state = State(root)
            self.addCleanup(state.db.close)
            cleanup_completed(state)
            self.assertFalse((root / "articles/delivered/audio.mp3").exists())
            self.assertTrue((root / "articles/unfinished/audio.mp3").exists())
            self.assertFalse(state.pending_cleanup())

    def test_missing_audio_is_already_clean_and_unknown_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory))
            self.addCleanup(state.db.close)
            state.discover("source", [{"id": "article", "time": "2026-11-12T12:00:00Z"}])
            job = state.due("source", 1)[0]
            state.update(job["key"], state="complete", cleanup_pending=1)
            path = Path(directory) / "articles" / job["key"]
            path.mkdir(parents=True)
            (path / "unrelated.txt").write_text("keep me")
            cleanup_completed(state)
            self.assertTrue((path / "unrelated.txt").exists())
            self.assertFalse(state.pending_cleanup())


if __name__ == "__main__":
    unittest.main()
