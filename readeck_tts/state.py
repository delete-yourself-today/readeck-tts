from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from pathlib import Path
import sqlite3


def job_key(source, article_id):
    return hashlib.sha256(f"{source}\n{article_id}".encode()).hexdigest()[:24]


@contextmanager
def data_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another worker is already using this data directory") from None
        yield


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS checkpoints (source TEXT PRIMARY KEY, cursor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                source TEXT NOT NULL, article_id TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'pending', snapshot TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT, abs_item_id TEXT, abs_episode_id TEXT, uploaded_at REAL,
                cleanup_pending INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source, article_id)
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "cleanup_pending" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE jobs ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 0")
                self.db.execute("UPDATE jobs SET cleanup_pending=1 WHERE state='complete'")

        with self.db:
            for name, definition in {
                "listened": "INTEGER NOT NULL DEFAULT 0",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "tag_removed": "INTEGER NOT NULL DEFAULT 0",
                "audio_deleted": "INTEGER NOT NULL DEFAULT 0",
                "finish_error": "TEXT",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def finish_candidates(self, source, *, archive, remove_tag, delete_audio):
        actions = [name + "=0" for name, enabled in (
            ("archived", archive), ("tag_removed", remove_tag), ("audio_deleted", delete_audio)) if enabled]
        if not actions:
            return []
        return self.db.execute("""SELECT * FROM jobs WHERE source=? AND state='complete'
            AND abs_item_id IS NOT NULL AND abs_episode_id IS NOT NULL AND ("""
            + " OR ".join(actions) + ") ORDER BY rowid", (source,)).fetchall()

    def cursor(self, source):
        row = self.db.execute("SELECT cursor FROM checkpoints WHERE source=?", (source,)).fetchone()
        if not row:
            return None
        return (datetime.fromisoformat(row[0].replace("Z", "+00:00")) - timedelta(seconds=1)).isoformat()

    def discover(self, source, events):
        # IDs enter the durable inbox in the same transaction as the checkpoint.
        times = [datetime.fromisoformat(e["time"].replace("Z", "+00:00")) for e in events]
        with self.db:
            for event in events:
                article_id = str(event["id"])
                if event.get("type") == "delete":
                    self.db.execute("UPDATE jobs SET state='cancelled' WHERE source=? AND article_id=? AND state!='complete'", (source, article_id))
                    continue
                self.db.execute("""INSERT INTO jobs(source, article_id, key) VALUES(?,?,?)
                    ON CONFLICT(source,article_id) DO UPDATE SET
                    state=CASE WHEN jobs.state IN ('ignored','cancelled') THEN 'pending' ELSE jobs.state END
                """, (source, article_id, job_key(source, article_id)))
            if times:
                self.db.execute("""INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(source)
                    DO UPDATE SET cursor=excluded.cursor""", (source, max(times).astimezone(timezone.utc).isoformat()))

    def update(self, key, **fields):
        allowed = {"state", "snapshot", "attempts", "next_attempt", "error", "abs_item_id", "abs_episode_id", "uploaded_at", "cleanup_pending",
                   "listened", "archived", "tag_removed", "audio_deleted", "finish_error"}
        if not fields or fields.keys() - allowed:
            raise ValueError("Invalid job update")
        assignments = ",".join(f"{name}=?" for name in fields)
        with self.db:
            self.db.execute(f"UPDATE jobs SET {assignments} WHERE key=?", (*fields.values(), key))

    def due(self, source, now):
        return self.db.execute("""SELECT * FROM jobs WHERE source=? AND state NOT IN
            ('complete','ignored','cancelled') AND next_attempt<=? ORDER BY rowid""", (source, now)).fetchall()

    def get(self, key):
        return self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()

    def pending_cleanup(self):
        return self.db.execute("SELECT key FROM jobs WHERE state='complete' AND cleanup_pending=1").fetchall()

    def status(self):
        return [dict(row) for row in self.db.execute("""SELECT key,article_id,state,attempts,error,
            abs_item_id,abs_episode_id,cleanup_pending,listened,archived,tag_removed,audio_deleted,finish_error FROM jobs ORDER BY rowid""")]
