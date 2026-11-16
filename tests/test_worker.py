from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from mutagen.mp3 import MP3

from readeck_tts.clients import Audiobookshelf, ConfigurationError, Kokoro, Readeck, RemoteError
from readeck_tts.config import Config
from readeck_tts.state import State, data_lock, job_key
from readeck_tts.text import article_blocks, article_text
from readeck_tts.worker import Worker
from readeck_tts.__main__ import main


MP3_AUDIO = (bytes.fromhex("fffb9000") + bytes(413)) * 20


class FakeReadeck:
    def __init__(self):
        self.events = [{"id": "a", "time": "2026-11-12T12:00:00Z", "type": "update"}]
        self.metadata = {"title": "Test article", "url": "https://example.org/article",
                         "authors": ["An author"], "labels": ["tts"], "loaded": True,
                         "has_article": True, "created": "2026-11-11T12:00:00Z"}

    def sync(self, since):
        return self.events

    def bookmark(self, article_id):
        return self.metadata

    def article(self, article_id):
        return [{"type": "paragraph", "text": "A paragraph of article text."}]


class FakeKokoro:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def generate(self, snapshot, path):
        self.calls += 1
        if self.fail:
            path.write_bytes(b"partial audio")
            raise RemoteError("Kokoro", "audio download")
        path.write_bytes(MP3_AUDIO)


