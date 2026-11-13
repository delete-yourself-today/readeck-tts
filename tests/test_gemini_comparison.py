import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
import wave

from scripts.compare_gemini import audio_parts, build_request, generate, write_audio


class GeminiComparisonTests(unittest.TestCase):
    def setUp(self):
        self.blocks = [{"type": "title", "text": "Title"},
                       {"type": "paragraph", "text": "Body."}]
        self.pcm = b"\x00\x00" * 24000

    def test_modern_models_keep_style_out_of_the_transcript(self):
        for model in ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"):
            with self.subTest(model=model):
                url, payload = build_request(self.blocks, model, "Charon")
                self.assertTrue(url.endswith("/interactions"))
                self.assertFalse(payload["store"])
                self.assertEqual(payload["model"], model)
                content = payload["input"][0]["content"][0]
                self.assertEqual(content["text"], "Title\n\n<short pause>\n\nBody.")
                self.assertIn("literary narration", content["annotations"][0]["style"])
                self.assertEqual(payload["generation_config"]["speech_config"], [{"voice": "Charon"}])

    def test_pro_uses_instruction_prompt_and_original_paragraphs(self):
        url, payload = build_request(self.blocks, "gemini-2.5-pro-preview-tts", "Charon")
        self.assertTrue(url.endswith("/models/gemini-2.5-pro-preview-tts:generateContent"))
        text = payload["contents"][0]["parts"][0]["text"]
        self.assertIn("word for word", text)
        self.assertTrue(text.endswith("PASSAGE:\n\nTitle\n\nBody."))
        self.assertNotIn("<short pause>", text)

    def test_wav_and_raw_pcm_responses_produce_equivalent_playable_files(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(self.pcm)
        cases = [(buffer.getvalue(), "audio/wav"), (self.pcm, "audio/L16;codec=pcm;rate=24000")]
        with tempfile.TemporaryDirectory() as directory:
            for index, (audio, mime) in enumerate(cases):
                with self.subTest(mime=mime):
                    output = Path(directory) / f"{index}.wav"
                    seconds = write_audio([(base64.b64encode(audio).decode(), mime)], output)
                    self.assertEqual(seconds, 1)
                    with wave.open(str(output)) as wav:
                        self.assertEqual(wav.getframerate(), 24000)
                        self.assertEqual(wav.readframes(wav.getnframes()), self.pcm)

    def test_incomplete_and_empty_generations_are_rejected(self):
        for result, modern in (({"status": "incomplete"}, True),
                               ({"status": "completed", "steps": []}, True),
                               ({"candidates": []}, False),
                               ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, False)):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                audio_parts(result, modern)

    def test_completed_generation_records_usage_and_redacts_audio_metadata(self):
        encoded = base64.b64encode(self.pcm).decode()
        result = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;rate=24000", "data": encoded}}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 25},
            "responseId": "response"}
        client = MagicMock()
        response = client.post.return_value.__enter__.return_value
        response.ok = True
        response.status_code = 200
        response.json.return_value = result
        with tempfile.TemporaryDirectory() as directory:
            summary = generate({"blocks": self.blocks}, directory, api_key="test-secret",
                               model="gemini-2.5-pro-preview-tts", session=client)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["audio_seconds"], 1)
            self.assertEqual(summary["usage"], result["usageMetadata"])
            metadata = (Path(directory) / "response_metadata.json").read_text()
            self.assertNotIn(encoded, metadata)
            self.assertIn("audio saved separately", metadata)
            for path in Path(directory).iterdir():
                self.assertNotIn(b"test-secret", path.read_bytes())
            with self.assertRaisesRegex(ValueError, "already contains audio"):
                generate({"blocks": self.blocks}, directory, api_key="test-secret",
                         model="gemini-2.5-pro-preview-tts", session=client)
            self.assertEqual(client.post.call_count, 1)

    def test_api_errors_are_saved_and_never_create_audio(self):
        client = MagicMock()
        response = client.post.return_value.__enter__.return_value
        response.ok = False
        response.status_code = 429
        response.json.return_value = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "test-secret"}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                generate({"blocks": self.blocks}, directory, api_key="test-secret",
                         model="gemini-3.8-flash-tts", session=client)
            self.assertFalse((Path(directory) / "audio.wav").exists())
            for path in Path(directory).iterdir():
                self.assertNotIn("test-secret", path.read_text())
            self.assertEqual(json.loads((Path(directory) / "summary.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
