# Group Quiz (Manual Quiz Sessions) — Plan

> Status: **bana (Nov 2026)** — `src/bot/group_quiz.py` + `src/bot/my_quiz.py`. Defaults chune: koi bhi quiz bana sake, sirf group admin chalaye, timer = quiz ka, negative 0, /score mein nahi, koi daily limit nahi. Baaki: negative marking, My result DM, speed.
> Auto quiz (har ghante 1 poll) jaisa hai waisa rahega. Ye ek **naya, alag feature** hai.

---

## 1. Kya chahiye (ek line mein)

Admin ek **quiz set** banaye (jaise 40–50 questions), phir **kisi bhi group** mein ek command/button se
chalaye: questions ek ke baad ek timer ke saath aayein, beech-beech mein leaderboard, end mein final result.
Jaise baaki bade quiz bots karte hain — lekin hamare bot jaisa saaf, premium, aur restart-safe.

---

## 2. Owner / Admin ka flow

### 2.1 Quiz set banana (bot ke DM mein)
`/start → Admin Panel → 📚 My Quizzes → ➕ New Quiz`

1. **Naam** bhejo (jaise "CLAT Polity Mock 1").
2. **Questions daalo** — teen tareeke, mix bhi chalega:
   - `.txt` file → hamara universal parser (koi bhi format, report ke saath)
   - Telegram quiz polls **forward** karo (kisi bhi channel/bot se)
   - **Bank se uthao**: category + kitne (jaise "Polity se 40 random")
3. **Settings** (buttons se, default already sahi):
   | Setting | Default | Options |
   |---|---|---|
   | Time per question | 30s | 10 / 15 / 20 / 30 / 45 / 60 |
   | Questions shuffle | ON | ON / OFF |
   | Options shuffle | OFF | ON / OFF |
   | Negative marking | 0 | 0 / 0.25 / 0.33 / 0.5 |
   | Leaderboard har | 10 questions | 5 / 10 / 20 / sirf end mein |
4. **Save** → quiz ko ID milti hai (`QZ12`) + card: `▶️ Start in a group` · `✏️ Edit` · `🗑 Delete` · `📤 Share`

### 2.2 Group mein start karna — 3 raste
| Kahan se | Kaise | Check |
|---|---|---|
| Group mein | `/startquiz QZ12` (ya sirf `/startquiz` → list se chuno) | Sirf **group admin** (anonymous admin bhi) |
| Bot DM se | Quiz card → `▶️ Start in a group` → un groups ki list jahan aap admin ho + bot admin hai → tap | Dono admin |
| Link se | `📤 Share` link → group chuno → bot add/start | Admin |

Start ke baad group mein:
```
📚 CLAT Polity Mock 1
40 questions · 30s each · −0.25 negative
Starting in 3… 2… 1…
[⏸ Pause] [⏭ Skip] [⏹ Stop]   ← sirf admin ke liye kaam karte hain
```

### 2.3 Quiz chalte waqt
- Har question = Telegram **quiz poll** (`open_period` = timer). Upar chhota sa `Q 12/40`.
- Timer khatam → 2s gap → agla question. (Groups mein "sab ne answer diya to aage" nahi — wahan kabhi sab answer nahi karte.)
- **Lamba question** (>300 chars) → pehle text message, phir poll mein "Q12 — upar padho".
- Har 10 questions ke baad **mini leaderboard** (top 5).
- **Controls** (admin only, buttons + commands): `⏸ Pause` `▶️ Resume` `⏭ Skip` `⏹ Stop` `🐢 Slow` `⚡ Fast`
- Koi member stop dabaye → popup "Only group admins".

### 2.4 End
```
🏆 FINAL RESULT — CLAT Polity Mock 1
🥇 Amit   34/40 · 85% · avg 11.2s
🥈 Bina   31/40 · 77% · avg 14.0s
🥉 ...
Total players: 63
[🔁 Play again] [📊 My result] [📚 More quizzes]
```
`📊 My result` → user ke DM mein uska poora result (kaunse galat, sahi answer, explanation).

---

## 3. Rules jo pakke honge (yahin se bade bots fail hote hain)

