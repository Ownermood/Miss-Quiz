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
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Poll, Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.bot.ui import UI, get_thread_id

logger = logging.getLogger(__name__)

BATTLE_QUESTIONS = 10     # defaults; the challenger can change them in the setup card
BATTLE_SECONDS   = 20     # Telegram open_period per question
Q_CHOICES        = (5, 10, 15, 20, 30)   # quick presets; ➖/➕ pick any count
Q_MIN, Q_MAX     = 3, 50
T_CHOICES        = (10, 15, 20, 30, 60, 240)   # Telegram allows up to 600 s
INVITE_TTL       = 300    # seconds a setup/challenge stays open
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
    return f"{secs // 60}m" if secs >= 60 and secs % 60 == 0 else f"{secs}s"


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
        for b in self._bt_state().values():
            if b.state != "done" and not b.expired() and uid in b.players:
                return True
        return False

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
        """Step 1: the challenger picks questions / time (setup card)."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not user or not chat or not msg:
            return
        self._bt_cleanup()
        if self._bt_busy(user.id):
            await self._reply(update, "⚔️ You already have a battle running or waiting.")
            return
        if len(self._bt_pool()) < Q_CHOICES[0]:
            await self._reply(update, "⚠️ Not enough questions for a battle yet.")
            return

        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode="dm" if chat.type == "private" else "group",
                   p1=user.id, p1_name=self._bt_name(user), state="setup")
        b.n_q = min(BATTLE_QUESTIONS, max(q for q in Q_CHOICES if q <= len(self._bt_pool())))

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

        text, kb = self._bt_setup_view(b)
        sent = await self._reply(update, text, reply_markup=kb)
        b.msg_id = getattr(sent, "message_id", None)
        self._bt_state()[bid] = b
        logger.info(f"[BATTLE] {bid} setup mode={b.mode} p1={b.p1} p2={b.p2}")

    # ── views ────────────────────────────────────────────────────────────

    def _bt_settings_line(self, b: "Battle") -> str:
        return f"<blockquote>🎯 Questions › {b.n_q}\n⏱ Time › {_tlabel(b.secs)} each</blockquote>"

    def _bt_q_max(self) -> int:
        return max(Q_MIN, min(Q_MAX, len(self._bt_pool())))

    def _bt_setup_view(self, b: "Battle", page: str = "main"):
        """One screen with every option (current = green), then a summary
        screen with Edit / Create Challenge."""
        vs = UI.mention(b.p2, b.p2_name) if b.p2 else "<i>anyone</i>"
        B = lambda text, data, style="primary": InlineKeyboardButton(text, callback_data=data, style=style)
        go = lambda v: B(v[0], f"bt_q:{b.id}:{v[1]}")
        if page == "sum":
            total = b.n_q * b.secs
            text = (f"⚔️ <b>BATTLE SUMMARY</b>\n{UI.mention(b.p1, b.p1_name)}  vs  {vs}\n"
                    f"{self._bt_settings_line(b)}\n"
                    f"⌛ <i>Up to {_tlabel(total) if total < 3600 else f'{total // 60}m'} in total</i>")
            return text, InlineKeyboardMarkup([
                [B("✏️ Edit", f"bt_p:{b.id}:main")],
                [B("⚔️ Create Challenge", f"bt_go:{b.id}", "success")],
                [B("❌ Cancel", f"bt_dec:{b.id}", "danger")],
            ])
        qmax, n = self._bt_q_max(), b.n_q
        text = (f"⚔️ <b>BATTLE SETUP</b>\n{UI.mention(b.p1, b.p1_name)}  vs  {vs}\n"
                f"{self._bt_settings_line(b)}\n"
                f"<i>Green = selected. ➖/➕ for any number ({Q_MIN}–{qmax}).</i>")
        hdr = lambda t: InlineKeyboardButton(t, callback_data=f"bt_x:{b.id}")
        rows = [[hdr("🎯 QUESTIONS")],
                [go(("➖5", n - 5)), go(("➖1", n - 1)),
                 InlineKeyboardButton(str(n), callback_data=f"bt_x:{b.id}", style="success"),
                 go(("➕1", n + 1)), go(("➕5", n + 5))],
                [B(str(q), f"bt_q:{b.id}:{q}", "success" if n == q else "primary")
                 for q in Q_CHOICES if q <= qmax],
                [hdr("⏱ TIME PER QUESTION")]]
        tb = [B(_tlabel(t), f"bt_t:{b.id}:{t}", "success" if b.secs == t else "primary") for t in T_CHOICES]
        rows += [tb[k:k + 3] for k in range(0, len(tb), 3)]
        rows.append([B("➡️ Next", f"bt_p:{b.id}:sum", "success")])
        rows.append([B("❌ Cancel", f"bt_dec:{b.id}", "danger")])
        return text, InlineKeyboardMarkup(rows)

    def _bt_challenge_view(self, b: "Battle", bot_username: str):
        if b.mode == "group":
            vs = UI.mention(b.p2, b.p2_name) if b.p2 else "<i>anyone</i>"
            text = (f"⚔️ <b>1v1 BATTLE</b>\n{UI.mention(b.p1, b.p1_name)}  vs  {vs}\n"
                    f"{self._bt_settings_line(b)}\n"
                    f"⌛ <i>Open for {INVITE_TTL // 60} min</i>")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"bt_acc:{b.id}", style="success"),
                InlineKeyboardButton("❌ Decline", callback_data=f"bt_dec:{b.id}", style="danger"),
            ]])
            return text, kb
        link = f"https://t.me/{bot_username}?start=bt_{b.id}"
        text = (f"⚔️ <b>1v1 BATTLE</b>\n"
                f"Send this link to a friend — when they open it, the battle starts in both your DMs.\n"
                f"{self._bt_settings_line(b)}\n"
                f"🔗 {html.escape(link)}\n⌛ <i>Link valid {INVITE_TTL // 60} min</i>")
        share = ("https://t.me/share/url?url=" + quote(link, safe="") +
                 "&text=" + quote("Battle me on Miss Quiz ⚔️"))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share invite", url=share)],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"bt_dec:{b.id}", style="danger")],
        ])
        return text, kb

    # ── buttons: setup / accept / decline / rematch ──────────────────────

    async def handle_battle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await query.answer("This battle is no longer available.", show_alert=True)
            return

        if action == "bt_x":                       # section label / counter
            await query.answer()
            return
        if action in ("bt_q", "bt_t", "bt_c", "bt_go", "bt_p"):
            if user.id != b.p1 or b.state != "setup":
                await query.answer("Only the challenger can change this.", show_alert=True)
                return
            if action == "bt_p":
                await query.answer()
                page = parts[2] if len(parts) > 2 and parts[2] == "sum" else "main"
                await self._bt_edit(query, *self._bt_setup_view(b, page))
                return
            try:
                val = int(parts[2]) if action != "bt_go" else 0
            except (IndexError, ValueError):
                await query.answer(); return
            if action == "bt_q":
                b.n_q = max(Q_MIN, min(val, self._bt_q_max()))
            elif action == "bt_t" and val in T_CHOICES:
                b.secs = val
            elif action == "bt_c":
                b.category = b.cat_options[val] if 0 <= val < len(b.cat_options) else ""
            elif action == "bt_go":
                have = len(self._bt_pool(b.category))
                if have < b.n_q:
                    await query.answer(f"Only {have} questions available — pick fewer.",
                                       show_alert=True)
                    return
                b.state, b.created = "pending", time.monotonic()
                await query.answer("Challenge created! ⚔️")
                await self._bt_edit(query, *self._bt_challenge_view(b, context.bot.username))
                return
            await query.answer()
            await self._bt_edit(query, *self._bt_setup_view(b))
            return

        if action == "bt_dec":
            if user.id == b.p1 and b.state in ("setup", "pending"):
                b.state = "done"
                await query.answer("Battle cancelled.")
                await self._bt_edit(query, f"⚔️ Battle cancelled by {UI.mention(b.p1, b.p1_name)}.")
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
                ok, why = False, "You already have a battle running or waiting."
            if not ok:
                await query.answer(why, show_alert=True)
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
            if b.mode == "dm":   # rematch accepted in DM: tell the challenger too
                try:
                    await context.bot.send_message(b.p1, f"⚔️ <b>{html.escape(b.p2_name)} accepted — starting!</b>",
                                                   parse_mode=ParseMode.HTML)
                except Exception:
                    pass
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
        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode=old.mode, p1=user.id, p1_name=self._bt_name(user),
                   p2=other, p2_name=other_name, chat_id=old.chat_id, thread_id=old.thread_id,
                   n_q=old.n_q, secs=old.secs, category=old.category)
        self._bt_state()[bid] = b
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
            logger.warning(f"[BATTLE] rematch {bid} could not be sent: {e}")

    async def battle_join_from_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    bid: str) -> None:
        """Called from /start bt_<id> in a DM."""
        user = update.effective_user
        self._bt_cleanup()
        b = self._bt_state().get(bid)
        if not b or b.mode != "dm":
            await self._reply(update, "⚔️ This battle link is invalid or has expired.")
            return
        ok, why = b.can_accept(user.id)
        if ok and self._bt_busy(user.id):
            ok, why = False, "You already have a battle running or waiting."
        if not ok:
            await self._reply(update, f"⚔️ {why}")
            return
        b.start(user.id, self._bt_name(user))
        intro = (
            f"⚔️  <b>BATTLE STARTED</b>\n{UI.LINE}\n\n"
            f"{UI.mention(b.p1, b.p1_name)}  vs  {UI.mention(b.p2, b.p2_name)}\n\n"
            f"🎯 {b.n_q} questions · ⏱ {_tlabel(b.secs)} each\n"
            f"First question in {START_DELAY}s…"
        )
        for uid in b.players:
            try:
                await context.bot.send_message(uid, intro, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"[BATTLE] {bid} intro to {uid} failed: {e}")
                b.state = "done"
                await self._bt_notify(b, context, "⚠️ Battle cancelled: could not message both players.")
                return
        asyncio.create_task(self._bt_run(b, context))

    # ── game loop ────────────────────────────────────────────────────────

    async def _bt_run(self, b: Battle, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            questions = self._bt_pick_questions(b)
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
    async def _bt_edit(query, text: str, reply_markup=None) -> None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception as e:
            if "not modified" not in str(e).lower():
                logger.debug(f"[BATTLE] edit failed: {e}")
