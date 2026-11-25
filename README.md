# Readeck TTS

Poll Readeck for articles labeled `tts`, generate narration through Kokoro,
ElevenLabs, or Gemini, and upload saved audio as episodes of a `Readeck` podcast
inside the existing Audiobookshelf `Podcasts` library.

Add `tts` to request an episode, including for an archived article. The worker
removes the label after Audiobookshelf confirms the episode is available.
Apply the label again to generate a fresh episode with the current voice and
model settings. Existing episodes are left in Audiobookshelf.

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
| `READECK_API_TOKEN` | Token with access to bookmarks and extracted articles, plus bookmark write access to clear requests |
| `AUDIOBOOKSHELF_URL` | Audiobookshelf base URL, without `/api` |
| `AUDIOBOOKSHELF_API_TOKEN` | Token for the account you listen with, with library access, upload permission, and delete permission when audio deletion is enabled |

Optional settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TTS_PROVIDER` | `kokoro` | `kokoro`, `elevenlabs`, or `gemini` |
| `READECK_TAG` | `tts` | Label that selects articles |
| `AUDIOBOOKSHELF_LIBRARY` | `Podcasts` | Existing podcast library name |
| `AUDIOBOOKSHELF_PODCAST` | `Readeck` | Show name and directory within that library |
| `AUDIOBOOKSHELF_FOLDER_ID` | Auto-select | Required only when the library has multiple folders |
| `KOKORO_API_KEY` | None | Optional bearer token for Kokoro |
| `KOKORO_BASE_URL` | None | Required when using Kokoro; endpoint including `/v1` |
| `KOKORO_VOICE` | `af_heart` | Voice or comma-separated list randomly selected per new job |
| `KOKORO_MODEL` | `kokoro` | Model name understood by the endpoint |
| `ELEVENLABS_API_KEY` | None | Required when using ElevenLabs |
| `ELEVENLABS_MODEL` | `eleven_v4_turbo` | `eleven_v4` or `eleven_v4_turbo` |
| `ELEVENLABS_VOICE` | `JBFqnCBsd6RMkjVDRZzb` | George's voice ID, or a comma-separated pool of voice IDs |
| `GEMINI_API_KEY` | None | Required when using Gemini |
| `GEMINI_MODEL` | `gemini-3.8-flash-lite-tts` | `gemini-3.8-flash-tts` or `gemini-3.8-flash-lite-tts` |
| `GEMINI_VOICE` | `Charon` | Voice name/ID, or a comma-separated pool |
| `ARCHIVE_ON_FINISH` | `false` | Archive the Readeck article after listening finishes |
| `DELETE_AUDIO_ON_FINISH` | `false` | Delete the finished episode and its audio from Audiobookshelf |
| `POLL_INTERVAL_SECONDS` | `300` | Time between discovery and listening-completion polls |
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

Only the selected provider's credentials are required for new jobs. Kokoro keeps
its existing configuration and remains the default. Hosted models are explicitly
limited to the families listed above; Gemini Pro and older ElevenLabs models are
not supported by the worker.

For ElevenLabs v4 Turbo:

```dotenv
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=your-key
ELEVENLABS_MODEL=eleven_v4_turbo
ELEVENLABS_VOICE=JBFqnCBsd6RMkjVDRZzb
```

For Gemini 3.8 Flash-Lite:

```dotenv
TTS_PROVIDER=gemini
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-3.8-flash-lite-tts
GEMINI_VOICE=Sulafat
```

Both hosted providers require `ffmpeg` for assembling one final MP3; it is
included in the Docker image. Install it separately for native development.
Startup checks validate the selected model, credentials, and every configured
voice using read-only API calls. They do not generate paid speech.

The container stores state and temporary audio in `/data`. Choose its host
storage through the volume mapping; the Compose template uses a named volume.
For native development only, `DATA_DIR` can override the default `./data` path.

The layout stays opinionated: one show, one episode per request, MP3 output,
sequential generation, immediate polling at startup, and audio cleanup after
confirmed delivery. Naming, credentials, and timing are configurable; there are
no alternate library layouts or backend-specific deployment requirements.
Text, provider, model, voice, delivery settings, and destination are frozen when
a job is first snapshotted, so configuration changes do not silently alter an
in-progress delivery.

When any completion action is enabled, each poll (including `once`) checks Audiobookshelf's `isFinished`
flag for delivered episodes. This uses the API token's listening account;
marking an episode finished manually also triggers completion actions. Missing
progress or a missing episode does not count as finished.

Both listening completion actions are disabled by default and can be enabled
independently with `ARCHIVE_ON_FINISH=true` and `DELETE_AUDIO_ON_FINISH=true`.
Boolean settings also accept `1`/`0`, `yes`/`no`,
and `on`/`off`, ignoring case. Blank values use the default `false`.
When both actions are enabled, the worker archives the article before deleting
the Audiobookshelf episode and its server audio. Client downloads are outside this cleanup.

Listening completion and each successful action are stored in SQLite so failures
retry on subsequent polls and survive restarts. `status` exposes `listened`,
`archived`, `audio_deleted`, and `finish_error`. An archive failure delays
audio deletion when both actions are enabled. A deleted bookmark needs no
further Readeck changes.

Permission errors require operator intervention: `check`, `once`, and `run`
reject an Audiobookshelf token without upload permission. When
`DELETE_AUDIO_ON_FINISH=true`, they also check delete permission before processing.
Readeck write permission is checked by the server when a bookmark update is
attempted. HTTP 401/403 responses produce a service-specific configuration
error and exit the worker with status 1, rather than retrying article generation.
For completion actions, the HTTP failure is also saved in `finish_error`; already
successful steps remain recorded. Correct the token/permissions or disable the
relevant action and restart. Compose's `unless-stopped` policy restarts a failed
worker, so unresolved permission errors may repeat in its logs.

These settings also apply to previously delivered jobs in the retained database.
Enabling a previously disabled action applies it on a later poll, including after
another action has already completed. Keep the database volume when upgrading.
Only jobs matching the current Readeck source and saved Audiobookshelf destination
are eligible for listening completion actions.

Tag removal is part of delivery and preserves other labels. If it fails,
`status` reports `tag_error` and the worker retries on later polls using the
label saved with the job. Older snapshots use the current `READECK_TAG`.
When upgrading with an existing database, tags on already delivered articles
are cleared as fulfilled requests. The former `REMOVE_TAG_ON_FINISH` setting
can be removed from deployment configuration.

## Run with Docker Compose

Copy [compose.yaml](compose.yaml) into your deployment directory and edit its
Readeck/Audiobookshelf URLs and API tokens, then configure a speech provider.
Optional environment variables are
commented out with their default values. The template uses the published image
at `ghcr.io/delete-yourself-today/readeck-tts:latest` and a persistent named volume.

```bash
docker compose pull
docker compose run --rm readeck-tts check
docker compose up -d
docker compose logs -f
```

The worker needs outbound access to Readeck, Audiobookshelf, and the selected
speech provider. It exposes no ports. When using Kokoro, its hostname must resolve
and be reachable from the container.
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

Kokoro's default voice is `af_heart`; set `KOKORO_VOICE` to change it for future jobs.
For variety, use a comma-separated list such as `KOKORO_VOICE=af_heart,af_bella`.
Each new article picks one voice with equal probability; consecutive repeats are
allowed. Whitespace around names is trimmed, duplicate names count once, and
empty entries are rejected. Every listed voice must be available at the endpoint.
The chosen voice is saved in the job snapshot and reused across retries and
restarts, even if the configured list changes. The same random selection applies
to `ELEVENLABS_VOICE` and `GEMINI_VOICE`, using their provider-specific IDs/names.
One voice narrates the whole article, including every chunk.

New jobs preserve headings (including their levels), paragraphs, list items,
and quotations from Readeck's article HTML. Kokoro narration adds explicit pauses:
1 second after the title, 0.8 seconds before headings, 0.6 seconds after headings
and around quotations, 0.4 seconds between paragraphs, and 0.3 seconds between
list items. Adjacent boundaries use the longer pause rather than stacking them.
HTML source line wraps and empty containers do not add pauses. Speed remains
`1.0`; playback speed can be adjusted in your player.

The Kokoro renderer uses the server's
[`[pause:1s]` syntax](https://github.com/remsky/Kokoro-FastAPI#text-control),
so the endpoint must support inline pause tokens. Very long articles scale down
pauses to approximately 240 seconds total, below the server's default 300-second
pause budget. Structured blocks, plain text, and the rendered speech input are
saved in the job snapshot; retries reuse that exact input. Existing snapshots
continue using their original input. Apply the request label again after it clears
to use the new settings on a previously delivered article.

ElevenLabs v4 and v4 Turbo share one HTTP API and the same rendering: a restrained
narration cue plus `[long pause]` after the title and before major headings
(levels 1–2), `[short pause]` between consecutive list items, and `[pause]` at
other block boundaries, with Stability `0.5` and Similarity `0.75`. Pauses are
preserved when a block boundary falls between requests. Gemini 3.8 uses the Interactions API with style metadata and
`<short pause>` tags. These qualitative pauses are interpreted by the model;
they do not promise the exact durations used by Kokoro. Playback speed stays
unchanged and no pronunciation overrides are applied.

Hosted articles are split at paragraph/sentence boundaries into requests of at
most 4,000 rendered characters, splitting exceptionally long sentences at words.
New ElevenLabs jobs also save `previous_text` and `next_text` context for each
request: up to two sentences from the neighboring chunks, capped at 500
characters per side at word boundaries. Context comes from source text without
generated narration tags and is sent separately from the text to be spoken.
The first request has no previous context, and the last has no next context.
Saved jobs retain their original text and context plan across retries; older
jobs without context continue using their original requests.
Each request's native audio and usage receipt are saved in a `chunks` directory
before assembly. An interrupted or failed generation reuses valid completed
chunks; a failed MP3 assembly does not regenerate them. A request interrupted
before its audio is durably saved may still have been billed and will need to
run again. There are no hidden automatic HTTP retries or provider fallbacks.

`KOKORO_BASE_URL` includes `/v1`. `KOKORO_API_KEY` is optional. The Readeck and
Audiobookshelf base URLs should not include `/api`. The worker does not use
OpenAI; the standalone Realtime comparison script below is separate.

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

Each request has its own job key within a Readeck instance and article ID. Job
discovery and sync checkpoints are saved in one transaction, with an overlapping
sync window. A file lock prevents two workers from using the same data directory
concurrently. Completed jobs remain in the database, including when their
episodes are removed from Audiobookshelf.

Each job snapshots its text, metadata, provider, model, voice, delivery options,
rendered requests, and destination before generation. Audio is validated,
tagged, fsynced, and renamed into place before
upload. Failed uploads reuse that audio. A crash between rename and the SQLite
update also recovers it. Interrupted local generation retries automatically;
Kokoro-FastAPI handles text chunking internally, and an interrupted Kokoro request
may repeat the whole article. Hosted providers resume from their completed native
audio chunks. Once delivery is confirmed, final audio, native chunks, and chunk
usage receipts are removed together; the ledger keeps the job snapshot.

Changing `TTS_PROVIDER` affects new jobs. Unfinished jobs continue with the
provider recorded in their snapshot, so retain that provider's credentials (and
`KOKORO_BASE_URL` for old Kokoro jobs) until their audio has been generated.
Snapshots created before provider selection was introduced are treated as
Kokoro jobs. Fully generated audio can be delivered without regenerating it.

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

`SIGTERM` and `SIGINT` stop the worker promptly, including during synchronous
HTTP requests. Shutdown interrupts the request and unwinds its file/socket
contexts, closes the worker's HTTP sessions and database, and releases the data
directory lock. It exits cleanly without charging an article retry attempt.
Partial generation files are discarded; an interrupted generation resumes
from its saved input and completed hosted chunks on restart. Completed local
audio is preserved until delivery is confirmed, and interrupted uploads reconcile against Audiobookshelf
before retrying. Disconnecting does not guarantee that a provider's server-side
computation or billing is cancelled.

### Credentials and destination validation

`run` and `once` perform the same read-only preflight as `check` before
discovering or generating anything: Readeck authentication, Audiobookshelf
authentication/upload permission, destination library/folder, and selected speech
provider model/voice availability. `run` repeats these checks on every poll, so
deleted libraries or revoked access are not hidden by a permanently cached
destination ID.

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
SQLite so recovery retries only the remaining delivery steps.

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
The next worker poll clears the request label in Readeck.
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

## Standalone OpenAI Realtime comparison

The optional comparison script generates local samples from an excerpt JSON
containing a `blocks` array (`type`, `text`, and optional heading `level`). It does
not discover jobs, change the ledger, or upload episodes. The production worker
continues to use Kokoro.

```bash
.venv/bin/pip install -e '.[comparison]'
# Add OPENAI_API_KEY to .env; the comparison script loads it locally.
.venv/bin/python scripts/compare_realtime.py path/to/excerpt.json \
  --model gpt-realtime-2.1 --voice cedar --output data/comparisons/sample
