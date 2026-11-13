import json
import logging
import os
import re
import time

from mutagen.mp3 import MP3
from mutagen.id3 import COMM, TALB, TCON, TDRC, TIT2, TPE1, TXXX

from .cache import cleanup_completed
from .clients import Audiobookshelf, ConfigurationError, Kokoro, Readeck, RemoteError
from .state import State


log = logging.getLogger(__name__)


def fsync_file(path):
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def valid_audio(path):
    if not path.is_file():
        return False
    try:
        return MP3(path).info.length > 0
    except Exception:
        return False


def safe_component(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:140] or "Untitled"


class Worker:
    def __init__(self, config, state=None, readeck=None, kokoro=None, abs_client=None):
        self.config = config
        self.state = state or State(config.data_dir)
        self.readeck = readeck or Readeck(config)
        self.kokoro = kokoro or Kokoro(config)
        self.abs = abs_client or Audiobookshelf(config)
        cleanup_completed(self.state)

    def discover(self):
        events = self.readeck.sync(self.state.cursor(self.config.readeck_url))
        self.state.discover(self.config.readeck_url, events)
        log.info("Sync recorded %d changes", len(events))

    def destination(self):
        return {"url": self.config.abs_url, "library_id": self.abs.library_id,
                "folder_id": self.abs.folder_id, "podcast": self.config.podcast}

    def complete(self, key, existing):
        # Commit delivery before unlinking so a crash can only leave redundant local audio.
        self.state.update(key, state="complete", abs_item_id=existing[0],
                          abs_episode_id=existing[1], cleanup_pending=1, error=None)
        cleanup_completed(self.state)
        log.info("Delivered job %s as episode %s", key, existing[1])

    def snapshot(self, job):
        bookmark = self.readeck.bookmark(job["article_id"])
        if bookmark.get("is_deleted") or self.config.label not in bookmark.get("labels", []):
            self.state.update(job["key"], state="ignored", error=None)
            return None
        if not bookmark.get("loaded", bookmark.get("state") == 0):
            raise RuntimeError("Readeck is still extracting this article")
        if not bookmark.get("has_article"):
            self.state.update(job["key"], state="ignored", error="Bookmark has no extracted article")
            return None
        text = self.readeck.text(job["article_id"])
        if not text.strip():
            raise RuntimeError("Readeck returned an empty article")
        title = bookmark.get("title") or "Untitled"
        author = ", ".join(bookmark.get("authors") or []) or bookmark.get("site_name") or "Readeck"
        snapshot = {
            "text": title + "\n\n" + text, "title": title,
            "author": safe_component(author), "url": bookmark.get("url", ""),
            "article_id": job["article_id"], "voice": self.config.voice, "model": self.config.model,
            "created": bookmark.get("created", ""),
            "destination": self.destination(),
        }
        self.state.update(job["key"], snapshot=json.dumps(snapshot), error=None)
        return snapshot

    def generate(self, job, snapshot, audio):
        if valid_audio(audio):
            self.state.update(job["key"], state="audio_ready", error=None)
            return
        part = audio.with_suffix(".mp3.part")
        self.state.update(job["key"], state="generating")
        try:
            self.kokoro.generate(snapshot, part)
            if not valid_audio(part):
                raise RuntimeError("Kokoro returned invalid or empty MP3 audio")
            tagged = MP3(part)
            if tagged.tags is None:
                tagged.add_tags()
            tagged.tags.add(TIT2(encoding=3, text=snapshot["title"]))
            tagged.tags.add(TPE1(encoding=3, text=snapshot["destination"]["podcast"]))
            tagged.tags.add(TALB(encoding=3, text=snapshot["destination"]["podcast"]))
            tagged.tags.add(TCON(encoding=3, text="Podcast"))
            if snapshot["created"]:
                tagged.tags.add(TDRC(encoding=3, text=snapshot["created"][:19]))
            tagged.tags.add(COMM(encoding=3, lang="eng", desc="", text=f"{snapshot['author']}\n{snapshot['url']}\nAI-generated narration."))
            tagged.tags.add(TXXX(encoding=3, desc="Readeck URL", text=snapshot["url"]))
            tagged.tags.add(TXXX(encoding=3, desc="Readeck TTS ID", text=job["key"]))
            tagged.save()
            fsync_file(part)
            os.replace(part, audio)
            descriptor = os.open(audio.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            part.unlink(missing_ok=True)
        self.state.update(job["key"], state="audio_ready", error=None)

    def process(self, job):
        key = job["key"]
        directory = self.config.data_dir / "articles" / key
        audio = directory / "audio.mp3"
        snapshot = json.loads(job["snapshot"]) if job["snapshot"] else self.snapshot(job)
        if snapshot is None:
            return
        if snapshot["destination"] != self.destination():
            raise ConfigurationError("Destination changed for an existing job; restore its Audiobookshelf configuration")
        directory.mkdir(parents=True, exist_ok=True)

        # Reconcile before generating, including after restoring an older local backup.
        existing = self.abs.find_item(key)
        if existing:
            self.complete(key, existing)
            return
        if job["state"] == "awaiting_index":
            self.abs.request_scan()
            self.state.update(key, next_attempt=time.time() + self.config.retry_interval,
                              error="Upload accepted; waiting for Audiobookshelf indexing")
            return
        if job["state"] == "uploading" and job["uploaded_at"] and time.time() - job["uploaded_at"] < 120:
            self.state.update(key, next_attempt=time.time() + self.config.retry_interval,
                              error="Upload outcome uncertain; waiting for indexing before retry")
            return
        self.generate(job, snapshot, audio)
        self.state.update(key, state="uploading", uploaded_at=time.time())
        self.abs.upload(snapshot, key, audio)
        self.state.update(key, state="awaiting_index", next_attempt=time.time() + self.config.retry_interval, error=None)
        self.abs.request_scan()
        log.info("Uploaded job %s; waiting for indexing", key)

    def work(self, limit=None):
        cleanup_completed(self.state)
        jobs = self.state.due(self.config.readeck_url, time.time())
        for job in jobs[:limit] if limit is not None else jobs:
            try:
                self.process(job)
            except ConfigurationError:
                raise
            except RemoteError as error:
                error.check_credentials()
                if error.status is None or error.status == 429 or error.status >= 500:
                    raise
                if error.status == 404 and job["snapshot"] is None:
                    # Only a missing Readeck bookmark before snapshotting is cancellation.
                    if str(error).startswith("Readeck:"):
                        self.state.update(job["key"], state="cancelled", error=None)
                        continue
                self.failed(job, error)
            except Exception as error:
                self.failed(job, error)

    def failed(self, job, error):
        attempts = job["attempts"] + 1
        delay = min(3600, self.config.retry_interval * 2 ** min(attempts - 1, 7))
        self.state.update(job["key"], attempts=attempts, next_attempt=time.time() + delay, error=str(error))
        log.warning("Job %s failed; retry in %ss: %s", job["key"], delay, error)
