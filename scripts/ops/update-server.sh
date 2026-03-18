#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-nanobot}"
SERVICE_NAME="${SERVICE_NAME:-nanobot-gateway}"
APP_DIR="${APP_DIR:-/opt/omnibot}"
BRANCH="${1:-${BRANCH:-dev/upstream-clean-main}}"

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run this script as root or with sudo" >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "ERROR: service user not found: $SERVICE_USER" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "ERROR: app dir not found: $APP_DIR" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "ERROR: git repo not found: $APP_DIR" >&2
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/pip" ]]; then
  echo "ERROR: venv pip not found: $APP_DIR/.venv/bin/pip" >&2
  echo "HINT: create the venv manually before using this script." >&2
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/nanobot" ]]; then
  echo "ERROR: nanobot executable not found: $APP_DIR/.venv/bin/nanobot" >&2
  exit 1
fi

run_as_service_user() {
  sudo -u "$SERVICE_USER" -H "$@"
}

run_git_as_service_user() {
  if [[ -n "${HTTP_PROXY:-}" || -n "${HTTPS_PROXY:-}" || -n "${ALL_PROXY:-}" ]]; then
    sudo -u "$SERVICE_USER" -H env \
      HTTP_PROXY="${HTTP_PROXY:-}" \
      HTTPS_PROXY="${HTTPS_PROXY:-}" \
      ALL_PROXY="${ALL_PROXY:-}" \
      git -c http.version=HTTP/1.1 -C "$APP_DIR" "$@"
  else
    sudo -u "$SERVICE_USER" -H git -C "$APP_DIR" "$@"
  fi
}

repo_dirty=0
if ! run_as_service_user bash -lc "cd '$APP_DIR' && git diff --quiet --ignore-submodules=all && git diff --cached --quiet --ignore-submodules=all"; then
  repo_dirty=1
fi

untracked="$(run_git_as_service_user ls-files --others --exclude-standard)"
if [[ $repo_dirty -ne 0 || -n "$untracked" ]]; then
  echo "ERROR: git working tree is not clean; refusing to update." >&2
  echo >&2
  run_git_as_service_user status --short >&2
  echo >&2
  echo "HINT: clean or stash local changes first, then rerun." >&2
  exit 1
fi

old_commit="$(run_git_as_service_user rev-parse --short HEAD)"

echo "== Nanobot server update =="
echo "APP_DIR=$APP_DIR"
echo "SERVICE_USER=$SERVICE_USER"
echo "SERVICE_NAME=$SERVICE_NAME"
echo "BRANCH=$BRANCH"
echo "OLD_COMMIT=$old_commit"
echo "Rollback if needed: sudo -u $SERVICE_USER -H git -C $APP_DIR checkout $old_commit"
echo

echo "[1/6] Fetching latest refs..."
run_git_as_service_user fetch --all --tags

echo "[2/6] Checking out branch $BRANCH..."
run_git_as_service_user checkout "$BRANCH"

echo "[3/6] Pulling latest commit..."
if ! run_git_as_service_user pull --ff-only; then
  echo >&2
  echo "ERROR: fast-forward update failed. Local branch has diverged from origin/$BRANCH." >&2
  echo "HINT: inspect 'git status' and 'git log --oneline --decorate --graph --all -20' before retrying." >&2
  exit 1
fi

echo "[4/6] Updating submodules..."
run_git_as_service_user submodule update --init --recursive --depth 1

echo "[5/6] Reinstalling application..."
run_as_service_user bash -lc "cd '$APP_DIR' && '$APP_DIR/.venv/bin/pip' install -e ."

echo "[6/6] Restarting service..."
systemctl restart "$SERVICE_NAME"

echo "Waiting for service health check..."
sleep 3
if [[ "$(systemctl is-active "$SERVICE_NAME")" != "active" ]]; then
  echo "ERROR: service failed health check: $SERVICE_NAME" >&2
  journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2
  exit 1
fi

new_commit="$(run_git_as_service_user rev-parse --short HEAD)"

echo
echo "Update succeeded."
echo "OLD_COMMIT=$old_commit"
echo "NEW_COMMIT=$new_commit"
echo "Rollback command: sudo -u $SERVICE_USER -H git -C $APP_DIR checkout $old_commit && sudo systemctl restart $SERVICE_NAME"
echo
echo "Submodules:"
run_git_as_service_user submodule status
echo
echo "Service status:"
systemctl status "$SERVICE_NAME" --no-pager | sed -n '1,12p'
echo
echo "Recent logs:"
journalctl -u "$SERVICE_NAME" -n 20 --no-pager