class FakeABS:
    library_id = "library"
    folder_id = "folder"

    def __init__(self):
        self.uploads = []
        self.indexed = {}
        self.fail = False
        self.lost_response = False
        self.auto_index = True

    def find_item(self, key):
        return self.indexed.get(key)

    def upload(self, snapshot, key, audio):
        if self.fail:
            raise RemoteError("Audiobookshelf", "upload", 503)
        self.uploads.append(key)
        if self.auto_index:
            self.indexed[key] = ("show", "episode-" + key)
        if self.lost_response:
            raise RemoteError("Audiobookshelf", "upload")

    def request_scan(self):
        pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected remote call in recovery unit test"))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config("http://readeck", "secret", "http://abs", "secret",
                             data_dir=Path(self.tmp.name), retry_interval=1)
        self.state = State(self.config.data_dir)
        self.addCleanup(self.state.db.close)
        self.readeck, self.kokoro, self.abs = FakeReadeck(), FakeKokoro(), FakeABS()
        self.worker = Worker(self.config, self.state, self.readeck, self.kokoro, self.abs)
        self.worker.discover()
        self.key = job_key(self.config.readeck_url, "a")

    def job(self):
        return self.state.get(self.key)

    def retry_now(self):
        self.state.update(self.key, next_attempt=0, uploaded_at=0)
        self.worker.work()

    def test_repeated_sync_and_restart_do_not_regenerate(self):
        self.worker.work()
        self.assertEqual(self.job()["state"], "awaiting_index")
        self.worker.discover()
        restarted = Worker(self.config, self.state, self.readeck, self.kokoro, self.abs)
        self.state.update(self.key, next_attempt=0)
        restarted.work()
        restarted.discover()
        restarted.work()
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(self.job()["abs_episode_id"], "episode-" + self.key)
        self.assertEqual(self.kokoro.calls, 1)
        self.assertEqual(len(self.abs.uploads), 1)
        self.assertFalse((self.config.data_dir / "articles" / self.key).exists())
        self.assertEqual(self.job()["cleanup_pending"], 0)

    def prepare_listening(self):
        self.config = replace(self.config, archive_on_finish=True,
                              remove_tag_on_finish=True, delete_audio_on_finish=True)
        self.worker.config = self.config
        self.worker.snapshot(self.job())
        self.worker.complete(self.key, ("show", "episode"))
        self.abs.is_finished = Mock(return_value=True)
        self.abs.delete_episode = Mock()
        self.readeck.finish = Mock()

    def test_finished_article_updates_readeck_before_deleting_audio(self):
        self.prepare_listening()
        calls = Mock()
        calls.attach_mock(self.readeck.finish, "readeck")
        calls.attach_mock(self.abs.delete_episode, "delete")
        self.worker.finish_listened()
        self.assertEqual([call[0] for call in calls.mock_calls], ["readeck", "delete"])
        self.readeck.finish.assert_called_once_with("a", archive=True, remove_tag="tts")
        self.assertEqual(tuple(self.job()[name] for name in ("listened", "archived", "tag_removed", "audio_deleted")), (1, 1, 1, 1))
        self.worker.finish_listened()
        self.abs.is_finished.assert_called_once()
        self.abs.delete_episode.assert_called_once_with("show", "episode")
        self.worker.discover()
        self.worker.work()
        self.assertEqual(self.kokoro.calls, 0)
        self.assertEqual(self.job()["state"], "complete")

    def test_completion_permission_errors_stop_worker_and_preserve_audio(self):
        self.prepare_listening()
        for status in (401, 403):
            with self.subTest(status=status):
                self.readeck.finish.side_effect = RemoteError("Readeck", "PATCH bookmark", status)
                with self.assertRaisesRegex(ConfigurationError, f"Readeck:.*HTTP {status}"):
                    self.worker.finish_listened()
                self.abs.delete_episode.assert_not_called()
                self.assertIn(f"HTTP {status}", self.job()["finish_error"])
                self.assertEqual(self.job()["state"], "complete")
                self.assertEqual(self.job()["archived"], 0)

    def test_default_options_leave_finished_articles_untouched(self):
        self.prepare_listening()
        self.worker.config = Config("http://readeck", "secret", "http://abs", "secret")
        self.worker.finish_listened()
        self.abs.is_finished.assert_not_called()
        self.readeck.finish.assert_not_called()
        self.abs.delete_episode.assert_not_called()

    def test_unfinished_article_is_untouched(self):
        self.prepare_listening()
        self.abs.is_finished.return_value = False
        self.worker.finish_listened()
        self.readeck.finish.assert_not_called()
        self.abs.delete_episode.assert_not_called()
        self.assertEqual(self.job()["listened"], 0)

    def test_each_finish_option_is_independent(self):
        from itertools import product
        self.prepare_listening()
        for archive, tag, audio in product((False, True), repeat=3):
            with self.subTest(archive=archive, tag=tag, audio=audio):
                self.state.update(self.key, listened=0, archived=0, tag_removed=0, audio_deleted=0)
                self.readeck.finish.reset_mock()
                self.abs.delete_episode.reset_mock()
                self.abs.is_finished.reset_mock()
                self.worker.config = replace(self.config, archive_on_finish=archive,
                                             remove_tag_on_finish=tag, delete_audio_on_finish=audio)
                self.worker.finish_listened()
                self.assertEqual(self.readeck.finish.called, archive or tag)
                if archive or tag:
                    self.readeck.finish.assert_called_once_with("a", archive=archive, remove_tag="tts" if tag else None)
                self.assertEqual(self.abs.delete_episode.called, audio)
                self.assertEqual(self.abs.is_finished.called, archive or tag or audio)

    def test_readeck_failure_preserves_audio_and_retries_after_restart(self):
        self.prepare_listening()
        self.readeck.finish.side_effect = RemoteError("Readeck", "PATCH", 503)
        self.worker.finish_listened()
        self.abs.delete_episode.assert_not_called()
        self.assertEqual(self.job()["listened"], 1)
        self.assertIsNotNone(self.job()["finish_error"])
        self.readeck.finish.side_effect = None
        self.abs.is_finished.side_effect = AssertionError("Completion must be durable")
        restarted = Worker(self.config, self.state, self.readeck, self.kokoro, self.abs)
        restarted.finish_listened()
        self.abs.delete_episode.assert_called_once()
        self.assertIsNone(self.job()["finish_error"])

    def test_deletion_failure_does_not_repeat_readeck_updates(self):
        self.prepare_listening()
        self.abs.delete_episode.side_effect = RemoteError("Audiobookshelf", "DELETE", 503)
        self.worker.finish_listened()
        self.assertEqual(self.job()["audio_deleted"], 0)
        self.abs.delete_episode.side_effect = None
        self.worker.finish_listened()
        self.readeck.finish.assert_called_once()
        self.assertEqual(self.job()["audio_deleted"], 1)

    def test_finish_uses_saved_tag_and_destination(self):
        self.prepare_listening()
        self.worker.config = replace(self.config, label="other")
        self.worker.finish_listened()
        self.readeck.finish.assert_called_once_with("a", archive=True, remove_tag="tts")
        self.state.update(self.key, audio_deleted=0)
        self.worker.config = replace(self.config, abs_url="http://other")
        self.abs.delete_episode.reset_mock()
        self.worker.finish_listened()
        self.abs.delete_episode.assert_not_called()
        self.assertIn("Destination changed", self.job()["finish_error"])

    def test_once_runs_completion_pass(self):
        self.prepare_listening()
        with patch("readeck_tts.__main__.Config.from_env", return_value=self.config), \
                patch("readeck_tts.__main__.Worker", return_value=self.worker), \
                patch("readeck_tts.__main__.check_services"), \
                patch("sys.argv", ["readeck-tts", "once"]):
            main()
        self.assertEqual(self.job()["audio_deleted"], 1)
        self.assertEqual(self.kokoro.calls, 0)

    def test_lost_deletion_commit_retries_without_progress_or_readeck_changes(self):
        self.prepare_listening()
        update = self.state.update

        def lose_commit(key, **fields):
            if fields.get("audio_deleted"):
                raise RuntimeError("simulated crash before deletion commit")
            update(key, **fields)

        self.state.update = lose_commit
        self.worker.finish_listened()
        self.state.update = update
        self.abs.is_finished.side_effect = AssertionError("Deleted progress is unavailable")
        self.worker.finish_listened()
        self.readeck.finish.assert_called_once()
        self.assertEqual(self.abs.delete_episode.call_count, 2)
        self.assertEqual(self.job()["audio_deleted"], 1)

    def test_legacy_snapshot_uses_current_tag(self):
        self.prepare_listening()
        snapshot = json.loads(self.job()["snapshot"])
        snapshot.pop("label")
        self.state.update(self.key, snapshot=json.dumps(snapshot))
        self.worker.finish_listened()
        self.readeck.finish.assert_called_once_with("a", archive=True, remove_tag="tts")

    def test_missing_bookmark_does_not_prevent_audio_cleanup(self):
        self.prepare_listening()
        self.readeck.finish.side_effect = RemoteError("Readeck", "PATCH", 404)
        self.worker.finish_listened()
        self.abs.delete_episode.assert_called_once()

    def test_upload_failure_reuses_saved_audio_and_input(self):
        self.abs.fail = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        self.assertEqual(self.job()["attempts"], 0)
        self.assertEqual(self.job()["state"], "uploading")
        self.readeck.metadata["title"] = "Edited later"
        self.worker = Worker(replace(self.config, voice="af_bella"), self.state,
                             self.readeck, self.kokoro, self.abs)
        self.abs.fail = False
        self.retry_now()
        self.assertEqual(self.kokoro.calls, 1)
        snapshot = json.loads(self.job()["snapshot"])
        self.assertEqual(snapshot["voice"], "af_heart")
        self.assertEqual(snapshot["title"], "Test article")

    def test_partial_generation_is_discarded_and_retried(self):
        self.kokoro.fail = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        self.assertEqual(self.job()["state"], "generating")
        self.assertFalse(list(self.config.data_dir.rglob("*.part")))
        self.kokoro.fail = False
        self.retry_now()
        self.assertEqual(self.kokoro.calls, 2)
        self.assertEqual(len(self.abs.uploads), 1)

    def test_structure_and_pause_input_are_frozen_across_failed_generation(self):
        blocks = article_blocks('<p>Opening.</p><h2>A section</h2><p>Body.</p>')
        self.readeck.article = Mock(return_value=blocks)
        self.kokoro.fail = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        snapshot = json.loads(self.job()["snapshot"])
        self.assertEqual(snapshot["blocks"], [{"type": "title", "text": "Test article"}] + blocks)
        self.assertEqual(snapshot["text"], "Test article\n\nOpening.\n\nA section\n\nBody.")
        self.assertEqual(snapshot["speech_input"], "Test article\n\n[pause:1s]\n\nOpening."
                         "\n\n[pause:0.8s]\n\nA section\n\n[pause:0.6s]\n\nBody.")
        self.readeck.article.side_effect = AssertionError("Must reuse the saved snapshot")
        self.worker = Worker(self.config, self.state, self.readeck, self.kokoro, self.abs)
        self.kokoro.fail = False
        self.kokoro.generate = Mock(wraps=self.kokoro.generate)
        self.retry_now()
        self.assertEqual(self.kokoro.generate.call_args.args[0], snapshot)

    def test_legacy_snapshots_can_still_generate(self):
        snapshot = self.worker.snapshot(self.job())
        snapshot.pop("blocks")
        snapshot.pop("speech_input")
        snapshot.pop("provider")
        self.state.update(self.key, snapshot=json.dumps(snapshot))
        self.readeck.article = Mock(side_effect=AssertionError("Must reuse the saved snapshot"))
        self.kokoro.generate = Mock(wraps=self.kokoro.generate)
        self.worker.work()
        self.assertEqual(self.kokoro.generate.call_args.args[0], snapshot)
        self.assertEqual(self.job()["state"], "awaiting_index")

    def test_provider_switch_resumes_saved_job_with_original_provider(self):
        config = replace(self.config, provider="elevenlabs", model="eleven_v4_turbo",
                         voice="voice-one", elevenlabs_token="secret")
        self.worker = Worker(config, self.state, self.readeck, self.kokoro, self.abs)
        self.kokoro.fail = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        saved = json.loads(self.job()["snapshot"])
        self.assertEqual(saved["provider"], "elevenlabs")
        self.assertTrue(saved["speech_chunks"])
        new_config = replace(self.config, provider="gemini", model="gemini-3.8-flash-lite-tts",
                             voice="Sulafat", gemini_token="secret", elevenlabs_token="secret")
        current = FakeKokoro()
        self.kokoro.fail = False
        self.kokoro.session = Mock()
        self.worker = Worker(new_config, self.state, self.readeck, current, self.abs)
        self.addCleanup(self.worker.close)
        with patch("readeck_tts.worker.speech_client", return_value=self.kokoro) as factory:
            self.retry_now()
        factory.assert_called_once_with(new_config, "elevenlabs")
        self.assertEqual(current.calls, 0)
        self.assertEqual(json.loads(self.job()["snapshot"]), saved)

    def test_cached_audio_survives_provider_switch_without_old_credentials(self):
        self.abs.fail = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        self.abs.fail = False
        new_config = replace(self.config, provider="gemini", model="gemini-3.8-flash-lite-tts",
                             voice="Charon", gemini_token="secret")
        new_tts = FakeKokoro()
        self.worker = Worker(new_config, self.state, self.readeck, new_tts, self.abs)
        with patch("readeck_tts.worker.speech_client", side_effect=AssertionError("No old credentials needed")):
            self.retry_now()
        self.assertEqual(new_tts.calls, 0)
        self.assertEqual(self.kokoro.calls, 1)

    def test_each_new_article_selects_a_voice(self):
        self.worker.config = replace(self.config, voice="af_heart,af_bella")
        self.state.discover(self.config.readeck_url, [{"id": "b", "time": "2026-11-12T12:01:00Z"}])
        with patch("readeck_tts.worker.random.choice", side_effect=["af_bella", "af_heart"]) as choose:
            self.worker.work()
        self.assertEqual(choose.call_count, 2)
        for call in choose.call_args_list:
            self.assertEqual(call.args, (("af_heart", "af_bella"),))
        snapshots = [json.loads(self.state.get(job_key(self.config.readeck_url, article))["snapshot"])
                     for article in ("a", "b")]
        self.assertCountEqual([snapshot["voice"] for snapshot in snapshots], ["af_bella", "af_heart"])

    def test_selected_voice_survives_failed_generation_restart_and_config_change(self):
        self.worker.config = replace(self.config, voice="af_heart,af_bella")
        self.kokoro.fail = True
        with patch("readeck_tts.worker.random.choice", return_value="af_bella") as choose:
            with self.assertRaises(RemoteError):
                self.worker.work()
        choose.assert_called_once()
        self.assertEqual(json.loads(self.job()["snapshot"])["voice"], "af_bella")
        self.worker = Worker(replace(self.config, voice="af_heart"), self.state,
                             self.readeck, self.kokoro, self.abs)
        generate = self.kokoro.generate
        self.kokoro.generate = Mock(wraps=generate)
        self.kokoro.fail = False
        with patch("readeck_tts.worker.random.choice") as choose:
            self.retry_now()
        choose.assert_not_called()
        self.assertEqual(self.kokoro.generate.call_args.args[0]["voice"], "af_bella")
        self.assertEqual(self.kokoro.calls, 2)

    def test_crash_after_audio_rename_recovers_without_regeneration(self):
        update = self.state.update

        def crash(key, **fields):
            if fields.get("state") == "audio_ready":
                raise RuntimeError("simulated crash after saving audio")
            update(key, **fields)

        self.state.update = crash
        self.worker.work()
        self.state.update = update
        self.assertEqual(self.job()["state"], "generating")
        self.retry_now()
        self.assertEqual(self.kokoro.calls, 1)
        self.assertEqual(len(self.abs.uploads), 1)

    def test_lost_upload_response_reconciles_existing_episode(self):
        self.abs.lost_response = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        self.assertEqual(self.job()["state"], "uploading")
        self.retry_now()
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(len(self.abs.uploads), 1)
        self.assertEqual(self.kokoro.calls, 1)

    def test_accepted_upload_waits_for_index_without_reuploading(self):
        self.abs.auto_index = False
        self.worker.work()
        for _ in range(3):
            self.retry_now()
        self.assertEqual(self.job()["state"], "awaiting_index")
        self.assertEqual(len(self.abs.uploads), 1)
        self.assertEqual(self.kokoro.calls, 1)
        self.assertTrue((self.config.data_dir / "articles" / self.key / "audio.mp3").exists())

    def test_ignored_article_is_reconsidered_on_label_change(self):
        self.readeck.metadata["labels"] = []
        self.worker.work()
        self.assertEqual(self.job()["state"], "ignored")
        self.readeck.metadata["labels"] = ["tts"]
        self.worker.discover()
        self.worker.work()
        self.assertEqual(self.kokoro.calls, 1)

    def test_custom_tag_and_podcast_name_flow_into_snapshot_and_audio_metadata(self):
        config = replace(self.config, label="listen", podcast="Morning Reads", voice="af_bella")
        self.readeck.metadata["labels"] = ["listen"]
        worker = Worker(config, self.state, self.readeck, self.kokoro, self.abs)
        worker.work()
        snapshot = json.loads(self.job()["snapshot"])
        self.assertEqual(snapshot["voice"], "af_bella")
        self.assertEqual(snapshot["destination"]["podcast"], "Morning Reads")
        audio = MP3(config.data_dir / "articles" / self.key / "audio.mp3")
        self.assertEqual(str(audio.tags["TALB"]), "Morning Reads")
        self.assertEqual(str(audio.tags["TPE1"]), "Morning Reads")

    def test_deletion_cancels_pending_work(self):
        self.readeck.events[0]["type"] = "delete"
        self.worker.discover()
        self.worker.work()
        self.assertEqual(self.job()["state"], "cancelled")
        self.assertEqual(self.kokoro.calls, 0)

    def test_existing_remote_episode_avoids_generation(self):
        self.abs.indexed[self.key] = ("show", "existing")
        self.worker.work()
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(self.kokoro.calls, 0)

    def test_destination_changes_do_not_silently_duplicate(self):
        self.abs.fail = True
        with self.assertRaises(RemoteError):
            self.worker.work()
        self.worker = Worker(replace(self.config, podcast="Other show"), self.state,
                             self.readeck, self.kokoro, self.abs)
        self.abs.fail = False
        with self.assertRaisesRegex(ConfigurationError, "Destination changed"):
            self.retry_now()
        self.assertEqual(len(self.abs.uploads), 0)

    def test_revoked_credentials_stop_batch_without_charging_article_retries(self):
        self.state.discover(self.config.readeck_url, [{"id": "b", "time": "2026-11-12T12:01:00Z"}])
        self.readeck.bookmark = Mock(side_effect=RemoteError("Readeck", "GET bookmark", 401))
        with self.assertRaisesRegex(ConfigurationError, "credentials were rejected"):
            self.worker.work()
        self.assertEqual(self.readeck.bookmark.call_count, 1)
        self.assertEqual(self.job()["attempts"], 0)
        self.assertEqual(self.kokoro.calls, 0)

    def test_upload_permission_revoked_preserves_completed_audio(self):
        self.abs.upload = Mock(side_effect=RemoteError("Audiobookshelf", "POST upload", 403))
        with self.assertRaisesRegex(ConfigurationError, "access denied"):
            self.worker.work()
        self.assertEqual(self.job()["attempts"], 0)
        self.abs.upload = FakeABS.upload.__get__(self.abs)
        self.retry_now()
        self.assertEqual(self.kokoro.calls, 1)

    def test_checkpoint_and_inbox_rollback_together(self):
        previous = self.state.cursor(self.config.readeck_url)
        events = [{"id": "new", "time": "2026-11-13T12:00:00Z"},
                  {"time": "2026-11-13T12:00:00Z"}]
        with self.assertRaises(KeyError):
            self.state.discover(self.config.readeck_url, events)
        self.assertEqual(self.state.cursor(self.config.readeck_url), previous)
        self.assertIsNone(self.state.get(job_key(self.config.readeck_url, "new")))

    def test_second_worker_cannot_claim_same_data_directory(self):
        with data_lock(self.config.data_dir):
            with self.assertRaises(RuntimeError):
                with data_lock(self.config.data_dir):
                    pass

    def test_reconcile_records_existing_episode_without_generation_or_upload(self):
        self.worker.work()
        self.assertEqual(self.job()["state"], "awaiting_index")
        self.abs.resolve = Mock()
        with patch("readeck_tts.__main__.Config.from_env", return_value=self.config), \
                patch("readeck_tts.__main__.Worker", return_value=self.worker), \
                patch("sys.argv", ["readeck-tts", "reconcile", "--key", self.key]), \
                patch("builtins.print"):
            main()
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(self.kokoro.calls, 1)
        self.assertEqual(len(self.abs.uploads), 1)
        self.assertFalse((self.config.data_dir / "articles" / self.key / "audio.mp3").exists())

    def test_cleanup_failure_does_not_undo_delivery_and_retries_locally(self):
        self.worker.work()
        audio = self.config.data_dir / "articles" / self.key / "audio.mp3"
        self.state.update(self.key, next_attempt=0)
        with patch.object(Path, "unlink", side_effect=PermissionError("disk permissions")):
            self.worker.work()
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(self.job()["cleanup_pending"], 1)
        self.assertEqual(self.job()["attempts"], 0)
        self.assertTrue(audio.exists())
        self.worker.work()
        self.assertFalse(audio.exists())
        self.assertEqual(self.job()["cleanup_pending"], 0)
        self.assertIsNone(self.job()["error"])
        self.assertEqual(self.kokoro.calls, 1)
        self.assertEqual(len(self.abs.uploads), 1)

    def test_restart_cleans_up_crash_after_delivery_commit(self):
        self.worker.work()
        self.state.update(self.key, state="complete", abs_item_id="show",
                          abs_episode_id="episode", cleanup_pending=1)
        directory = self.config.data_dir / "articles" / self.key
        (directory / "audio.mp3.part").write_bytes(b"stale partial")
        Worker(self.config, self.state, self.readeck, self.kokoro, self.abs)
        self.assertFalse(directory.exists())
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(self.kokoro.calls, 1)

    def test_failed_delivery_commit_preserves_audio(self):
        self.worker.work()
        job = self.job()
        update = self.state.update

        def fail_commit(key, **fields):
            if fields.get("state") == "complete":
                raise RuntimeError("cannot commit completion")
            update(key, **fields)

        self.state.update = fail_commit
        with self.assertRaisesRegex(RuntimeError, "cannot commit completion"):
            self.worker.process(job)
        self.assertTrue((self.config.data_dir / "articles" / self.key / "audio.mp3").exists())
        self.assertEqual(self.job()["state"], "awaiting_index")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("http://readeck", "secret", "http://abs", "secret")

    def test_completion_endpoints_quote_ids_and_preserve_unrelated_tags(self):
        client = Readeck(self.config)
        self.addCleanup(client.session.close)
        client.request = Mock()
        client.request.return_value.__enter__ = Mock()
        client.request.return_value.__exit__ = Mock(return_value=False)
        client.finish("article/id", archive=True, remove_tag="listen")
        client.request.assert_called_once_with("PATCH", "/bookmarks/article%2Fid",
                                               json={"is_archived": True, "remove_labels": ["listen"]})
        abs_client = Audiobookshelf(self.config)
        self.addCleanup(abs_client.session.close)
        abs_client.json = Mock(return_value={"isFinished": True})
        self.assertTrue(abs_client.is_finished("show/id", "episode/id"))
        abs_client.json.assert_called_once_with("GET", "/me/progress/show%2Fid/episode%2Fid")
        abs_client.json.side_effect = RemoteError("Audiobookshelf", "GET", 404)
        self.assertFalse(abs_client.is_finished("show", "episode"))
        abs_client.json.side_effect = RemoteError("Audiobookshelf", "GET", 503)
        with self.assertRaises(RemoteError):
            abs_client.is_finished("show", "episode")
        abs_client.request = client.request
        abs_client.request.reset_mock()
        abs_client.delete_episode("show/id", "episode/id")
        abs_client.request.assert_called_once_with("DELETE", "/podcasts/show%2Fid/episode/episode%2Fid", params={"hard": 1})
        abs_client.request.side_effect = RemoteError("Audiobookshelf", "DELETE", 404)
        abs_client.delete_episode("show", "episode")
        abs_client.request.side_effect = RemoteError("Audiobookshelf", "DELETE", 403)
        with self.assertRaises(RemoteError):
            abs_client.delete_episode("show", "episode")

    def test_podcast_episode_lookup_follows_pagination_and_filename(self):
        client = Audiobookshelf(self.config)
        client.library_id, client.folder_id = "library", "folder"
        client.json = Mock(side_effect=[
            {"results": [{"id": "other", "path": "/podcasts/Other", "folderId": "folder"}], "total": 101},
            {"results": [{"id": "show", "path": "/podcasts/Readeck", "folderId": "folder"}], "total": 101},
            {"id": "show", "media": {"episodes": [{"id": "episode", "audioFile": {
                "metadata": {"filename": "rdtts-abc.mp3"}}}]}},
        ])
        self.assertEqual(client.find_item("abc"), ("show", "episode"))
        self.assertEqual(client.json.call_args_list[1].kwargs["params"]["page"], 1)

    def test_ambiguous_folder_requires_configuration(self):
        client = Audiobookshelf(self.config)
        client.json = Mock(return_value={"libraries": [{"id": "library", "name": "Podcasts",
                          "mediaType": "podcast", "folders": [{"id": "one"}, {"id": "two"}]}]})
        with self.assertRaisesRegex(RuntimeError, "AUDIOBOOKSHELF_FOLDER_ID"):
            client.resolve()

    def test_article_extraction_preserves_inline_text_and_paragraphs(self):
        self.assertEqual(article_text('<p>Hello <em>world</em>.</p><script>bad()</script><p>A &amp; B</p>'),
                         "Hello world.\n\nA & B")

    def test_readeck_article_returns_structured_html_blocks(self):
        client = Readeck(self.config)
        self.addCleanup(client.session.close)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.text = "<h2>Section</h2><p>Body.</p>"
        client.request = Mock(return_value=response)
        self.assertEqual(client.article("id/with/slash"), [
            {"type": "heading", "text": "Section", "level": 2},
            {"type": "paragraph", "text": "Body."},
        ])
        client.request.assert_called_once_with("GET", "/bookmarks/id%2Fwith%2Fslash/article")

    def test_kokoro_sends_saved_pause_input_and_supports_legacy_plain_text(self):
        client = Kokoro(self.config)
        self.addCleanup(client.session.close)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [MP3_AUDIO]
        client.request = Mock(return_value=response)
        snapshot = {"text": "Title\n\nBody.", "voice": "af_heart", "model": "kokoro"}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audio.mp3"
            for speech_input in (None, "Title\n\n[pause:1s]\n\nBody."):
                with self.subTest(speech_input=speech_input):
                    if speech_input:
                        snapshot["speech_input"] = speech_input
                    client.generate(snapshot, output)
                    payload = client.request.call_args.kwargs["json"]
                    self.assertEqual(payload["input"], speech_input or snapshot["text"])
                    self.assertEqual(payload["speed"], 1.0)
                    self.assertEqual(output.read_bytes(), MP3_AUDIO)


if __name__ == "__main__":
    unittest.main()
