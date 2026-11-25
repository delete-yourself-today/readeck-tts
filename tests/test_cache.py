from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from readeck_tts.cache import cleanup_completed
from readeck_tts.state import State


class CacheTests(unittest.TestCase):
    def test_only_completed_jobs_lose_native_chunks_and_usage_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory))
            self.addCleanup(state.db.close)
            state.discover("source", [{"id": "a", "time": "2026-11-12T12:00:00Z"},
                                      {"id": "b", "time": "2026-11-12T12:01:00Z"}])
            jobs = state.due("source", 1)
            for job in jobs:
                chunks = Path(directory) / "articles" / job["key"] / "chunks"
                chunks.mkdir(parents=True)
                for name in ("000000.mp3", "000000.json", "000001.wav", "000002.wav.part", "concat.txt", "unrelated.txt"):
                    (chunks / name).write_text("cached")
            state.update(jobs[0]["key"], state="complete", cleanup_pending=1)
            cleanup_completed(state)
            completed = Path(directory) / "articles" / jobs[0]["key"] / "chunks"
            pending = Path(directory) / "articles" / jobs[1]["key"] / "chunks"
            self.assertEqual([path.name for path in completed.iterdir()], ["unrelated.txt"])
            self.assertTrue((pending / "000000.mp3").exists())

    def test_upgrade_cleans_legacy_completed_audio_but_preserves_unfinished_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "state.sqlite3")) as db:
                db.executescript("""
                    CREATE TABLE jobs (
                        source TEXT NOT NULL, article_id TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL DEFAULT 'pending', snapshot TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                        error TEXT, abs_item_id TEXT, abs_episode_id TEXT, uploaded_at REAL,
                        PRIMARY KEY(source, article_id)
                    );
                    INSERT INTO jobs (source, article_id, key, state, abs_item_id, abs_episode_id)
                        VALUES ('source', 'a', 'delivered', 'complete', 'show', 'episode');
                    INSERT INTO jobs (source, article_id, key, state)
                        VALUES ('source', 'b', 'unfinished', 'awaiting_index');
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
            delivered, = state.pending_tag_removals("source")
            self.assertEqual((delivered["key"], delivered["abs_episode_id"]), ("delivered", "episode"))
            event = {"id": "a", "time": "2026-11-12T12:00:00Z"}
            state.discover("source", [event], requested_again={"a"})
            self.assertEqual(state.latest_job("source", "a")["key"], "delivered")
            state.update("delivered", tag_removed=1)
            state.discover("source", [event], requested_again={"a"})
            self.assertNotEqual(state.latest_job("source", "a")["key"], "delivered")

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