```

Each invocation sends the excerpt to OpenAI and incurs API usage. The default
model is `gpt-realtime-2.1`, with Cedar at normal playback speed. The prompt asks
for faithful, restrained literary narration, using block types as silent delivery
cues. The sample directory contains `audio.wav` (24 kHz mono PCM16), the exact
request, the model's returned transcript, and a summary with completion status,
duration, and reported token usage. The transcript is model-reported, not an
independent transcription or guarantee of verbatim audio. Failed generations
retain their diagnostic summary and any partial audio. Existing audio is never
overwritten; choose a new directory to generate another take.

## Standalone ElevenLabs comparison

Add `ELEVENLABS_API_KEY` to `.env`, then generate the same excerpt with
Multilingual v2 and the default George storytelling voice:

```bash
.venv/bin/python scripts/compare_elevenlabs.py path/to/excerpt.json \
  --output data/comparisons/elevenlabs-sample
```

Use `--voice-id` to select another available voice. The script uses the same
structural pause timings as Kokoro, rendered as ElevenLabs break tags for
Multilingual v2. Speed remains `1.0`; the remaining voice settings use the
documented defaults. To compare Eleven v4 using the same voice:

```bash
.venv/bin/python scripts/compare_elevenlabs.py path/to/excerpt.json \
  --model eleven_v4 --output data/comparisons/elevenlabs-v4-sample
