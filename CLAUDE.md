# CLAUDE.md — Miss Quiz Bot

Ye file batati hai ki is repo mein kaam kaise karna hai. Har session ki shuruaat mein isse follow karo.
Owner Hinglish mein baat karta hai, jawab bhi Hinglish mein, short aur seedha do.

---

## 1. Project kya hai

**Miss Quiz**: Telegram quiz bot sabke liye: koi bhi subject, koi bhi exam, ya bas apna quiz. Kisi ek exam (jaise CLAT) ka naam UI/landing page pe mat chipkao; question bank abhi zyada GK/CLAT wala hai, bas itna.

- Python 3.11 (`runtime.txt`), python-telegram-bot v22, MongoDB (pymongo), Flask admin panel, APScheduler
- Deploy: **VPS (main)** — `deploy/setup_vps.sh` + `deploy/miss-quiz.service` (systemd, user `missquiz`, `/opt/miss-quiz`, polling mode, local MongoDB 127.0.0.1, panel sirf SSH tunnel se). Docker: `docker compose up -d` (bot + MongoDB). Render/Procfile/wsgi hata diye (VPS main)
- Modes: `MODE=polling` (local) / `MODE=webhook` (production) — `main.py`

## 2. Code ka naksha

```
main.py                     Entry point. Polling/webhook mode, startup, restart flag
src/core/config.py          Env config. OWNER_ID, WIFU_ID, AUTHORIZED_USERS
src/core/database.py        DatabaseManager (MongoDB). Collections neeche
src/core/quiz.py            QuizManager. Questions load + random question
src/bot/handlers_main.py    TelegramQuizBot class (mixins jodta hai) + saare add_handler
src/bot/commands/           user_cmds.py, quiz_cmds.py, admin_cmds.py (mixins)
src/bot/dev_commands.py     Developer commands + check_access()
src/bot/dev_broadcast.py    Broadcast / delbroadcast
src/bot/dev_quiz_editor.py  Inline-button quiz editor (/editquiz, edit_quiz_* callbacks)
src/bot/quiz_parser.py      universal_parse (text) + items_to_questions (rows) + bulk_import (dup_list, problems)
src/bot/file_extract.py     Koi bhi file → questions: txt md csv tsv xlsx xls json docx odt ods odp pptx pdf (pypdf + pdfplumber, jo zyada Q nikale) html rtf epub zip; purana .doc/.ppt best-effort. Images + scan PDF = OCR sirf jab server pe `tesseract-ocr` ho, warna saaf note. Kabhi raise nahi karta
src/bot/poll_manager.py     Poll answers, poll_id mapping (battle polls pehle battle_on_poll_answer ko jaate hain)
src/bot/admin_panel.py      /admin panel: har admin command ka button (stats, reload, ➕ Add Questions = ek hi button: text/file/forwarded quiz poll, Done tak leta rahe; edit/delete quiz, broadcast + confirm, undo broadcast, start photo, Admins, restart). **Admins**: sirf owner/WIFU add/remove kar sakte (`_can_manage_admins`; `/dev add|remove` bhi owner-only). Add = ID ya forward → custom title (≤32) → ek emoji (premium `custom_emoji_id` save) → `developers` doc mein `title/emoji/emoji_id`; naye admin ko bot DM bhejta hai (fail ho to owner ko batata hai); remove pe bhi DM. Find Question (ID ya shabd → Edit/Delete buttons). Delete = confirm screen (pura question + answer) phir delete, `/delquiz 12` seedha confirm. Panel buttons usi message ko edit karte hain (`_MsgProxy(edit=True)`, Back auto-add). Input wale buttons `ap_await` set karte hain, agla message handle_admin_input (group -1) leta hai, /cancel. Entry button /start pe red, sirf admin DM. Start photo set (photo ya link → file_id, `settings` collection). Har callback pe auth
src/bot/premium.py          PremiumExtBot: HAR outgoing message/caption/poll/button ka emoji apne aap premium (PACK_NEW + PACK exact, SUBST look-alike; 3 packs; Admin Panel → Emoji Test se check). 'Back' button hamesha red. Har HTML message apne aap poora bold + extra blank lines saaf (`style_request`, `BOLD_TEXT=0` se band; <code>/<pre> wale messages bold nahi hote). Premium reject ho to plain resend. Naye emoji ke liye bas PACK/SUBST mein jodo
src/bot/battle.py           1v1 battle (BattleMixin + Battle). Group + DM. Khule challenge `battles` mein, chalti battle memory mein
src/bot/group_quiz.py       Group Quiz (GroupQuizMixin): /startquiz (group admin) → apna quiz ya Bot 10/20/30 Q; quiz card ka 👥 Start in Group (`startgroup=gq_<id>`). Timed polls, har 10 pe leaderboard, Pause/Resume/Skip/Stop (admin ya starter), final result + Play again. Ek group = ek session (`ACTIVE`), us waqt auto quiz skip. `quiz_sessions` mein save → restart ke baad agle Q se resume. /stopquiz. /score mein nahi judta
src/bot/my_quiz.py          Create Own Quiz / /myquiz (MyQuizMixin): DM mein text/file/forwarded quiz poll → draft (user_data mq_draft, group -4) → Next → 4 steps (naam / timer / negative / shuffle) → `quiz_sets`. Koi quiz limit nahi (page numbers), file ≤20 MB. 🔄 Re-read file = save hui source file naye parser se dobara padho (same ID). Bot pool mein nahi jaate. 📄 Question Paper (`_build_printable_html`) = ek HTML file jo khud practice test hai: bubble tap → Check my score (negative ke saath) → Wrong only filter; print = saaf A4 + answer key
src/bot/tracking.py         User/group tracking
src/bot/ui.py               UI strings, LINE/THIN, mention helpers
src/utils/scheduler.py      AutoQuizScheduler. Har group ko auto quiz bhejta hai
src/utils/certificate.py    Group Quiz Top-3 certificate PNG (1600x1132, Pillow; dark award style: charcoal + laal curved panel, gold title, gold script naam, rank ke rang ki foil seal). Fonts assets/fonts mein (OFL; na mile to DejaVu). Fancy Unicode naam (𝙺𝚞𝚕𝚊 → Kula) plain ho jaate, Hindi = Noto Devanagari, emoji-only → "Quiz Champion"
src/web/app.py              Flask: / (landing page, templates/landing.html, `BOT_USERNAME`), /health, /admin, /webhook, /api/*
src/web/miniapp.py          Telegram Mini App (/app + /app/api/quiz|check|submit): quiz_sets ko Practice/Exam mein khelo. Har call pe initData HMAC verify (24h, fail-closed); answer pehle kabhi nahi jaata; submit pe plays +1 (20s mein ek baar). Quiz card pe 📱 Open in App sirf DM + `MINI_APP_URL` (https) set ho to. Page: templates/miniapp.html (Telegram theme rang, sab text escape)
templates/admin.html, static/js/admin.js   Web admin panel
tests/                      ~320 tests (pytest, mongomock): web security, scheduler, battle, admin panel, parser, files, ...
```

