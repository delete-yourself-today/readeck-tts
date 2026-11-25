from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest


MP3_AUDIO = (bytes.fromhex("fffb9000") + bytes(413)) * 20
PROJECT = Path(__file__).resolve().parents[1]


class ServiceHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, data, content_type="application/json"):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/profile":
            data = {"id": "user"}
        elif path == "/api/me":
            data = {"permissions": {"upload": True, "delete": True}}
        elif path == "/api/libraries":
            data = {"libraries": [{"id": "library", "name": "Podcasts", "mediaType": "podcast",
                                    "folders": [{"id": "folder"}]}]}
        elif path == "/v1/audio/voices":
            data = {"voices": ["af_heart"]}
        elif path == "/api/bookmarks/sync":
            data = [] if self.server.mode == "idle" else [
                {"id": "article", "time": "2026-11-12T12:00:00Z", "type": "update"}]
        elif path == "/api/bookmarks/article":
            data = {"title": "Shutdown test", "url": "https://example.com/article", "labels": self.server.labels,
                    "loaded": True, "has_article": True, "created": "2026-11-12T12:00:00Z"}
        elif path == "/api/bookmarks/article/article":
            self.respond(b"<p>A test article.</p>", "text/html")
            return
        elif path == "/api/libraries/library/items":
            items = [{"id": "show", "folderId": "folder", "path": "/podcasts/Readeck"}] if self.server.filename else []
            data = {"results": items, "total": len(items)}
        elif path == "/api/items/show":
            data = {"id": "show", "media": {"episodes": [{"id": "episode", "audioFile": {
                "metadata": {"filename": self.server.filename}}}]}}
        else:
            self.send_error(404)
            return
        try:
            self.respond(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/bookmarks/article" and body == {"remove_labels": ["tts"]}:
            self.server.labels = [label for label in self.server.labels if label != "tts"]
            self.respond({})
        else:
            self.send_error(400)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            if self.path == "/v1/audio/speech":
                self.server.generations += 1
                if self.server.mode == "generation_headers":
                    self.server.blocked.set()
                    self.server.release.wait(15)
                if self.server.mode == "generation_stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(MP3_AUDIO)))
                    self.end_headers()
                    self.wfile.write(MP3_AUDIO[:417])
                    self.wfile.flush()
                    self.server.blocked.set()
                    self.server.release.wait(15)
                    self.wfile.write(MP3_AUDIO[417:])
                    return
                self.respond(MP3_AUDIO, "audio/mpeg")
            elif self.path == "/api/upload":
                self.server.uploads += 1
                self.server.filename = re.search(rb'filename="(rdtts-[a-f0-9]+\.mp3)"', body)[1].decode()
                if self.server.mode == "upload":
                    self.server.blocked.set()
                    self.server.release.wait(15)
                self.respond({})
            elif self.path == "/api/libraries/library/scan":
                self.respond({})
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(os.name == "posix", "Tests POSIX container shutdown signals")
class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ServiceHandler)
        self.server.mode = "idle"
        self.server.labels = ["tts", "keep"]
        self.server.blocked = threading.Event()
        self.server.release = threading.Event()
        self.server.filename = None
        self.server.generations = self.server.uploads = 0
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.environment = os.environ | {
            "READECK_URL": url, "READECK_API_TOKEN": "test-token",
            "AUDIOBOOKSHELF_URL": url, "AUDIOBOOKSHELF_API_TOKEN": "test-token",
            "KOKORO_BASE_URL": url + "/v1", "KOKORO_API_KEY": "",
            "AUDIOBOOKSHELF_LIBRARY": "Podcasts", "AUDIOBOOKSHELF_PODCAST": "Readeck",
            "AUDIOBOOKSHELF_FOLDER_ID": "", "READECK_TAG": "tts", "KOKORO_VOICE": "af_heart",
            "DATA_DIR": str(self.data), "LOG_LEVEL": "INFO", "TTS_TIMEOUT_SECONDS": "1800",
            "POLL_INTERVAL_SECONDS": "300", "RETRY_INTERVAL_SECONDS": "30",
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "127.0.0.1,localhost",
        }
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def start(self, *args):
        log_path = self.root / f"process-{len(self.processes)}.log"
        with log_path.open("w") as output:
            process = subprocess.Popen([sys.executable, "-m", "readeck_tts", *args], cwd=PROJECT,
                                       env=self.environment, stdout=output, stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process, log_path

    def wait_for(self, condition, process, log_path):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if condition():
                return
            if process.poll() is not None:
                self.fail(log_path.read_text())
            time.sleep(0.02)
        self.fail("Worker did not reach the test checkpoint:\n" + log_path.read_text())

    def stop(self, process, log_path, signum=signal.SIGTERM):
        started = time.monotonic()
        process.send_signal(signum)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("Worker ignored shutdown during blocked work:\n" + log_path.read_text())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(process.returncode, 0, log_path.read_text())
        self.assertIn("Shutdown requested", log_path.read_text())

    def job(self):
        with closing(sqlite3.connect(self.data / "state.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM jobs").fetchone())

    def test_sigterm_and_sigint_stop_idle_worker(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                process, log_path = self.start("run")
                self.wait_for(lambda: "Sync recorded 0 changes" in log_path.read_text(), process, log_path)
                self.stop(process, log_path, signum)
        self.assertEqual(self.server.generations, 0)

    def test_sigterm_interrupts_waiting_for_kokoro_headers_and_recovers(self):
        self.server.mode = "generation_headers"
        process, log_path = self.start("run")
        self.wait_for(self.server.blocked.is_set, process, log_path)
        self.stop(process, log_path)
        self.assertEqual(self.job()["state"], "generating")
        self.assertEqual(self.job()["attempts"], 0)
        self.assertFalse(list(self.data.rglob("*.part")))
        self.server.mode = "success"
        resumed, resumed_log = self.start("once", "--max-jobs", "1")
        resumed.wait(timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed_log.read_text())
        self.assertEqual(self.server.generations, 2)
        self.assertEqual(self.server.uploads, 1)

    def test_sigterm_interrupts_partial_audio_download(self):
        self.server.mode = "generation_stream"
        process, log_path = self.start("run")
        self.wait_for(lambda: self.server.blocked.is_set() and bool(list(self.data.rglob("*.part"))), process, log_path)
        self.stop(process, log_path)
        self.assertEqual(self.job()["state"], "generating")
        self.assertEqual(self.job()["attempts"], 0)
        self.assertFalse(list(self.data.rglob("*.part")))
        self.assertFalse(list(self.data.rglob("audio.mp3")))
        self.assertEqual(self.server.uploads, 0)

    def test_interrupted_upload_preserves_audio_and_reconciles_without_resending(self):
        self.server.mode = "upload"
        process, log_path = self.start("run")
        self.wait_for(self.server.blocked.is_set, process, log_path)
        self.stop(process, log_path)
        job = self.job()
        self.assertEqual(job["state"], "uploading")
        self.assertEqual(job["attempts"], 0)
        self.assertTrue(list(self.data.rglob("audio.mp3")))
        resumed, resumed_log = self.start("reconcile", "--key", job["key"])
        resumed.wait(timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed_log.read_text())
        self.assertEqual(self.job()["state"], "complete")
        self.assertFalse(list(self.data.rglob("audio.mp3")))
        self.assertEqual(self.server.generations, 1)
        self.assertEqual(self.server.uploads, 1)
        self.assertEqual(self.server.labels, ["tts", "keep"])
        polled, poll_log = self.start("once")
        polled.wait(timeout=10)
        self.assertEqual(polled.returncode, 0, poll_log.read_text())
        self.assertEqual(self.server.labels, ["keep"])
        self.assertEqual(self.job()["tag_removed"], 1)
        self.assertEqual(self.server.uploads, 1)


if __name__ == "__main__":
    unittest.main()
