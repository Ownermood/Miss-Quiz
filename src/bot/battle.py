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

BATTLE_QUESTIONS = 5
BATTLE_SECONDS   = 20     # Telegram open_period per question
INVITE_TTL       = 120    # seconds a challenge stays open
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
    state:     str = "pending"         # pending | running | done
    created:   float = field(default_factory=time.monotonic)
    msg_id:    Optional[int] = None    # challenge message (group)
    scores:    Dict[int, int] = field(default_factory=dict)
    times:     Dict[int, float] = field(default_factory=dict)
    rounds:    Dict[str, Tuple[int, _Round]] = field(default_factory=dict)  # poll_id -> (q_idx, round)

    @property
    def players(self) -> List[int]:
        return [p for p in (self.p1, self.p2) if p]

    def expired(self, now: Optional[float] = None) -> bool:
        return self.state == "pending" and (now or time.monotonic()) - self.created > INVITE_TTL

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

    def _bt_pick_questions(self) -> List[Dict]:
        picked, seen = [], set()
        for _ in range(BATTLE_QUESTIONS * 6):
            q = self.quiz_manager.get_random_question(chat_id=0)
            if not q:
                break
            key = q.get("id") or q.get("question")
            if key in seen or len(q.get("options") or []) < 2:
                continue
            seen.add(key)
            picked.append(q)
            if len(picked) == BATTLE_QUESTIONS:
                break
        return picked

    @staticmethod
    def _bt_name(user) -> str:
        return UI.display_name(user)

    # ── /battle ──────────────────────────────────────────────────────────

    async def cmd_battle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not user or not chat or not msg:
            return
        self._bt_cleanup()
        if self._bt_busy(user.id):
            await self._reply(update, "⚔️ You already have a battle running or waiting.")
            return
        if len(getattr(self.quiz_manager, "questions", []) or []) < BATTLE_QUESTIONS:
            await self._reply(update, "⚠️ Not enough questions for a battle yet.")
            return

        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode="dm" if chat.type == "private" else "group",
                   p1=user.id, p1_name=self._bt_name(user))

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
            vs = UI.mention(b.p2, b.p2_name) if b.p2 else "<i>anyone</i>"
            text = (
                f"⚔️  <b>1v1 BATTLE</b>\n{UI.LINE}\n\n"
                f"{UI.mention(b.p1, b.p1_name)}  vs  {vs}\n\n"
                f"🎯 {BATTLE_QUESTIONS} questions · ⏱ {BATTLE_SECONDS}s each\n"
                f"⌛ Challenge open for {INVITE_TTL // 60} min"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept",  callback_data=f"bt_acc:{bid}"),
                InlineKeyboardButton("❌ Decline", callback_data=f"bt_dec:{bid}"),
            ]])
            sent = await self._reply(update, text, reply_markup=kb)
            b.msg_id = getattr(sent, "message_id", None)
        else:
            link = f"https://t.me/{context.bot.username}?start=bt_{bid}"
            text = (
                f"⚔️  <b>1v1 BATTLE</b>\n{UI.LINE}\n\n"
                f"Send this link to a friend. When they open it, the battle starts "
                f"here in both your DMs.\n\n"
                f"🔗 <code>{html.escape(link)}</code>\n\n"
                f"🎯 {BATTLE_QUESTIONS} questions · ⏱ {BATTLE_SECONDS}s each · "
                f"⌛ link valid {INVITE_TTL // 60} min"
            )
            share = ("https://t.me/share/url?url=" + quote(link, safe="") +
                     "&text=" + quote("Battle me on Miss Quiz ⚔️"))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Share invite", url=share)],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"bt_dec:{bid}")],
            ])
            await self._reply(update, text, reply_markup=kb)

        self._bt_state()[bid] = b
        logger.info(f"[BATTLE] {bid} created mode={b.mode} p1={b.p1} p2={b.p2}")

    # ── accept / decline ─────────────────────────────────────────────────

    async def handle_battle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user  = update.effective_user
        if not query or not user or not query.data:
            return
        action, _, bid = query.data.partition(":")
        b = self._bt_state().get(bid)
        if not b:
            await query.answer("This battle is no longer available.", show_alert=True)
            return

        if action == "bt_dec":
            if user.id == b.p1 and b.state == "pending":
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
                f"⚔️  <b>BATTLE STARTED</b>\n{UI.LINE}\n\n"
                f"{UI.mention(b.p1, b.p1_name)}  vs  {UI.mention(b.p2, b.p2_name)}\n\n"
                f"First question in {START_DELAY}s…",
            )
            if hasattr(self, "ensure_user_registered"):
                try:
                    self.ensure_user_registered(user, source="battle")
                except Exception:
                    pass
            asyncio.create_task(self._bt_run(b, context))

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
            f"🎯 {BATTLE_QUESTIONS} questions · ⏱ {BATTLE_SECONDS}s each\n"
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
            questions = self._bt_pick_questions()
            if len(questions) < BATTLE_QUESTIONS:
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
                        timeout=BATTLE_SECONDS + 1,
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
            question=f"⚔️ {n}/{BATTLE_QUESTIONS}  {q['question']}"[:300],
            options=options, type=Poll.QUIZ, correct_option_id=correct,
            is_anonymous=False, open_period=BATTLE_SECONDS,
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
        line = lambda u, n: (f"{UI.mention(u, n)}  ›  <b>{b.scores[u]}</b>/{BATTLE_QUESTIONS}"
                             f"  ·  {b.times[u]:.1f}s")
        if winner:
            wname = b.p1_name if winner == b.p1 else b.p2_name
            head = f"🏆 Winner: {UI.mention(winner, wname)}"
        else:
            head = "🤝 It's a draw!"
        text = (
            f"⚔️  <b>BATTLE RESULT</b>\n{UI.LINE}\n\n"
            f"{line(b.p1, b.p1_name)}\n{line(b.p2, b.p2_name)}\n\n"
            f"{head}\n\n<i>Tie-break: less time on correct answers.</i>\n"
            f"Play again: /battle"
        )
        await self._bt_notify(b, context, text)
        if self.db:
            try:
                await asyncio.to_thread(self.db.record_battle_result, b.p1, b.p2, winner)
            except Exception as e:
                logger.error(f"[BATTLE] {b.id} record_battle_result failed: {e}")
        logger.info(f"[BATTLE] {b.id} done scores={b.scores} winner={winner}")

    async def _bt_notify(self, b: Battle, context, text: str) -> None:
        if b.mode == "group":
            targets = [(b.chat_id, b.thread_id)]
        else:
            targets = [(u, None) for u in b.players]
        for chat_id, thread_id in targets:
            kw = {"parse_mode": ParseMode.HTML}
            if thread_id:
                kw["message_thread_id"] = thread_id
            try:
                await context.bot.send_message(chat_id, text, **kw)
            except Exception as e:
                logger.warning(f"[BATTLE] {b.id} notify {chat_id} failed: {e}")

    @staticmethod
    async def _bt_edit(query, text: str) -> None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug(f"[BATTLE] edit failed: {e}")