**Battle:** `/battle` (group: open ya reply-target; DM: invite link `start=bt_<id>`). Main card: 📝 Create Own Quiz (green; group mein DM link) · 📚 My Quizzes | 🤖 Bot Quiz · ⚙️ Customize · Cancel. Bot Quiz = Classic 10Q·20s / Quick 5Q·15s / Marathon 20Q·30s / Last (users.battle_pref), 1 tap. Customize = timer 10s 15s 20s / 25s 30s 1m + 🎯 Questions (bina rang ke option buttons, tap ya type) + Back/Cancel + Create Challenge. My Quizzes = apne quiz (`quiz_sets`) se 1 tap challenge; group mein sirf group admin (getChatMember). Challenge card pe ✏️ Change (accept se pehle). +1 sahi, tie → kam time. Battle answers regular stats mein nahi jaate. `users` pe `$inc`: `battle_wins`, `battle_losses`, `battle_draws`, `battles_played`. DM mein har poll ka owner hai (forwarded poll se cheating band). Khule challenge (setup/pending) `battles` collection mein save hote hain (TTL 24h, `_bt_save`/`_bt_get`), to update/restart ke baad bhi buttons aur invite link chalte hain; sirf chalti (running) battle restart pe khatam. Purana button dabao to "This battle has ended" + ⚔️ New Battle. DM invite: Send to a friend = inline mode (`handle_battle_inline`, query `bt_<id>`, sirf challenger; bold + premium card + Accept url `start=bt_<id>_go` jo seedha battle shuru karta hai; premium reject ho to plain). Inline mode band ho (@BotFather /setinline) to share-URL fallback (plain text). + Copy link; friend ko Accept/Decline card, accept pe challenger ka card edit, decline pe challenger ko msg.

