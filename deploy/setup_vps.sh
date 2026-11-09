#!/usr/bin/env bash
# Miss Quiz Bot — VPS setup (Ubuntu 22.04). Safe to run again.
# Built for a server that already runs OTHER bots: it never upgrades existing
# packages, never restarts other services, never touches the firewall, and
# caps this bot's (and its MongoDB's) RAM/CPU.
#
#   sudo bash setup_vps.sh --check    # only show what would happen, change nothing
#   sudo bash setup_vps.sh            # bot + local MongoDB (if 27017 is free)
#   sudo bash setup_vps.sh --atlas    # bot only, keep using MongoDB Atlas
set -euo pipefail

REPO="https://github.com/ownermood/miss-quiz.git"
BRANCH="ccr-f1e736ea-a5z34q"
APP_DIR="/opt/miss-quiz"
APP_USER="missquiz"
LOCAL_MONGO=1; CHECK=0
for a in "$@"; do
    case "$a" in --atlas) LOCAL_MONGO=0 ;; --check) CHECK=1 ;; esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
say()  { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m!! $*\033[0m"; }
port_busy() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }

# apt: install ONLY what is missing, never upgrade, never auto-restart services
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
missing() { local m=(); for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || m+=("$p"); done; echo "${m[@]:-}"; }

NEED_PKGS=$(missing git python3 python3-venv python3-pip curl gnupg ca-certificates)
MONGO_PLAN="none"
if [[ $LOCAL_MONGO -eq 1 ]]; then
    if port_busy 27017; then MONGO_PLAN="reuse existing MongoDB on 27017 (own database 'quiz_bot')"
    elif command -v mongod >/dev/null; then MONGO_PLAN="use installed mongod"
    else MONGO_PLAN="install MongoDB 7, bind 127.0.0.1, cache limited to 1 GB"; fi
fi

say "Plan (nothing else on this server is touched)"
echo "  missing packages to install : ${NEED_PKGS:-none}"
echo "  MongoDB                     : $MONGO_PLAN"
echo "  code                        : $APP_DIR (user $APP_USER)"
echo "  service                     : miss-quiz (max 2 GB RAM, 2 CPU cores, low priority)"
echo "  firewall / other services   : NOT touched"
echo "  listening ports right now   :"
ss -ltnp 2>/dev/null | awk 'NR>1 {print "    " $4 "  " $6}' || true
[[ $CHECK -eq 1 ]] && { echo -e "\n--check: nothing changed."; exit 0; }

if [[ -n "$NEED_PKGS" ]]; then
    say "Installing missing packages: $NEED_PKGS"
    apt-get update -y
    # shellcheck disable=SC2086
    apt-get install -y --no-upgrade $NEED_PKGS
fi

if [[ "$MONGO_PLAN" == install* ]]; then
    say "MongoDB 7 (only reachable from this server)"
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc \
        | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg --yes
    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list
    apt-get update -y
    apt-get install -y --no-upgrade mongodb-org
    # By default MongoDB takes ~50% of RAM for cache — cap it so other bots keep theirs
    if ! grep -q "cacheSizeGB" /etc/mongod.conf; then
        sed -i 's/^storage:/storage:\n  wiredTiger:\n    engineConfig:\n      cacheSizeGB: 1/' /etc/mongod.conf
    fi
    systemctl enable --now mongod
fi

say "App user + code"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone -b "$BRANCH" "$REPO" "$APP_DIR"     # private repo: asks GitHub username + token
else
    # repo belongs to $APP_USER; tell git (running as root) that's fine
    git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull origin "$BRANCH"
fi

say "Python environment (private venv, system Python untouched)"
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

if grep -q '^TELEGRAM_TOKEN=your_bot_token_here' "$APP_DIR/.env" || ! grep -q '^OWNER_ID=[0-9]' "$APP_DIR/.env"; then
    echo -e "\n\033[1;33mNEXT: nano $APP_DIR/.env  → set TELEGRAM_TOKEN and OWNER_ID, then: systemctl start miss-quiz\033[0m"
else
    systemctl restart miss-quiz
    echo -e "\n\033[1;32mBot started. Logs: journalctl -u miss-quiz -f\033[0m"
fi