| # | Rule | Kyun |
|---|---|---|
| 1 | **Ek group (ya ek topic) mein ek hi session** | Do quiz mix na hon |
| 2 | Session chalte waqt us group ka **auto quiz ruk jaata hai**, khatam hote hi wapas | Beech mein hourly poll na ghuse |
| 3 | **Restart-safe**: session MongoDB mein (`quiz_sessions`) — bot restart ho to agle question se **wahin se chalu** | Reference bot ka session restart pe gum ho jaata hai |
| 4 | Har user ka **sirf pehla answer** count, forwarded poll se cheating nahi | Fair scoring |
| 5 | Scoring = +1 sahi, −negative galat; **tie → kam total time** | Saaf winner |
| 6 | Per group ~20 msg/min limit: 1 poll + kabhi kabhi leaderboard → kabhi limit nahi chhuta. 429 aaye to `retry_after` ruko, question skip nahi | Telegram limit |
| 7 | Bot ka admin/poll right beech mein chala jaye → session **pause + admin ko DM** | Chupchaap fail nahi |
| 8 | Topic group: quiz **usi topic** mein jahan start hua; topic band ho → pause + message | Forum groups |
| 9 | Sirf group admin start/stop; quiz banane wala owner/devs (Phase 1) | Spam/abuse nahi |
| 10 | Session ke answers regular `/score` mein **gin-ne hain ya nahi** → owner decide kare (default: haan) | Neeche question |
| 11 | 30 min koi activity nahi (pause pe bhool gaye) → auto stop + result | Ghost sessions nahi |

---

## 4. Data (MongoDB)

```
quizzes        { quiz_id: "QZ12", title, owner_id, question_ids: [..] | questions: [..],
                 settings: { time, shuffle_q, shuffle_opts, negative, lb_every },
                 created_at, plays }
quiz_sessions  { chat_id, thread_id, quiz_id, status: running|paused|done,
                 order: [question ids], idx, started_by, started_at, speed,
                 current_poll_id, scores: { uid: {name, correct, wrong, time_ms} } }
quiz_results   { quiz_id, chat_id, user_id, correct, wrong, score, time_ms, answers: [...], at }
```
Indexes: `quizzes.quiz_id` unique · `quiz_sessions.chat_id+thread_id` unique (ek group = ek session) ·
`quiz_results.user_id`, `quiz_results.quiz_id`.

---

## 5. Code kahan banega (existing pattern follow)

| File | Kaam |
|---|---|
| `src/bot/quiz_sets.py` (naya) | Quiz banana/edit/list (DM) — Admin Panel se khulega |
| `src/bot/group_session.py` (naya) | Session runner: start, loop, pause/resume/skip/stop, leaderboard, resume-after-restart |
| `src/core/database.py` | `quizzes`, `quiz_sessions`, `quiz_results` ke helper |
| `src/bot/poll_manager.py` | Session poll answers pehle session ko (battle jaisa hook) |
| `src/utils/scheduler.py` | Session chal raha ho to us group ko auto quiz skip |
| `src/bot/admin_panel.py` | `📚 My Quizzes` button |
| Reuse | `quiz_parser.universal_parse`, `premium.py` (emoji/bold/red Back), `_send_in_topic`, battle ka scoring logic |

---

## 6. Phases

| Phase | Kya milega | Done = |
|---|---|---|
| **A** | Quiz set: naam + `.txt`/forward/bank se questions + settings + list/delete (DM) | 50-question set ban jaye, ID mile |
| **B** | Group session: `/startquiz QZ12`, countdown, polls, timer, final leaderboard, `/stopquiz` | Asli group mein 40 Q end tak chale |
| **C** | Controls (pause/resume/skip/speed), mid leaderboard, auto-quiz pause, "Start in group" button from DM | Admin bina command ke chala sake |
| **D** | Restart-safe resume, `📊 My result` DM, Play again, share link | Bot restart ke baad bhi quiz poora ho |
| **E** | (Quiz Builder bot) koi bhi user apna quiz banaye — alag token, same code | Owner decide kare |

Har phase: tests (fake Telegram ke saath poora 40-Q session), `py_compile`, commit, aur VPS pe `update.sh`.

---

## 7. Reference bot (`Quizbot-main.zip`) se kya seekha

- **Liya (idea level)**: admin check `getChatMember` + anonymous admin, session-per-chat, timer ke baad agla question, beech mein leaderboard, pause/skip/speed commands, lamba question pehle text mein.
- **Behtar karenge**: session MongoDB mein (unka restart pe gum), ek hi library/process (unke 2 bots Pyrogram + PTB), auto quiz ke saath clash nahi, topic groups, premium UI.
- Unka code copy **nahi** — apne existing structure mein likhenge.

---

## 8. Owner se sawaal (jawab ke baad Phase A shuru)

1. **Quiz kaun bana sakta hai?** (a) sirf owner + devs  (b) har group admin apne group ke liye
2. **Default time per question?** 30s theek?
3. **Negative marking default?** 0 ya −0.25?
4. Group quiz ke answers user ke **normal `/score` mein judein**? (haan / nahi, sirf quiz result mein)
5. Ek group mein ek din mein kitne quiz session max? (spam control — default: koi limit nahi)
