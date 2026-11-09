"""
battle.py — 1v1 Battle mode (groups and DMs).

Group:  /battle            → open challenge, first person to tap Accept plays
        /battle (reply)    → only the replied-to user can accept
DM:     /battle            → invite link; friend opens it, both get the same
                             questions in their own DMs at the same time

Rules: BATTLE_QUESTIONS questions, BATTLE_SECONDS per question, +1 per correct
answer. Tie on score → lower total time on correct answers wins.
Battle answers do NOT touch the regular score/stats (no farming).

State is in memory: a running battle is lost on restart (battles last ~2 min).
"""

import asyncio
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from telegram import (
    CopyTextButton, InlineQueryResultArticle, InputTextMessageContent, LinkPreviewOptions,
    InlineKeyboardButton, InlineKeyboardMarkup, Poll, SwitchInlineQueryChosenChat, Update,
)
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.bot.ui import UI, get_thread_id

logger = logging.getLogger(__name__)

BATTLE_QUESTIONS = 10     # defaults; the challenger can change them in the setup card
BATTLE_SECONDS   = 20     # Telegram open_period per question
Q_CHOICES        = (5, 10, 15, 20, 25, 30)   # presets; any number can be typed
Q_MIN, Q_MAX     = 3, 50
T_MIN, T_MAX     = 5, 600                    # Telegram open_period limits
# Battle fields saved in MongoDB while a challenge is open (setup/pending)
_PERSIST = ("id", "mode", "p1", "p1_name", "p2", "p2_name", "chat_id", "thread_id",
            "state", "n_q", "secs", "category", "msg_id", "set_id", "set_title")
_ENDED_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⚔️ New Battle", callback_data="start_battle",
                                                        style="success")]])
# One-tap modes: key -> (label, questions, seconds). Classic is the big green one.
MODES = {"classic": ("🎯 Classic", 10, 20), "quick": ("⚡ Quick", 5, 15),
         "marathon": ("🧠 Marathon", 20, 30)}
T_CHOICES        = (10, 15, 20, 25, 30, 60)    # presets; any time 5s–10m can be typed
INVITE_TTL       = 86400  # open challenge stays valid (no visible limit; memory safety only)
START_DELAY      = 3      # seconds between "battle starts" and first question
ROUND_GAP        = 1      # pause after each question before the next one


@dataclass
class _Round:
    correct:   int
    sent_at:   float
    owner:     Optional[int] = None    # DM: the only player allowed to answer this poll
    answered:  set = field(default_factory=set)
    done:      asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Battle:
    """Pure battle state + scoring. No Telegram calls here (unit-testable)."""
    id:        str
    mode:      str                     # "group" | "dm"
    p1:        int
    p1_name:   str
    p2:        Optional[int] = None    # None = open challenge
    p2_name:   str = ""
    chat_id:   Optional[int] = None    # group battles only
    thread_id: Optional[int] = None
    state:     str = "pending"         # setup | pending | running | done
    n_q:       int = BATTLE_QUESTIONS   # chosen in the setup card
    secs:      int = BATTLE_SECONDS
    category:  str = ""                 # "" = all topics
    cat_options: List[str] = field(default_factory=list)
    last:      Optional[Tuple[int, int]] = None   # challenger's last custom (n_q, secs)
    set_id:    str = ""                 # battle on the challenger's own quiz (quiz_sets)
    set_title: str = ""
    created:   float = field(default_factory=time.monotonic)
    msg_id:    Optional[int] = None    # challenge message (group)
    scores:    Dict[int, int] = field(default_factory=dict)
    times:     Dict[int, float] = field(default_factory=dict)
    rounds:    Dict[str, Tuple[int, _Round]] = field(default_factory=dict)  # poll_id -> (q_idx, round)

    @property
    def players(self) -> List[int]:
        return [p for p in (self.p1, self.p2) if p]

    def expired(self, now: Optional[float] = None) -> bool:
        return self.state in ("setup", "pending") and (now or time.monotonic()) - self.created > INVITE_TTL

    def can_accept(self, uid: int) -> Tuple[bool, str]:
        if self.state != "pending":
            return False, "This battle is no longer open."
        if self.expired():
            return False, "This challenge has expired."
        if uid == self.p1:
            return False, "You can't battle yourself."
        if self.p2 and uid != self.p2:
            return False, "This challenge is for someone else."
        return True, ""

    def start(self, uid: int, name: str) -> None:
        self.p2, self.p2_name, self.state = uid, name, "running"
        self.scores = {self.p1: 0, uid: 0}
        self.times  = {self.p1: 0.0, uid: 0.0}

    def record_answer(self, poll_id: str, uid: int, option: Optional[int],
                      now: Optional[float] = None) -> bool:
        """Count a player's first answer on a battle poll. Returns True if counted."""
        entry = self.rounds.get(poll_id)
        if not entry or self.state != "running" or uid not in self.players:
            return False
        _, rnd = entry
        if rnd.owner is not None and uid != rnd.owner:
            return False   # e.g. opponent answering a forwarded copy of this poll
        if uid in rnd.answered:
            return False
        rnd.answered.add(uid)
        if option is not None and option == rnd.correct:
            self.scores[uid] += 1
            self.times[uid]  += max(0.0, (now or time.monotonic()) - rnd.sent_at)
        if self.mode == "group":
            # one shared poll per round: done when both players answered
            if set(self.players) <= rnd.answered:
                rnd.done.set()
        else:
            # one poll per player per round: done when both of this round's polls answered
            q_idx = entry[0]
            answered = set()
            for qi, r in self.rounds.values():
                if qi == q_idx:
                    answered |= r.answered
            if set(self.players) <= answered:
                for qi, r in self.rounds.values():
                    if qi == q_idx:
                        r.done.set()
        return True

    def result(self) -> Tuple[Optional[int], Optional[int]]:
        """(winner, loser); (None, None) on a full draw."""
        a, b = self.p1, self.p2
        if self.scores[a] != self.scores[b]:
            return (a, b) if self.scores[a] > self.scores[b] else (b, a)
        if self.scores[a] == 0 or abs(self.times[a] - self.times[b]) < 0.01:
            return None, None
        return (a, b) if self.times[a] < self.times[b] else (b, a)


