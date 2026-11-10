# Miss Quiz Bot

Telegram quiz bot for exam preparation (CLAT first, more exams next).
Python 3.10+ · python-telegram-bot 22 · MongoDB.

Community: [t.me/VillanCommunity](https://t.me/VillanCommunity)

## What it does

| For | Features |
|---|---|
| **Users** | `/quiz` questions, `/battle` 1v1 (group or DM invite), `/score`, `/stats`, `/achievements`, `/categories`, `/help` |
| **Groups** | Auto quiz every hour where the bot is admin (new poll first, old one deleted); "make me admin" reminder once a day where it isn't; topic groups use the open topic |
| **Admins** | Red **Admin Panel** button on `/start` (DM only): bot stats (live), add questions (text, any file, forwarded quiz polls), find / edit / delete questions, broadcast with full Telegram formatting, admins with custom title + emoji, start photo, emoji test, reload, restart |
| **Look** | Premium (custom) emoji everywhere when the owner has Telegram Premium, bold text, coloured buttons, red Back |

Question files: `.txt .md .csv .tsv .xlsx .json .docx .odt .pptx .pdf .html .rtf` — almost any layout
(numbered or not, A)/(a)/1./Option A, answer line, ✅ marks, answer key at the end, Hindi).
Images and scanned PDFs are not supported (they need OCR).

## Run on a VPS (recommended)

Ubuntu 22.04, safe next to other bots (no package upgrades, no firewall changes,
RAM/CPU capped):

```bash
git clone https://github.com/Ownermood/MissQuiz.git /opt/miss-quiz
bash /opt/miss-quiz/deploy/setup_vps.sh --check   # shows the plan, changes nothing
bash /opt/miss-quiz/deploy/setup_vps.sh           # installs and creates the service
nano /opt/miss-quiz/.env                          # TELEGRAM_TOKEN, OWNER_ID
systemctl start miss-quiz
```

Update later: `bash /opt/miss-quiz/deploy/update.sh` · Logs: `journalctl -u miss-quiz -f`

## Run locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill TELEGRAM_TOKEN, OWNER_ID, MONGODB_URL
python main.py
```

Or with Docker (bot + MongoDB): `docker compose up -d`

## Settings (`.env`)

| Name | Needed | Meaning |
|---|---|---|
| `TELEGRAM_TOKEN` | yes | From @BotFather |
| `OWNER_ID` | yes | Your Telegram user ID |
| `MONGODB_URL` | yes | e.g. `mongodb://127.0.0.1:27017` |
| `MODE` | | `polling` (default) or `webhook` (+ `WEBHOOK_URL`) |
| `ADMIN_API_KEY` | for web panel | Web admin panel / API is off without it |
| `PREMIUM_EMOJI` | | `1` only if the bot owner has Telegram Premium |
| `BOLD_TEXT` | | `0` to turn off bold-everything |
| `WIFU_ID` | | Second owner |

## Development

```bash
python -m pytest -q -o addopts=""      # tests
python -m py_compile $(git ls-files '*.py')
```

Project rules, architecture and roadmap: [`CLAUDE.md`](CLAUDE.md) ·
Planned group quiz sessions: [`docs/GROUP_QUIZ_PLAN.md`](docs/GROUP_QUIZ_PLAN.md)

## License

MIT — see [LICENSE](LICENSE).
