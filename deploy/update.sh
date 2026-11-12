#!/usr/bin/env bash
# Pull the latest code and restart only this bot.   sudo bash /opt/miss-quiz/deploy/update.sh
set -euo pipefail
APP_DIR="/opt/miss-quiz"; BRANCH="ccr-f1e736ea-a5z34q"
[[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }
git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull origin "$BRANCH"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
chown -R missquiz:missquiz "$APP_DIR"
systemctl restart miss-quiz
sleep 3
journalctl -u miss-quiz -n 15 --no-pager
