import logging
from pathlib import PurePosixPath
from urllib.parse import quote

import requests

from .text import article_text


log = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """Processing needs operator intervention rather than another article attempt."""


class RemoteError(RuntimeError):
    def __init__(self, service, operation, status=None):
        self.service = service
        self.status = status
        super().__init__(f"{service}: {operation} failed" + (f" (HTTP {status})" if status else " (connection or timeout)"))

    def check_credentials(self):
        if self.status == 401:
            raise ConfigurationError(f"{self.service}: credentials were rejected (HTTP 401); check the API token and recreate the container after changing .env") from None
        if self.status == 403:
            raise ConfigurationError(f"{self.service}: access denied (HTTP 403); check token permissions and access to the requested resource") from None


def check_services(config, readeck, kokoro, abs_client):
    """Read-only preflight shared by check, startup, and periodic revalidation."""
    readeck.json("GET", "/profile")
    user = abs_client.json("GET", "/me")
    if not user.get("permissions", {}).get("upload"):
        raise ConfigurationError("Audiobookshelf: the token's user needs upload permission")
    destination = abs_client.resolve()
    voices = kokoro.json("GET", "/audio/voices")["voices"]
    names = [v if isinstance(v, str) else v["id"] for v in voices]
    for voice in config.voices:
        if voice not in names:
            raise ConfigurationError(f"Kokoro voice {voice!r} is unavailable; set KOKORO_VOICE")
    return {"readeck": "ok", "audiobookshelf": destination, "kokoro": "ok", "voice": config.voice}


class API:
    def __init__(self, name, url, token=""):
        self.name = name
        self.url = url.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", (10, 60))
        try:
            response = self.session.request(method, self.url + path, **kwargs)
        except requests.RequestException:
            raise RemoteError(self.name, f"{method} {path}") from None
        if not response.ok:
            status = response.status_code
            response.close()
            raise RemoteError(self.name, f"{method} {path}", status)
        return response

    def json(self, method, path, **kwargs):
        with self.request(method, path, **kwargs) as response:
            try:
                return response.json()
            except ValueError:
                raise RemoteError(self.name, f"invalid JSON from {path}") from None


class Readeck(API):
    def __init__(self, config):
        super().__init__("Readeck", config.readeck_url + "/api", config.readeck_token)

    def sync(self, since=None):
        events = self.json("GET", "/bookmarks/sync", params={"since": since} if since else {})
        if not isinstance(events, list):
            raise RuntimeError("Unexpected Readeck sync response; expected a list")
        return events

    def bookmark(self, article_id):
        return self.json("GET", "/bookmarks/" + quote(article_id, safe=""))

    def text(self, article_id):
        with self.request("GET", "/bookmarks/" + quote(article_id, safe="") + "/article") as response:
            return article_text(response.text)


class Kokoro(API):
    def __init__(self, config):
        super().__init__("Kokoro", config.kokoro_url, config.kokoro_token)
        self.timeout = config.tts_timeout

    def generate(self, snapshot, output):
        with self.request("POST", "/audio/speech", json={
            "input": snapshot["text"], "model": snapshot["model"],
            "voice": snapshot["voice"], "response_format": "mp3", "speed": 1.0,
        }, stream=True, timeout=(10, self.timeout)) as response:
            try:
                with output.open("wb") as handle:
                    for block in response.iter_content(chunk_size=64 * 1024):
                        handle.write(block)
            except requests.RequestException:
                raise RemoteError(self.name, "audio download") from None


class Audiobookshelf(API):
    def __init__(self, config):
        super().__init__("Audiobookshelf", config.abs_url + "/api", config.abs_token)
        self.config = config
        self.library_id = None
        self.folder_id = None
        self.podcast_item_id = None

    def resolve(self):
        previous = self.library_id, self.folder_id
        self.library_id = self.folder_id = None
        libraries = self.json("GET", "/libraries")["libraries"]
        matches = [lib for lib in libraries if lib["name"] == self.config.library]
        if not matches:
            raise ConfigurationError(f"Audiobookshelf library {self.config.library!r} is missing or inaccessible; create it, grant this token access, or set AUDIOBOOKSHELF_LIBRARY")
        if len(matches) > 1:
            raise ConfigurationError(f"Multiple Audiobookshelf libraries named {self.config.library!r}; use an unambiguous library name")
        library = matches[0]
        if library.get("mediaType") != "podcast":
            raise ConfigurationError("The destination library must be a podcast library; check AUDIOBOOKSHELF_LIBRARY")
        folders = library["folders"]
        if self.config.folder_id:
            folders = [folder for folder in folders if folder["id"] == self.config.folder_id]
        if len(folders) != 1:
            raise ConfigurationError("Set AUDIOBOOKSHELF_FOLDER_ID: the destination must resolve to exactly one folder")
        self.library_id, self.folder_id = library["id"], folders[0]["id"]
        if previous != (self.library_id, self.folder_id):
            self.podcast_item_id = None
        return {"name": library["name"], "id": self.library_id, "folder_id": self.folder_id,
                "podcast": self.config.podcast}

    def find_item(self, key):
        marker = "rdtts-" + key
        if not self.podcast_item_id:
            page = 0
            while True:
                data = self.json("GET", f"/libraries/{quote(self.library_id, safe='')}/items",
                                 params={"limit": 100, "page": page})
                items = data["results"]
                for item in items:
                    name = PurePosixPath(item.get("path", "").replace("\\", "/")).name
                    if name == self.config.podcast and item.get("folderId") == self.folder_id:
                        self.podcast_item_id = item["id"]
                        break
                if self.podcast_item_id or not items or (page + 1) * 100 >= data.get("total", 0):
                    break
                page += 1
        if not self.podcast_item_id:
            return None
        try:
            item = self.json("GET", f"/items/{quote(self.podcast_item_id, safe='')}")
        except RemoteError as error:
            if error.status == 404:
                self.podcast_item_id = None
            raise
        for episode in item["media"].get("episodes", []):
            metadata = episode.get("audioFile", {}).get("metadata", {})
            filename = metadata.get("filename") or PurePosixPath(metadata.get("path", "")).name
            if filename == marker + ".mp3":
                return item["id"], episode["id"]
        return None

    def upload(self, snapshot, key, audio):
        target = snapshot["destination"]
        with audio.open("rb") as handle:
            with self.request("POST", "/upload", data={
                "library": target["library_id"], "folder": target["folder_id"],
                "title": target["podcast"],
            }, files={"audio": (f"rdtts-{key}.mp3", handle, "audio/mpeg")}, timeout=(10, 600)):
                pass

    def request_scan(self):
        try:
            with self.request("POST", f"/libraries/{quote(self.library_id, safe='')}/scan"):
                pass
        except RemoteError as error:
            if error.status == 403:
                log.info("Library scan requires admin permission; waiting for Audiobookshelf's file watcher")
            elif error.status == 401:
                error.check_credentials()
            else:
                log.warning("Could not request library scan; will check indexing again")
