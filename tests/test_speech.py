import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from mutagen.mp3 import MP3
import requests

from readeck_tts.audio import join_audio, valid_chunk, write_audio
from readeck_tts.clients import ConfigurationError, RemoteError, check_services
from readeck_tts.config import Config, SPEECH_DEFAULTS
from readeck_tts.narration import neighboring_text, render, speech_snapshot
from readeck_tts.text import article_blocks
from readeck_tts.speech import ElevenLabs, Gemini, speech_client


MP3_AUDIO = (bytes.fromhex("fffb9000") + bytes(413)) * 20


class HostedConfigurationTests(unittest.TestCase):
    def environment(self, provider):
        return {"READECK_URL": "http://reader", "READECK_API_TOKEN": "reader-secret",
                "AUDIOBOOKSHELF_URL": "http://abs", "AUDIOBOOKSHELF_API_TOKEN": "abs-secret",
                "TTS_PROVIDER": provider, "ELEVENLABS_API_KEY": "eleven-secret", "GEMINI_API_KEY": "gemini-secret"}

    def test_only_selected_provider_credentials_are_required(self):
        for provider, key in (("elevenlabs", "ELEVENLABS_API_KEY"), ("gemini", "GEMINI_API_KEY")):
            environment = self.environment(provider)
            other = "GEMINI_API_KEY" if provider == "elevenlabs" else "ELEVENLABS_API_KEY"
            environment.pop(other)
            with self.subTest(provider=provider), patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()
                self.assertEqual((config.model, config.voice), SPEECH_DEFAULTS[provider])
                self.assertEqual(config.kokoro_url, "")
                self.assertNotIn("secret", repr(config))
            environment.pop(key)
            with patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(ValueError, key):
                Config.from_env()

    def test_hosted_voice_pools_and_inactive_provider_credentials_are_preserved(self):
        for provider in ("elevenlabs", "gemini"):
            variable = provider.upper() + "_VOICE"
            with self.subTest(provider=provider), patch.dict(os.environ,
                    self.environment(provider) | {variable: " first, second ,first "}, clear=True):
                config = Config.from_env()
                self.assertEqual(config.voices, ("first", "second"))
                self.assertEqual(config.elevenlabs_token, "eleven-secret")
                self.assertEqual(config.gemini_token, "gemini-secret")
            with patch.dict(os.environ, self.environment(provider) | {variable: "first,"}, clear=True):
                with self.assertRaisesRegex(ValueError, variable):
                    Config.from_env()

    def test_unsupported_provider_and_model_families_fail_before_network_calls(self):
        for provider, variable, model in (("gemini", "GEMINI_MODEL", "gemini-2.5-pro-preview-tts"),
                                          ("elevenlabs", "ELEVENLABS_MODEL", "eleven_multilingual_v2")):
            with self.subTest(provider=provider), patch.dict(os.environ,
                    self.environment(provider) | {variable: model}, clear=True):
                with self.assertRaisesRegex(ValueError, variable):
                    Config.from_env()
        with patch.dict(os.environ, self.environment("openai"), clear=True):
            with self.assertRaisesRegex(ValueError, "TTS_PROVIDER"):
                Config.from_env()


class HostedSpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config("http://reader", "secret", "http://abs", "secret",
                             provider="elevenlabs", model="eleven_v4_turbo", voice="voice",
                             elevenlabs_token="eleven-secret", gemini_token="gemini-secret")
        self.client = ElevenLabs(self.config)
        self.addCleanup(self.client.session.close)
        self.snapshot = {"provider": "elevenlabs", "model": "eleven_v4_turbo", "voice": "voice",
                         "speech_chunks": ["First chunk.", "Second chunk."],
                         "speech_options": {"stability": 0.5, "similarity_boost": 0.75}}
        self.output = self.root / "audio.mp3.part"

    def test_preflight_checks_active_model_and_all_voices_without_generating(self):
        self.client.json = Mock(side_effect=[
            [{"model_id": "eleven_v4_turbo", "can_do_text_to_speech": True}],
            {"voice_id": "one"}, {"voice_id": "two"},
        ])
        readeck, abs_client = Mock(), Mock()
        abs_client.json.return_value = {"permissions": {"upload": True, "delete": True}}
        config = replace(self.config, voice="one,two")
        with patch("readeck_tts.speech.require_ffmpeg"):
            result = check_services(config, readeck, self.client, abs_client)
        self.assertEqual(result["elevenlabs"], "ok")
        self.assertTrue(all(call.args[0] == "GET" for call in self.client.json.call_args_list))
        self.assertEqual(self.client.json.call_args_list[-1].args[1], "/v1/voices/two")

    def test_gemini_preflight_handles_voice_pagination_and_missing_names(self):
        client = Gemini(self.config)
        self.addCleanup(client.session.close)
        client.json = Mock(side_effect=[{}, {"voices": [], "next_page_token": "next"},
                                       {"voices": [{"id": "charon", "display_name": "Charon"}]}])
        with patch("readeck_tts.speech.require_ffmpeg"):
            client.check("gemini-3.8-flash-lite-tts", ("Charon",))
        self.assertEqual(client.json.call_args.kwargs["params"]["page_token"], "next")
        client.json = Mock(side_effect=[{}, {"voices": []}])
        with patch("readeck_tts.speech.require_ffmpeg"), self.assertRaisesRegex(ConfigurationError, "GEMINI_VOICE"):
            client.check("gemini-3.8-flash-lite-tts", ("missing",))

    def test_interrupted_second_chunk_reuses_first_across_restart(self):
        calls = []
        fail = True

        def generate_chunk(snapshot, text, output):
            calls.append(text)
            if fail and text == "Second chunk.":
                output.write_bytes(b"partial")
                raise RemoteError("ElevenLabs", "download")
            output.write_bytes(MP3_AUDIO)
            return {"character-cost": "10"}

        self.client.generate_chunk = generate_chunk
        with patch("readeck_tts.speech.require_ffmpeg"), self.assertRaises(RemoteError):
            self.client.generate(self.snapshot, self.output)
        self.assertTrue((self.root / "chunks/000000.mp3").exists())
        self.assertFalse(list(self.root.rglob("*.part")))
        fail = False
        restarted = ElevenLabs(self.config)
        self.addCleanup(restarted.session.close)
        restarted.generate_chunk = generate_chunk
        with patch("readeck_tts.speech.require_ffmpeg"), patch("readeck_tts.speech.join_audio") as join:
            restarted.generate(self.snapshot, self.output)
        self.assertEqual(calls, ["First chunk.", "Second chunk.", "Second chunk."])
        self.assertEqual(len(join.call_args.args[0]), 2)
        receipt = json.loads((self.root / "chunks/000000.json").read_text())
        self.assertEqual(receipt["usage"]["character-cost"], "10")

    def test_failed_assembly_reuses_all_completed_audio(self):
        def generate_chunk(snapshot, text, output):
            output.write_bytes(MP3_AUDIO)
            return {}
        self.client.generate_chunk = Mock(side_effect=generate_chunk)
        with patch("readeck_tts.speech.require_ffmpeg"), \
                patch("readeck_tts.speech.join_audio", side_effect=RuntimeError("conversion failed")):
            with self.assertRaisesRegex(RuntimeError, "conversion failed"):
                self.client.generate(self.snapshot, self.output)
        with patch("readeck_tts.speech.require_ffmpeg"), patch("readeck_tts.speech.join_audio"):
            self.client.generate(self.snapshot, self.output)
        self.assertEqual(self.client.generate_chunk.call_count, 2)

    def test_context_is_sent_separately_and_preserved_when_resuming(self):
        self.snapshot["speech_contexts"] = [
            {"next_text": "Second chunk."}, {"previous_text": "First chunk."}]
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {}
        response.iter_content.return_value = [MP3_AUDIO]
        self.client.request = Mock(side_effect=[response, RemoteError("ElevenLabs", "download")])
        with patch("readeck_tts.speech.require_ffmpeg"), self.assertRaises(RemoteError):
            self.client.generate(self.snapshot, self.output)
        first = self.client.request.call_args_list[0].kwargs["json"]
        self.assertEqual(first["text"], "First chunk.")
        self.assertEqual(first["next_text"], "Second chunk.")
        self.assertNotIn("previous_text", first)
        restarted = ElevenLabs(self.config)
        self.addCleanup(restarted.session.close)
        restarted.request = Mock(return_value=response)
        with patch("readeck_tts.speech.require_ffmpeg"), patch("readeck_tts.speech.join_audio"):
            restarted.generate(self.snapshot, self.output)
        restarted.request.assert_called_once()
        second = restarted.request.call_args.kwargs["json"]
        self.assertEqual(second["text"], "Second chunk.")
        self.assertEqual(second["previous_text"], "First chunk.")
        self.assertNotIn("next_text", second)

    def test_legacy_plans_send_no_context_and_invalid_plans_fail_before_generation(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {}
        response.iter_content.return_value = [MP3_AUDIO]
        self.client.request = Mock(return_value=response)
        self.client.generate_chunk(self.snapshot, "First chunk.", self.output)
        payload = self.client.request.call_args.kwargs["json"]
        self.assertNotIn("previous_text", payload)
        self.assertNotIn("next_text", payload)
        self.client.request.reset_mock()
        for contexts in ([], None, [{}, {"text": "unexpected"}], [{}, {"next_text": 5}]):
            with self.subTest(contexts=contexts), patch("readeck_tts.speech.require_ffmpeg"):
                with self.assertRaisesRegex(ConfigurationError, "context plan"):
                    self.client.generate(self.snapshot | {"speech_contexts": contexts}, self.output)
        self.client.request.assert_not_called()

    def test_invalid_audio_is_not_committed_to_the_cache(self):
        def bad_audio(snapshot, text, output):
            output.write_bytes(b"not audio")
            return {}
        self.client.generate_chunk = bad_audio
        with patch("readeck_tts.speech.require_ffmpeg"), self.assertRaisesRegex(RuntimeError, "invalid"):
            self.client.generate(self.snapshot, self.output)
        self.assertFalse(list((self.root / "chunks").iterdir()))

    def test_provider_errors_do_not_echo_credentials_or_article_text(self):
        for status, error_type in ((400, ConfigurationError), (402, ConfigurationError),
                                    (404, ConfigurationError), (429, RemoteError), (500, RemoteError)):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                response._content = b"eleven-secret and private article text"
                response._content_consumed = True
                self.client.session.request = Mock(return_value=response)
                with self.assertRaises(error_type) as caught:
                    self.client.request("POST", "/v1/text-to-speech/voice")
                self.assertNotIn("eleven-secret", str(caught.exception))
                self.assertNotIn("private article", str(caught.exception))

    def test_gemini_incomplete_response_is_not_accepted_as_audio(self):
        client = Gemini(self.config)
        self.addCleanup(client.session.close)
        client.json = Mock(return_value={"status": "incomplete"})
        snapshot = self.snapshot | {"provider": "gemini", "model": "gemini-3.8-flash-lite-tts",
                                    "voice": "Charon", "speech_options": {"style": "saved style"}}
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            client.generate_chunk(snapshot, "Input.", self.output)
        self.assertFalse(self.output.exists())
        payload = client.json.call_args.kwargs["json"]
        self.assertEqual(payload["input"][0]["content"][0]["annotations"][0]["style"], "saved style")

    def test_missing_credentials_for_saved_provider_is_an_operator_error(self):
        with self.assertRaisesRegex(ConfigurationError, "GEMINI_API_KEY"):
            speech_client(replace(self.config, gemini_token=""), "gemini")
        with self.assertRaisesRegex(ConfigurationError, "KOKORO_BASE_URL"):
            speech_client(self.config, "kokoro")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed for the native audio assembly test")
    def test_real_ffmpeg_assembles_native_wav_chunks_into_mp3(self):
        pcm = b"\x00\x00" * 24000
        parts = []
        for index in range(2):
            path = self.root / f"{index:06d}.wav"
            write_audio([(base64.b64encode(pcm).decode(), "audio/L16;rate=24000")], path)
            parts.append(path)
        join_audio(parts, self.output)
        self.assertTrue(valid_chunk(self.output, "mp3"))
        self.assertAlmostEqual(MP3(self.output).info.length, 2, delta=0.1)


class NarrationPlanTests(unittest.TestCase):
    def test_cleanup_keeps_prose_and_shortens_destinations_for_all_providers(self):
        blocks = article_blocks(
            '<p>Read <a href="https://example.com/report">the report</a> '
            '(https://example.com/wiki/Foo_(bar)).</p>'
            '<p>https://example.com/standalone?tracking=1</p><p>example.org/guide</p><p>✦</p><p>***</p>'
            '<pre><code>print("Skip this code")</code></pre><pre>Keep this verse.</pre>'
            '<p>Visit https://www.example.com/report?tracking=1#part.</p>'
            '<p>Use <code>npm</code> for the 3.5% sample.</p>'
        )
        for provider in ("kokoro", "elevenlabs", "gemini"):
            with self.subTest(provider=provider):
                plan = speech_snapshot(blocks, provider)
                for text in [plan["speech_input"], *plan.get("speech_chunks", [])]:
                    for expected in ("Read the report.", "Visit example.com.", "Keep this verse.",
                                     "Use npm for the 3.5% sample."):
                        self.assertIn(expected, text)
                    for artifact in ("https://", "tracking", "example.org", "Skip this code", "✦", "***"):
                        self.assertNotIn(artifact, text)

    def test_elevenlabs_context_uses_cleaned_text_and_artifact_only_plans_are_empty(self):
        prose = "Earlier sentence. " * 155 + "Visit https://example.com/report."
        blocks = article_blocks(f"<p>{prose}</p><p>{'Later sentence. ' * 150}</p>")
        plan = speech_snapshot(blocks, "elevenlabs")
        self.assertEqual(plan["speech_contexts"][1]["previous_text"], "Earlier sentence. Visit example.com.")
        artifacts = article_blocks("<p>[https://example.com/path]</p><p>———</p><pre><code>x</code></pre>")
        self.assertEqual(speech_snapshot(artifacts, "elevenlabs")["speech_input"], "")

    def test_long_articles_are_bounded_without_losing_or_repeating_words(self):
        blocks = [{"type": "title", "text": "Article title"},
                  {"type": "paragraph", "text": "A fairly long sentence about an idea. " * 1500}]
        for provider in ("elevenlabs", "gemini"):
            with self.subTest(provider=provider):
                snapshot = speech_snapshot(blocks, provider)
                self.assertGreater(len(snapshot["speech_chunks"]), 5)
                self.assertTrue(all(0 < len(chunk) <= 4000 for chunk in snapshot["speech_chunks"]))
                text = " ".join(snapshot["speech_chunks"])
                for tag in ("[Thoughtful, measured narration]", "[short pause]", "[pause]",
                            "[long pause]", "<short pause>"):
                    text = text.replace(tag, "")
                self.assertEqual(text.split(), " ".join(block["text"] for block in blocks).split())

    def test_elevenlabs_structural_pause_hierarchy(self):
        blocks = [{"type": kind, "text": text} for kind, text in (
            ("title", "Title"), ("paragraph", "Opening."), ("paragraph", "Next."),
            ("heading", "Section"), ("paragraph", "Body."), ("list_item", "One"),
            ("list_item", "Two"), ("blockquote", "Quote."), ("paragraph", "End."))]
        self.assertEqual(render(blocks, "elevenlabs"),
                         "[Thoughtful, measured narration]\n\nTitle\n\n[long pause]\n\nOpening."
                         "\n\n[pause]\n\nNext.\n\n[long pause]\n\nSection\n\n[pause]\n\nBody."
                         "\n\n[pause]\n\nOne\n\n[short pause]\n\nTwo\n\n[pause]\n\nQuote."
                         "\n\n[pause]\n\nEnd.")
        self.assertEqual(render([{"type": "heading", "level": 3, "text": "Minor"}],
                                "elevenlabs", blocks[1]),
                         "[Thoughtful, measured narration]\n\n[pause]\n\nMinor")

    def test_chunk_boundaries_preserve_pauses_and_context_uses_only_source_words(self):
        blocks = [{"type": "paragraph", "text": ("Earlier sentence. " * 130).strip()},
                  {"type": "paragraph", "text": ("Middle sentence. " * 130).strip()},
                  {"type": "heading", "level": 2, "text": "New section"},
                  {"type": "paragraph", "text": ("Later sentence. " * 130).strip()}]
        plan = speech_snapshot(blocks, "elevenlabs")
        self.assertEqual(len(plan["speech_chunks"]), 3)
        self.assertTrue(plan["speech_chunks"][1].startswith(
            "[Thoughtful, measured narration]\n\n[pause]\n\nMiddle"))
        self.assertTrue(plan["speech_chunks"][2].startswith(
            "[Thoughtful, measured narration]\n\n[pause]\n\nLater"))
        self.assertEqual(plan["speech_contexts"], [
            {"next_text": "Middle sentence. Middle sentence."},
            {"previous_text": "Earlier sentence. Earlier sentence.",
             "next_text": "Later sentence. Later sentence."},
            {"previous_text": "Middle sentence. New section"}])
        heading_plan = speech_snapshot([
            {"type": "paragraph", "text": "Earlier sentence. " * 160},
            {"type": "heading", "level": 2, "text": "New section " * 120}], "elevenlabs")
        self.assertTrue(heading_plan["speech_chunks"][1].startswith(
            "[Thoughtful, measured narration]\n\n[long pause]\n\nNew section"))
        single = speech_snapshot([blocks[2]], "elevenlabs")
        self.assertEqual(single["speech_contexts"], [{}])
        self.assertNotIn("[pause]", single["speech_chunks"][0])

    def test_neighboring_text_caps_long_sentences_at_whole_words(self):
        blocks = [{"text": "start " + "middle " * 150 + "end"}]
        for tail in (False, True):
            excerpt = neighboring_text(blocks, tail=tail)
            self.assertLessEqual(len(excerpt), 500)
            self.assertNotIn("[", excerpt)
            self.assertTrue(all(word in {"start", "middle", "end"} for word in excerpt.split()))
        self.assertTrue(neighboring_text(blocks).startswith("start "))
        self.assertTrue(neighboring_text(blocks, tail=True).endswith(" end"))

    def test_single_extremely_long_word_is_split_without_character_loss(self):
        block = {"type": "paragraph", "text": "x" * 10000}
        plan = speech_snapshot([block], "gemini")
        self.assertEqual("".join(chunk.replace("<short pause>", "").replace("\n", "")
                                 for chunk in plan["speech_chunks"]), block["text"])


if __name__ == "__main__":
    unittest.main()
