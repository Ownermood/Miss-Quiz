# CLAUDE.md — Miss Quiz Bot

Ye file batati hai ki is repo mein kaam kaise karna hai. Har session ki shuruaat mein isse follow karo.
Owner Hinglish mein baat karta hai, jawab bhi Hinglish mein, short aur seedha do.

---

## 1. Project kya hai

**Miss Quiz**: Telegram quiz bot (abhi CLAT focus). Goal hai multi-exam aur multi-subject, phir global.

- Python 3.11 (`runtime.txt`), python-telegram-bot v22, MongoDB (pymongo), Flask admin panel, APScheduler
- Deploy: Render (`render.yaml`), Heroku-style `Procfile`, Docker (`Dockerfile`, `docker-compose.yml`)
- Modes: `MODE=polling` (local) / `MODE=webhook` (production) — `main.py`

## 2. Code ka naksha

```
main.py                     Entry point. Polling/webhook mode, startup, restart flag
src/core/config.py          Env config. OWNER_ID, WIFU_ID, AUTHORIZED_USERS
src/core/database.py        DatabaseManager (MongoDB). Collections neeche
src/core/quiz.py            QuizManager. Questions load + random question
src/bot/handlers_main.py    TelegramQuizBot class (mixins jodta hai) + saare add_handler
src/bot/handlers.py         Sirf compatibility shim. Naya code yahan mat likho
src/bot/commands/           user_cmds.py, quiz_cmds.py, admin_cmds.py (mixins)
src/bot/dev_commands.py     Developer commands + check_access()
src/bot/dev_broadcast.py    Broadcast / delbroadcast
src/bot/dev_quiz_editor.py  Inline-button quiz editor (/editquiz, edit_quiz_* callbacks)
src/bot/quiz_parser.py      .txt bulk import parser (/importquiz)
src/bot/poll_manager.py     Poll answers, poll_id mapping
src/bot/tracking.py         User/group tracking
src/bot/ui.py               UI strings, LINE/THIN, mention helpers
src/utils/scheduler.py      AutoQuizScheduler. Har group ko auto quiz bhejta hai
src/web/app.py              Flask: /health, /admin, /webhook, /api/*
src/web/wsgi.py             Gunicorn entry
templates/admin.html, static/js/admin.js   Web admin panel
tests/                      test_config.py, test_quiz_manager.py, test_ui.py
```

**MongoDB collections:** `questions`, `users`, `groups`, `broadcasts`, `activities`, `developers`, `poll_map`, `auto_quiz_state`, `_counters` (auto-increment quiz id).

**Auth layers:**
- Bot: `_is_owner()` / `_is_authorized()` (`handlers_main.py`), `check_access()` (`dev_commands.py`). Owner + WIFU + `developers` collection.
- Web API: `_check_api_auth()` (`src/web/app.py`), `ADMIN_API_KEY` header `X-Admin-Key`.
- Webhook: `WEBHOOK_SECRET_TOKEN` header `X-Telegram-Bot-Api-Secret-Token`.

**Auto quiz flow (`scheduler.py`):** poll bina timer (koi `open_period` nahi) bheja jaata hai, interval 60 min. Naya bhejne se pehle purana delete hota hai. `message_id` `auto_quiz_state` mein save hota hai, to restart ke baad bhi delete chalta hai. Abhi sirf groups mein chalta hai, channels mein nahi (`is_anonymous=False` hard-coded hai).

## 3. Commands (dev)

```bash
python -m py_compile $(git ls-files '*.py')           # har change ke baad zaroor
pip install -r requirements.txt                       # agar blinker error aaye: pip install --ignore-installed blinker -r requirements.txt
python -m pytest -q                                   # pytest.ini mein --cov hai (pytest-cov chahiye)
python -m pytest -q -o addopts=""                     # bina coverage ke
MODE=polling python main.py                           # local run (TELEGRAM_TOKEN, OWNER_ID, MONGODB_URL chahiye)
```

Env vars: `.env.example` dekho. Extra: `ADMIN_API_KEY`, `WEBHOOK_SECRET_TOKEN`, `WEBHOOK_URL`/`RENDER_URL`, `WIFU_ID`.

## 4. Kaam karne ke rules (sakht)

