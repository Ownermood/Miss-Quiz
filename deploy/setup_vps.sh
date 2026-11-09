#!/usr/bin/env bash
# Miss Quiz Bot — one-time VPS setup (Ubuntu 22.04). Safe to run again.
#   sudo bash setup_vps.sh            # bot + local MongoDB (recommended)
#   sudo bash setup_vps.sh --atlas    # bot only, keep using MongoDB Atlas
set -euo pipefail

REPO="https://github.com/ownermood/miss-quiz.git"
BRANCH="ccr-f1e736ea-a5z34q"
APP_DIR="/opt/miss-quiz"
APP_USER="missquiz"
LOCAL_MONGO=1
[[ "${1:-}" == "--atlas" ]] && LOCAL_MONGO=0

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
say() { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m!! $*\033[0m"; }
port_busy() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }

say "What is already running on this server (other bots are left alone)"
ss -ltnp 2>/dev/null | awk 'NR==1 || /LISTEN/' || true

say "System packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv python3-pip \
    ufw fail2ban curl gnupg ca-certificates unattended-upgrades

if [[ $LOCAL_MONGO -eq 1 ]] && port_busy 27017; then
    warn "Something already listens on 27017 (existing MongoDB / Docker). Not installing another one;"
    warn "the bot will use it with its own database 'quiz_bot'."
    MONGO_EXISTS=1
fi
if [[ $LOCAL_MONGO -eq 1 ]] && [[ -z "${MONGO_EXISTS:-}" ]] && ! command -v mongod >/dev/null; then
    say "MongoDB 7 (local, only reachable from this server)"
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc \
        | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg --yes
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list
    apt-get update -y && apt-get install -y mongodb-org
    systemctl enable --now mongod
fi

say "App user + code"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone -b "$BRANCH" "$REPO" "$APP_DIR"     # private repo: asks GitHub username + token
else
    git -C "$APP_DIR" pull origin "$BRANCH"
fi

say "Python environment"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

if [[ ! -f "$APP_DIR/.env" ]]; then
    say "Creating .env (fill in TELEGRAM_TOKEN and OWNER_ID next)"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    sed -i 's/^MODE=.*/MODE=polling/' "$APP_DIR/.env"
    grep -q '^HOST=' "$APP_DIR/.env" || echo 'HOST=127.0.0.1' >> "$APP_DIR/.env"   # web panel not public
    PANEL_PORT=5000
    while port_busy "$PANEL_PORT"; do PANEL_PORT=$((PANEL_PORT + 1)); done     # don't clash with other apps
    sed -i "s/^PORT=.*/PORT=$PANEL_PORT/" "$APP_DIR/.env"
    say "Web panel port: $PANEL_PORT (localhost only)"
    if [[ $LOCAL_MONGO -eq 1 ]]; then
        sed -i 's#^MONGODB_URL=.*#MONGODB_URL=mongodb://127.0.0.1:27017#' "$APP_DIR/.env"
    fi
    python3 - "$APP_DIR/.env" <<'PY'
import secrets, sys, re
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"^ADMIN_API_KEY=.*$", "ADMIN_API_KEY=" + secrets.token_urlsafe(32), s, flags=re.M)
s = re.sub(r"^SESSION_SECRET=.*$", "SESSION_SECRET=" + secrets.token_urlsafe(32), s, flags=re.M)
open(p, "w").write(s)
PY
fi
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

say "systemd service (auto start on boot, auto restart on crash)"
cp "$APP_DIR/deploy/miss-quiz.service" /etc/systemd/system/miss-quiz.service
systemctl daemon-reload
systemctl enable miss-quiz

# Firewall: never switch it on here — that would block other bots' webhook /
# dashboard ports. If it's already on, only make sure SSH stays allowed.
if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow OpenSSH >/dev/null
    say "Firewall already active — left as it is (SSH allowed)"
else
    warn "Firewall (ufw) is OFF. Not enabling it automatically, so your other bots keep working."
    warn "This bot needs NO open port. Enable later only after allowing your other bots' ports."
fi
systemctl enable --now fail2ban

if grep -q '^TELEGRAM_TOKEN=your_bot_token_here' "$APP_DIR/.env" || ! grep -q '^OWNER_ID=[0-9]' "$APP_DIR/.env"; then
    echo -e "\n\033[1;33mNEXT: nano $APP_DIR/.env  → set TELEGRAM_TOKEN and OWNER_ID, then: systemctl start miss-quiz\033[0m"
else
    systemctl restart miss-quiz
    echo -e "\n\033[1;32mBot started. Logs: journalctl -u miss-quiz -f\033[0m"
fi