**UI rule (premium.style_markup, har keyboard pe):** ek screen = ek green (pehla full-width main action), blue nahi, Back/Cancel/Delete red, Back ho to Home hata. Peeche jaane wala left, aage wala right.

**Branding:** naam "Miss Quiz Bot", community `t.me/VillanCommunity` (`ui.COMMUNITY_URL`). `/start` = photo (agar set) + bold blockquote caption + rangeen buttons (`_start_kb`). `/help` = 3 bold blockquotes (Play / Progress / Groups) + buttons, koi admin section nahi. Command menu mein sirf user commands; admin sab kuch /start ke red Admin Panel se (purane per-admin menus startup pe delete).

**MongoDB collections:** `settings` (key/value), `questions`, `users`, `groups`, `broadcasts`, `activities`, `developers`, `poll_map`, `auto_quiz_state`, `battles` (open challenges, TTL), `quiz_sets` (users ke apne quiz), `quiz_sessions` (chalte group quiz), `_counters` (auto-increment quiz id).

**Auth layers:**
- Bot: `_is_owner()` / `_is_authorized()` (`handlers_main.py`), `check_access()` (`dev_commands.py`). Owner + WIFU + `developers` collection.
- Web API: `_check_api_auth()` (`src/web/app.py`). Fail-closed: `ADMIN_API_KEY` set nahi to API band. Sirf header `X-Admin-Key`, constant-time compare. Panel (`admin.js`) key sessionStorage mein rakhta hai.
- Webhook: hamesha verify hota hai. Secret `config.webhook_secret(token)` se aata hai: `WEBHOOK_SECRET_TOKEN` ya bot token se derived (HMAC). Wahi `set_webhook` ko jaata hai.

**Auto quiz sirf wahan jahan bot admin hai:** `groups.bot_is_admin` (my_chat_member se update). `get_groups_due_for_quiz` known non-admin groups chhod deta hai; status unknown ho to scheduler ek baar `getChatMember` se check karke save karta hai. Admin banne/hatne pe group mein ON/paused message. Non-admin groups ko "➕ Make me admin" button wala alert (`ui.ADMIN_ALERT_TEXT`, `make_admin_keyboard`): scheduler job har 60 min check karta hai, roz 1 alert (24h gap), koi cap nahi (`admin_reminder_count`, `admin_reminder_at`), admin bante hi reset. `/quiz` aur `/battle` admin ke bina bhi chalte hain.

**Auto quiz flow (`scheduler.py`):** poll bina timer (koi `open_period` nahi) bheja jaata hai, interval 60 min. Pehle naya bheja jaata hai, phir purana delete hota hai (send fail ho to purana bacha rehta hai). 429 pe `retry_after` ke baad retry, network error pe 2 min baad retry. `message_id` `auto_quiz_state` mein save hota hai, to restart ke baad bhi delete chalta hai. Topic (forum) groups: quiz us topic mein jaata hai jahan log aakhri baar likhe (`groups.message_thread_id`); wo topic band/delete ho to General mein bhejta hai aur topic bhool jaata hai (`_send_in_topic`, `clear_group_thread`) — group inactive nahi hota. Abhi sirf groups mein chalta hai, channels mein nahi (`is_anonymous=False` hard-coded hai).