**Code:**
- Fix suggest ya implement karne se pehle asli code padho. Andaza mat lagao.
- Chhote, targeted edits karo. Poori file dobara mat likho.
- Jo feature chal raha hai use mat hatao, aur logic bina zaroorat mat badlo.
- UI text/style, button callback_data, DB schema/field names, API routes, command names aur pricing waise hi rehne do, jab tak badalna zaroori na ho.
- Existing helpers aur patterns reuse karo (`UI`, `_reply`, `check_access`, `asyncio.to_thread(self.db...)`). Duplicate helper mat banao.
- Bina pooche bade refactor ya file split mat karo.
- Risky change se pehle us file ka backup lo, ya kam se kam alag commit mein rakho.
- Blocking pymongo calls async handlers mein `asyncio.to_thread(...)` se hi karo.

**Security aur paise (sabse upar priority):**
- Security aur financial bugs naye features se pehle fix karo.
- Admin, owner ya developer ka har command/callback auth check se shuru ho. Group ke commands mein `getChatMember` se admin hai ya nahi check karo.
- Secrets (token, API key, Mongo URL, password) kabhi log, commit ya report mein mat daalo. Sirf env vars mein.
- Auth "fail-closed" ho: key set nahi hai to access band, khula nahi.
- Payment aayega to: verification, idempotency (duplicate transaction na ho), atomic DB update, refunds. Verification kabhi kamzor mat karo.

**Token bachana:**
- Badi file poori mat padho. `grep`, line range aur targeted reads use karo.
- Jo file padh li aur badli nahi, use dobara mat padho.
- Chat mein bade code blocks mat chhapo, seedha patch karo.
- Bina maange broad audit mat karo.
- Kaam phases mein karo, jo phase ho gaya use dobara mat karo.

**Testing aur imaandari:**
- Edit se pehle root cause dhoondho. Function badalne se pehle uske callers, imports aur shared state check karo.
- Har phase ke baad: `py_compile` + jo tests relevant hon. Success, failure, duplicate request aur edge cases test karo.
- Jo test chalaya hi nahi, use pass mat bolo. Test nahi chal paya to saaf batao kyun.

**Git:**
- Branch: session mein jo branch di gayi ho, wahi use karo. Default branch pe seedha push mat karo.
- Commit messages saaf hon (`fix:`, `feat:`, `security:`, `docs:`). Ek commit mein ek kaam.
- PR tabhi banao jab owner bole.

**Jawab ka format (har kaam ke end mein):**
1. Kya badla: file + function + ek line
2. Kaunse tests asli mein chalaye, aur unka result
3. Kya fail hua
4. Kya baaki hai

Pakke bugs aur sirf "possible risks" alag-alag likho.

## 5. Faisle jo ho chuke hain (dobara mat poochna)

- **Bot mein AI nahi (abhi):** koi LLM API ya API key nahi.
- **Questions kahin se bhi aa sakte hain:** koi bhi AI chat (Gemini, ChatGPT, Claude...), khud likhe hue, PYQ, books/notes, ya free question banks. Bot ko source se matlab nahi, sirf format se. Isliye importer source-agnostic rahe: tolerant parsing, saaf error report, duplicate detection. Kisi ek AI ya tool pe depend karne wala code mat likho. Google AI Pro (Jio offer) se Gemini API quota nahi milta. Baad mein AI jodna ho to free tier (AI Studio key, backup Groq) + har explanation DB mein cache. DB fields abhi se tayyar rakho.
- **Auto quiz:** bina timer, 1 ghanta khula, phir purana delete aur turant naya. Groups mein ye pehle se chal raha hai.
- **Channels:** Telegram channel mein quiz poll anonymous hi ho sakta hai, isliye wahan scoring/leaderboard nahi hogi. Channel quiz ke saath "🏆 Score ke saath khelo" button jo bot DM pe le jaye. Bot ko channel mein Post + Delete permission chahiye.
- **Do bots, ek codebase:** Miss Quiz (official content) + Quiz Builder (koi bhi admin apna quiz bana ke group/channel mein chalaye). Same engine, DB aur leaderboard, alag bot tokens.
- **Scope:** pehle Indian exams (CLAT, UPSC, SSC, Banking, CUET...), phir global. Hindi + English.

## 6. Roadmap (isi order mein)

