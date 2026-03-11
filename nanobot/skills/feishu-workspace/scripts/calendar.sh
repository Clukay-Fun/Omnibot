#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
SCRIPT_PATH="$SCRIPT_DIR/calendar.py"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo '{"ok": false, "error": {"kind": "runtime_error", "message": "Expected project virtualenv at <repo>/.venv/bin/python. This wrapper only supports source checkout + project venv execution.", "code": null, "status": 500, "request_id": null}}'
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_PATH" "$@"
