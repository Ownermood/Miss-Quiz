"""
Group Quiz — run a whole quiz in a group (own quiz set or bot questions).

Start:  /startquiz in a group (admins) → pick a quiz, or the quiz card's
        "👥 Start in Group" link (t.me/<bot>?startgroup=gq_<set id>).
Flow:   countdown → one timed quiz poll after another → mini leaderboard every
        10 questions → final result + Play again.
Rules:  one session per group · only group admins (or the starter) control it ·
        first answer per user counts · negative marking support (-0.25 standard) ·
        the group's hourly auto quiz waits while a session runs ·
        sessions are saved in MongoDB (`quiz_sessions`) and continue after a restart.
Answers here don't change /score (same as battles).
"""

import asyncio
import html
import logging
import random
import re
import time
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Poll, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.bot.ui import UI, get_thread_id

logger = logging.getLogger(__name__)

START_DELAY = 3          # countdown before Q1
GAP         = 2          # pause after each question
LB_EVERY    = 10         # mini leaderboard every N questions
BOT_COUNTS  = (10, 20, 30)
BOT_SECS    = 20
IDLE_PAUSE  = 30 * 60    # a paused session is closed after 30 min
SLACK       = 1          # extra second after the poll timer before moving on

ACTIVE: Dict[int, "Session"] = {}       # chat_id → running/paused session (scheduler reads this)


def is_active(chat_id: int) -> bool:
    s = ACTIVE.get(chat_id)
    return bool(s and s.status in ("lobby", "running", "paused"))


@dataclass
class Session:
    chat_id:   int
    title:     str
    questions: List[Dict]
    secs:      int
    started_by: int
    starter_name: str = ""
    chat_title: str = ""
    thread_id: Optional[int] = None
    set_id:    str = ""
    idx:       int = 0                     # next question to send
    status:    str = "lobby"               # lobby | running | paused | stopped | done
    scores:    Dict[str, Dict] = field(default_factory=dict)   # uid → {name, correct, wrong, score, time}
    ctrl_msg_id: Optional[int] = None
    paused_at: float = 0.0
    polls:     Dict[str, Tuple[int, int, float]] = field(default_factory=dict)  # poll → (idx, correct, sent)
    answered:  set = field(default_factory=set)
    wake:      asyncio.Event = field(default_factory=asyncio.Event)
    skip:      bool = False
    negative_rate: float = 0.0             # marks cut per wrong answer (host can turn on −0.25 in the lobby)
    marks_per_q:   float = 1.0             # +1.0 marks per correct answer
    user_history:  Dict[str, List[Dict]] = field(default_factory=dict) # uid → list of answered questions
    ready_players: Dict[str, str] = field(default_factory=dict)        # uid_str → player display name
    mention_players: bool = True
    certificates:    bool = True
    auto_pause:      bool = False
    unanswered_streak: int = 0
    part_start: int = -1                    # first question index of this part (-1 = not a part)
    pinned:    bool = False                 # lobby/control message pinned by the bot
    lobby_page: str = "main"                # main | settings (not saved)

    def to_doc(self) -> Dict:
        return {
            "chat_id": self.chat_id, "chat_title": self.chat_title, "thread_id": self.thread_id,
            "title": self.title, "questions": self.questions, "secs": self.secs,
            "started_by": self.started_by, "starter_name": self.starter_name, "set_id": self.set_id,
            "idx": self.idx, "status": self.status, "scores": self.scores, "ctrl_msg_id": self.ctrl_msg_id,
            "negative_rate": self.negative_rate, "marks_per_q": self.marks_per_q,
            "user_history": self.user_history, "ready_players": self.ready_players,
            "mention_players": self.mention_players, "certificates": self.certificates,
            "auto_pause": self.auto_pause, "part_start": self.part_start, "pinned": self.pinned,
        }

    @classmethod
    def from_doc(cls, d: Dict) -> "Session":
        return cls(
            chat_id=d["chat_id"], chat_title=d.get("chat_title", ""), title=d.get("title", "Quiz"),
            questions=d.get("questions") or [], secs=int(d.get("secs") or BOT_SECS),
            started_by=d.get("started_by", 0), starter_name=d.get("starter_name", ""),
            thread_id=d.get("thread_id"), set_id=d.get("set_id", ""),
            idx=int(d.get("idx") or 0), status=d.get("status", "running"),
            scores=d.get("scores") or {}, ctrl_msg_id=d.get("ctrl_msg_id"),
            negative_rate=float(d.get("negative_rate") or 0.0),
            marks_per_q=float(d.get("marks_per_q") or 1.0),
            user_history=d.get("user_history") or {},
            ready_players=d.get("ready_players") or {},
            mention_players=bool(d.get("mention_players", True)),
            certificates=bool(d.get("certificates", True)),
            auto_pause=bool(d.get("auto_pause", False)),
            part_start=int(d["part_start"]) if d.get("part_start") is not None else -1,
            pinned=bool(d.get("pinned")),
        )

    def record(self, poll_id: str, uid: int, name: str, option_ids) -> bool:
        """True when the poll belongs to this session (answer counted or ignored)."""
        info = self.polls.get(poll_id)
        if not info:
            return False
        if (poll_id, uid) in self.answered or not option_ids:
            return True
        self.answered.add((poll_id, uid))
        q_idx, correct, sent = info
        sc = self.scores.setdefault(str(uid), {"name": name, "correct": 0, "wrong": 0, "score": 0.0, "time": 0.0})
        sc["name"] = name or sc.get("name", "")
        is_right = (option_ids[0] == correct)
        if is_right:
            sc["correct"] += 1
            sc["time"] = round(sc["time"] + max(0.0, time.monotonic() - sent), 2)
        else:
            sc["wrong"] += 1

        # Calculate net score with negative marking
        sc["score"] = round(sc["correct"] * self.marks_per_q - sc["wrong"] * self.negative_rate, 2)

        # Record answer history for personal review (/result)
        if 0 <= q_idx < len(self.questions):
            q_data = self.questions[q_idx]
            q_text = str(q_data.get("question") or "")
            opts = [str(o) for o in (q_data.get("options") or [])]
            selected_txt = opts[option_ids[0]] if 0 <= option_ids[0] < len(opts) else "Option"
            correct_txt = opts[correct] if 0 <= correct < len(opts) else "Option"
            exp = str(q_data.get("explanation") or "").strip()
            self.user_history.setdefault(str(uid), []).append({
                "q_num": q_idx + 1,
                "question": q_text,
                "selected": selected_txt,
                "correct": correct_txt,
                "is_correct": is_right,
                "explanation": exp
            })
        return True

    def ranking(self) -> List[Tuple[int, Dict]]:
        rows = [(int(u), s) for u, s in self.scores.items()]
        # Sort by points descending, then total time ascending, then wrong answers ascending
        return sorted(rows, key=lambda r: (-r[1].get("score", r[1]["correct"]), r[1]["time"], r[1]["wrong"]))


def _kb(s: Session) -> InlineKeyboardMarkup:
    c = s.chat_id
    toggle = (InlineKeyboardButton("▶️ Resume", callback_data=f"gq|resume|{c}", style="success")
              if s.status == "paused" else
              InlineKeyboardButton("⏸ Pause", callback_data=f"gq|pause|{c}", style="primary"))
    return InlineKeyboardMarkup([[toggle, InlineKeyboardButton("⏭ Skip", callback_data=f"gq|skip|{c}", style="primary")],
                                 [InlineKeyboardButton("⏹ Stop", callback_data=f"gq|stop|{c}", style="danger")]])


