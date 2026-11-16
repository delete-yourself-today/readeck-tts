"""Speech providers, including durable per-request audio for paid generation."""

import json
import logging
from urllib.parse import quote

import requests

from .audio import commit_file, join_audio, require_ffmpeg, valid_chunk, write_audio
from .clients import API, ConfigurationError, Kokoro, RemoteError
from .config import ELEVENLABS_MODELS, GEMINI_MODELS


log = logging.getLogger(__name__)


class HostedSpeech(API):
    def request(self, method, path, **kwargs):
        try:
            return super().request(method, path, **kwargs)
        except RemoteError as error:
            if error.status in {400, 402, 404, 422}:
                raise ConfigurationError(f"{self.name}: request rejected (HTTP {error.status}); check model, voice, account quota, and API key permissions") from None
            raise

    def generate(self, snapshot, output):
        require_ffmpeg()
        if snapshot["model"] not in self.models:
            raise ConfigurationError(f"{self.name}: saved job uses an unsupported model")
        chunks = snapshot.get("speech_chunks")
        if not chunks or not all(isinstance(chunk, str) and chunk.strip() for chunk in chunks):
            raise ConfigurationError(f"{self.name}: saved job is missing its speech chunk plan")
        contexts = snapshot.get("speech_contexts", [{} for _ in chunks])
        if (not isinstance(contexts, list) or len(contexts) != len(chunks)
                or any(not isinstance(context, dict)
                       or any(key not in {"previous_text", "next_text"}
                              or not isinstance(value, str) or not value.strip()
                              for key, value in context.items()) for context in contexts)):
            raise ConfigurationError(f"{self.name}: saved job has an invalid speech context plan")
        directory = output.parent / "chunks"
        directory.mkdir(exist_ok=True)
        paths = []
        for index, text in enumerate(chunks):
            target = directory / f"{index:06d}.{self.audio_format}"
            paths.append(target)
            if valid_chunk(target, self.audio_format):
                continue
            part = target.with_name(target.name + ".part")
            try:
                log.info("Generating %s chunk %d/%d", self.name, index + 1, len(chunks))
                usage = self.generate_chunk(snapshot, text, part, **contexts[index])
                if not valid_chunk(part, self.audio_format):
                    raise RuntimeError(f"{self.name} returned invalid or empty audio")
                commit_file(part, target)
                # Commit audio first: failure to save usage must not cause another paid call.
                receipt = target.with_suffix(".json")
                receipt_part = receipt.with_name(receipt.name + ".part")
                receipt_part.write_text(json.dumps({"provider": snapshot["provider"],
                    "model": snapshot["model"], "voice": snapshot["voice"], "usage": usage}, indent=2) + "\n")
                commit_file(receipt_part, receipt)
            finally:
                part.unlink(missing_ok=True)
                target.with_suffix(".json.part").unlink(missing_ok=True)
        join_audio(paths, output)


class ElevenLabs(HostedSpeech):
    models = ELEVENLABS_MODELS
    audio_format = "mp3"

    def __init__(self, config):
        super().__init__("ElevenLabs", "https://api.elevenlabs.io")
        self.session.headers["xi-api-key"] = config.elevenlabs_token
        self.timeout = config.tts_timeout

    def check(self, model, voices):
        require_ffmpeg()
        models = self.json("GET", "/v1/models")
        if not any(item["model_id"] == model and item.get("can_do_text_to_speech") for item in models):
            raise ConfigurationError("ELEVENLABS_MODEL is unavailable to this API key")
        for voice in voices:
            self.json("GET", "/v1/voices/" + quote(voice, safe=""))

    def generate_chunk(self, snapshot, text, output, *, previous_text=None, next_text=None):
        context = {key: value for key, value in
                   (("previous_text", previous_text), ("next_text", next_text)) if value}
        with self.request("POST", "/v1/text-to-speech/" + quote(snapshot["voice"], safe=""),
                          params={"output_format": "mp3_44100_128"}, json={
                              "text": text, "model_id": snapshot["model"],
                              "voice_settings": snapshot["speech_options"],
                              "seed": snapshot.get("speech_seed", 12345),
                              **context,
                          }, stream=True, timeout=(10, self.timeout)) as response:
            try:
                with output.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        handle.write(chunk)
            except requests.RequestException:
                raise RemoteError(self.name, "audio download") from None
            return {key: response.headers.get(key) for key in ("character-cost", "request-id", "history-item-id")}


class Gemini(HostedSpeech):
    models = GEMINI_MODELS
    audio_format = "wav"

    def __init__(self, config):
        super().__init__("Gemini", "https://generativelanguage.googleapis.com/v1beta")
        self.session.headers["x-goog-api-key"] = config.gemini_token
        self.timeout = config.tts_timeout

    def check(self, model, voices):
        require_ffmpeg()
        self.json("GET", "/models/" + quote(model, safe=""))
        for voice in voices:
            if voice.startswith("voice_"):
                self.json("GET", "/voices/" + quote(voice, safe=""))
                continue
            page_token = None
            found = False
            while True:
                params = {"search": voice, "page_size": 1000}
                if page_token:
                    params["page_token"] = page_token
                page = self.json("GET", "/voices", params=params)
                if any(voice in {item.get("id"), item.get("display_name")} for item in page.get("voices", [])):
                    found = True
                    break
                next_token = page.get("next_page_token")
                if not next_token or next_token == page_token:
                    break
                page_token = next_token
            if not found:
                raise ConfigurationError(f"Gemini voice {voice!r} is unavailable; set GEMINI_VOICE")

    def generate_chunk(self, snapshot, text, output):
        result = self.json("POST", "/interactions", json={
            "model": snapshot["model"], "store": False,
            "input": [{"type": "user_input", "content": [{
                "type": "text", "text": text,
                "annotations": [{"type": "speech_metadata", "style": snapshot["speech_options"]["style"]}],
            }]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": snapshot["voice"]}]},
        }, timeout=(10, self.timeout))
        if result.get("status") != "completed":
            raise RuntimeError("Gemini did not complete the speech generation")
        parts = [(part["data"], part.get("mime_type", "audio/wav"))
                 for step in result.get("steps", []) if step.get("type") == "model_output"
                 for part in step.get("content", []) if part.get("type") == "audio"]
        write_audio(parts, output)
        return result.get("usage", {})


def speech_client(config, provider=None):
    provider = provider or config.provider
    if provider == "kokoro":
        if not config.kokoro_url:
            raise ConfigurationError("Set KOKORO_BASE_URL to resume this Kokoro job")
        return Kokoro(config)
    if provider == "elevenlabs":
        if not config.elevenlabs_token:
            raise ConfigurationError("Set ELEVENLABS_API_KEY to generate or resume ElevenLabs jobs")
        return ElevenLabs(config)
    if provider == "gemini":
        if not config.gemini_token:
            raise ConfigurationError("Set GEMINI_API_KEY to generate or resume Gemini jobs")
        return Gemini(config)
    raise ConfigurationError("Saved job has an unknown speech provider")
