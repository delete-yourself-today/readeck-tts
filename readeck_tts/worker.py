import json
import logging
import os
import random
import re
import time

from mutagen.mp3 import MP3
from mutagen.id3 import COMM, TALB, TCON, TDRC, TIT2, TPE1, TXXX

from .cache import cleanup_completed
from .clients import Audiobookshelf, ConfigurationError, Readeck, RemoteError
from .narration import speech_snapshot
from .speech import speech_client
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
    def __init__(self, config, state=None, readeck=None, tts=None, abs_client=None):
        self.config = config
        self._owns_state = state is None
        self.state = state or State(config.data_dir)
        self._owned_clients = []

        def use_client(supplied, factory):
            if supplied is not None:
                return supplied
            client = factory(config)
            self._owned_clients.append(client)
            return client

        try:
            self.readeck = use_client(readeck, Readeck)
            self.tts = use_client(tts, speech_client)
            self._speech_clients = {config.provider: self.tts}
            self.abs = use_client(abs_client, Audiobookshelf)
            cleanup_completed(self.state)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            for client in self._owned_clients:
                client.session.close()
        finally:
            if self._owns_state:
                self.state.db.close()

    def discover(self):
        source = self.config.readeck_url
        for job in self.state.pending_tag_removals(source):
            self.clear_delivered_tag(job)
        events = self.readeck.sync(self.state.cursor(source))
        requested_again = set()
        for article_id in {str(event["id"]) for event in events if event.get("type") != "delete"}:
            previous = self.state.latest_job(source, article_id)
            if previous and previous["state"] == "complete" and previous["tag_removed"]:
                try:
                    if self.has_label(article_id, self.config.label):
                        requested_again.add(article_id)
                except RemoteError as error:
                    if error.status != 404:
                        raise
        self.state.discover(source, events, requested_again)
        log.info("Sync recorded %d changes", len(events))

    def has_label(self, article_id, label):
        bookmark = self.readeck.bookmark(article_id)
        return not bookmark.get("is_deleted") and label in bookmark.get("labels", [])

    def clear_delivered_tag(self, job):
        snapshot = json.loads(job["snapshot"]) if job["snapshot"] else {}
        label = snapshot.get("label", self.config.label)
        article_id = job["article_id"]
        key = job["key"]
        try:
            if self.has_label(article_id, label):
                self.readeck.finish(article_id, remove_tag=label)
        except RemoteError as error:
            if error.status != 404:
                self.state.update(key, tag_error=str(error))
                error.check_credentials()
                log.warning("Tag removal for delivered job %s will retry next poll: %s", key, error)
                return
        self.state.update(key, tag_removed=1, tag_error=None)

    def finish_listened(self):
        """Retry each post-listening action without reopening a delivered job."""
        jobs = self.state.finish_candidates(
            self.config.readeck_url, archive=self.config.archive_on_finish,
            delete_audio=self.config.delete_audio_on_finish)
        for job in jobs:
            key = job["key"]
            try:
                snapshot = json.loads(job["snapshot"]) if job["snapshot"] else None
                if not snapshot or snapshot["destination"] != self.destination():
                    self.state.update(key, finish_error="Destination changed; restore the original configuration to finish cleanup")
                    continue
                if not job["listened"]:
                    if not self.abs.is_finished(job["abs_item_id"], job["abs_episode_id"]):
                        self.state.update(key, finish_error=None)
                        continue
                    # Save evidence before deletion can remove remote progress.
                    self.state.update(key, listened=1)
                if self.config.archive_on_finish and not job["archived"]:
                    try:
                        self.readeck.finish(job["article_id"], archive=True)
                    except RemoteError as error:
                        if error.status != 404:
                            raise
                        # A deleted bookmark needs no further Readeck changes.
                    self.state.update(key, archived=1)
                if self.config.delete_audio_on_finish and not job["audio_deleted"]:
                    self.abs.delete_episode(job["abs_item_id"], job["abs_episode_id"])
                    self.state.update(key, audio_deleted=1)
                self.state.update(key, finish_error=None)
                log.info("Finished post-listening actions for job %s", key)
            except RemoteError as error:
                self.state.update(key, finish_error=str(error))
                error.check_credentials()
                log.warning("Post-listening actions for job %s will retry next poll: %s", key, error)
            except Exception as error:
                self.state.update(key, finish_error=str(error))
                log.warning("Post-listening actions for job %s will retry next poll: %s", key, error)

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
        blocks = self.readeck.article(job["article_id"])
        if not blocks:
            raise RuntimeError("Readeck returned an empty article")
        title = bookmark.get("title") or "Untitled"
        blocks = [{"type": "title", "text": title}] + blocks
        author = ", ".join(bookmark.get("authors") or []) or bookmark.get("site_name") or "Readeck"
        snapshot = {
            "text": "\n\n".join(block["text"] for block in blocks), "title": title,
            "blocks": blocks, **speech_snapshot(blocks, self.config.provider),
            "author": safe_component(author), "url": bookmark.get("url", ""),
            "article_id": job["article_id"], "label": self.config.label, "voice": random.choice(self.config.voices), "model": self.config.model,
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
            provider = snapshot.get("provider", "kokoro")
            if provider not in self._speech_clients:
                client = speech_client(self.config, provider)
                self._speech_clients[provider] = client
                self._owned_clients.append(client)
            self._speech_clients[provider].generate(snapshot, part)
            if not valid_audio(part):
                raise RuntimeError("Speech provider returned invalid or empty MP3 audio")
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
            self.clear_delivered_tag(self.state.get(key))
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