```

The v4 renderer uses the same narration cue and structural `[short pause]`,
`[pause]`, and `[long pause]` tags as the worker. It sends only Stability and Similarity settings;
v4 does not support the v2 SSML, Style, or Speed controls. Neither version applies
pronunciation overrides or rewrites the article. Use `--model eleven_v4_turbo`
to compare the Turbo variant with the same text rendering and voice settings. The script
makes one generation request with no automatic retries and saves `audio.mp3`,
the exact request, and a summary containing duration and any returned credit
cost and request identifiers. Existing or partial audio is never overwritten.
This uses ElevenLabs account credits and does not upload to Audiobookshelf or
modify worker jobs.

## Standalone Gemini comparison

Add `GEMINI_API_KEY` from Google AI Studio to `.env`. Generate a sample with:

```bash
.venv/bin/python scripts/compare_gemini.py path/to/excerpt.json \
  --model gemini-3.8-flash-tts --voice Charon --output data/comparisons/gemini-sample
```

The supported models are `gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts`,
and `gemini-2.5-pro-preview-tts`. The 3.8 models use the Interactions API with
delivery style in separate metadata and inline `<short pause>` tags. Pro uses
`generateContent` with an instruction prompt and the original paragraph layout.
All versions preserve the source wording and leave playback speed unchanged.

The script sends one generation request with no automatic retries and saves
`audio.wav`, the exact request, response metadata without base64 audio, and a
summary with duration and token usage. It accepts both complete WAV and raw
PCM16 responses and checks completion status before accepting a sample. Existing
or partial audio is never overwritten. No worker jobs or podcast episodes are
created. Each generation uses the Gemini API key's project quota and billing.

## Optional Kokoro server setup

Use any reachable Kokoro-compatible HTTP endpoint. For a native Apple Silicon
server, see [the optional macOS setup guide](deploy/kokoro-macos/README.md).
