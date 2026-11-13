# Optional Kokoro server on Apple Silicon

The worker only requires an HTTP endpoint compatible with Kokoro-FastAPI. This
helper runs that backend natively on macOS with MPS acceleration; it is not
required when using an existing endpoint elsewhere.

With Homebrew and uv installed:

```bash
brew install espeak-ng ffmpeg
mkdir -p ~/src
git clone https://github.com/remsky/Kokoro-FastAPI.git ~/src/kokoro-fastapi
cd ~/src/kokoro-fastapi
git checkout --detach v0.10.0
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python docker/scripts/download_model.py --output api/src/models/v1_0
```

Then run `deploy/kokoro-macos/configure.py` from this repository on the Mac.
It creates an opinionated configuration under the current user's home:

```text
~/src/kokoro-fastapi/.env
~/.local/share/kokoro-service/run.sh
~/Library/LaunchAgents/local.kokoro-server.plist
~/Library/Logs/kokoro-server/stdout.log
~/Library/Logs/kokoro-server/stderr.log
```

An existing backend `.env` is preserved. The helper configures one worker on
`0.0.0.0:8880`, with no API authentication, for a trusted LAN. Use the backend's
own settings for authentication or other server options. Set the article
worker's `KOKORO_BASE_URL` to `http://YOUR_MAC_HOSTNAME:8880/v1`; its
`KOKORO_VOICE` controls article narration independently of the backend default.

Load and inspect the user service:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.kokoro-server.plist
launchctl print gui/$(id -u)/local.kokoro-server
launchctl kickstart -k gui/$(id -u)/local.kokoro-server
tail -f ~/Library/Logs/kokoro-server/stderr.log
```

It starts at user login and restarts after exit. It remains independent of SSH
sessions. It does not start before user login; logging out ends the GUI service
session. No root access or LaunchDaemon is used. Stop and unload it with:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.kokoro-server.plist
```

If upgrading an existing installation with a different launchd label, unload
the old service before loading this one so they do not compete for port 8880.
Startup uses the installed interpreter directly, without installing packages
or downloading models. The upstream release is pinned; dependency resolution
can still change when installing again.

The backend provides a voice player at `/web/`, API docs at `/docs`, health at
`/health`, and voices at `/v1/audio/voices`. To test from another LAN machine:

```bash
python3 scripts/check_kokoro.py http://YOUR_MAC_HOSTNAME:8880/v1 \
  --text-file examples/narration.txt --output /tmp/kokoro-article.wav
```

This uses only Python's standard library. It requests WAV narration, finalizes
streaming WAV headers, and reports audio duration and generation time.
