#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-nanobot-gateway}"
SERVICE_USER="${SERVICE_USER:-nanobot}"
SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
APP_DIR="${APP_DIR:-/opt/ominibot}"
HOME_DIR="${HOME_DIR:-/home/$SERVICE_USER}"
CONFIG_PATH="${CONFIG_PATH:-$HOME_DIR/.nanobot/config.json}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run this script as root or with sudo" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "ERROR: app dir not found: $APP_DIR" >&2
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/nanobot" ]]; then
  echo "ERROR: nanobot executable not found: $APP_DIR/.venv/bin/nanobot" >&2
  exit 1
fi

cat >"$UNIT_PATH" <<EOF
[Unit]
Description=Nanobot Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR
Environment=HOME=$HOME_DIR
ExecStart=$APP_DIR/.venv/bin/nanobot gateway --config $CONFIG_PATH
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "Installed systemd unit: $UNIT_PATH"
echo
echo "Next commands:"
echo "  systemctl start $SERVICE_NAME"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
