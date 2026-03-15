#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-$(pwd)}"
CONFIG_PATH="${2:-$HOME/.nanobot/config.json}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "ERROR: app dir not found: $APP_DIR" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "ERROR: config file not found: $CONFIG_PATH" >&2
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "ERROR: missing Python virtualenv at $APP_DIR/.venv" >&2
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/nanobot" ]]; then
  echo "ERROR: nanobot executable not found at $APP_DIR/.venv/bin/nanobot" >&2
  exit 1
fi

echo "== Nanobot Feishu preflight =="
echo "APP_DIR=$APP_DIR"
echo "CONFIG_PATH=$CONFIG_PATH"

"$APP_DIR/.venv/bin/python" - <<'PY' "$CONFIG_PATH"
import json
import os
import stat
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
data = json.loads(config_path.read_text(encoding="utf-8"))

errors = []
warnings = []

agents = data.get("agents", {}).get("defaults", {})
channels = data.get("channels", {})
feishu = channels.get("feishu", {})
gateway = data.get("gateway", {})

model = agents.get("model", "")
workspace = Path(agents.get("workspace", "~/.nanobot/workspace")).expanduser()
send_progress = channels.get("sendProgress", None)
send_tool_hints = channels.get("sendToolHints", None)

if not model:
    errors.append("agents.defaults.model is missing")

if not feishu.get("enabled"):
    errors.append("channels.feishu.enabled must be true")

mode = feishu.get("mode")
if mode != "websocket":
    warnings.append(f"channels.feishu.mode is '{mode}', first deployment is recommended to use 'websocket'")

if not feishu.get("appId"):
    errors.append("channels.feishu.appId is missing")

if not feishu.get("appSecret"):
    errors.append("channels.feishu.appSecret is missing")

allow_from = feishu.get("allowFrom", [])
if not allow_from:
    warnings.append("channels.feishu.allowFrom is empty; no one will be allowed to talk to the bot")
elif allow_from == ["*"]:
    warnings.append("channels.feishu.allowFrom is ['*']; tighten this after first successful onboarding")

if send_progress is not True:
    warnings.append("channels.sendProgress is not true; tool/search progress feedback may not appear in Feishu")

if send_tool_hints is not True:
    warnings.append("channels.sendToolHints is not true; tool hint progress may not appear in Feishu")

streaming_scope = feishu.get("streamingScope", "dm")
if streaming_scope != "dm":
    warnings.append(f"channels.feishu.streamingScope is '{streaming_scope}'; first deployment is recommended to use 'dm'")

port = gateway.get("port", 18790)
host = gateway.get("host", "0.0.0.0")

mode_bits = stat.S_IMODE(os.stat(config_path).st_mode)
if mode_bits & 0o077:
    warnings.append(f"config file permissions are too open ({oct(mode_bits)}); recommended chmod 600")

print(f"model: {model}")
print(f"workspace: {workspace}")
print(f"feishu mode: {mode}")
print(f"gateway: {host}:{port}")
print(f"workspace exists: {workspace.exists()}")

if errors:
    print("\nERRORS:")
    for item in errors:
        print(f"- {item}")
    sys.exit(1)

if warnings:
    print("\nWARNINGS:")
    for item in warnings:
        print(f"- {item}")
else:
    print("\nNo warnings.")
PY

echo
echo "== nanobot status =="
"$APP_DIR/.venv/bin/nanobot" status

echo
echo "Preflight finished."