PART_ASK      = 50       # a quiz bigger than this is played in parts (e.g. 200 today, next 200 tomorrow)
PART_SIZES    = (25, 50, 100, 200)
SCHEDULE_PART = 100      # a scheduled big quiz plays its next 100 questions
USE_LOBBY     = True     # "I'm ready" lobby before a group quiz (tests switch it off)
LOBBY_TIMEOUT = 120      # auto-start lobby after 120s if host hasn't tapped Start Now


def _lobby_text(s: Session, extra_note: str = "") -> str:
    n_ready = len(s.ready_players)
    neg = f"−{s.negative_rate}" if s.negative_rate > 0 else "OFF"
    if s.ready_players:
        names = [UI.mention(int(uid), name) for uid, name in list(s.ready_players.items())[:30]]
        more = f" <i>+{n_ready - 30} more</i>" if n_ready > 30 else ""
        ready = "<blockquote expandable>" + " · ".join(names) + more + "</blockquote>"
    else:
        ready = "<blockquote><i>Waiting for players…</i></blockquote>"
    note = f"{extra_note}\n" if extra_note else ""
    return (f"🎯 <b>QUIZ LOBBY</b> — {html.escape(s.title)}\n"
            f"<blockquote>❓ Questions › {len(s.questions)}\n⏱ Timer › {s.secs}s each\n"
            f"⚖️ Negative › {neg}\n👑 Host › {UI.mention(s.started_by, s.starter_name or 'Host')}</blockquote>\n"
            f"🙋 <b>Ready ({n_ready})</b>\n{ready}\n{note}"
            f"<i>Tap “I'm Ready” to play. The host starts the quiz.</i>")


def _lobby_kb(s: Session) -> InlineKeyboardMarkup:
    c = s.chat_id
    if s.lobby_page == "settings":
        on = lambda flag: "✅" if flag else "▫️"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{on(s.negative_rate > 0)} Negative −0.25", callback_data=f"gq|tog_neg|{c}"),
             InlineKeyboardButton(f"{on(s.certificates)} Certificates", callback_data=f"gq|tog_cert|{c}")],
            [InlineKeyboardButton(f"{on(s.mention_players)} Mention players", callback_data=f"gq|tog_men|{c}"),
             InlineKeyboardButton(f"{on(s.auto_pause)} Auto pause", callback_data=f"gq|tog_ap|{c}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"gq|lback|{c}", style="danger")],
        ])
    n_ready = len(s.ready_players)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🙋 I'm Ready ({n_ready})", callback_data=f"gq|ready|{c}", style="success")],
        [InlineKeyboardButton("▶️ Start Now", callback_data=f"gq|launch|{c}", style="primary"),
         InlineKeyboardButton("⏰ Start in 30s", callback_data=f"gq|autostart|{c}", style="primary")],
        [InlineKeyboardButton("⚙️ Settings", callback_data=f"gq|lset|{c}", style="primary"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"gq|cancel_lobby|{c}", style="danger")],
    ])


def _medal(i: int) -> str:
    return ("🥇", "🥈", "🥉")[i] if i < 3 else f"{i + 1}."


PER_RESULT_MSG = 60      # players per final-result message (Telegram: 4096 chars)
_PRAISE = ("👑 <b>CHAMPION!</b> Outstanding performance — the crown is yours!",
           "🔥 <b>Brilliant runner-up!</b> So close to the top — superb!",
           "⭐ <b>Great job, 3rd place!</b> A podium finish — well played!")