**Phase 0: Base aur security** ← abhi yahan
- [ ] `_check_api_auth()` fail-closed karo (sirf `DYNO` pe nahi, har jagah). `hmac.compare_digest`. Query-string `api_key` hatao.
- [ ] `static/js/admin.js` request ke saath `X-Admin-Key` bheje (key user se lo, sessionStorage mein rakho)
- [ ] Webhook mode mein `WEBHOOK_SECRET_TOKEN` zaroori ho, nahi to startup pe fail ya auto-generate karke `set_webhook` mein do
- [ ] `/webhook` error mein `str(e)` client ko mat bhejo
- [ ] `render.yaml`: `DATABASE_URL` (Postgres) hatao, `MONGODB_URL`, `ADMIN_API_KEY`, `WEBHOOK_SECRET_TOKEN` (`sync: false`) jodo
- [ ] Tests chalne layak banao (deps install, pytest-cov)
- [ ] Startup pe saare questions memory mein load na hon. DB se filter karke random question lo (`$sample`), indexes jodo. Isi se timeout aate hain.
- [ ] README: auto quiz interval 30 se 60 min

**Phase 1: Multi-subject + channels**
- [ ] Question schema: `exam`, `subject`, `topic`, `difficulty`, `explanation`, `lang`, `source`, `verified` (purane fields chalte rahein)
- [ ] `quiz_parser.py`: Explanation/Category/Difficulty/Exam lines padhe (format neeche)
- [ ] Har group/channel ki settings: subjects, interval, quiet hours, language, auto-delete on/off
- [ ] Channel support: register on admin add, `is_anonymous=True`, DM deep-link button
- [ ] Har chat mein X din tak repeat na ho (DB mein)
- [ ] Send fail ho to retry. Inactive groups pause. Rate limit ke liye staggering + 429 handling.

**Phase 2: Quiz Builder MVP**
- [ ] DM mein quiz banana (step by step), forward kiye poll se import, .txt/.csv import
- [ ] `/startquiz` group/channel mein, admin check ke saath. Schedule, pause, stop.
- [ ] Results: har question ke stats, leaderboard, CSV export

**Phase 3: Engagement**
- [ ] Daily challenge, live tournament, speed round, survival, DM mock test
- [ ] Streaks, XP/levels, badges, seasons, weekly/monthly/group/subject leaderboards
- [ ] Spaced repetition (galat answer 1/3/7 din baad), weak-topic report
- [ ] 1v1 battle, referral

**Phase 4: Kamai** (payment rules section 4 ke hisaab se)
- [ ] Telegram Stars / UPI, premium user, creator plan, certificates

**Phase 5: AI (optional, free tier) aur global**
- [ ] AI explanation (cached), multi-language, Mini App

## 7. Import format (`.txt`)

Questions kisi bhi source se aa sakte hain, bas file is format mein ho:

```
Q1. Question text
A) option  B) option  C) option  D) option
Answer: B
Explanation: max 180 characters
Category: Polity | Difficulty: Medium | Exam: CLAT
```

Phase 1 se pehle parser sirf purane fields padhta hai. Explanation/Category/Difficulty/Exam ka support Phase 1 mein aayega.

## 8. Telegram limits jo yaad rakhne hain

- Poll: question ≤ 300 chars, option ≤ 100 chars, explanation ≤ 200 chars (`scheduler.py` pehle se clamp karta hai)
- `open_period` aur `close_date` saath mein nahi chalte
- Channel mein non-anonymous poll nahi bhej sakte. Anonymous poll ke `poll_answer` updates nahi aate.
- Rate limit: ~30 msg/sec poore bot ke liye, ~20 msg/min ek group mein. 429 aaye to `retry_after` tak ruko.
- Bot apne bheje messages groups mein delete kar sakta hai. Channel mein delete permission chahiye.
- Naye poll features (Bot API 9.6: `correct_option_ids`, `allows_revoting`) use karne se pehle core.telegram.org pe verify karo.

## 9. Pata hai jo problems hain

- (Phase 0) Web API auth fail-open hai Render pe; webhook secret optional; `render.yaml` DB galat.
- (Phase 0) Questions startup pe memory mein load hote hain, isliye slow startup aur timeouts. Pichhle commits sirf timeout badha rahe the, asli fix nahi.
- Delete pehle hota hai aur send baad mein. Send fail ho to group 1 ghanta khaali rehta hai.
- Tests bahut kam hain.