def _tlabel(secs: int) -> str:
    """10 -> '10s', 60 -> '1m', 240 -> '4m'."""
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m" + (f" {secs % 60}s" if secs % 60 else "")


def _parse_secs(text: str) -> Optional[int]:
    """'45' / '45s' / '2m' / '1:30' / '1m 30s' -> seconds, else None."""
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+):(\d{1,2})", t)
    if m:
        return int(m[1]) * 60 + int(m[2])
    m = re.fullmatch(r"(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?", t)
    if m and (m[1] or m[2]):
        return int(m[1] or 0) * 60 + int(m[2] or 0)
    return int(t) if t.isdigit() else None


def dataclass_replace_mode(b: "Battle", mode: str) -> "Battle":
    import dataclasses
    return dataclasses.replace(b, mode=mode)


class BattleMixin:
    """Mixed into TelegramQuizBot. Uses self.db, self.quiz_manager, self._reply."""

    # ── state helpers ────────────────────────────────────────────────────

    def _bt_state(self):
        if not hasattr(self, "_battles"):
            self._battles: Dict[str, Battle] = {}
            self._battle_polls: Dict[str, str] = {}   # poll_id -> battle_id
        return self._battles

    def _bt_busy(self, uid: int) -> bool:
        """Only a battle being played blocks a user; open challenges don't."""
        return any(b.state == "running" and uid in b.players for b in self._bt_state().values())

    async def _bt_save(self, b: "Battle") -> None:
        """Open challenge → MongoDB (so buttons/links survive a restart);
        anything else → removed from MongoDB."""
        if not self.db or not hasattr(self.db, "save_battle"):
            return
        try:
            if b.state in ("setup", "pending"):
                doc = {k: getattr(b, k) for k in _PERSIST}
                doc["created_at"] = time.time() - (time.monotonic() - b.created)
                doc["expires_at"] = datetime.fromtimestamp(doc["created_at"] + INVITE_TTL,
                                                           timezone.utc).replace(tzinfo=None)
                await asyncio.to_thread(self.db.save_battle, doc)
            else:
                await asyncio.to_thread(self.db.delete_battle, b.id)
        except Exception as e:
            logger.debug(f"[BATTLE] save {b.id} failed: {e}")

    async def _bt_get(self, bid: str) -> Optional["Battle"]:
        """Battle from memory, or an open one from MongoDB (after a restart)."""
        b = self._bt_state().get(bid)
        if b or not bid or not self.db or not hasattr(self.db, "get_battle"):
            return b
        try:
            doc = await asyncio.to_thread(self.db.get_battle, bid)
            if not isinstance(doc, dict) or doc.get("state") not in ("setup", "pending"):
                return None
            b = Battle(**{k: doc[k] for k in _PERSIST if k in doc})
            b.created = time.monotonic() - max(0.0, time.time() - float(doc.get("created_at") or time.time()))
        except Exception as e:
            logger.debug(f"[BATTLE] load {bid} failed: {e}")
            return None
        if b.expired():
            return None
        self._bt_state()[bid] = b
        return b

    async def _bt_drop_open(self, uid: int, context) -> None:
        """Close this user's own open challenges (setup/pending) — a new
        /battle or accepting someone else replaces them."""
        if self.db and hasattr(self.db, "open_battles_of"):
            try:
                docs = await asyncio.to_thread(self.db.open_battles_of, uid)
                for d in docs if isinstance(docs, list) else []:
                    await self._bt_get(d.get("id", ""))
            except Exception as e:
                logger.debug(f"[BATTLE] open_battles_of failed: {e}")
        for b in list(self._bt_state().values()):
            if b.p1 != uid or b.state not in ("setup", "pending"):
                continue
            b.state = "done"
            await self._bt_save(b)
            if b.msg_id:
                try:
                    await context.bot.edit_message_text(
                        "⚔️ <i>This challenge was replaced by a new one.</i>",
                        chat_id=b.chat_id or b.p1, message_id=b.msg_id, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    def _bt_cleanup(self) -> None:
        battles = self._bt_state()
        for bid in [k for k, b in battles.items() if b.state == "done" or b.expired()]:
            b = battles.pop(bid)
            for pid in list(b.rounds):
                self._battle_polls.pop(pid, None)

    def _bt_pool(self, category: str = "") -> List[Dict]:
        qs = [q for q in (getattr(self.quiz_manager, "questions", []) or [])
              if len(q.get("options") or []) >= 2]
        if category:
            qs = [q for q in qs if (q.get("category") or "").lower() == category.lower()]
        return qs

    def _bt_pick_questions(self, b: "Battle") -> List[Dict]:
        import random
        pool = self._bt_pool(b.category)
        return random.sample(pool, min(b.n_q, len(pool)))

    def _bt_top_categories(self, limit: int = 6) -> List[str]:
        from collections import Counter
        c = Counter((q.get("category") or "General") for q in self._bt_pool())
        return [name for name, n in c.most_common(limit) if n >= Q_CHOICES[0]]

    @staticmethod
    def _bt_name(user) -> str:
        return UI.display_name(user)

    # ── /battle ──────────────────────────────────────────────────────────

    async def cmd_battle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Step 1: the challenger picks a mode (or Custom) on the setup card."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not user or not chat or not msg:
            return
        self._bt_cleanup()
        if self._bt_busy(user.id):
            await self._reply(update, "⚔️ You're already in a battle — finish it first.")
            return
        await self._bt_drop_open(user.id, context)
        if len(self._bt_pool()) < Q_CHOICES[0]:
            await self._reply(update, "⚠️ Not enough questions for a battle yet.")
            return

        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode="dm" if chat.type == "private" else "group",
                   p1=user.id, p1_name=self._bt_name(user), state="setup")
        b.n_q = min(BATTLE_QUESTIONS, max(q for q in Q_CHOICES if q <= len(self._bt_pool())))
        if self.db and hasattr(self.db, "get_battle_pref"):
            try:
                pref = await asyncio.to_thread(self.db.get_battle_pref, user.id)
                if (isinstance(pref, tuple) and len(pref) == 2
                        and pref not in [(m[1], m[2]) for m in MODES.values()]):
                    b.last = pref
            except Exception as e:
                logger.debug(f"[BATTLE] load pref failed: {e}")

        if b.mode == "group":
            # From a button the message is the bot's own; its reply_to is the
            # user's /start, so only a real user reply picks a target.
            from_bot = bool(getattr(msg, "from_user", None) and msg.from_user.is_bot)
            target = msg.reply_to_message.from_user if (msg.reply_to_message and not from_bot) else None
            if target:
                if target.is_bot:
                    await self._reply(update, "🤖 You can't challenge a bot.")
                    return
                if target.id == user.id:
                    await self._reply(update, "🙃 You can't battle yourself.")
                    return
                if self._bt_busy(target.id):
                    await self._reply(update, "⏳ That player is already in a battle.")
                    return
                b.p2, b.p2_name = target.id, self._bt_name(target)
            b.chat_id, b.thread_id = chat.id, get_thread_id(update)

        text, kb = self._bt_setup_view(b, "mode", getattr(context.bot, "username", ""))
        if update.callback_query:
            sent = await self._bt_edit(update.callback_query, text, kb)
            b.msg_id = getattr(sent, "message_id", None) or getattr(msg, "message_id", None)
        else:
            sent = await self._reply(update, text, reply_markup=kb)
            b.msg_id = getattr(sent, "message_id", None)
        self._bt_state()[bid] = b
        await self._bt_save(b)
        logger.info(f"[BATTLE] {bid} setup mode={b.mode} p1={b.p1} p2={b.p2}")

    # ── views ────────────────────────────────────────────────────────────

    def _bt_settings_line(self, b: "Battle") -> str:
        own = f"📝 Quiz › <b>{html.escape(b.set_title)}</b>\n" if b.set_id else ""
        return (f"<blockquote>{own}🎯 Questions › <b>{b.n_q}</b>\n⏱ Timer › <b>{_tlabel(b.secs)}</b> each\n"
                f"⌛ Max Duration › <b>{_tlabel(b.n_q * b.secs)}</b></blockquote>")

    def _bt_q_max(self) -> int:
        return max(Q_MIN, min(Q_MAX, len(self._bt_pool())))

    def _bt_setup_view(self, b: "Battle", page: str = "mode", bot_username: str = ""):
        """mode: main card · set: selected custom quiz · bot: one-tap modes ·
        cust: timer + questions · q: question count."""
        vs = UI.mention(b.p2, b.p2_name) if b.p2 else "<i>anyone</i>"
        B = lambda text, data, style=None: InlineKeyboardButton(text, callback_data=data, style=style)
        lab = lambda k: f"{MODES[k][0]} · {MODES[k][1]}Q · {_tlabel(MODES[k][2])}"
        head = f"{UI.mention(b.p1, b.p1_name)}  vs  {vs}\n"
        back = lambda to: B("⬅️ Back", f"bt_p:{b.id}:{to}", "danger")
        cancel = B("❌ Cancel", f"bt_dec:{b.id}", "danger")
        if page == "set" and b.set_id:
            text = (
                f"⚔️  <b>CUSTOM QUIZ BATTLE</b>\n{head}"
                f"{self._bt_settings_line(b)}\n"
                f"<i>Launch a 1v1 battle with this quiz, customize timer/questions, or open full quiz options:</i>"
            )
            rows = [
                [B("⚔️ Create 1v1 Challenge", f"bt_go:{b.id}", "success")],
                [B("⏱ Timer & Questions", f"bt_p:{b.id}:cust", None)]
                + ([B("📖 Full Quiz Menu", f"mq_open:{b.set_id}", None)] if b.mode == "dm" else []),
            ]
            if b.mode == "dm":
                play_row = [B("▶️ Play Solo", f"mq_prac:{b.set_id}", "success")]
                if bot_username:
                    play_row.append(InlineKeyboardButton(
                        "👥 Host in Group", url=f"https://t.me/{bot_username}?startgroup=gq_{b.set_id}", style="success"
                    ))
                rows.append(play_row)
            rows.append([B("⬅️ My Quizzes", f"bt_my:{b.id}", "danger"), cancel])
            return text, InlineKeyboardMarkup(rows)
        if page == "bot":
            text = f"🤖  <b>BOT QUIZ</b>\n{head}<i>Pick a mode — the challenge is created right away.</i>"
            rows = [[B(lab("classic"), f"bt_m:{b.id}:classic", "success")],
                    [B(lab("quick"), f"bt_m:{b.id}:quick", None), B(lab("marathon"), f"bt_m:{b.id}:marathon", None)]]
            if b.last:
                rows.append([B(f"🔁 Last · {b.last[0]}Q · {_tlabel(b.last[1])}", f"bt_m:{b.id}:last", None)])
            rows.append([back("mode"), cancel])
            return text, InlineKeyboardMarkup(rows)
        if page == "q":
            qmax = self._bt_q_max()
            text = (f"🎯  <b>HOW MANY QUESTIONS?</b>\n{head}"
                    f"<i>Tap one, or type any number ({Q_MIN}–{qmax}).</i>")
            opts = [B(("✅ " if b.n_q == n else "") + f"{n} Questions", f"bt_q:{b.id}:{n}",
                      "success" if b.n_q == n else None) for n in Q_CHOICES if n <= qmax]
            rows = [opts[k:k + 2] for k in range(0, len(opts), 2)] + [[back("cust"), cancel]]
            return text, InlineKeyboardMarkup(rows)
        if page == "cust":
            text = (f"⚙️  <b>CUSTOMIZE BATTLE</b>\n{head}{self._bt_settings_line(b)}\n"
                    f"<i>Pick a timer (or type one: 45, 2m). Tap Questions to change the count.</i>")
            opts = [B(("✅ " if b.secs == t else "") + _tlabel(t), f"bt_t:{b.id}:{t}",
                      "success" if b.secs == t else None) for t in T_CHOICES]
            rows = [opts[k:k + 2] for k in range(0, len(opts), 2)]
            back_target = "set" if b.set_id else "mode"
            rows += [[B(f"🎯 Questions: {b.n_q}", f"bt_p:{b.id}:q", None)],
                     [B("⚔️ Create Challenge", f"bt_go:{b.id}", "success")],
                     [back(back_target), cancel]]
            return text, InlineKeyboardMarkup(rows)
        text = f"⚔️  <b>1v1 BATTLE</b>\n{head}<i>Make your own quiz, pick from My Quizzes, or play with the bot's questions.</i>"
        if b.mode == "group" and bot_username:      # quizzes are made in the bot's DM
            create = InlineKeyboardButton("➕ Create Own Quiz", url=f"https://t.me/{bot_username}?start=mq_new",
                                          style="success")
        else:
            create = B("➕ Create Own Quiz", f"bt_new:{b.id}", "success")
        foot = [B("🏠 Home", "nav_home", "primary"), cancel] if b.mode == "dm" else [cancel]
        rows = [[create],
                [B("📚 My Quizzes", f"bt_my:{b.id}", None), B("🤖 Bot Quiz", f"bt_p:{b.id}:bot", None)],
                [B("⚙️ Customize", f"bt_p:{b.id}:cust", None)],
                foot]
        return text, InlineKeyboardMarkup(rows)

    def _bt_my_view(self, b: "Battle", sets: List[Dict], page: int = 1, pages: int = 1):
        B = lambda text, data, style=None: InlineKeyboardButton(text, callback_data=data, style=style)
        if not sets:
            text = ("📚  <b>MY QUIZZES</b>\n<i>No quizzes yet. Create one from your questions — "
                    "text, a file or forwarded quiz polls.</i>")
            rows = [[B("➕ Create Own Quiz", f"bt_new:{b.id}", "success")]]
        else:
            from src.bot.my_quiz import page_rows
            text = ("📚  <b>MY QUIZZES · 1v1 BATTLE</b>" + (f" · page {page}/{pages}" if pages > 1 else "") +
                    "\n<i>Select any quiz below to battle, customize, or open its options:</i>")
            rows = [[B(f"📝 {s.get('title', 'Quiz')[:28]} · {s.get('count', 0)}Q", f"bt_s:{b.id}:{s['id']}", None)]
                    for s in sets]
            rows += page_rows(pages, page, lambda n: f"bt_my:{b.id}:{n}")
        rows.append([B("⬅️ Back", f"bt_p:{b.id}:mode", "danger"), B("❌ Cancel", f"bt_dec:{b.id}", "danger")])
        return text, InlineKeyboardMarkup(rows)

    @staticmethod
    async def _bt_is_group_admin(context, chat_id: int, uid: int) -> bool:
        try:
            m = await context.bot.get_chat_member(chat_id, uid)
            return getattr(m, "status", "") in ("administrator", "creator")
        except Exception:
            return False

    def _bt_apply_set(self, b: "Battle", qs: Dict) -> None:
        b.set_id, b.set_title = qs["id"], str(qs.get("title") or "Quiz")[:40]
        b.n_q = max(1, min(int(qs.get("count") or len(qs.get("questions") or [])), Q_MAX))
        b.secs = max(T_MIN, min(int(qs.get("secs") or BATTLE_SECONDS), T_MAX))

    async def _bt_questions(self, b: "Battle") -> List[Dict]:
        """Questions for this battle: the challenger's own quiz, or the bot's pool."""
        import random
        if not b.set_id:
            return self._bt_pick_questions(b)
        try:
            qs = await asyncio.to_thread(self.db.get_quiz_set, b.set_id)
        except Exception as e:
            logger.warning(f"[BATTLE] {b.id} quiz set {b.set_id} load failed: {e}")
            return []
        pool = list((qs or {}).get("questions") or []) if isinstance(qs, dict) else []
        return random.sample(pool, min(b.n_q, len(pool)))

    async def _bt_create(self, b: "Battle", save_pref: bool = False) -> Optional[str]:
        """setup → pending. Returns an error text, or None when created."""
        if not b.set_id:
            have = len(self._bt_pool(b.category))
            if have < b.n_q:
                return f"Only {have} questions available — pick fewer."
        b.state, b.created = "pending", time.monotonic()
        if save_pref and self.db and hasattr(self.db, "set_battle_pref"):
            try:
                await asyncio.to_thread(self.db.set_battle_pref, b.p1, b.n_q, b.secs)
            except Exception as e:
                logger.debug(f"[BATTLE] save pref failed: {e}")
        logger.info(f"[BATTLE] {b.id} created n_q={b.n_q} secs={b.secs}")
        return None

    async def handle_battle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Group -3: the challenger typed a number while on the Questions or
        Timer step → apply it and edit the same setup message."""
        ud = getattr(context, "user_data", None)
        aw = ud.get("bt_await") if ud else None
        msg, chat = update.effective_message, update.effective_chat
        if not aw or not msg or not chat or chat.id != aw["chat"] or not getattr(msg, "text", None):
            return
        b = await self._bt_get(aw["bid"])
        if not b or b.state != "setup" or b.expired():
            ud.pop("bt_await", None)
            return
        if aw["kind"] == "q":
            val = int(msg.text.strip()) if msg.text.strip().isdigit() else None
            if val is None:
                return                                   # normal chat, not for us
            b.n_q = max(Q_MIN, min(val, self._bt_q_max()))
            aw["kind"] = "cust"                          # back to Customize
        else:
            val = _parse_secs(msg.text)
            if val is None:
                return
            b.secs = max(T_MIN, min(val, T_MAX))
        text, kb = self._bt_setup_view(b, "cust")
        try:
            await context.bot.edit_message_text(text, chat_id=chat.id, message_id=b.msg_id,
                                                parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                logger.debug(f"[BATTLE] input edit failed: {e}")
        await self._bt_save(b)
        try:
            await msg.delete()                           # keep the chat clean (needs admin in groups)
        except Exception:
            pass
        raise ApplicationHandlerStop

    def _bt_challenge_view(self, b: "Battle", bot_username: str, inline: bool = False):
        if b.mode == "group":
            vs = UI.mention(b.p2, b.p2_name) if b.p2 else "<i>anyone</i>"
            text = (f"⚔️ <b>1v1 BATTLE</b>\n{UI.mention(b.p1, b.p1_name)}  vs  {vs}\n"
                    f"{self._bt_settings_line(b)}")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚔️ Accept Battle", callback_data=f"bt_acc:{b.id}", style="success")],
                [InlineKeyboardButton("✏️ Change", callback_data=f"bt_ch:{b.id}", style="primary"),
                 InlineKeyboardButton("❌ Decline" if b.p2 else "❌ Cancel",
                                      callback_data=f"bt_dec:{b.id}", style="danger")],
            ])
            return text, kb
        link = f"https://t.me/{bot_username}?start=bt_{b.id}"
        text = (f"⚔️ <b>INVITE READY</b>\n{self._bt_settings_line(b)}\n"
                f"📤 <i>Send it to a friend. When they tap Accept, the battle starts in both your DMs.</i>")
        msg = (f"⚔️ {b.p1_name} challenged you to a 1v1 quiz battle! "
               f"{b.n_q} questions · {_tlabel(b.secs)} each. Tap to accept 👇")
        share = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote(msg)
        send = (InlineKeyboardButton(       # bold + premium invite card via inline mode
                    "📤 Send to a friend", style="success",
                    switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                        query=f"bt_{b.id}", allow_user_chats=True, allow_group_chats=True))
                if inline else InlineKeyboardButton("📤 Send to a friend", url=share, style="success"))
        kb = InlineKeyboardMarkup([
            [send],
            [InlineKeyboardButton("📋 Copy link", copy_text=CopyTextButton(link), style="primary")],
            [InlineKeyboardButton("✏️ Change", callback_data=f"bt_ch:{b.id}", style="primary"),
             InlineKeyboardButton("❌ Cancel", callback_data=f"bt_dec:{b.id}", style="danger")],
        ])
        return text, kb

    # ── buttons: setup / accept / decline / rematch ──────────────────────

    async def handle_battle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Loads the battle (memory or MongoDB), handles the button, saves it."""
        data = getattr(update.callback_query, "data", "") or ""
        parts = data.split(":")
        bid = parts[1] if len(parts) > 1 else ""
        if parts[0] != "bt_rm":
            await self._bt_get(bid)
        await self._bt_callback(update, context)
        b = self._bt_state().get(bid)
        if b:
            await self._bt_save(b)

    async def _bt_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user  = update.effective_user
        if not query or not user or not query.data:
            return
        parts = query.data.split(":")
        action, bid = parts[0], (parts[1] if len(parts) > 1 else "")
        if action == "bt_rm":
            await self._bt_rematch(query, user, bid, context)
            return
        b = self._bt_state().get(bid)
        if not b or b.expired():
            await query.answer("This battle has ended — start a new one.")
            await self._bt_edit(query, "⚔️ <i>This battle has ended.</i>", _ENDED_KB)
            return

        if action == "bt_ch":                      # ✏️ Change on the challenge card
            if user.id != b.p1 or b.state != "pending":
                await query.answer("Only the challenger can change this (before it's accepted).",
                                   show_alert=True)
                return
            b.state = "setup"
            await query.answer()
            await self._bt_edit(query, *self._bt_setup_view(b, "mode", context.bot.username))
            return

        if action in ("bt_q", "bt_t", "bt_m", "bt_go", "bt_p", "bt_my", "bt_s", "bt_new"):
            if user.id != b.p1 or b.state != "setup":
                await query.answer("Only the challenger can change this.", show_alert=True)
                return
            ud = getattr(context, "user_data", None)
            if ud is not None:
                ud.pop("bt_await", None)
            arg = parts[2] if len(parts) > 2 else ""
            remember = lambda kind: (ud is not None and query.message and ud.__setitem__(
                "bt_await", {"bid": b.id, "kind": kind, "chat": query.message.chat.id}))
            uname = context.bot.username
            if action == "bt_new":                     # DM: start making a quiz right here
                await query.answer()
                if hasattr(self, "mq_start_draft"):
                    await self.mq_start_draft(query, context, user)
                return
            if action == "bt_my":
                from src.bot.my_quiz import load_page

                async def call(fn, *a):
                    if not self.db or not hasattr(self.db, fn):
                        return None
                    try:
                        return await asyncio.to_thread(getattr(self.db, fn), *a)
                    except Exception as e:
                        logger.debug(f"[BATTLE] {fn} failed: {e}")
                        return None
                sets, page, pages = await load_page(lambda *a: call("list_quiz_sets", *a),
                                                    lambda *a: call("count_quiz_sets", *a),
                                                    user.id, int(arg) if arg.isdigit() else 1)
                await query.answer()
                await self._bt_edit(query, *self._bt_my_view(b, sets, page, pages))
                return
            if action == "bt_s":
                qs = None
                if self.db and hasattr(self.db, "get_quiz_set"):
                    try:
                        qs = await asyncio.to_thread(self.db.get_quiz_set, arg)
                    except Exception:
                        qs = None
                if not isinstance(qs, dict) or qs.get("owner") != user.id:
                    await query.answer("That quiz is no longer available.", show_alert=True)
                    return
                if b.mode == "group" and not await self._bt_is_group_admin(context, b.chat_id, user.id):
                    await query.answer("In groups only admins can start a battle with their own quiz. "
                                       "Use Bot Quiz, or play it in DM.", show_alert=True)
                    return
                self._bt_apply_set(b, qs)
                await query.answer()
                await self._bt_edit(query, *self._bt_setup_view(b, "set", uname))
                return
            if action == "bt_p":
                page = arg if arg in ("bot", "cust", "q", "set") else "mode"
                if page == "bot":
                    b.set_id = b.set_title = ""
                if page in ("cust", "q"):
                    remember(page)
                await query.answer()
                await self._bt_edit(query, *self._bt_setup_view(b, page, uname))
                return
            if action in ("bt_q", "bt_t"):
                if not arg.isdigit():
                    await query.answer(); return
                if action == "bt_q":
                    b.n_q = max(Q_MIN, min(int(arg), self._bt_q_max()))
                else:
                    b.secs = max(T_MIN, min(int(arg), T_MAX))
                remember("cust")
                await query.answer()
                await self._bt_edit(query, *self._bt_setup_view(b, "cust", uname))
                return
            if action == "bt_m":
                if arg in MODES:
                    _, b.n_q, b.secs = MODES[arg]
                elif arg == "last" and b.last:
                    b.n_q, b.secs = b.last
                else:
                    await query.answer(); return
                b.set_id = b.set_title = ""                # bot questions
            err = await self._bt_create(b, save_pref=(action == "bt_go" and not b.set_id))
            if err:
                await query.answer(err, show_alert=True)
                return
            await query.answer("Challenge created! ⚔️")
            await self._bt_edit(query, *self._bt_challenge_view(b, uname, self._bt_inline_on(context)))
            return

        if action == "bt_dec":
            if user.id == b.p1 and b.state in ("setup", "pending"):
                b.state = "done"
                await query.answer("Battle cancelled.")
                done_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚔️ New Battle", callback_data="start_battle", style="success"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav_home", style="primary"),
                ]]) if b.mode == "dm" else None
                await self._bt_edit(query, f"⚔️ Battle cancelled by {UI.mention(b.p1, b.p1_name)}.", done_kb)
            elif b.mode == "dm" and not b.p2 and b.state == "pending":
                await query.answer("Declined.")
                await self._bt_edit(query, "❌ You declined the battle.")
                try:
                    await context.bot.send_message(
                        b.p1, f"❌ {UI.mention(user.id, self._bt_name(user))} declined your battle invite. "
                              f"It's still open for others.", parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            elif b.p2 and user.id == b.p2 and b.state == "pending":
                b.state = "done"
                await query.answer("Declined.")
                await self._bt_edit(query, f"⚔️ {UI.mention(b.p2, b.p2_name)} declined the battle.")
            else:
                await query.answer("Only the players can do that.", show_alert=True)
            return

        if action == "bt_acc":
            ok, why = b.can_accept(user.id)
            if ok and self._bt_busy(user.id):
                ok, why = False, "You're already in a battle — finish it first."
            if not ok:
                await query.answer(why, show_alert=True)
                return
            await self._bt_drop_open(user.id, context)
            ok, why = b.can_accept(user.id)            # re-check: someone may have accepted meanwhile
            if not ok or self._bt_busy(user.id):
                await query.answer(why or "You're already in a battle.", show_alert=True)
                return
            b.start(user.id, self._bt_name(user))
            await query.answer("Battle accepted! ⚔️")
            await self._bt_edit(
                query,
                f"⚔️ <b>BATTLE STARTED</b>\n"
                f"{UI.mention(b.p1, b.p1_name)}  vs  {UI.mention(b.p2, b.p2_name)}\n"
                f"{self._bt_settings_line(b)}\n"
                f"First question in {START_DELAY}s…",
            )
            if hasattr(self, "ensure_user_registered"):
                try:
                    await asyncio.to_thread(self.ensure_user_registered, user, None, "battle")
                except Exception:
                    pass
            if b.mode == "dm":   # tell the challenger: edit their invite card, else a new message
                await self._bt_tell_challenger(b, context)
            asyncio.create_task(self._bt_run(b, context))

    async def _bt_rematch(self, query, user, old_bid: str, context) -> None:
        old = getattr(self, "_bt_finished", {}).get(old_bid)
        if not old or user.id not in old.players:
            await query.answer("Rematch is no longer available — start a new /battle.", show_alert=True)
            return
        self._bt_cleanup()
        other = old.p2 if user.id == old.p1 else old.p1
        other_name = old.p2_name if user.id == old.p1 else old.p1_name
        if self._bt_busy(user.id) or self._bt_busy(other):
            await query.answer("One of you is already in a battle.", show_alert=True)
            return
        await self._bt_drop_open(user.id, context)
        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode=old.mode, p1=user.id, p1_name=self._bt_name(user),
                   p2=other, p2_name=other_name, chat_id=old.chat_id, thread_id=old.thread_id,
                   n_q=old.n_q, secs=old.secs, category=old.category,
                   set_id=old.set_id, set_title=old.set_title)
        self._bt_state()[bid] = b
        await self._bt_save(b)
        await query.answer("Rematch sent! ⚔️")
        text, _ = self._bt_challenge_view(dataclass_replace_mode(b, "group"), context.bot.username)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"bt_acc:{bid}", style="success"),
            InlineKeyboardButton("❌ Decline", callback_data=f"bt_dec:{bid}", style="danger"),
        ]])
        target = b.chat_id if b.mode == "group" else other
        kw = {"parse_mode": ParseMode.HTML, "reply_markup": kb}
        if b.mode == "group" and b.thread_id:
            kw["message_thread_id"] = b.thread_id
        try:
            await context.bot.send_message(target, "🔁 <b>REMATCH</b>\n" + text, **kw)
        except Exception as e:
            b.state = "done"
            await self._bt_save(b)
            logger.warning(f"[BATTLE] rematch {bid} could not be sent: {e}")

    @staticmethod
    def _bt_inline_on(context) -> bool:
        """True when inline mode is enabled for the bot (@BotFather /setinline)."""
        return bool(getattr(getattr(context.bot, "bot", None), "supports_inline_queries", False))

    def _bt_inline_result(self, b: "Battle", bot_username: str, premium: bool):
        from src.bot.premium import premiumize_html, premiumize_markup, style_markup, tidy_html
        text = (f"⚔️ <b>1v1 QUIZ BATTLE</b>\n{UI.mention(b.p1, b.p1_name)} challenged you!\n"
                f"{self._bt_settings_line(b)}\n"
                f"<i>Tap Accept — the battle starts in both your DMs.</i>")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⚔️ Accept Battle", url=f"https://t.me/{bot_username}?start=bt_{b.id}_go", style="success")]])
        text, kb = tidy_html(text), style_markup(kb)
        if premium:
            text, kb = premiumize_html(text), premiumize_markup(kb)
        return InlineQueryResultArticle(
            id=b.id, title="⚔️ Send battle invite",
            description=f"{b.n_q} questions · {_tlabel(b.secs)} each",
            input_message_content=InputTextMessageContent(
                text, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)),
            reply_markup=kb)

    async def handle_battle_inline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Inline query 'bt_<id>' from the challenger → a bold, premium invite
        card with an Accept button. Anything else → no results."""
        from src.bot.premium import premium_on
        iq = update.inline_query
        if not iq:
            return
        q = (iq.query or "").strip()
        b = await self._bt_get(q[3:]) if q.startswith("bt_") else None
        ok = bool(b and b.mode == "dm" and b.state == "pending" and not b.expired()
                  and iq.from_user and iq.from_user.id == b.p1)
        tries = ([premium_on(), False] if premium_on() else [False]) if ok else [None]
        for premium in tries:
            try:
                res = [self._bt_inline_result(b, context.bot.username, premium)] if ok else []
                await iq.answer(res, cache_time=0, is_personal=True)
                return
            except Exception as e:
                logger.debug(f"[BATTLE] inline answer failed (premium={premium}): {e}")

    async def _bt_tell_challenger(self, b: "Battle", context) -> None:
        """DM battle accepted: edit the challenger's invite card, else send a message."""
        note = (f"✅ {UI.mention(b.p2, b.p2_name)} accepted your battle!\n"
                f"{self._bt_settings_line(b)}\nFirst question in {START_DELAY}s…")
        try:
            if not b.msg_id:
                raise ValueError("no invite card")
            await context.bot.edit_message_text(note, chat_id=b.p1, message_id=b.msg_id,
                                                parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await context.bot.send_message(b.p1, note, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    async def battle_join_from_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    bid: str) -> None:
        """Called from /start bt_<id> in a DM. 'bt_<id>_go' (Accept button on
        the shared invite card) accepts right away; a plain link shows the card."""
        user = update.effective_user
        self._bt_cleanup()
        go = bid.endswith("_go")
        bid = bid[:-3] if go else bid
        b = await self._bt_get(bid)
        if not b or b.mode != "dm" or b.expired() or b.state not in ("pending", "running"):
            await self._reply(update, "⚔️ This battle invite has ended.", reply_markup=_ENDED_KB)
            return
        if user.id == b.p1:
            await self._reply(update, "⚔️ This is your own invite — send it to a friend 📤")
            return
        ok, why = b.can_accept(user.id)
        if not ok:
            await self._reply(update, f"⚔️ {why}", reply_markup=_ENDED_KB)
            return
        if go:
            if self._bt_busy(user.id):
                await self._reply(update, "⚔️ You're already in a battle — finish it first.")
                return
            await self._bt_drop_open(user.id, context)
            ok, why = b.can_accept(user.id)            # re-check after the await
            if not ok:
                await self._reply(update, f"⚔️ {why}", reply_markup=_ENDED_KB)
                return
            b.start(user.id, self._bt_name(user))
            await self._bt_save(b)
            await self._reply(update, f"⚔️ <b>BATTLE STARTED</b>\n"
                                      f"{UI.mention(b.p1, b.p1_name)}  vs  {UI.mention(b.p2, b.p2_name)}\n"
                                      f"{self._bt_settings_line(b)}\nFirst question in {START_DELAY}s…")
            await self._bt_tell_challenger(b, context)
            asyncio.create_task(self._bt_run(b, context))
            return
        text = (f"⚔️ <b>BATTLE INVITE</b>\n{UI.mention(b.p1, b.p1_name)} challenged you!\n"
                f"{self._bt_settings_line(b)}\n"
                f"<i>Tap Accept — the battle starts in both your DMs.</i>")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚔️ Accept Battle", callback_data=f"bt_acc:{b.id}", style="success")],
            [InlineKeyboardButton("❌ Decline", callback_data=f"bt_dec:{b.id}", style="danger")],
        ])
        await self._reply(update, text, reply_markup=kb)

    # ── game loop ────────────────────────────────────────────────────────

    async def _bt_run(self, b: Battle, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            questions = await self._bt_questions(b)
            if len(questions) < b.n_q:
                b.state = "done"
                await self._bt_notify(b, context, "⚠️ Battle cancelled: not enough questions.")
                return
            await asyncio.sleep(START_DELAY)
            for i, q in enumerate(questions, 1):
                if b.state != "running":
                    return
                rounds = await self._bt_send_question(b, context, q, i)
                if not rounds:
                    b.state = "done"
                    await self._bt_notify(b, context, "⚠️ Battle stopped: could not send a question.")
                    return
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(r.done.wait() for r in rounds)),
                        timeout=b.secs + 1,
                    )
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(ROUND_GAP)
            b.state = "done"
            await self._bt_finish(b, context)
        except Exception as e:
            b.state = "done"
            logger.error(f"[BATTLE] {b.id} crashed: {e}", exc_info=True)
            await self._bt_notify(b, context, "⚠️ Battle stopped due to an error.")

    async def _bt_send_question(self, b: Battle, context, q: Dict, n: int) -> List[_Round]:
        options = [str(o)[:100] for o in q["options"]][:10]
        correct = int(q.get("correct_answer", 0))
        if not 0 <= correct < len(options):
            correct = 0
        kwargs = dict(
            question=f"⚔️ {n}/{b.n_q}  {q['question']}"[:300],
            options=options, type=Poll.QUIZ, correct_option_id=correct,
            is_anonymous=False, open_period=b.secs,
        )
        targets = [(b.chat_id, b.thread_id)] if b.mode == "group" else [(u, None) for u in b.players]
        rounds = []
        for chat_id, thread_id in targets:
            kw = dict(kwargs, chat_id=chat_id)
            if thread_id:
                kw["message_thread_id"] = thread_id
            try:
                msg = await context.bot.send_poll(**kw)
            except Exception as e:
                logger.warning(f"[BATTLE] {b.id} send_poll to {chat_id} failed: {e}")
                return []
            rnd = _Round(correct=correct, sent_at=time.monotonic(),
                         owner=chat_id if b.mode == "dm" else None)
            pid = str(msg.poll.id)
            b.rounds[pid] = (n, rnd)
            self._battle_polls[pid] = b.id
            rounds.append(rnd)
        return rounds

    def battle_on_poll_answer(self, poll_id: str, uid: int, option_ids) -> bool:
        """Hook for handle_poll_answer. True = this poll belongs to a battle."""
        polls = getattr(self, "_battle_polls", None)
        if not polls or poll_id not in polls:
            return False
        b = self._bt_state().get(polls[poll_id])
        if b:
            b.record_answer(poll_id, uid, option_ids[0] if option_ids else None)
        return True

    async def _bt_finish(self, b: Battle, context) -> None:
        winner, loser = b.result()
        line = lambda u, n: (f"{UI.mention(u, n)}  ›  <b>{b.scores[u]}</b>/{b.n_q}"
                             f"  ·  {b.times[u]:.1f}s")
        if winner:
            wname = b.p1_name if winner == b.p1 else b.p2_name
            head = f"🏆 Winner: {UI.mention(winner, wname)}"
        else:
            head = "🤝 It's a draw!"
        text = (
            f"⚔️  <b>BATTLE RESULT</b>\n{UI.LINE}\n\n"
            f"{line(b.p1, b.p1_name)}\n{line(b.p2, b.p2_name)}\n\n"
            f"{head}\n\n<i>Tie-break: less time on correct answers.</i>"
        )
        self._bt_finished_put(b)
        await self._bt_notify(b, context, text, InlineKeyboardMarkup([[
            InlineKeyboardButton("🔁 Rematch", callback_data=f"bt_rm:{b.id}", style="success")]]))
        if self.db:
            try:
                await asyncio.to_thread(self.db.record_battle_result, b.p1, b.p2, winner)
            except Exception as e:
                logger.error(f"[BATTLE] {b.id} record_battle_result failed: {e}")
        logger.info(f"[BATTLE] {b.id} done scores={b.scores} winner={winner}")

    def _bt_finished_put(self, b: "Battle") -> None:
        """Keep finished battles 30 min for Rematch (bounded)."""
        fin = self.__dict__.setdefault("_bt_finished", {})
        fin[b.id] = b
        b.created = time.monotonic()
        for k in [k for k, v in fin.items() if time.monotonic() - v.created > 1800][:100]:
            fin.pop(k, None)
        while len(fin) > 500:
            fin.pop(next(iter(fin)))

    async def _bt_notify(self, b: Battle, context, text: str, kb=None) -> None:
        if b.mode == "group":
            targets = [(b.chat_id, b.thread_id)]
        else:
            targets = [(u, None) for u in b.players]
        for chat_id, thread_id in targets:
            kw = {"parse_mode": ParseMode.HTML}
            if kb is not None:
                kw["reply_markup"] = kb
            if thread_id:
                kw["message_thread_id"] = thread_id
            try:
                await context.bot.send_message(chat_id, text, **kw)
            except Exception as e:
                logger.warning(f"[BATTLE] {b.id} notify {chat_id} failed: {e}")

    @staticmethod
    async def _bt_edit(query, text: str, reply_markup=None):
        msg = getattr(query, "message", None)
        try:
            return await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as e:
            err = str(e).lower()
            if "not modified" in err:
                return msg
            if msg is not None and hasattr(msg, "reply_text"):
                try:
                    return await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                except Exception as e2:
                    logger.debug(f"[BATTLE] reply fallback failed: {e2}")
            logger.debug(f"[BATTLE] edit failed: {e}")
            return None
