# Readeck TTS

Poll Readeck for articles labeled `tts`, generate narration through a configured
Kokoro HTTP endpoint, and upload saved audio as episodes of a `Readeck` podcast
inside the existing Audiobookshelf `Podcasts` library.

## Configuration

All configuration uses environment variables. For local development, `.env` is
loaded without overriding existing environment variables. The Compose template
defines settings directly in its `environment` section: fill in the required
values and uncomment optional settings to override defaults. It does not load
the development `.env` file. Keep a configured deployment copy outside the
source checkout so credentials are not committed. The development `.env` is
ignored by Git and excluded from the image.

Required settings:

| Variable | Meaning |
| --- | --- |
| `READECK_URL` | Readeck instance base URL, without `/api` |
| `READECK_API_TOKEN` | Token with access to bookmarks and extracted articles |
| `AUDIOBOOKSHELF_URL` | Audiobookshelf base URL, without `/api` |
| `AUDIOBOOKSHELF_API_TOKEN` | Token with library access and upload permission |
| `KOKORO_BASE_URL` | Kokoro API URL including `/v1`, reachable from the worker |

Optional settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `READECK_TAG` | `tts` | Label that selects articles |
| `AUDIOBOOKSHELF_LIBRARY` | `Podcasts` | Existing podcast library name |
| `AUDIOBOOKSHELF_PODCAST` | `Readeck` | Show name and directory within that library |
| `AUDIOBOOKSHELF_FOLDER_ID` | Auto-select | Required only when the library has multiple folders |
| `KOKORO_API_KEY` | None | Optional bearer token for Kokoro |
| `KOKORO_VOICE` | `af_heart` | Narration voice for new jobs |
| `KOKORO_MODEL` | `kokoro` | Model name understood by the endpoint |
| `POLL_INTERVAL_SECONDS` | `300` | Time between discovery polls |
| `RETRY_INTERVAL_SECONDS` | `30` | Service retry delay, indexing checks, and initial article retry delay |
| `TTS_TIMEOUT_SECONDS` | `1800` | Socket read timeout while waiting for audio; not an overall job deadline |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

Optional blank values use their defaults; required blank values fail validation.
A blank `READECK_TAG` therefore still means `tts`, not all articles. Surrounding
whitespace is trimmed. Intervals must be positive integers. URLs may include an
installation subpath but cannot contain credentials, query strings, or fragments.
The podcast name must be a simple folder name of at most 60 characters.
Repeated whitespace in the podcast name is collapsed to match Audiobookshelf's
directory naming.

The container stores state and temporary audio in `/data`. Choose its host
storage through the volume mapping; the Compose template uses a named volume.
For native development only, `DATA_DIR` can override the default `./data` path.

The layout stays opinionated: one show, one episode per article, MP3 output,
sequential generation, immediate polling at startup, and audio cleanup after
confirmed delivery. Naming, credentials, and timing are configurable; there are
no alternate library layouts or backend-specific deployment requirements.
Text, voice, and destination are frozen when a job is first snapshotted, so
configuration changes do not silently alter an in-progress delivery.

## Run with Docker Compose

Copy [compose.yaml](compose.yaml) into your deployment directory and edit its
three required URLs and two API tokens. Optional environment variables are
commented out with their default values. The template uses the published image
at `ghcr.io/delete-yourself-today/readeck-tts:latest` and a persistent named volume.

```bash
docker compose pull
docker compose run --rm readeck-tts check
docker compose up -d
docker compose logs -f
```

The worker needs outbound access to Readeck, Kokoro, and Audiobookshelf. It exposes
no ports. The Kokoro hostname must resolve and be reachable from the container.
The Kokoro server stays separate and does not need to mount this volume.

### Published image

The [image workflow](.github/workflows/docker-image.yml) builds and publishes
`ghcr.io/delete-yourself-today/readeck-tts:latest` plus a `sha-<commit>` tag on pushes to
`main`. It builds Linux AMD64 and ARM64 images, uses GitHub Actions caching,
and authenticates using the repository's built-in `GITHUB_TOKEN`; no separate
registry credentials are needed. Pull requests do not trigger this workflow.
Action versions are pinned to verified commit hashes.

