"""Durable audio files and conversion for hosted speech providers."""

import base64
import io
import os
import re
import shutil
import subprocess
import wave

from mutagen.mp3 import MP3

from .clients import ConfigurationError


def sync_file(path):
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def commit_file(part, target):
    sync_file(part)
    os.replace(part, target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def valid_chunk(path, audio_format):
    if not path.is_file():
        return False
    try:
        if audio_format == "wav":
            with wave.open(str(path)) as wav:
                return (wav.getnchannels() == 1 and wav.getsampwidth() == 2
                        and wav.getnframes() > 0 and wav.getframerate() > 0
                        and len(wav.readframes(wav.getnframes())) == wav.getnframes() * 2)
        return MP3(path).info.length > 0
    except Exception:
        return False


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise ConfigurationError("Hosted speech providers require ffmpeg; install it or use the Docker image")


def join_audio(paths, output):
    require_ffmpeg()
    # All paths are application-generated basenames in a single chunk directory.
    manifest = paths[0].parent / "concat.txt"
    manifest.write_text("".join(f"file '{path.name}'\n" for path in paths))
    with subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "concat", "-safe", "1", "-i", str(manifest.resolve()),
                           "-vn", "-ac", "1", "-ar", "44100", "-codec:a", "libmp3lame",
                           "-q:a", "2", "-f", "mp3", str(output)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
        try:
            process.communicate()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if process.returncode:
            raise RuntimeError("ffmpeg could not assemble narration; saved chunks will be reused")


def write_audio(parts, output):
    pcm = []
    sample_rate = None
    for encoded, mime_type in parts:
        audio = base64.b64decode(encoded, validate=True)
        if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
            with wave.open(io.BytesIO(audio)) as wav:
                if (wav.getnchannels(), wav.getsampwidth()) != (1, 2):
                    raise RuntimeError("Expected mono PCM16 audio")
                rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())
                if len(frames) != wav.getnframes() * 2:
                    raise RuntimeError("Gemini returned truncated WAV audio")
        elif mime_type.lower().startswith(("audio/l16", "audio/pcm")):
            match = re.search(r"rate=(\d+)", mime_type)
            rate = int(match[1]) if match else 24000
            frames = audio
        else:
            raise RuntimeError(f"Unsupported audio format: {mime_type}")
        if sample_rate is not None and rate != sample_rate:
            raise RuntimeError("Gemini changed sample rate within the response")
        if not frames or len(frames) % 2:
            raise RuntimeError("Gemini returned empty or incomplete PCM16 audio")
        sample_rate = rate
        pcm.append(frames)
    if not pcm:
        raise RuntimeError("Gemini returned empty audio")
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for frames in pcm:
            wav.writeframesraw(frames)
    return sum(len(frames) for frames in pcm) / (2 * sample_rate)