## 3. Commands (dev)

```bash
python -m py_compile $(git ls-files '*.py')           # har change ke baad zaroor
pip install -r requirements.txt                       # agar blinker error aaye: pip install --ignore-installed blinker -r requirements.txt
python -m pytest -q                                   # pytest.ini mein --cov hai (pytest-cov chahiye)
python -m pytest -q -o addopts=""                     # bina coverage ke (fast)
MODE=polling python main.py                           # local run (TELEGRAM_TOKEN, OWNER_ID, MONGODB_URL chahiye)
```

Env vars: `.env.example` dekho. `PREMIUM_EMOJI=1` sirf tab jab bot owner ke paas Telegram Premium ho (warna normal emoji). Extra: `ADMIN_API_KEY`, `WEBHOOK_SECRET_TOKEN`, `MINI_APP_URL` (Mini App ka public https base), `WEBHOOK_URL`/`RENDER_URL`, `WIFU_ID`.

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

**Phase 0: Base aur security** ✅ (branch `ccr-f1e736ea-a5z34q`)
- [x] `_check_api_auth()` fail-closed, `hmac.compare_digest`, query-string key hata di
- [x] `admin.js` `X-Admin-Key` bhejta hai
- [x] Webhook secret hamesha verify (derived ya env), error text leak band
- [x] `render.yaml` MongoDB + `ADMIN_API_KEY`; Dockerfile se Postgres hata; `.dockerignore`
- [x] Tests chalte hain (stale tests theek), naye tests: web security, scheduler, DB batch
- [x] Startup pe questions 2 baar fetch hote the, ab 1 baar. In-memory cache abhi rakha (scale pe dobara dekhna: `$sample` + filters)
- [x] Partial bulk insert pe galat ID mapping fix
- [x] Polling mode interval 30 se 60 min, README update
- [x] Auto quiz: send-then-delete, 429/network retry
- [ ] Dashboard `/api/metrics` route exist nahi karta (`admin.js` call karta hai) → bana ya call hatao
- [ ] Admin panel broadcast button: API sirf preview deti hai (`status: not_sent`), JS `result.queued` dhoondhta hai → hamesha "Failed" dikhata hai

**Phase 1: Multi-subject + channels**
- [ ] Question schema: `exam`, `subject`, `topic`, `difficulty`, `explanation`, `lang`, `source`, `verified` (purane fields chalte rahein)
- [ ] `quiz_parser.py`: Explanation/Category/Difficulty/Exam lines padhe (format neeche)
- [ ] Har group/channel ki settings: subjects, interval, quiet hours, language, auto-delete on/off
- [ ] Channel support: register on admin add, `is_anonymous=True`, DM deep-link button
- [ ] Har chat mein X din tak repeat na ho (DB mein)
- [ ] Send fail ho to retry. Inactive groups pause. Rate limit ke liye staggering + 429 handling.

**Group Quiz (manual sessions)** → `docs/GROUP_QUIZ_PLAN.md`. Bana: A (quiz sets = My Quizzes), B, C, D (resume). Baaki: negative marking, 📊 My result DM, speed control, per-user quiz_results

**Phase 2: Quiz Builder MVP**
- [ ] DM mein quiz banana (step by step), forward kiye poll se import, .txt/.csv import
- [ ] `/startquiz` group/channel mein, admin check ke saath. Schedule, pause, stop.
- [ ] Results: har question ke stats, leaderboard, CSV export

**Phase 3: Engagement**
- [ ] Daily challenge, live tournament, speed round, survival, DM mock test
- [ ] Streaks, XP/levels, badges, seasons, weekly/monthly/group/subject leaderboards
- [ ] Spaced repetition (galat answer 1/3/7 din baad), weak-topic report
- [x] 1v1 battle (group + DM)
- [ ] Battle: ELO/rating, battle leaderboard, `/stats` mein W/L, restart-safe state
- [ ] Referral

