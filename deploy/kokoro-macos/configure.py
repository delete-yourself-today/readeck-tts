#!/usr/bin/env python3
"""Configure an installed Kokoro-FastAPI checkout for macOS launchd."""

from pathlib import Path
import plistlib
import shlex


home = Path.home()
checkout = home / "src/kokoro-fastapi"
deployment = home / ".local/share/kokoro-service"
logs = home / "Library/Logs/kokoro-server"
agents = home / "Library/LaunchAgents"
for directory in (deployment, logs, agents):
    directory.mkdir(parents=True, exist_ok=True)

env = checkout / ".env"
if not env.exists():
    env.write_text(
        f"MODEL_DIR={checkout}/api/src/models\n"
        f"VOICES_DIR={checkout}/api/src/voices/v1_0\n"
        f"WEB_PLAYER_PATH={checkout}/web\n"
        "USE_GPU=true\nDEVICE_TYPE=mps\nDEFAULT_VOICE=af_heart\n"
        "CORS_ENABLED=false\n"
    )
    env.chmod(0o600)

runner = deployment / "run.sh"
runner.write_text(
    "#!/bin/bash\nset -euo pipefail\n"
    'export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"\n'
    "export PYTORCH_ENABLE_MPS_FALLBACK=1\n"
    "export HF_HUB_DISABLE_TELEMETRY=1\n"
    "export API_LOG_LEVEL=INFO\n"
    f"export PYTHONPATH={shlex.quote(str(checkout))}\n"
    f"cd {shlex.quote(str(checkout))}\n"
    f"exec {shlex.quote(str(checkout / '.venv/bin/python'))} -m uvicorn "
    "api.src.main:app --host 0.0.0.0 --port 8880 --workers 1\n"
)
runner.chmod(0o755)

label = "local.kokoro-server"
common = {
    "Label": label,
    "ProgramArguments": [str(runner)],
    "WorkingDirectory": str(checkout),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "StandardOutPath": str(logs / "stdout.log"),
    "StandardErrorPath": str(logs / "stderr.log"),
}
agent = agents / f"{label}.plist"
agent.write_bytes(plistlib.dumps(common))
print(f"LaunchAgent: {agent}")