class GroupQuizMixin:

    # ── helpers ──────────────────────────────────────────────────────────

    async def _gq_db(self, fn: str, *args, default=None):
        if not self.db or not hasattr(self.db, fn):
            return default
        try:
            return await asyncio.to_thread(getattr(self.db, fn), *args)
        except Exception as e:
            logger.warning(f"[GQUIZ] {fn} failed: {e}")
            return default

    async def _gq_save(self, s: Session) -> None:
        if s.status in ("lobby", "running", "paused"):
            await self._gq_db("save_quiz_session", s.to_doc())
        else:
            await self._gq_db("delete_quiz_session", s.chat_id)

    async def _gq_is_admin(self, context, chat_id: int, user, msg=None) -> bool:
        if msg is not None and getattr(getattr(msg, "sender_chat", None), "id", None) == chat_id:
            return True                                    # anonymous group admin
        return await self._bt_is_group_admin(context, chat_id, user.id)

    def _gq_head(self, s: Session) -> str:
        neg = f"\n⚖️ Negative › -{s.negative_rate} marks" if s.negative_rate > 0 else ""
        return (f"📚 <b>{html.escape(s.title)}</b>\n"
                f"<blockquote>❓ Questions › {len(s.questions)}\n⏱ Timer › {s.secs}s each{neg}\n"
                f"👤 Started by › {UI.mention(s.started_by, s.starter_name or 'admin')}</blockquote>\n")

    def _gq_board(self, s: Session, top: int) -> str:
        rank = s.ranking()
        if not rank:
            return "<i>No answers yet.</i>"
        total = max(1, min(s.idx, len(s.questions)))
        lines = []
        for i, (uid, sc) in enumerate(rank[:top]):
            pts = f" · <b>{sc.get('score', sc['correct'])} pts</b>" if s.negative_rate > 0 else ""
            lines.append(f"{_medal(i)} {UI.mention(uid, sc.get('name') or 'Player')} › "
                         f"<b>{sc['correct']}</b>/{total} ({round(100 * sc['correct'] / total)}%){pts}")
        return "<blockquote>" + "\n".join(lines) + "</blockquote>"

    async def _gq_send(self, context, s: Session, text: str, kb=None):
        kw = {"parse_mode": ParseMode.HTML}
        if kb is not None:
            kw["reply_markup"] = kb
        for _attempt in range(3):
            call_kw = dict(kw)
            if s.thread_id:
                call_kw["message_thread_id"] = s.thread_id
            try:
                return await context.bot.send_message(s.chat_id, text, **call_kw)
            except Exception as e:
                err = str(e).lower()
                if s.thread_id and any(w in err for w in ("topic", "thread", "closed")):
                    next_tid = await self._gq_db("mark_group_topic_closed", s.chat_id, s.thread_id, default=None)
                    logger.info(f"[GQUIZ] topic {s.thread_id} in {s.chat_id} closed — switching to {next_tid or 'Main'}")
                    s.thread_id = next_tid
                    continue
                logger.warning(f"[GQUIZ] {s.chat_id} send failed: {e}")
                return None
        return None

    async def _gq_edit_ctrl(self, context, s: Session, note: str = "") -> None:
        if not s.ctrl_msg_id:
            return
        try:
            await context.bot.edit_message_text(
                self._gq_head(s) + note, chat_id=s.chat_id, message_id=s.ctrl_msg_id,
                parse_mode=ParseMode.HTML,
                reply_markup=_kb(s) if s.status in ("running", "paused") else None)
        except Exception as e:
            if "not modified" not in str(e).lower():
                logger.debug(f"[GQUIZ] ctrl edit failed: {e}")

    # ── start ────────────────────────────────────────────────────────────

    async def cmd_startquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/startquiz [quiz id] in a group (admins)."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not user or not chat:
            return
        if chat.type == "private":
            await self._reply(update, "👥 <b>Run this in your group:</b> add me there and send /startquiz, "
                                      "or open a quiz in /myquiz → 👥 Start in Group.")
            return
        if not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can start a quiz.</b>")
            return
        if is_active(chat.id):
            await self._reply(update, "⏳ <b>A quiz is already running here.</b> Stop it first with ⏹.")
            return
        arg = (context.args or [""])[0] if getattr(context, "args", None) else ""
        if arg:
            await self._gq_start_set(update, context, arg.removeprefix("gq_"))
            return
        sets = await self._gq_db("list_quiz_sets", user.id, 8, 0, default=[])
        rows = [[InlineKeyboardButton(f"📝 {x.get('title', 'Quiz')[:28]} · {x.get('count', 0)}Q",
                                      callback_data=f"gq|set|{user.id}|{x['id']}", style="primary")]
                for x in (sets if isinstance(sets, list) else [])]
        rows.append([InlineKeyboardButton(f"🤖 {n} Q", callback_data=f"gq|bot|{user.id}|{n}", style="primary")
                     for n in BOT_COUNTS])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"gq|close|{user.id}", style="danger")])
        await self._reply(update, "👥 <b>UNIVERSAL GROUP QUIZ</b>\n<i>Pick one of your quizzes, or bot questions.</i>",
                          reply_markup=InlineKeyboardMarkup(rows))

    async def gq_start_from_link(self, update: Update, context, set_id: str) -> None:
        """/start gq_<id> in a group (the quiz card's Start in Group link)."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can start a quiz.</b>")
            return
        if is_active(chat.id):
            await self._reply(update, "⏳ <b>A quiz is already running here.</b>")
            return
        await self._gq_start_set(update, context, set_id)

    async def _gq_start_set(self, update, context, set_id: str, auto_part: int = 0) -> None:
        """auto_part > 0 (scheduled quiz): a big quiz plays its next part of that
        size by itself instead of asking which part."""
        m = re.fullmatch(r"bot(\d{1,3})", set_id or "")
        if m:                                             # shared bot quiz: gq_bot20
            pool = list(self.quiz_manager.questions or [])
            n = max(1, min(int(m.group(1)), 50, len(pool)))
            if len(pool) < 3:
                await self._reply(update, "⚠️ <b>Not enough questions yet.</b>")
                return
            await self._gq_begin(context, update.effective_chat.id, get_thread_id(update), update.effective_user,
                                 f"Bot Quiz · {n} questions", random.sample(pool, n), BOT_SECS, "")
            return
        qs = await self._gq_db("get_quiz_set", set_id)
        if isinstance(qs, dict) and len(qs.get("questions") or []) > PART_ASK:
            chat_id = update.effective_chat.id
            if not auto_part:
                await self._reply(update, *await self._gq_part_view(update.effective_user.id, set_id, qs, chat_id))
                return
            allq = list(qs["questions"])
            start = int(((qs.get("progress") or {}).get(str(chat_id))) or 0)
            if start >= len(allq):
                start = 0                                    # finished earlier → start over
            chunk = allq[start:start + auto_part]
            await self._gq_begin(context, chat_id, get_thread_id(update), update.effective_user,
                                 f"{qs.get('title') or 'Quiz'} · Q{start + 1}–Q{start + len(chunk)}",
                                 chunk, int(qs.get("secs") or BOT_SECS), set_id, part_start=start)
            return
        if not isinstance(qs, dict) or not qs.get("questions"):
            await self._reply(update, "⚠️ <b>Quiz not found.</b> Check the quiz ID in /myquiz.")
            return
        await self._gq_begin(context, update.effective_chat.id, get_thread_id(update), update.effective_user,
                             str(qs.get("title") or "Quiz"), list(qs["questions"]),
                             int(qs.get("secs") or BOT_SECS), set_id)

    async def _gq_begin(self, context, chat_id: int, thread_id, user, title: str,
                        questions: List[Dict], secs: int, set_id: str = "",
                        negative_rate: float = 0.0, skip_lobby: Optional[bool] = None,
                        part_start: int = -1) -> Optional[Session]:
        if is_active(chat_id):
            return None
        if skip_lobby is None:
            skip_lobby = not USE_LOBBY
        if self.db and hasattr(self.db, "get_group_open_thread"):
            res_tid = await self._gq_db("get_group_open_thread", chat_id, thread_id, default=thread_id)
            if isinstance(res_tid, int) or res_tid is None:
                thread_id = res_tid
        random.shuffle(questions)
        chat_title = ""
        try:
            ch = await context.bot.get_chat(chat_id)
            chat_title = getattr(ch, "title", "") or ""
        except Exception:
            pass
        s = Session(
            chat_id=chat_id, chat_title=chat_title, title=title[:60], questions=questions,
            secs=max(5, min(secs, 600)), started_by=user.id, starter_name=self._bt_name(user),
            thread_id=thread_id, set_id=set_id, negative_rate=negative_rate,
            status="running" if skip_lobby else "lobby", part_start=part_start,
        )
        # Host is automatically the 1st ready player
        s.ready_players[str(user.id)] = s.starter_name or "Host"
        ACTIVE[chat_id] = s
        if skip_lobby:
            sent = await self._gq_send(context, s, self._gq_head(s) + f"🚀 <b>Starting in {START_DELAY}s…</b>", _kb(s))
            s.ctrl_msg_id = getattr(sent, "message_id", None)
            await self._gq_save(s)
            asyncio.create_task(self._gq_run(s, context))
        else:
            sent = await self._gq_send(context, s, _lobby_text(s), _lobby_kb(s))
            s.ctrl_msg_id = getattr(sent, "message_id", None)
            await self._gq_pin(context, s)
            await self._gq_save(s)                       # lobby survives a restart too
            logger.info(f"[GQUIZ] {chat_id} opened lobby '{title}' ({len(questions)} Q, {s.secs}s) by {user.id}")
            self._gq_lobby_watchdog(context, s)
        return s

    def _gq_lobby_watchdog(self, context, s: Session) -> None:
        if LOBBY_TIMEOUT <= 0:
            return

        async def _auto():
            await asyncio.sleep(LOBBY_TIMEOUT)
            if ACTIVE.get(s.chat_id) is s and s.status == "lobby":
                await self._gq_launch_from_lobby(context, s)
        asyncio.create_task(_auto())

    async def _gq_pin(self, context, s: Session) -> None:
        """Pin the lobby (= control panel) WITH a notification, so every member
        hears about the quiz once. Needs 'Pin messages' right;
        the 'pinned a message' notice is deleted by gq_on_pin_notice."""
        if not s.ctrl_msg_id:
            return
        try:
            await context.bot.pin_chat_message(s.chat_id, s.ctrl_msg_id, disable_notification=False)
            s.pinned = True
        except Exception as e:
            logger.debug(f"[GQUIZ] {s.chat_id} pin skipped: {e}")

    async def _gq_unpin(self, context, s: Session) -> None:
        if not s.pinned or not s.ctrl_msg_id:
            return
        try:
            await context.bot.unpin_chat_message(s.chat_id, message_id=s.ctrl_msg_id)
        except Exception as e:
            logger.debug(f"[GQUIZ] {s.chat_id} unpin failed: {e}")
        s.pinned = False

    async def gq_on_pin_notice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete the 'Miss Quiz pinned a message' notice the bot itself caused."""
        msg = update.effective_message
        me = getattr(context.bot, "id", None)
        if msg and msg.from_user and me and msg.from_user.id == me:
            try:
                await msg.delete()
            except Exception:
                pass

    async def _gq_part_view(self, uid: int, sid: str, qs: Dict, chat_id: int, start: Optional[int] = None):
        """Big quiz → choose how many to play now; continues where this group stopped."""
        total = len(qs.get("questions") or [])
        if start is None:
            start = int(((qs.get("progress") or {}).get(str(chat_id))) or 0)
        start = max(0, min(start, total))
        title = html.escape(str(qs.get("title") or "Quiz"))
        done = f"Q1–Q{start}" if start else "none yet"
        text = (f"📚 <b>{title}</b>\n<blockquote>❓ Total › {total} questions\n"
                f"✅ Played here › {done}\n▶️ Next › " + (f"from Q{start + 1}" if start < total else "all done 🎉") +
                "</blockquote>\n<i>How many questions now? The rest stay for next time.</i>")
        left = total - start
        sizes = [n for n in PART_SIZES if n < left] + ([left] if 0 < left <= PART_SIZES[-1] else [])
        btns = [InlineKeyboardButton(f"▶️ {n} Q · Q{start + 1}–Q{start + n}",
                                     callback_data=f"gq|part|{uid}|{sid}|{n}|{start}", style="primary")
                for n in sizes]
        rows = [btns[k:k + 2] for k in range(0, len(btns), 2)]
        if start:
            rows.append([InlineKeyboardButton("🔁 Start again from Q1", callback_data=f"gq|part|{uid}|{sid}|0|0")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"gq|close|{uid}", style="danger")])
        return text, InlineKeyboardMarkup(rows)

    async def _gq_launch_from_lobby(self, context, s: Session) -> None:
        if s.status != "lobby":
            return
        s.status = "running"
        await self._gq_edit_ctrl(context, s, f"🚀 <b>Quiz Live! ({len(s.ready_players)} players joined)</b>")
        if s.mention_players and s.ready_players:
            mentions = ", ".join(
                UI.mention(int(uid), name) for uid, name in list(s.ready_players.items())[:25]
            )
            await self._gq_send(
                context, s,
                f"🔔 <b>TOURNAMENT STARTING IN {START_DELAY} SECONDS!</b>\n"
                f"Get ready: {mentions} 🚀"
            )
        await self._gq_save(s)
        asyncio.create_task(self._gq_run(s, context))

    # ── loop ─────────────────────────────────────────────────────────────

    async def _gq_run(self, s: Session, context, delay: Optional[float] = None) -> None:
        try:
            await asyncio.sleep(START_DELAY if delay is None else delay)
            n = len(s.questions)
            while s.idx < n and s.status in ("running", "paused"):
                if s.status == "paused":
                    s.wake.clear()
                    try:
                        await asyncio.wait_for(s.wake.wait(), timeout=max(1.0, IDLE_PAUSE - (time.monotonic() - s.paused_at)))
                    except asyncio.TimeoutError:
                        s.status = "stopped"
                    continue
                q, num = s.questions[s.idx], s.idx + 1
                s.idx += 1
                ans_before = len(s.answered)
                if not await self._gq_ask(context, s, q, num, n):
                    s.status = "stopped"
                    await self._gq_send(context, s, "⚠️ <b>Quiz stopped:</b> I can't send polls here "
                                                    "(check my permissions).")
                    break
                await self._gq_save(s)
                s.wake.clear()
                s.skip = False
                try:
                    await asyncio.wait_for(s.wake.wait(), timeout=s.secs + SLACK)
                except asyncio.TimeoutError:
                    pass
                if s.status == "stopped":
                    break
                # Check Auto-Pause if nobody answered this poll
                if len(s.answered) == ans_before:
                    s.unanswered_streak += 1
                    if s.auto_pause and s.unanswered_streak >= 3 and s.status == "running" and s.idx < n:
                        s.status = "paused"
                        s.paused_at = time.monotonic()
                        s.unanswered_streak = 0
                        await self._gq_edit_ctrl(context, s, "⏸ <b>Auto-Paused (no answers for 3 questions).</b>")
                        await self._gq_send(
                            context, s,
                            "⏸ <b>Quiz Auto-Paused</b> because 3 questions went unanswered.\n"
                            "<i>Tap ▶️ Resume on the control panel above when players are ready!</i>",
                            _kb(s)
                        )
                        continue
                else:
                    s.unanswered_streak = 0
                if num % LB_EVERY == 0 and num < n:
                    await self._gq_send(context, s, f"📊 <b>After {num} questions</b>\n{self._gq_board(s, 5)}")
                await asyncio.sleep(GAP)
            await self._gq_finish(context, s)
        except Exception as e:
            logger.error(f"[GQUIZ] {s.chat_id} crashed: {e}", exc_info=True)
            s.status = "stopped"
            await self._gq_finish(context, s)

    async def _gq_ask(self, context, s: Session, q: Dict, num: int, n: int) -> bool:
        opts = [str(o)[:100] for o in q.get("options") or []][:10]
        correct = int(q.get("correct_answer", 0))
        if len(opts) < 2:
            return True                                    # broken question: just skip it
        if not 0 <= correct < len(opts):
            correct = 0
        cat_e = UI.cat_emoji(q.get("category", "")) if q.get("category") else "💡"
        head = f"[{num}/{n}] {cat_e} "
        qtext = str(q.get("question") or "")
        if len(head) + len(qtext) > 300:                   # long question: text first
            await self._gq_send(context, s, f"❓ <b>Q{num}/{n} {cat_e}</b>\n{html.escape(qtext)}")
            qtext = "Read the question above ⬆️"
        kw = dict(chat_id=s.chat_id, question=head + qtext, options=opts, type=Poll.QUIZ,
                  correct_option_id=correct, is_anonymous=False, open_period=s.secs)
        exp = str(q.get("explanation") or "").strip()
        if exp:
            kw["explanation"] = exp[:200]
        msg = None
        for _attempt in range(3):
            call_kw = dict(kw)
            if s.thread_id:
                call_kw["message_thread_id"] = s.thread_id
            try:
                msg = await context.bot.send_poll(**call_kw)
                break
            except Exception as e:
                err = str(e).lower()
                if s.thread_id and any(w in err for w in ("topic", "thread", "closed")):
                    next_tid = await self._gq_db("mark_group_topic_closed", s.chat_id, s.thread_id, default=None)
                    logger.info(f"[GQUIZ] poll topic {s.thread_id} in {s.chat_id} closed — switching to {next_tid or 'Main'}")
                    s.thread_id = next_tid
                    continue
                logger.warning(f"[GQUIZ] {s.chat_id} poll failed: {e}")
                return False
        if not msg:
            return False
        pid = str(msg.poll.id)
        s.polls[pid] = (num - 1, correct, time.monotonic())
        self._gq_polls()[pid] = s.chat_id
        return True

    def _gq_polls(self) -> Dict[str, int]:
        if not hasattr(self, "_gq_poll_map"):
            self._gq_poll_map: Dict[str, int] = {}
        return self._gq_poll_map

    def gq_on_poll_answer(self, poll_id: str, user, option_ids) -> bool:
        """Hook for handle_poll_answer. True = this poll belongs to a group quiz."""
        chat_id = self._gq_polls().get(poll_id)
        s = ACTIVE.get(chat_id) if chat_id is not None else None
        if not s:
            return chat_id is not None
        return s.record(poll_id, user.id, self._bt_name(user), list(option_ids or []))

    def _gq_share_kb(self, context, s: Session) -> InlineKeyboardMarkup:
        """Play again (admins) + Share Quiz + Download HTML Report."""
        key = s.set_id or f"bot{len(s.questions)}"
        link = f"https://t.me/{getattr(context.bot, 'username', '')}?startgroup=gq_{key}"
        msg = (f"🏆 Play “{s.title}” in your group with Miss Quiz! "
               f"{len(s.questions)} questions · {s.secs}s each. Tap the link and pick your group 👇")
        share = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote(msg)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Play again", callback_data=f"gq|again|{s.chat_id}", style="success"),
             InlineKeyboardButton("📤 Share Quiz", url=share, style="primary")],
            [InlineKeyboardButton("🌐 Download HTML Report", callback_data=f"gq|html|{s.chat_id}", style="primary")],
        ])

    def _gq_result_texts(self, s: Session, stopped: bool) -> List[str]:
        """Podium with greetings for the top 3, then EVERY player with marks breakdown."""
        rank = s.ranking()
        total = max(1, min(s.idx, len(s.questions)))
        neg_note = f" (Negative: -{s.negative_rate})" if s.negative_rate > 0 else ""
        head = (f"🏆 <b>FINAL RESULT</b> — {html.escape(s.title)}\n"
                + (f"<i>Stopped after {s.idx} of {len(s.questions)} questions.</i>\n" if stopped else ""))
        if not rank:
            return [head + "<i>Nobody answered this time. Play again? 🙂</i>"]
        podium = ["🎉 <b>Congratulations to our winners!</b>"]
        for i, (uid, sc) in enumerate(rank[:3]):
            pts_str = f" · <b>{sc.get('score', sc['correct'])} pts</b>" if s.negative_rate > 0 else ""
            podium.append(f"{_medal(i)} {UI.mention(uid, sc.get('name') or 'Player')} › "
                          f"<b>{sc['correct']}</b>/{total}{pts_str}\n{_PRAISE[i]}")

        def format_line(i, uid, sc):
            if s.negative_rate > 0:
                return (f"{_medal(i)} {UI.mention(uid, sc.get('name') or 'Player')} › "
                        f"<b>{sc.get('score', sc['correct'])} pts</b> ({sc['correct']}/{total}) · "
                        f"✅ {sc['correct']} ❌ {sc.get('wrong', 0)}")
            else:
                return (f"{_medal(i)} {UI.mention(uid, sc.get('name') or 'Player')} › "
                        f"<b>{sc['correct']}</b>/{total} · {round(100 * sc['correct'] / total)}% · "
                        f"✅ {sc['correct']} ❌ {sc.get('wrong', 0)}")

        lines = [format_line(i, uid, sc) for i, (uid, sc) in enumerate(rank)]
        foot = f"\n👥 Players › {len(rank)} · ❓ Questions › {total}{neg_note}\n💡 <i>Tip: Send /result to get your wrong answers review in DM!</i>"
        texts = []
        for k in range(0, len(lines), PER_RESULT_MSG):
            chunk = "<blockquote expandable>" + "\n".join(lines[k:k + PER_RESULT_MSG]) + "</blockquote>"
            if k == 0:
                texts.append(head + "\n\n".join(podium) + "\n\n📋 <b>All players</b>\n" + chunk)
            else:
                texts.append(f"📋 <b>All players</b> ({k + 1}–{min(k + PER_RESULT_MSG, len(lines))})\n" + chunk)
        texts[-1] += foot
        return texts

    async def _gq_finish(self, context, s: Session) -> None:
        if ACTIVE.get(s.chat_id) is s:
            ACTIVE.pop(s.chat_id, None)
        await self._gq_unpin(context, s)
        asked = min(s.idx, len(s.questions))
        if s.set_id and s.part_start >= 0 and asked:      # big quiz: next time continue after this part
            await self._gq_db("set_quiz_progress", s.set_id, s.chat_id, s.part_start + asked)
        stopped = s.status == "stopped"
        s.status = "done" if not stopped else "stopped"
        await self._gq_save(s)
        for pid in list(s.polls):
            self._gq_polls().pop(pid, None)
        if not hasattr(self, "_gq_last"):
            self._gq_last: Dict[int, Session] = {}
        self._gq_last[s.chat_id] = s
        texts = self._gq_result_texts(s, stopped)
        for k, text in enumerate(texts):
            await self._gq_send(context, s, text, self._gq_share_kb(context, s) if k == len(texts) - 1 else None)
        await self._gq_edit_ctrl(context, s, "🏁 <b>Finished.</b>" if not stopped else "⏹ <b>Stopped.</b>")

        # Send Top-3 Winner Certificate Image Cards when certificates=True
        import inspect as _inspect
        rank = s.ranking()
        if s.certificates and rank and not stopped and _inspect.iscoroutinefunction(getattr(context.bot, "send_photo", None)):
            try:
                import io as _io
                from telegram import InputFile as _InputFile
                from src.utils.certificate import generate_certificate_image
                total_asked = max(1, min(s.idx, len(s.questions)))
                bot_uname = getattr(context.bot, "username", "MissQuizBot") or "MissQuizBot"
                medals = ("🥇", "🥈", "🥉")
                for idx_r, (uid, sc) in enumerate(rank[:3]):
                    r_pos = idx_r + 1
                    p_name = sc.get("name") or "Player"
                    cor = int(sc.get("correct", 0))
                    pts = float(sc.get("score", cor))
                    t_sec = float(sc.get("time", 0.0))
                    png_bytes = await asyncio.to_thread(
                        generate_certificate_image,
                        player_name=p_name,
                        quiz_title=s.title,
                        group_title=s.chat_title or "Tournament Group",
                        host_name=s.starter_name or "Host",
                        rank_pos=r_pos,
                        correct=cor,
                        total_q=total_asked,
                        score_pts=pts,
                        time_secs=t_sec,
                        bot_username=bot_uname,
                    )
                    kw_photo = {
                        "chat_id": s.chat_id,
                        "photo": _InputFile(_io.BytesIO(png_bytes), filename=f"certificate_rank_{r_pos}.png"),
                        "caption": f"{medals[idx_r]} Congratulations {UI.mention(uid, p_name)}!",
                        "parse_mode": ParseMode.HTML,
                    }
                    if s.thread_id:
                        kw_photo["message_thread_id"] = s.thread_id
                    await context.bot.send_photo(**kw_photo)
                    await asyncio.sleep(0.4)
            except Exception as ce:
                logger.warning(f"[GQUIZ] certificate generation failed: {ce}")

        logger.info(f"[GQUIZ] {s.chat_id} ended ({'stopped' if stopped else 'done'}) players={len(s.scores)}")

    # ── speed control commands ───────────────────────────────────────────

    async def cmd_slow(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/slow in group: increase question timer by 10s (admins)."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        s = ACTIVE.get(chat.id) if chat else None
        if not s:
            await self._reply(update, "ℹ️ <b>No active quiz running here.</b>")
            return
        if user.id != s.started_by and not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can adjust quiz speed.</b>")
            return
        s.secs = min(300, s.secs + 10)
        await self._gq_save(s)
        await self._reply(update, f"🐢 <b>Speed decreased:</b> timer set to <b>{s.secs}s</b> per question.")

    async def cmd_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/fast in group: decrease question timer by 5s (admins)."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        s = ACTIVE.get(chat.id) if chat else None
        if not s:
            await self._reply(update, "ℹ️ <b>No active quiz running here.</b>")
            return
        if user.id != s.started_by and not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can adjust quiz speed.</b>")
            return
        s.secs = max(5, s.secs - 5)
        await self._gq_save(s)
        await self._reply(update, f"⚡ <b>Speed increased:</b> timer set to <b>{s.secs}s</b> per question.")

    async def cmd_normal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/normal in group: reset question timer to default (admins)."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        s = ACTIVE.get(chat.id) if chat else None
        if not s:
            await self._reply(update, "ℹ️ <b>No active quiz running here.</b>")
            return
        if user.id != s.started_by and not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can adjust quiz speed.</b>")
            return
        s.secs = BOT_SECS
        await self._gq_save(s)
        await self._reply(update, f"⏱️ <b>Speed reset:</b> timer restored to default <b>{s.secs}s</b> per question.")

    # ── personal /result & /compare ──────────────────────────────────────

    async def cmd_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/result - get personal detailed performance review and wrong answers in DM."""
        user, chat = update.effective_user, update.effective_chat
        if not user or not chat:
            return

        s = ACTIVE.get(chat.id)
        if not s and hasattr(self, "_gq_last"):
            s = self._gq_last.get(chat.id)

        if not s:
            for past in getattr(self, "_gq_last", {}).values():
                if str(user.id) in past.scores:
                    s = past
                    break

        if not s or str(user.id) not in s.scores:
            await self._reply(update, "ℹ️ <b>No quiz record found for you.</b> Participate in a quiz first!")
            return

        sc = s.scores[str(user.id)]
        total = max(1, min(s.idx, len(s.questions)))
        correct = sc.get("correct", 0)
        wrong = sc.get("wrong", 0)
        score = sc.get("score", correct)
        accuracy = round(100 * correct / total) if total else 0
        ans_list = s.user_history.get(str(user.id), [])

        report = [
            f"📊 <b>Personal Quiz Scorecard</b>",
            f"<b>Quiz:</b> {html.escape(s.title)}",
            f"━━━━━━━━━━━━━━━━━━",
            f"🎯 <b>Net Score:</b> <b>{score} pts</b>" + (f" (Negative: -{s.negative_rate})" if s.negative_rate > 0 else ""),
            f"✅ <b>Correct:</b> {correct}",
            f"❌ <b>Wrong:</b> {wrong}",
            f"⏳ <b>Total Attempted:</b> {correct + wrong} / {total}",
            f"📈 <b>Accuracy:</b> {accuracy}%",
            f"⏱️ <b>Total Time:</b> {sc.get('time', 0.0)}s",
            f"━━━━━━━━━━━━━━━━━━"
        ]

        wrong_answers = [item for item in ans_list if not item.get("is_correct")]
        if wrong_answers:
            report.append("\n❌ <b>Questions you missed:</b>\n")
            for w in wrong_answers[:15]:
                q_text = html.escape(w['question'][:120])
                your_ans = html.escape(str(w.get('selected', '')))
                right_ans = html.escape(str(w.get('correct', '')))
                exp = html.escape(str(w.get('explanation', '')[:100]))
                exp_text = f"\n   💡 <i>{exp}</i>" if exp else ""
                report.append(
                    f"❓ <b>Q{w['q_num']}:</b> {q_text}\n"
                    f"   ❌ Your Answer: <code>{your_ans}</code>\n"
                    f"   ✅ Correct: <b>{right_ans}</b>{exp_text}\n"
                )
        else:
            report.append("\n🌟 <b>Flawless! Zero wrong answers recorded.</b>")

        full_text = "\n".join(report)

        if chat.type != "private":
            try:
                await context.bot.send_message(user.id, full_text, parse_mode=ParseMode.HTML)
                await self._reply(update, f"📩 {UI.mention(user.id, user.first_name)}, I have sent your detailed scorecard to your DM!")
            except Exception:
                bot_user = getattr(context.bot, "username", "")
                await self._reply(update, f"⚠️ {UI.mention(user.id, user.first_name)}, please start me in private chat first (DM) so I can send your scorecard: @{bot_user}")
        else:
            await self._reply(update, full_text)

    async def cmd_compare(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/compare @username - compare quiz stats between two users in the group."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not user or not chat or chat.type == "private":
            await self._reply(update, "👥 Use <code>/compare</code> in a group after or during a quiz!")
            return

        s = ACTIVE.get(chat.id)
        if not s and hasattr(self, "_gq_last"):
            s = self._gq_last.get(chat.id)

        if not s:
            await self._reply(update, "ℹ️ <b>No active or recent quiz session found here.</b>")
            return

        target_uid = None
        target_name = None

        if msg.reply_to_message and msg.reply_to_message.from_user:
            t_user = msg.reply_to_message.from_user
            if not t_user.is_bot:
                target_uid = t_user.id
                target_name = t_user.first_name

        if not target_uid and context.args:
            arg = context.args[0].lstrip("@").lower()
            for uid_str, sc in s.scores.items():
                if sc.get("name", "").lower() == arg:
                    target_uid = int(uid_str)
                    target_name = sc.get("name")
                    break

        if not target_uid:
            await self._reply(update, "👉 <b>Usage:</b> Reply to someone with <code>/compare</code> or use <code>/compare @username</code>")
            return

        if target_uid == user.id:
            await self._reply(update, "😄 You cannot compare with yourself! Pick a friend or rival.")
            return

        sc1 = s.scores.get(str(user.id))
        sc2 = s.scores.get(str(target_uid))

        if not sc1 or not sc2:
            await self._reply(update, "⚠️ Both users must have answered at least one question in this quiz.")
            return

        p1_name = sc1.get("name") or user.first_name
        p2_name = target_name or sc2.get("name") or "Opponent"

        p1_score = sc1.get("score", sc1.get("correct", 0))
        p2_score = sc2.get("score", sc2.get("correct", 0))

        p1_time = sc1.get("time", 0.0)
        p2_time = sc2.get("time", 0.0)

        if p1_score > p2_score or (p1_score == p2_score and p1_time < p2_time):
            winner = f"🏆 <b>Winner:</b> {UI.mention(user.id, p1_name)} 🎉"
        elif p2_score > p1_score or (p2_score == p1_score and p2_time < p1_time):
            winner = f"🏆 <b>Winner:</b> {UI.mention(target_uid, p2_name)} 🎉"
        else:
            winner = "🤝 <b>It's an exact TIE!</b>"

        card = (
            f"⚔️ <b>QUIZ HEAD-TO-HEAD COMPARISON</b>\n"
            f"<b>Quiz:</b> {html.escape(s.title)}\n\n"
            f"<blockquote>"
            f"👤 <b>{html.escape(p1_name)}</b> vs <b>{html.escape(p2_name)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Points:</b> {p1_score} vs {p2_score}\n"
            f"✅ <b>Correct:</b> {sc1.get('correct', 0)} vs {sc2.get('correct', 0)}\n"
            f"❌ <b>Wrong:</b> {sc1.get('wrong', 0)} vs {sc2.get('wrong', 0)}\n"
            f"⏱️ <b>Time:</b> {p1_time}s vs {p2_time}s\n"
            f"</blockquote>\n\n"
            f"{winner}"
        )
        await self._reply(update, card)

    # ── buttons ──────────────────────────────────────────────────────────

    async def handle_group_quiz_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query, user = update.callback_query, update.effective_user
        if not query or not user or not query.data:
            return
        parts = query.data.split("|")
        action = parts[1] if len(parts) > 1 else ""
        msg = query.message
        chat_id = msg.chat.id if msg else 0

        if action in ("set", "bot", "close", "part"):      # picker: only who opened it
            owner = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
            if user.id != owner:
                await query.answer("This menu isn't yours — send /startquiz.", show_alert=True)
                return
            if action == "close":
                await query.answer()
                try:
                    await msg.delete()
                except Exception:
                    pass
                return
            if is_active(chat_id):
                await query.answer("A quiz is already running here.", show_alert=True)
                return
            if not await self._gq_is_admin(context, chat_id, user):
                await query.answer("Only group admins can start a quiz.", show_alert=True)
                return
            arg = parts[3] if len(parts) > 3 else ""
            if action == "part":                             # gq|part|uid|sid|count|start
                qs = await self._gq_db("get_quiz_set", arg)
                if not isinstance(qs, dict) or not qs.get("questions"):
                    await query.answer("That quiz is no longer available.", show_alert=True)
                    return
                count = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
                start = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
                allq = list(qs["questions"])
                if count <= 0 or start >= len(allq):           # "start again from Q1" → menu at 0
                    await query.answer()
                    text, kb = await self._gq_part_view(user.id, arg, qs, chat_id, start=0)
                    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                    return
                chunk = allq[start:start + count]
                await query.answer("Opening the lobby! 🚀")
                try:
                    await msg.delete()
                except Exception:
                    pass
                thread = getattr(msg, "message_thread_id", None) if getattr(msg, "is_topic_message", False) else None
                await self._gq_begin(context, chat_id, thread, user,
                                     f"{qs.get('title') or 'Quiz'} · Q{start + 1}–Q{start + len(chunk)}",
                                     chunk, int(qs.get("secs") or BOT_SECS), arg, part_start=start)
                return
            if action == "set":
                qs = await self._gq_db("get_quiz_set", arg)
                if not isinstance(qs, dict) or not qs.get("questions"):
                    await query.answer("That quiz is no longer available.", show_alert=True)
                    return
                if len(qs["questions"]) > PART_ASK:          # big quiz → play it in parts
                    await query.answer()
                    text, kb = await self._gq_part_view(user.id, arg, qs, chat_id)
                    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                    return
                title, questions, secs, sid = (str(qs.get("title") or "Quiz"), list(qs["questions"]),
                                               int(qs.get("secs") or BOT_SECS), arg)
            else:
                n = int(arg) if arg.isdigit() and int(arg) in BOT_COUNTS else BOT_COUNTS[0]
                pool = list(self.quiz_manager.questions or [])
                if len(pool) < 3:
                    await query.answer("Not enough questions yet.", show_alert=True)
                    return
                title, questions, secs, sid = (f"Bot Quiz · {min(n, len(pool))} questions",
                                               random.sample(pool, min(n, len(pool))), BOT_SECS, "")
            await query.answer("Starting! 🚀")
            try:
                await msg.delete()
            except Exception:
                pass
            thread = getattr(msg, "message_thread_id", None) if getattr(msg, "is_topic_message", False) else None
            await self._gq_begin(context, chat_id, thread, user, title, questions, secs, sid)
            return

        if action == "html":
            last = getattr(self, "_gq_last", {}).get(chat_id) or ACTIVE.get(chat_id)
            if not last:
                await query.answer("No completed quiz record found.", show_alert=True)
                return
            await query.answer("Generating your HTML scorecard...")
            try:
                import io
                from src.utils.quiz_report import render_analysis_html
                total_qs = max(1, min(last.idx, len(last.questions)))
                q_counts = {i + 1: {"correct_count": 0, "wrong_count": 0} for i in range(total_qs)}
                for hist in last.user_history.values():
                    for item in hist:
                        qn = item.get("q_num", 0)
                        if qn in q_counts:
                            if item.get("is_correct"):
                                q_counts[qn]["correct_count"] += 1
                            else:
                                q_counts[qn]["wrong_count"] += 1
                question_stats = [q_counts[i + 1] for i in range(total_qs)]

                q_doc = {
                    "quiz_name": last.title,
                    "title": last.title,
                    "qid": last.set_id or f"gq_{last.chat_id}",
                    "questions": last.questions[:total_qs],
                    "question_stats": question_stats,
                    "settings": {"duration": last.secs}
                }
                results = []
                for uid_str, sc in last.scores.items():
                    results.append({
                        "user_id": int(uid_str),
                        "user_name": sc.get("name", "Player"),
                        "name": sc.get("name", "Player"),
                        "score": sc.get("score", sc.get("correct", 0)),
                        "correct": sc.get("correct", 0),
                        "wrong": sc.get("wrong", 0),
                        "total_questions": total_qs,
                        "time_taken": round(float(sc.get("time", 0.0))),
                        "time": sc.get("time", 0.0),
                        "answers": last.user_history.get(uid_str, [])
                    })
                html_bytes, filename = await render_analysis_html(q_doc, results)
                doc_file = io.BytesIO(html_bytes)
                doc_file.name = filename
                await context.bot.send_document(
                    chat_id=user.id,
                    document=doc_file,
                    caption=f"🌐 <b>Interactive HTML Scorecard:</b> {html.escape(last.title)}\n<i>Open this .html file in any browser to review charts and questions!</i>",
                    parse_mode=ParseMode.HTML
                )
                await query.message.reply_text(
                    f"📩 {UI.mention(user.id, user.first_name)}, I have sent your interactive HTML scorecard to your DM!",
                    parse_mode=ParseMode.HTML
                )
            except Exception as ex:
                logger.error(f"[GQUIZ] HTML report failed: {ex}", exc_info=True)
                await query.answer("Please start me in DM first so I can send the file!", show_alert=True)
            return

        if action == "again":
            last = getattr(self, "_gq_last", {}).get(chat_id)
            if not last:
                await query.answer("Start a new one with /startquiz.", show_alert=True)
                return
            if not await self._gq_is_admin(context, chat_id, user):
                await query.answer("Only group admins can start a quiz.", show_alert=True)
                return
            if is_active(chat_id):
                await query.answer("A quiz is already running here.", show_alert=True)
                return
            await query.answer("Starting again! 🚀")
            await self._gq_begin(context, chat_id, last.thread_id, user, last.title,
                                 list(last.questions), last.secs, last.set_id)
            return

        s = ACTIVE.get(chat_id)
        if not s:
            await query.answer("This quiz has ended.", show_alert=True)
            return

        # ── Lobby "I'm ready!" button (ANY player in the group can tap!) ──
        if action == "ready":
            if s.status != "lobby":
                await query.answer("The quiz has already started! Answer the live polls 👇", show_alert=True)
                return
            uid_s = str(user.id)
            p_name = self._bt_name(user)
            if uid_s in s.ready_players:
                await query.answer(f"✅ You are already marked Ready! ({len(s.ready_players)} players in lobby)", show_alert=False)
                return
            s.ready_players[uid_s] = p_name
            await self._gq_save(s)
            await query.answer(f"🔥 You're Ready! ({len(s.ready_players)} joined)")
            try:
                await query.message.edit_text(_lobby_text(s), parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(s))
            except Exception:
                pass
            return

        # Host / Admin check for all other lobby & running controls
        if user.id != s.started_by and not await self._gq_is_admin(context, chat_id, user):
            await query.answer("⛔ Only the Quiz Host or Group Admins can change settings or start the quiz.", show_alert=True)
            return

        if s.status == "lobby":
            if action in ("lset", "lback"):
                s.lobby_page = "settings" if action == "lset" else "main"
                await query.answer()
                try:
                    await query.message.edit_text(_lobby_text(s), parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(s))
                except Exception:
                    pass
                return
            if action == "launch":
                await query.answer("🚀 Launching Quiz Now!")
                await self._gq_launch_from_lobby(context, s)
                return
            if action == "autostart":
                await query.answer("⏰ Auto-starting in 30 seconds!")
                try:
                    await query.message.edit_text(
                        _lobby_text(s, "⏰ <b>Auto-Start Activated:</b> Launching in <b>30 seconds</b>! Tap <b>I'm ready! ✅</b> fast!"),
                        parse_mode=ParseMode.HTML,
                        reply_markup=_lobby_kb(s),
                    )
                except Exception:
                    pass
                async def _delayed_launch():
                    await asyncio.sleep(30)
                    if ACTIVE.get(chat_id) is s and s.status == "lobby":
                        await self._gq_launch_from_lobby(context, s)
                asyncio.create_task(_delayed_launch())
                return
            if action == "tog_men":
                s.mention_players = not s.mention_players
                await query.answer(f"Mention Players: {'ON 🔔' if s.mention_players else 'OFF 🔕'}")
                try:
                    await query.message.edit_text(_lobby_text(s), parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(s))
                except Exception:
                    pass
                return
            if action == "tog_cert":
                s.certificates = not s.certificates
                await query.answer(f"Top-3 Certificates: {'ON 🏆' if s.certificates else 'OFF'}")
                try:
                    await query.message.edit_text(_lobby_text(s), parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(s))
                except Exception:
                    pass
                return
            if action == "tog_neg":
                s.negative_rate = 0.0 if s.negative_rate > 0 else 0.25
                await query.answer(f"Negative Marking: {'-' + str(s.negative_rate) if s.negative_rate > 0 else 'OFF'}")
                try:
                    await query.message.edit_text(_lobby_text(s), parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(s))
                except Exception:
                    pass
                return
            if action == "tog_ap":
                s.auto_pause = not s.auto_pause
                await query.answer(f"Auto Pause: {'ON ⏸' if s.auto_pause else 'OFF'}")
                try:
                    await query.message.edit_text(_lobby_text(s), parse_mode=ParseMode.HTML, reply_markup=_lobby_kb(s))
                except Exception:
                    pass
                return
            if action == "cancel_lobby":
                ACTIVE.pop(chat_id, None)
                s.status = "stopped"
                await self._gq_unpin(context, s)
                await self._gq_save(s)
                await query.answer("Quiz cancelled.")
                try:
                    await query.message.edit_text(
                        f"❌ <b>Quiz Lobby Cancelled</b> by {UI.mention(user.id, self._bt_name(user))}.",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return
        if action == "pause" and s.status == "running":
            s.status, s.paused_at = "paused", time.monotonic()
            await query.answer("Paused ⏸ — the current question still finishes.")
            await self._gq_edit_ctrl(context, s, "⏸ <b>Paused.</b>")
        elif action == "resume" and s.status == "paused":
            s.status = "running"
            s.wake.set()
            await query.answer("Resumed ▶️")
            await self._gq_edit_ctrl(context, s, "▶️ <b>Running.</b>")
        elif action == "skip" and s.status == "running":
            s.wake.set()
            await query.answer("Skipping ⏭")
        elif action == "stop":
            s.status = "stopped"
            s.wake.set()
            await query.answer("Stopping ⏹")
        else:
            await query.answer()
        await self._gq_save(s)

    async def cmd_stopquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if chat and chat.type == "private" and hasattr(self, "_mq_stop_solo") and user:
            stopped = self._mq_stop_solo(user.id)
            if stopped:
                await self._reply(update, "⏹ <b>Solo Quiz stopped.</b>")
                return
        s = ACTIVE.get(chat.id) if chat else None
        if not s:
            await self._reply(update, "ℹ️ <b>No quiz is running here.</b>")
            return
        if user.id != s.started_by and not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can stop the quiz.</b>")
            return
        s.status = "stopped"
        s.wake.set()

    async def cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/schedule <HH:MM> [quiz_id] or /schedule in <N>m [quiz_id] in a group."""
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        if not user or not chat or chat.type == "private":
            await self._reply(update, "👥 Use <code>/schedule</code> in a group to schedule a quiz!")
            return
        if not await self._gq_is_admin(context, chat.id, user, msg):
            await self._reply(update, "⛔ <b>Only group admins can schedule a quiz.</b>")
            return

        args = context.args or []
        if not args:
            await self._reply(
                update,
                "⏰ <b>Usage:</b>\n"
                "• <code>/schedule in 15m [quiz_id]</code> — schedule in 15 minutes\n"
                "• <code>/schedule 20:30 [quiz_id]</code> — schedule at 20:30 (today)\n"
                "<i>Example: /schedule in 10m gq_bot20</i>"
            )
            return

        delay_secs = None
        set_id = "bot20"

        if args[0].lower() == "in" and len(args) > 1 and args[1].lower().endswith("m"):
            try:
                mins = int(args[1][:-1])
                delay_secs = mins * 60
                if len(args) > 2:
                    set_id = args[2].removeprefix("gq_")
            except ValueError:
                pass
        elif ":" in args[0]:
            try:
                parts = args[0].split(":")
                target_h, target_m = int(parts[0]), int(parts[1])
                now = datetime.now()
                target_dt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
                if target_dt < now:
                    target_dt += timedelta(days=1)
                delay_secs = int((target_dt - now).total_seconds())
                if len(args) > 1:
                    set_id = args[1].removeprefix("gq_")
            except Exception:
                pass

        if delay_secs is None or delay_secs <= 0 or delay_secs > 86400 * 7:
            await self._reply(update, "⚠️ Invalid time format. Use <code>/schedule in 15m</code> or <code>/schedule 21:00</code>")
            return

        mins_left = max(1, round(delay_secs / 60))
        await self._reply(
            update,
            f"✅ <b>QUIZ SCHEDULED!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>Starting in:</b> ~{mins_left} minutes\n"
            f"📝 <b>Target Quiz:</b> <code>{html.escape(set_id)}</code>\n"
            f"👤 <b>Scheduled by:</b> {UI.mention(user.id, user.first_name)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Get ready! The bot will auto-launch the quiz.</i>"
        )

        async def scheduled_launch():
            await asyncio.sleep(delay_secs)
            if not is_active(chat.id):
                await context.bot.send_message(
                    chat.id,
                    "🚀 <b>SCHEDULED QUIZ STARTING NOW!</b>\nGet ready!",
                    parse_mode=ParseMode.HTML
                )
                await self._gq_start_set(update, context, set_id, auto_part=SCHEDULE_PART)

        asyncio.create_task(scheduled_launch())

    # ── after a restart ──────────────────────────────────────────────────

    async def gq_resume_all(self, bot) -> int:
        docs = await self._gq_db("list_quiz_sessions", default=[])
        context = SimpleNamespace(bot=bot)
        n = 0
        for d in docs if isinstance(docs, list) else []:
            try:
                s = Session.from_doc(d)
            except Exception as e:
                logger.warning(f"[GQUIZ] bad saved session: {e}")
                continue
            if is_active(s.chat_id) or s.idx >= len(s.questions):
                await self._gq_db("delete_quiz_session", s.chat_id)
                continue
            if s.status == "paused":
                s.paused_at = time.monotonic()
            ACTIVE[s.chat_id] = s
            if s.status == "lobby":                          # reopen the lobby with fresh buttons
                sent = await self._gq_send(context, s, _lobby_text(s, "♻️ <b>I restarted — the lobby is open again.</b>"),
                                           _lobby_kb(s))
                s.ctrl_msg_id = getattr(sent, "message_id", None) or s.ctrl_msg_id
                await self._gq_save(s)
                self._gq_lobby_watchdog(context, s)
                n += 1
                continue
            await self._gq_send(context, s, f"♻️ <b>I restarted — continuing “{html.escape(s.title)}” "
                                            f"from Q{s.idx + 1}.</b>")
            asyncio.create_task(self._gq_run(s, context))
            n += 1
        if n:
            logger.info(f"[GQUIZ] resumed {n} session(s) after restart")
        return n