**Phase 4: Kamai** (payment rules section 4 ke hisaab se)
- [ ] Telegram Stars / UPI, premium user, creator plan, certificates

**Phase 5: AI (optional, free tier) aur global**
- [ ] AI explanation (cached), multi-language, Mini App

## 7. Import format (`.txt`)

`quiz_parser.universal_parse()` (SmartQuizParser.parse pehle isse chalata hai, purana engine sirf fallback). Samajhta hai:
- Numbering: `1.` `1)` `Q1.` `Q1)` `Question 1:` `प्रश्न 1` ya bina number; multi-line question
- Options: `A)` `(a)` `a.` `A:` `Option A:` `1.` `(i)` `(क)`, ek line mein saare (`A) x B) y`), `* ` / `- ` bullets, markdown `**`
- Answer: `Answer/Ans/Correct Answer/उत्तर: B`, `(b)`, `Option B`, `2`, option ka text (`Delhi`), `B. Mumbai` (letter-text alag ho to text jeetega), option ke aage ✅ ✔ * (correct)
- File ke end mein answer key: `Answer Key:` ke neeche `1. A` / `1-a` / `1A 2B`
- `Explanation:` (multi-line), `Category: X | Difficulty: Y`
- Coaching PDFs: alag line wala watermark (`site.com`) aur page number skip; `Passage (Q.6-Q.10): …` text kisi option mein nahi judta; numbered statements + (a)–(d) = ek question; `(c) x (d) y` row split; chipki heading (`The ABDMWhen…`) hatti; answer key mein `Explanation:` ke baad bhi `N. (b)` answer padhe jaate
- 2–10 options. Jo samajh na aaye wo skip + report mein "Q12 — answer “E” doesn't match any option" jaisa kaaran
- Explanation DB mein save hoti hai aur quiz ke 💡 mein dikhti hai; har quiz explanation ke end mein `🆔 Q#id` hamesha

**Question dhoondhna:** quiz poll bot ke DM mein forward karo (poll id se exact match) → Edit/Delete; ya Find Question (ID, `Q#12`, shabd question/options mein, typo chalega).

## 8. Telegram limits jo yaad rakhne hain

- Poll: question ≤ 300 chars, option ≤ 100 chars, explanation ≤ 200 chars (`scheduler.py` pehle se clamp karta hai)
- `open_period` aur `close_date` saath mein nahi chalte
- Channel mein non-anonymous poll nahi bhej sakte. Anonymous poll ke `poll_answer` updates nahi aate.
- Rate limit: ~30 msg/sec poore bot ke liye, ~20 msg/min ek group mein. 429 aaye to `retry_after` tak ruko.
- Bot apne bheje messages groups mein delete kar sakta hai. Channel mein delete permission chahiye.
- Naye poll features (Bot API 9.6: `correct_option_ids`, `allows_revoting`) use karne se pehle core.telegram.org pe verify karo.

## 9. Pata hai jo problems hain

- Broadcast ab HTML mode mein hai (`text_html`/`caption_html`): Telegram ki native formatting chalti hai, haath se likha Markdown (`*bold*`) nahi.
- `/api/metrics` missing aur panel broadcast button ka mismatch (Phase 0 ke bache items).
- `datetime.utcnow()` deprecated hai (DB mein naive ISO strings save hote hain). Badalne se stored format badlega, isliye soch ke migrate karna.
- `send_poll` timeout pe Telegram ne poll shayad bhej diya ho; 2 min retry se ek extra poll reh sakta hai jo track nahi hota. Rare, accept kiya.
- Rate limit: `AIORateLimiter` (poore bot pe, 429 pe khud retry). Poll answers ki DB writes worker thread mein, `concurrent_updates(16)`; scheduler ki DB calls bhi thread mein. Load test: ~30 → ~240 answers/sec.
- Housekeeping job roz: `activities` 30 din, `poll_map` + memory polls 7 din (`purge_old_data`). Blocked user ka data delete nahi hota, sirf `pm_accessible=False`.
- Tests abhi bhi kam hain (handlers, parser, poll answers untested).
