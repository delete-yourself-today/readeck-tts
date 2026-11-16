#!/usr/bin/env python3
"""Generate a standalone ElevenLabs narration sample from an excerpt JSON."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

from dotenv import load_dotenv
from mutagen.mp3 import MP3
import requests

from readeck_tts.text import narration_input
from readeck_tts.narration import render


MODEL = "eleven_multilingual_v2"
V4_MODELS = ("eleven_v4", "eleven_v4_turbo")
GEORGE = "JBFqnCBsd6RMkjVDRZzb"


def speech_text(blocks, model=MODEL):
    if model in V4_MODELS:
        return render(blocks, "elevenlabs")
    return narration_input(blocks, '<break time="{duration}s" />')


def generate(excerpt, output, *, api_key, voice_id=GEORGE, model=MODEL, session=None):
    if model not in (MODEL, *V4_MODELS):
        raise ValueError("Supported models: eleven_multilingual_v2, eleven_v4, eleven_v4_turbo")
    blocks = [block for block in excerpt.get("blocks", []) if block.get("text", "").strip()]
    if not blocks:
        raise ValueError("Excerpt must contain nonempty article blocks")
    output = Path(output)
    if (output / "audio.mp3").exists() or (output / "audio.mp3.part").exists():
        raise ValueError("Output already contains audio; choose another directory for a new generation")
    output.mkdir(parents=True, exist_ok=True)
    settings = {"stability": 0.5, "similarity_boost": 0.75}
    if model == MODEL:
        settings.update(style=0.0, use_speaker_boost=True, speed=1.0)
    payload = {"model_id": model, "text": speech_text(blocks, model), "seed": 12345,
               "voice_settings": settings}
    (output / "request.json").write_text(json.dumps(
        {"voice_id": voice_id, "output_format": "mp3_44100_128", **payload},
        ensure_ascii=False, indent=2) + "\n")
    own_session = session is None
    client = session or requests.Session()
    started = time.monotonic()
    summary = {"model": model, "voice_id": voice_id, "status": "generating",
               "input_characters": len(payload["text"]), "speed": 1.0}
    try:
        with client.post("https://api.elevenlabs.io/v1/text-to-speech/" + voice_id,
                         params={"output_format": "mp3_44100_128"},
                         headers={"xi-api-key": api_key}, json=payload,
                         stream=True, timeout=(10, 240)) as response:
            summary["http_status"] = response.status_code
            for name in ("request-id", "history-item-id", "character-cost"):
                if name in response.headers:
                    summary[name.replace("-", "_")] = response.headers[name]
            if not response.ok:
                try:
                    detail = response.json().get("detail", {})
                except ValueError:
                    detail = "No JSON error detail"
                raise RuntimeError(f"ElevenLabs HTTP {response.status_code}: {detail}")
            with (output / "audio.mp3.part").open("wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    handle.write(chunk)
        duration = MP3(output / "audio.mp3.part").info.length
        if duration <= 0:
            raise RuntimeError("ElevenLabs returned empty audio")
        (output / "audio.mp3.part").replace(output / "audio.mp3")
        summary.update(status="completed", audio_seconds=round(duration, 3))
    except Exception as error:
        summary.update(status="failed", error=str(error).replace(api_key, "[REDACTED]"))
        raise
    finally:
        if own_session:
            client.close()
        summary["request_seconds"] = round(time.monotonic() - started, 3)
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("excerpt", type=Path)
    parser.add_argument("--model", choices=[MODEL, *V4_MODELS], default=MODEL)
    parser.add_argument("--voice-id", default=GEORGE, help="Defaults to George, a British storytelling voice")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env")
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        parser.error("Set ELEVENLABS_API_KEY in the environment or .env")
    output = args.output or args.excerpt.parent / f"{args.model}-{args.voice_id}"
    print(f"Generating {args.model}, voice {args.voice_id}", flush=True)
    try:
        summary = generate(json.loads(args.excerpt.read_text()), output, api_key=key,
                           voice_id=args.voice_id, model=args.model)
    except Exception as error:
        print(str(error).replace(key, "[REDACTED]"), file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    print(f"Sample: {output / 'audio.mp3'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
