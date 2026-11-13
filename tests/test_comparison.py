import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock
from unittest.mock import patch
import wave

from scripts.compare_realtime import generate
from scripts.compare_elevenlabs import generate as generate_elevenlabs, speech_text


class RealtimeComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "sample"
        self.excerpt = {"blocks": [{"type": "title", "text": "Title"},
                                   {"type": "paragraph", "text": "A sentence."}]}
        self.connection = Mock()
        self.connect = Mock(return_value=self.connection)

    def events(self, *events):
        self.connection.recv.side_effect = [json.dumps(event) for event in (
            {"type": "session.created", "session": {"id": "session"}},
            {"type": "session.updated", "session": {"model": "resolved-model"}},
            *events,
        )]

    def test_audio_transcript_usage_and_reproducible_request_are_saved(self):
        pcm = b"\x00\x00" * 24000
        self.events(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()},
            {"type": "response.output_audio_transcript.done", "transcript": "Title. A sentence."},
            {"type": "response.done", "response": {"id": "response", "status": "completed",
                                                   "usage": {"total_tokens": 100}}},
        )
        summary = generate(self.excerpt, self.output, api_key="test-secret", connect=self.connect)
        self.assertEqual(summary["audio_seconds"], 1)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["resolved_model"], "resolved-model")
        self.assertEqual(summary["usage"], {"total_tokens": 100})
        with wave.open(str(self.output / "audio.wav")) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
            self.assertEqual(wav.readframes(wav.getnframes()), pcm)
        self.assertEqual((self.output / "transcript.txt").read_text(), "Title. A sentence.\n")
        self.assertEqual(json.loads((self.output / "request.json").read_text())["blocks"], self.excerpt["blocks"])
        for path in self.output.iterdir():
            self.assertNotIn(b"test-secret", path.read_bytes())
        sent = [json.loads(call.args[0]) for call in self.connection.send.call_args_list]
        self.assertIsNone(sent[0]["session"]["audio"]["input"]["turn_detection"])
        self.assertEqual(json.loads(sent[1]["item"]["content"][0]["text"])["blocks"], self.excerpt["blocks"])
        self.assertEqual(sent[2]["response"]["max_output_tokens"], "inf")
        self.connection.close.assert_called_once()

    def test_incomplete_response_preserves_usage_and_partial_audio(self):
        self.events(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\x00\x00").decode()},
            {"type": "response.done", "response": {"status": "incomplete", "usage": {"total_tokens": 50},
                                                   "status_details": {"reason": "max_output_tokens"}}},
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            generate(self.excerpt, self.output, api_key="test-secret", connect=self.connect)
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["status_details"], {"reason": "max_output_tokens"})
        self.assertEqual(summary["usage"], {"total_tokens": 50})
        with wave.open(str(self.output / "audio.wav")) as wav:
            self.assertEqual(wav.getnframes(), 1)
        self.connection.close.assert_called_once()

    def test_api_error_stops_generation_and_redacts_saved_error(self):
        self.connection.recv.return_value = json.dumps({"type": "error", "error": {
            "code": "test_error", "message": "Rejected test-secret"}})
        with self.assertRaisesRegex(RuntimeError, "test_error"):
            generate(self.excerpt, self.output, api_key="test-secret", connect=self.connect)
        summary = (self.output / "summary.json").read_text()
        self.assertNotIn("test-secret", summary)
        self.assertIn("[REDACTED]", summary)
        self.connection.send.assert_not_called()

    def test_existing_audio_is_never_overwritten_or_regenerated(self):
        self.output.mkdir()
        (self.output / "audio.wav").write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "already contains audio"):
            generate(self.excerpt, self.output, api_key="test-secret", connect=self.connect)
        self.connect.assert_not_called()
        self.assertEqual((self.output / "audio.wav").read_bytes(), b"keep")


class ElevenLabsComparisonTests(unittest.TestCase):
    def test_v4_uses_delivery_tags_and_supported_voice_settings(self):
        blocks = [{"type": "title", "text": "Title"}, {"type": "paragraph", "text": "Body."}]
        self.assertEqual(speech_text(blocks, "eleven_v4"),
                         "[Thoughtful, measured narration]\n\nTitle\n\n[short pause]\n\nBody.")
        client = MagicMock()
        response = client.post.return_value.__enter__.return_value
        response.ok = True
        response.status_code = 200
        response.headers = {}
        response.iter_content.return_value = [b"test audio"]
        for model in ("eleven_v4", "eleven_v4_turbo"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory, \
                    patch("scripts.compare_elevenlabs.MP3") as mp3:
                mp3.return_value.info.length = 2
                generate_elevenlabs({"blocks": blocks}, directory, api_key="test-secret",
                                   model=model, session=client)
                payload = client.post.call_args.kwargs["json"]
                self.assertEqual(payload["model_id"], model)
                self.assertEqual(payload["voice_settings"], {"stability": 0.5, "similarity_boost": 0.75})
                self.assertEqual(payload["text"], speech_text(blocks, "eleven_v4"))
                self.assertNotIn("<break", payload["text"])

    def test_break_tags_preserve_article_words_and_share_narration_timings(self):
        self.assertEqual(speech_text([
            {"type": "title", "text": "Title"},
            {"type": "paragraph", "text": "First."},
            {"type": "heading", "level": 2, "text": "Section"},
            {"type": "paragraph", "text": "Literal [pause:5s] token."},
        ]), 'Title\n\n<break time="1s" />\n\nFirst.\n\n<break time="0.8s" />\n\nSection'
            '\n\n<break time="0.6s" />\n\nLiteral [pause:5s] token.')

    def test_completed_generation_saves_audio_request_and_credit_header(self):
        client = MagicMock()
        response = client.post.return_value.__enter__.return_value
        response.ok = True
        response.status_code = 200
        response.headers = {"character-cost": "12", "request-id": "request"}
        response.iter_content.return_value = [b"test audio"]
        excerpt = {"blocks": [{"type": "paragraph", "text": "A sentence."}]}
        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.compare_elevenlabs.MP3") as mp3:
            mp3.return_value.info.length = 2
            summary = generate_elevenlabs(excerpt, directory, api_key="test-secret", session=client)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["character_cost"], "12")
            self.assertEqual(summary["audio_seconds"], 2)
            self.assertEqual((Path(directory) / "audio.mp3").read_bytes(), b"test audio")
            self.assertNotIn("test-secret", (Path(directory) / "request.json").read_text())
            self.assertEqual(client.post.call_args.kwargs["json"]["voice_settings"]["speed"], 1.0)
            self.assertEqual(client.post.call_args.kwargs["json"]["model_id"], "eleven_multilingual_v2")
            with self.assertRaisesRegex(ValueError, "already contains audio"):
                generate_elevenlabs(excerpt, directory, api_key="test-secret", session=client)
            self.assertEqual(client.post.call_count, 1)

    def test_api_error_saves_failure_without_treating_it_as_audio(self):
        client = MagicMock()
        response = client.post.return_value.__enter__.return_value
        response.ok = False
        response.status_code = 401
        response.headers = {}
        response.json.return_value = {"detail": "test-secret rejected"}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                generate_elevenlabs({"blocks": [{"type": "paragraph", "text": "A sentence."}]},
                                   directory, api_key="test-secret", session=client)
            summary = json.loads((Path(directory) / "summary.json").read_text())
            self.assertEqual(summary["status"], "failed")
            self.assertNotIn("test-secret", summary["error"])
            self.assertFalse((Path(directory) / "audio.mp3").exists())
            response.iter_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()