The Compose template uses `latest`; replace the tag with a published
`sha-<commit>` tag to pin a particular build. Forks publish under their own
lowercase `ghcr.io/<owner>/<repository>` name and should update the template.
For anonymous pulls, set the package's
visibility to public in GitHub's package settings. See GitHub's
[container registry guide](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

`AUDIOBOOKSHELF_LIBRARY=Podcasts` selects a podcast library.
`AUDIOBOOKSHELF_PODCAST=Readeck` selects the show within it. The first upload
creates its directory; Audiobookshelf's scanner creates the show and episodes.
An empty `AUDIOBOOKSHELF_FOLDER_ID` automatically selects the sole library
folder. If there are multiple folders, set the ID explicitly.

The default voice is `af_heart`; set `KOKORO_VOICE` to change it for future jobs.
`KOKORO_BASE_URL` includes `/v1`. `KOKORO_API_KEY` is optional. The Readeck and
Audiobookshelf base URLs should not include `/api`. No hosted OpenAI calls are
made. Only the Kokoro-compatible HTTP speech endpoint is used.

The service polls immediately on launch, then every 300 seconds by default.
With no saved checkpoint it discovers all existing bookmarks. Eligibility is
checked against each bookmark's current labels before generating. Later syncs
include updates, so adding `tts` to an older bookmark makes it eligible. There
is no separate first-run mode. Initial classification of a large library may
take several minutes. A nonempty extracted article is required.

## State and recovery

SQLite, text/voice snapshots, and audio live in the `data` named volume
(prefixed with the Compose project name). Keep this volume across deployments
and back it up while the worker is stopped. `docker compose down` preserves it;
`docker compose down -v` deletes
the ledger and audio cache. Do not mount it from a network filesystem.

Jobs are unique per Readeck instance URL and article ID. IDs and sync checkpoints
are saved in one transaction, with an overlapping sync window. A file lock
prevents two workers from using the same data directory concurrently. Completed
jobs stay completed when articles or voice configuration change. Regeneration is
not automatic.

Each job snapshots its text, metadata, model, voice, and destination before
generation. Audio is validated, tagged, fsynced, and renamed into place before
upload. Failed uploads reuse that audio. A crash between rename and the SQLite
update also recovers it. Interrupted local generation retries automatically;
Kokoro-FastAPI handles text chunking internally, and an interrupted request may
repeat the whole article. This implementation is intended for a self-hosted
Kokoro backend, without per-request provider charges.

Each episode uses the deterministic filename `rdtts-<job-key>.mp3` in the
configured show directory. Article titles, save dates, authors, and source URLs
are embedded as audio metadata. Save dates establish chronological episode
ordering. Before retrying, the worker looks for an indexed episode with that
filename, independent of the displayed episode title. Unknown uploads get a
two-minute indexing grace period before retrying the same path. An acknowledged
upload remains `awaiting_index` until its episode appears; it is not blindly
reuploaded.

Audiobookshelf's file watcher must be enabled, or the token must be allowed to
request a library scan. Upload access alone does not imply scan permission. If
indexing stays pending, check Audiobookshelf's watcher/scanner first. The worker
requests a scan after uploads and while waiting; 403 responses fall back to the
watcher. It records both the podcast item ID and episode ID on completion.
Article-specific failures retry with backoff capped at one hour. Network
failures, HTTP 429, and server errors pause processing globally until service
checks pass again, without charging individual jobs' retry counters. Changing
an existing job's destination configuration stops processing rather than
silently delivering it somewhere else.

### Credentials and destination validation

`run` and `once` perform the same read-only preflight as `check` before
discovering or generating anything: Readeck authentication, Audiobookshelf
authentication/upload permission, destination library/folder, and Kokoro voice
availability. `run` repeats these checks on every poll, so deleted libraries or
revoked access are not hidden by a permanently cached destination ID.

Missing required environment variables, HTTP 401/403 (except optional scan
permission), missing/inaccessible or ambiguous libraries, wrong library type,
invalid folder selection, and an unavailable voice exit nonzero with an
actionable error. Existing job state and audio remain intact. The worker does
not create missing libraries or silently switch to another library. If a
library is deleted and recreated, its new ID does not match older snapshotted
jobs; those require an explicit destination decision rather than an automatic
redirect.

Compose's `restart: unless-stopped` will retry a process that exits. For a
persistent configuration problem the container may therefore show as
restarting; inspect `docker compose logs`. Fix the environment values in your
deployment's `compose.yaml`, then use `docker compose up -d` to apply them.
`docker compose restart` alone does not apply environment changes. Service outages during
`run` keep the process alive and pause work for `RETRY_INTERVAL_SECONDS` before
checking again. `once` and `check` report the failure and exit nonzero.

Tokens and remote response bodies are not included in error logs. `status` and
`retry` only need the local data directory, so they work even while credentials
are missing or broken. They do not contact any remote API.

Local MP3 audio and partial downloads are deleted once Audiobookshelf confirms
the matching episode is indexed. An upload's HTTP success response alone is
not sufficient: audio stays cached while delivery is uncertain or indexing is
pending. The completion record, episode IDs, and input snapshot remain in
SQLite to prevent regeneration.

Completion is committed before deleting files. If a crash interrupts cleanup,
or a file cannot be removed, cleanup remains pending and retries on startup
and subsequent worker passes. Cleanup failures do not undo delivery or cause
another upload. Upgrading an older data directory also schedules cleanup of
previously completed jobs. Only known audio filenames are removed; unrelated
files are preserved. This does not change anything stored in Audiobookshelf.

Pending work is skipped when the
bookmark disappears or loses its tag before its text is snapshotted. After a
snapshot is made, generation/delivery continues using that snapshot unless a
deletion event cancels it. This does not delete anything from Audiobookshelf.

Useful commands against the same Compose volume:

```bash
docker compose run --rm readeck-tts status
docker compose run --rm readeck-tts once --max-jobs 1
docker compose run --rm readeck-tts reconcile --key JOB_KEY
docker compose run --rm readeck-tts cleanup
# Stop the running worker before changing local job state:
docker compose stop
docker compose run --rm readeck-tts retry --key JOB_KEY
docker compose up -d
```

`retry` requeues an unfinished job, reusing saved text and audio. It can retry
an acknowledged upload that is stuck in indexing; check the destination before
using it. It does not reset completed, ignored, or cancelled jobs.
`reconcile` only reads the remote podcast and marks the local job complete if
its exact episode filename exists, then removes that job's local cached audio.
It never generates, uploads, or requests a scan. Run it with the worker stopped
because it updates the local ledger. `cleanup` removes cached audio for already
completed jobs without contacting any remote service; it also requires the
worker to be stopped. It exits nonzero if cleanup is still pending.

## Local development

```bash
cp .env.example .env  # For a new development checkout; preserve existing credentials.
# Set the three URLs and both API tokens in .env.
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m readeck_tts check
.venv/bin/python -m readeck_tts once --max-jobs 1
.venv/bin/python -m readeck_tts run
```

To test a local image build instead of the published image, use
`docker build -t readeck-tts:dev .` and set `image: readeck-tts:dev` in a separate
deployment copy of the Compose template.

Native development supports Linux/macOS. The local data directory defaults to
`./data`; Compose uses `/data` inside its named volume. These are separate
ledgers. To reuse native test state in Compose, stop the native worker and mount
that complete directory as `/data` with suitable ownership for UID 10001 instead
of using a new named volume. The deterministic remote filename also lets a new
ledger reconcile existing episodes, provided the source and destination URLs
and podcast remain unchanged.

Initial live validation on November 12, 2026: one real tagged
article generated 412.1 seconds of MP3 narration, uploaded into the main
`Podcasts` library's `Readeck` show, and appeared as exactly one indexed episode
with a title, source description, and date. Read-only reconciliation recorded
its item/episode IDs in the local ledger. The worker is not running continuously.
The Dockerfile and Compose configuration have not yet been built or run; this
live test used the native Python environment.

## Optional Kokoro server setup

Use any reachable Kokoro-compatible HTTP endpoint. For a native Apple Silicon
server, see [the optional macOS setup guide](deploy/kokoro-macos/README.md).
