from dataclasses import dataclass, field
import os
from pathlib import Path
import unicodedata
from urllib.parse import urlsplit


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

    def __post_init__(self):
        voices = tuple(dict.fromkeys(voice.strip() for voice in self.voice.split(",")))
        if not all(voices):
            raise ValueError("KOKORO_VOICE must contain comma-separated voice names without empty entries")
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

        config = cls(
            readeck_url=required("READECK_URL").rstrip("/"),
            readeck_token=required("READECK_API_TOKEN"),
            abs_url=required("AUDIOBOOKSHELF_URL").rstrip("/"),
            abs_token=required("AUDIOBOOKSHELF_API_TOKEN"),
            kokoro_url=required("KOKORO_BASE_URL").rstrip("/"),
            kokoro_token=optional("KOKORO_API_KEY"),
            voice=optional("KOKORO_VOICE", cls.voice),
            model=optional("KOKORO_MODEL", cls.model),
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
