import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from readeck_tts.__main__ import main
from readeck_tts.clients import API, Audiobookshelf, ConfigurationError, RemoteError, check_services
from readeck_tts.config import Config, log_level_from_env


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("http://readeck", "secret", "http://abs", "secret")

    def required_environment(self):
        return {"READECK_URL": "https://reader.example.com", "READECK_API_TOKEN": "reader-secret",
                "AUDIOBOOKSHELF_URL": "https://audio.example.com", "AUDIOBOOKSHELF_API_TOKEN": "audio-secret",
                "KOKORO_BASE_URL": "http://speech:8880/v1"}

    def test_minimal_configuration_has_generic_defaults_and_no_personal_endpoint(self):
        with patch.dict(os.environ, self.required_environment(), clear=True):
            config = Config.from_env()
        self.assertEqual(config.kokoro_url, "http://speech:8880/v1")
        self.assertEqual((config.archive_on_finish, config.remove_tag_on_finish, config.delete_audio_on_finish),
                         (False, False, False))
        self.assertEqual((config.label, config.library, config.podcast, config.voice),
                         ("tts", "Podcasts", "Readeck", "af_heart"))
        self.assertNotIn("reader-secret", repr(config))
        self.assertNotIn("audio-secret", repr(config))

    def test_finish_options_default_off_and_parse_explicit_opt_ins(self):
        names = ("ARCHIVE_ON_FINISH", "REMOVE_TAG_ON_FINISH", "DELETE_AUDIO_ON_FINISH")
        for value, expected in (("", False), ("true", True), ("false", False), ("OFF", False)):
            with patch.dict(os.environ, self.required_environment() | dict.fromkeys(names, value), clear=True):
                config = Config.from_env()
                self.assertEqual((config.archive_on_finish, config.remove_tag_on_finish,
                                  config.delete_audio_on_finish), (expected,) * 3)
        for name in names:
            with patch.dict(os.environ, self.required_environment() | {name: "maybe"}, clear=True):
                with self.assertRaisesRegex(ValueError, name):
                    Config.from_env()

    def test_delete_permission_required_only_when_audio_deletion_enabled(self):
        readeck, kokoro, abs_client = Mock(), Mock(), Mock()
        abs_client.json.return_value = {"permissions": {"upload": True}}
        kokoro.json.return_value = {"voices": ["af_heart"]}
        with self.assertRaisesRegex(ConfigurationError, "DELETE_AUDIO_ON_FINISH=false"):
            check_services(replace(self.config, delete_audio_on_finish=True), readeck, kokoro, abs_client)
        check_services(replace(self.config, delete_audio_on_finish=False), readeck, kokoro, abs_client)

    def test_kokoro_endpoint_must_be_explicit(self):
        environment = self.required_environment()
        environment.pop("KOKORO_BASE_URL")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "KOKORO_BASE_URL"):
                Config.from_env()

    def test_configuration_overrides_trim_whitespace_and_preserve_url_subpaths(self):
        environment = self.required_environment() | {
            "READECK_URL": " https://reader.example.com/reader/ ",
            "READECK_TAG": " read-aloud ", "AUDIOBOOKSHELF_LIBRARY": " Listening ",
            "AUDIOBOOKSHELF_PODCAST": " Morning   Reads ", "KOKORO_VOICE": " af_bella ",
            "KOKORO_MODEL": " another-model ", "POLL_INTERVAL_SECONDS": "60",
            "RETRY_INTERVAL_SECONDS": "15", "TTS_TIMEOUT_SECONDS": "600",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
        self.assertEqual(config.readeck_url, "https://reader.example.com/reader")
        self.assertEqual((config.label, config.library, config.podcast, config.voice, config.model),
                         ("read-aloud", "Listening", "Morning Reads", "af_bella", "another-model"))
        self.assertEqual((config.poll_interval, config.retry_interval, config.tts_timeout), (60, 15, 600))

    def test_blank_optional_settings_use_defaults_instead_of_selecting_every_article(self):
        environment = self.required_environment() | {
            "READECK_TAG": " ", "KOKORO_VOICE": "", "AUDIOBOOKSHELF_PODCAST": " ",
            "POLL_INTERVAL_SECONDS": "", "DATA_DIR": " ",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
        self.assertEqual((config.label, config.voice, config.podcast), ("tts", "af_heart", "Readeck"))
        self.assertEqual(config.poll_interval, 300)
        self.assertEqual(config.data_dir, Path("data"))

    def test_bad_intervals_identify_the_setting(self):
        for name in ("POLL_INTERVAL_SECONDS", "RETRY_INTERVAL_SECONDS", "TTS_TIMEOUT_SECONDS"):
            for value in ("0", "-1", "abc", "1.5"):
                with self.subTest(name=name, value=value), \
                        patch.dict(os.environ, self.required_environment() | {name: value}, clear=True):
                    with self.assertRaisesRegex(ValueError, name):
                        Config.from_env()

    def test_voice_list_trims_names_and_removes_duplicates(self):
        environment = self.required_environment() | {"KOKORO_VOICE": " af_heart, af_bella ,af_heart "}
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
        self.assertEqual(config.voices, ("af_heart", "af_bella"))
        self.assertEqual(self.config.voices, ("af_heart",))

    def test_voice_list_rejects_empty_entries(self):
        for value in (",", "af_heart,", ",af_heart", "af_heart, ,af_bella"):
            with self.subTest(value=value), \
                    patch.dict(os.environ, self.required_environment() | {"KOKORO_VOICE": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "KOKORO_VOICE"):
                    Config.from_env()

    def test_bad_base_urls_identify_the_setting_without_echoing_secrets(self):
        for value in ("file:///tmp/api", "https://user:secret@example.com", "http://speech:bad/v1",
                      "http://speech/v1?token=secret", "http://speech/v1#fragment", "http://bad host/v1"):
            with self.subTest(value=value), \
                    patch.dict(os.environ, self.required_environment() | {"KOKORO_BASE_URL": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "KOKORO_BASE_URL") as caught:
                    Config.from_env()
                self.assertNotIn("secret", str(caught.exception))

    def test_log_level_defaults_validates_and_accepts_lowercase(self):
        for value, expected in (("", "INFO"), ("debug", "DEBUG"), (" Warning ", "WARNING")):
            with patch.dict(os.environ, {"LOG_LEVEL": value}, clear=True):
                self.assertEqual(log_level_from_env(), expected)
        with patch.dict(os.environ, {"LOG_LEVEL": "verbose"}, clear=True):
            with self.assertRaisesRegex(ValueError, "LOG_LEVEL"):
                log_level_from_env()

    def test_missing_token_names_the_variable_without_disclosing_other_token(self):
        with patch.dict(os.environ, {"READECK_URL": "http://readeck", "READECK_API_TOKEN": "secret",
                                     "AUDIOBOOKSHELF_URL": "http://abs"}, clear=True):
            with self.assertRaisesRegex(ValueError, "AUDIOBOOKSHELF_API_TOKEN") as caught:
                Config.from_env()
            self.assertNotIn("secret", str(caught.exception))

    def test_http_error_never_includes_response_body_or_token(self):
        api = API("Readeck", "http://readeck", "super-secret-token")
        response = requests.Response()
        response.status_code = 401
        response._content = b"a remote response containing super-secret-token"
        response._content_consumed = True
        api.session.request = Mock(return_value=response)
        with self.assertRaises(RemoteError) as caught:
            api.request("GET", "/profile")
        with self.assertRaises(ConfigurationError) as configuration:
            caught.exception.check_credentials()
        self.assertNotIn("super-secret-token", str(configuration.exception))

    def test_missing_library_is_operator_error_and_clears_cached_target(self):
        client = Audiobookshelf(self.config)
        client.library_id, client.folder_id = "old-library", "old-folder"
        client.json = Mock(return_value={"libraries": []})
        with self.assertRaisesRegex(ConfigurationError, "missing or inaccessible"):
            client.resolve()
        self.assertIsNone(client.library_id)
        self.assertIsNone(client.folder_id)

    def test_wrong_library_type_and_invalid_folder_fail_preflight(self):
        client = Audiobookshelf(self.config)
        for library in (
            {"name": "Podcasts", "id": "lib", "mediaType": "book", "folders": [{"id": "folder"}]},
            {"name": "Podcasts", "id": "lib", "mediaType": "podcast", "folders": []},
        ):
            with self.subTest(library=library):
                client.json = Mock(return_value={"libraries": [library]})
                with self.assertRaises(ConfigurationError):
                    client.resolve()

    def test_preflight_rejects_upload_permission_before_reading_voice_list(self):
        readeck, kokoro, abs_client = Mock(), Mock(), Mock()
        abs_client.json.return_value = {"permissions": {"upload": False}}
        with self.assertRaisesRegex(ConfigurationError, "upload permission"):
            check_services(self.config, readeck, kokoro, abs_client)
        kokoro.json.assert_not_called()
        abs_client.resolve.assert_not_called()

    def test_preflight_rejects_missing_voice(self):
        readeck, kokoro, abs_client = Mock(), Mock(), Mock()
        abs_client.json.return_value = {"permissions": {"upload": True, "delete": True}}
        kokoro.json.return_value = {"voices": ["af_bella"]}
        with self.assertRaisesRegex(ConfigurationError, "KOKORO_VOICE"):
            check_services(self.config, readeck, kokoro, abs_client)

    def test_preflight_checks_every_configured_voice(self):
        config = replace(self.config, voice="af_heart,af_bella")
        readeck, kokoro, abs_client = Mock(), Mock(), Mock()
        abs_client.json.return_value = {"permissions": {"upload": True, "delete": True}}
        for voices in (["af_heart", "af_bella"], [{"id": "af_heart"}, {"id": "af_bella"}]):
            with self.subTest(voices=voices):
                kokoro.json.return_value = {"voices": voices}
                self.assertEqual(check_services(config, readeck, kokoro, abs_client)["kokoro"], "ok")
        kokoro.json.return_value = {"voices": ["af_heart"]}
        with self.assertRaisesRegex(ConfigurationError, "af_bella.*KOKORO_VOICE"):
            check_services(config, readeck, kokoro, abs_client)

    def test_scan_permission_is_optional_but_expired_token_is_not(self):
        client = Audiobookshelf(self.config)
        client.library_id = "library"
        client.request = Mock(side_effect=RemoteError("Audiobookshelf", "scan", 403))
        client.request_scan()
        client.request.side_effect = RemoteError("Audiobookshelf", "scan", 401)
        with self.assertRaises(ConfigurationError):
            client.request_scan()

    def test_startup_configuration_error_prevents_discovery_and_work(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Mock()
            config = Config("http://readeck", "secret", "http://abs", "secret", data_dir=Path(directory))
            with patch("readeck_tts.__main__.Config.from_env", return_value=config), \
                    patch("readeck_tts.__main__.Worker", return_value=worker), \
                    patch("readeck_tts.__main__.check_services", side_effect=ConfigurationError("missing library")), \
                    patch("sys.argv", ["readeck-tts", "run"]), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 1)
            worker.discover.assert_not_called()
            worker.work.assert_not_called()

    def test_status_works_without_remote_credentials(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"DATA_DIR": directory}, clear=True), \
                patch("readeck_tts.__main__.load_dotenv"), \
                patch("readeck_tts.__main__.Config.from_env", side_effect=AssertionError("must not need credentials")), \
                patch("sys.argv", ["readeck-tts", "status"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main()
            self.assertEqual(json.loads(output.getvalue()), [])

    def test_check_does_not_create_state_or_acquire_a_worker_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "unused"
            config = replace(self.config, data_dir=data_dir)
            clients = [Mock(), Mock(), Mock()]
            with patch("readeck_tts.__main__.Config.from_env", return_value=config), \
                    patch("readeck_tts.__main__.Readeck", return_value=clients[0]), \
                    patch("readeck_tts.__main__.speech_client", return_value=clients[1]), \
                    patch("readeck_tts.__main__.Audiobookshelf", return_value=clients[2]), \
                    patch("readeck_tts.__main__.check_services", return_value={"kokoro": "ok"}), \
                    patch("sys.argv", ["readeck-tts", "check"]), patch("builtins.print"):
                main()
            self.assertFalse(data_dir.exists())
            for client in clients:
                client.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
