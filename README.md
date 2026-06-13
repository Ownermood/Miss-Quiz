<div align="center">

```
 ██████╗██╗      █████╗ ████████╗    ██╗   ██╗██╗███████╗██╗ ██████╗ ███╗   ██╗
██╔════╝██║     ██╔══██╗╚══██╔══╝    ██║   ██║██║██╔════╝██║██╔═══██╗████╗  ██║
██║     ██║     ███████║   ██║       ██║   ██║██║███████╗██║██║   ██║██╔██╗ ██║
██║     ██║     ██╔══██║   ██║       ╚██╗ ██╔╝██║╚════██║██║██║   ██║██║╚██╗██║
╚██████╗███████╗██║  ██║   ██║        ╚████╔╝ ██║███████║██║╚██████╔╝██║ ╚████║
 ╚═════╝╚══════╝╚═╝  ╚═╝   ╚═╝         ╚═══╝  ╚═╝╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝
```

# 🌷 Miss Quiz — CLAT Vision Quiz Bot 🎓

**Premium Telegram Quiz Bot · MongoDB · Python 3.11 · python-telegram-bot v22**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/CLAT_Vision)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248?style=for-the-badge&logo=mongodb&logoColor=white)](https://mongodb.com)
[![License](https://img.shields.io/badge/License-MIT-F59E0B?style=for-the-badge)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](Dockerfile)

<br/>

> *ʜɪɪɪɪ ᴅᴀʀʟɪɴɢ! 💕 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛʜᴇ ᴍᴏꜱᴛ ᴀᴅᴏʀᴀʙʟᴇ ᴄʟᴀᴛ ᴘʀᴇᴘ ᴄᴏᴍᴘᴀɴɪᴏɴ ᴏɴ ᴛᴇʟᴇɢʀᴀᴍ!*

</div>

---

## ✨ What is Miss Quiz?

**Miss Quiz** is a production-ready, feature-rich Telegram quiz bot built for **CLAT aspirants**. It delivers interactive multiple-choice questions, tracks progress across groups, runs a live global leaderboard, and auto-broadcasts quizzes every 30 minutes — all backed by MongoDB Atlas.

```
👤 Users          →   /quiz /score /stats /achievements /leaderboard
🛡️ Admins         →   /addquiz /delquiz /editquiz /importquiz /broadcast
📊 Analytics      →   /botstats + Web Admin Panel at /admin
⏰ Auto Quiz      →   Every 30 min to all registered groups
```

---

## 🚀 Features

<table>
<tr>
<td width="50%">

### 🎯 Quiz Engine
- Telegram native **Quiz Polls** (not messages)
- **8 categories** — Legal, English, GK, Polity, Math, Reasoning, History, Current Affairs
- Anti-repeat per chat — never sees the same question twice in a row
- Poll answer persistence across bot restarts (MongoDB + pickle backup)

### 🏆 Leaderboard & Stats
- Global / Weekly / Monthly leaderboards (paginated, top 50)
- Per-user stats — accuracy, streak, XP level, rank badge
- Group-wise leaderboard
- Achievement system with milestone badges

</td>
<td width="50%">

### ⚙️ Admin Tools
- `/addquiz` — Add single question inline
- `/importquiz` — Bulk import from `.txt` file (auto-detects format)
- `/delquiz` — Delete by reply-to-poll or interactive paginated menu
- `/editquiz` — Paginated question browser
- `/broadcast` + `/delbroadcast` — Mass messaging with recall
- `/reload` — Hot-reload questions from DB without restart

### 📡 Web Admin Panel
- Dark-mode SPA at `http://localhost:5000/admin`
- Question CRUD, user leaderboard, broadcast composer
- REST API for all operations (`/api/questions`, `/api/metrics`, ...)

</td>
</tr>
</table>

---

## 📁 Project Structure

```
Miss-Quiz/
│
├── main.py                     ← Entry point (polling + webhook modes)
│
├── src/
│   ├── bot/
│   │   ├── handlers_main.py    ← TelegramQuizBot (assembles all mixins)
│   │   ├── handlers.py         ← Compat shim → handlers_main
│   │   ├── ui.py               ← Design tokens, UI class, constants
│   │   ├── tracking.py         ← Group & user registration pipeline
│   │   ├── poll_manager.py     ← Poll persistence & answer handler
│   │   ├── quiz_parser.py      ← Bulk .txt import parser
│   │   ├── dev_commands.py     ← Developer-only extended commands
│   │   └── commands/
│   │       ├── user_cmds.py    ← /start /help /ping /info /categories
│   │       ├── quiz_cmds.py    ← /quiz /score /stats /achievements /botstats
│   │       ├── leaderboard_cmds.py  ← /leaderboard + pagination
│   │       └── admin_cmds.py   ← /addquiz /delquiz /editquiz + more
│   │
│   ├── core/
│   │   ├── config.py           ← Environment config (Config dataclass)
│   │   ├── database.py         ← MongoDB data access layer (1090 lines)
│   │   ├── quiz.py             ← QuizManager — in-memory cache + logic
│   │   └── exceptions.py       ← Custom exception hierarchy
│   │
│   ├── utils/
│   │   ├── rate_limiter.py     ← Sliding-window per-user rate limiting
│   │   └── scheduler.py        ← Auto-quiz scheduler (APScheduler)
│   │
│   └── web/
│       ├── app.py              ← Flask REST API + webhook endpoint
│       └── wsgi.py             ← Gunicorn / Waitress entry point
│
├── templates/
│   └── admin.html              ← Admin panel SPA (dark-mode)
├── static/js/
│   └── admin.js                ← Admin panel JavaScript
├── tests/                      ← 54 unit tests (pytest)
├── data/                       ← Poll cache, restart flag
├── Dockerfile                  ← Multi-stage production build
├── docker-compose.yml          ← Local dev stack
├── render.yaml                 ← One-click Render deployment
└── requirements.txt
```

---

## ⚡ Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/Ownermood/Miss-Quiz.git
cd Miss-Quiz
cp .env.example .env
```

Edit `.env`:
```env
TELEGRAM_TOKEN=your_bot_token_here
OWNER_ID=your_telegram_user_id
MONGODB_URL=mongodb+srv://user:pass@cluster.mongodb.net/
MONGODB_DB=quiz_bot
SESSION_SECRET=any_random_secret
```

### 2. Install & Run

```bash
pip install -r requirements.txt
python main.py
```

Bot starts in **polling mode** by default. Admin panel → `http://localhost:5000/admin`

### 3. Docker (Recommended)

```bash
# Copy and fill .env first
docker build -t miss-quiz .
docker run --env-file .env -p 5000:5000 miss-quiz
```

---

## 🌐 Deploy to Render (Free)

1. Fork this repo
2. Go to [render.com](https://render.com) → **New Web Service** → connect your fork
3. Set environment variables:

| Variable | Value |
|---|---|
| `TELEGRAM_TOKEN` | Your bot token from @BotFather |
| `OWNER_ID` | Your Telegram user ID |
| `MONGODB_URL` | MongoDB Atlas connection string |
| `SESSION_SECRET` | Any random string |
| `MODE` | `webhook` |
| `RENDER_URL` | Your render app URL |

4. Deploy — `render.yaml` handles everything automatically ✅

---

## 🎮 Bot Commands

### User Commands
| Command | Description |
|---|---|
| `/quiz` | Get a random quiz question |
| `/quiz Legal Reasoning` | Quiz from a specific category |
| `/score` | Your personal score card |
| `/stats` | Detailed performance stats |
| `/achievements` | Badges & milestones |
| `/leaderboard` | Global top 50 leaderboard |
| `/botstats` | Bot-wide analytics |
| `/categories` | Browse all quiz categories |
| `/help` | Command reference |
| `/ping` | Bot health check |

### Admin Commands *(Owner / Developer only)*
| Command | Description |
|---|---|
| `/addquiz` | Add a question (inline format) |
| `/importquiz` | Bulk import from `.txt` file |
| `/delquiz` | Delete a question |
| `/editquiz` | Browse & edit question bank |
| `/broadcast` | Send message to all users & groups |
| `/delbroadcast` | Recall last broadcast |
| `/reload` | Sync questions from MongoDB |
| `/restart` | Graceful bot restart |
| `/dev` | Developer control panel |

---

## 📊 Architecture

```
Telegram API
     │
     ▼
TelegramQuizBot  ←── inherits ──→  PollMixin
     │                             TrackingMixin
     │                             UserCommandsMixin
     │                             QuizCommandsMixin
     │                             LeaderboardMixin
     │                             AdminCommandsMixin
     │
     ├── QuizManager  ←── caches ──→  MongoDB (questions)
     │
     ├── DatabaseManager  ──────────→  MongoDB Atlas
     │       ├── users_col
     │       ├── groups_col
     │       ├── activities_col
     │       ├── poll_map_col
     │       └── broadcasts_col
     │
     ├── AutoQuizScheduler  ─────────→  every 30 min → all groups
     │
     └── Flask (Waitress)  ──────────→  /admin  /api/*  /webhook
```

---

## 🧪 Testing

```bash
pip install pytest pytest-asyncio pytest-cov
pytest tests/ -v
```

```
tests/test_config.py         — Config loading & validation
tests/test_quiz_manager.py   — QuizManager business logic
tests/test_ui.py             — UI helpers & design system
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
54 passed in 1.5s ✅
```

---

## 📦 Bulk Import Format (`.txt`)

Send a `.txt` file to the bot after `/importquiz`. The parser auto-detects:

```
1. Which Article of the Constitution abolishes untouchability?
A) Article 14
B) Article 17
C) Article 19
D) Article 21
Answer: B
Category: Legal Reasoning

2. ...
```

Supports numbered questions, lettered options (`A)` / `a.`), answer lines (`Answer:` / `Ans:` / asterisk marking), and automatic category tagging.

---

## 🏅 Rank System

| Rank | Score | Grade |
|---|---|---|
| 👑 LEGEND | 500+ | S |
| 🔱 MASTER | 200+ | A+ |
| ⚔️ EXPERT | 100+ | A |
| 🎯 ADVANCED | 50+ | B |
| 📈 RISING | 20+ | C |
| 🌱 BEGINNER | 5+ | D |
| 🎲 ROOKIE | 0–4 | E |

XP Level: Bronze → Silver → Gold → Platinum → Diamond → 💠 Legendary

---

## 🤝 Contributing

1. Fork the repo
2. Create a branch: `git checkout -b feature/amazing-feature`
3. Commit: `git commit -m "Add amazing feature"`
4. Push: `git push origin feature/amazing-feature`
5. Open a Pull Request

---

## 📜 License

MIT © [CLAT Vision](https://t.me/CLAT_Vision)

---

<div align="center">

**Made with 💕 for CLAT aspirants**

*ʏᴏᴜʀ ʟᴏᴠɪɴɢ Qᴜɪᴢ ʙᴜᴅᴅʏ ɪꜱ ᴀʟʟ ʏᴏᴜʀꜱ ~ 🥰*

[![Telegram](https://img.shields.io/badge/Join-CLAT_Vision-26A5E4?style=for-the-badge&logo=telegram)](https://t.me/CLAT_Vision)

</div>
