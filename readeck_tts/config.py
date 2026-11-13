from dataclasses import dataclass, field
import os
from pathlib import Path
import unicodedata
from urllib.parse import urlsplit


ELEVENLABS_MODELS = ("eleven_v4", "eleven_v4_turbo")
GEMINI_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
SPEECH_DEFAULTS = {
    "kokoro": ("kokoro", "af_heart"),
    "elevenlabs": ("eleven_v4_turbo", "JBFqnCBsd6RMkjVDRZzb"),
    "gemini": ("gemini-3.8-flash-lite-tts", "Charon"),
}


def log_level_from_env():
    level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    return level


@dataclass(frozen=True)
class Config:
    readeck_url: str
    readeck_token: str = field(repr=False)
    abs_url: str
    abs_token: str = field(repr=False)
    kokoro_url: str = ""
    kokoro_token: str = field(default="", repr=False)
    voice: str = "af_heart"
    voices: tuple[str, ...] = field(init=False)
    model: str = "kokoro"
    label: str = "tts"
    library: str = "Podcasts"
    podcast: str = "Readeck"
    folder_id: str = ""
    data_dir: Path = Path("data")
    poll_interval: int = 300
    retry_interval: int = 30
    tts_timeout: int = 1800
    provider: str = "kokoro"
    elevenlabs_token: str = field(default="", repr=False)
    gemini_token: str = field(default="", repr=False)

    def __post_init__(self):
        if self.provider not in SPEECH_DEFAULTS:
            raise ValueError("TTS_PROVIDER must be kokoro, elevenlabs, or gemini")
        allowed = {"elevenlabs": ELEVENLABS_MODELS, "gemini": GEMINI_MODELS}.get(self.provider)
        if allowed and self.model not in allowed:
            raise ValueError(f"{self.provider.upper()}_MODEL must be one of: {', '.join(allowed)}")
        voices = tuple(dict.fromkeys(voice.strip() for voice in self.voice.split(",")))
        if not all(voices):
            raise ValueError(f"{self.provider.upper()}_VOICE must contain comma-separated voices without empty entries")
        object.__setattr__(self, "voices", voices)

    @classmethod
    def from_env(cls):
        def optional(name, default=""):
            return os.getenv(name, "").strip() or default

        def required(name):
            value = optional(name)
            if not value:
                raise ValueError(f"Set {name} in the environment or .env")
            return value

        def positive_integer(name, default):
            try:
                value = int(optional(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} must be a positive integer") from None
            if value < 1:
                raise ValueError(f"{name} must be a positive integer")
            return value

        provider = optional("TTS_PROVIDER", "kokoro").lower()
        if provider not in SPEECH_DEFAULTS:
            raise ValueError("TTS_PROVIDER must be kokoro, elevenlabs, or gemini")
        default_model, default_voice = SPEECH_DEFAULTS[provider]
        prefix = provider.upper()
        # Retain credentials for other providers so unfinished snapshots can resume.
        elevenlabs_token = required("ELEVENLABS_API_KEY") if provider == "elevenlabs" else optional("ELEVENLABS_API_KEY")
        gemini_token = required("GEMINI_API_KEY") if provider == "gemini" else optional("GEMINI_API_KEY")
        config = cls(
            readeck_url=required("READECK_URL").rstrip("/"),
            readeck_token=required("READECK_API_TOKEN"),
            abs_url=required("AUDIOBOOKSHELF_URL").rstrip("/"),
            abs_token=required("AUDIOBOOKSHELF_API_TOKEN"),
            kokoro_url=(required("KOKORO_BASE_URL") if provider == "kokoro" else optional("KOKORO_BASE_URL")).rstrip("/"),
            kokoro_token=optional("KOKORO_API_KEY"),
            voice=optional(prefix + "_VOICE", default_voice),
            model=optional(prefix + "_MODEL", default_model),
            provider=provider, elevenlabs_token=elevenlabs_token, gemini_token=gemini_token,
            label=optional("READECK_TAG", cls.label),
            library=optional("AUDIOBOOKSHELF_LIBRARY", cls.library),
            podcast=unicodedata.normalize("NFC", " ".join(optional("AUDIOBOOKSHELF_PODCAST", cls.podcast).split())),
            folder_id=optional("AUDIOBOOKSHELF_FOLDER_ID"),
            data_dir=Path(optional("DATA_DIR", str(cls.data_dir))),
            poll_interval=positive_integer("POLL_INTERVAL_SECONDS", cls.poll_interval),
            retry_interval=positive_integer("RETRY_INTERVAL_SECONDS", cls.retry_interval),
            tts_timeout=positive_integer("TTS_TIMEOUT_SECONDS", cls.tts_timeout),
        )
        for field_name, env_name in (("readeck_url", "READECK_URL"), ("abs_url", "AUDIOBOOKSHELF_URL"),
                                     ("kokoro_url", "KOKORO_BASE_URL")):
            if field_name == "kokoro_url" and not config.kokoro_url:
                continue
            try:
                url = urlsplit(getattr(config, field_name))
                valid = (url.scheme in {"http", "https"} and url.hostname and not url.username
                         and not url.password and not url.query and not url.fragment
                         and not any(c.isspace() for c in getattr(config, field_name)))
                _ = url.port  # Validate an explicitly supplied port.
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"{env_name} must be an HTTP(S) base URL without credentials, query, or fragment")
        if (not config.podcast or config.podcast != config.podcast.strip(" .")
                or any(c in config.podcast for c in '/\\<>:"|?*')
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in config.podcast)
                or len(config.podcast) > 60):
            raise ValueError("AUDIOBOOKSHELF_PODCAST must be a simple folder name, at most 60 characters")
        return config
