#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.11}"
SCHEDULE="${SCHEDULE:-0 3 * * *}"
JOB="${SCHEDULE} ${PYTHON_BIN} ${REPO_ROOT}/ops/maintenance/sqlite_backup_rotate.py"

TMP_FILE="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'sqlite_backup_rotate.py' >"${TMP_FILE}" || true
printf '%s\n' "${JOB}" >>"${TMP_FILE}"
crontab "${TMP_FILE}"
rm -f "${TMP_FILE}"

echo "Installed cron job: ${JOB}"
