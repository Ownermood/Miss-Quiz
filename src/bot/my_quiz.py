"""
Users' own quizzes ("Create Own Quiz" / /myquiz).

A user sends questions in the bot's DM — pasted text, any file the importer
reads, or forwarded quiz polls — taps Done, names it, and gets a private quiz
set (`quiz_sets` collection) to battle with. These questions never go into the
bot's own pool or the auto quiz.
"""

import asyncio
import html
import logging
import secrets
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)

MAX_SETS      = 10                  # quizzes per user
MIN_Q         = 3                   # questions needed to save
MAX_Q_PER_SET = 100
MAX_FILE      = 10 * 1024 * 1024    # 10 MB

HELP = ("<blockquote>📄 Paste questions as text\n"
        "📎 Send a file: PDF, DOCX, PPTX, TXT, CSV, XLSX…\n"
        "🗳 Forward quiz polls</blockquote>\n"
        "<i>Each question needs its options and the answer. Send as many as you like, then tap Done.</i>")
EXAMPLE = ("<blockquote>1. Capital of India?\nA) Mumbai\nB) Delhi\nC) Pune\nAnswer: B</blockquote>")


def _btn(text: str, data: str, style: Optional[str] = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


def parse_questions(text: str, items: Optional[List[Dict]] = None) -> Tuple[List[Dict], List[Dict]]:
    """Text and/or structured rows → valid poll questions + problems (why skipped)."""
    from src.bot.quiz_parser import SmartQuizParser, items_to_questions, _clamp_option
    parser = SmartQuizParser()
    raw = parser.parse(text) if (text or "").strip() else []
    problems = list(getattr(parser, "problems", []) or [])
    if items:
        more, more_problems = items_to_questions(items)
        raw += more
        problems += more_problems
    out = []
    for it in raw:
        q = " ".join(str(it.get("question") or "").split())
        opts = [str(o).strip() for o in it.get("options") or []]
        c = it.get("correct_answer")
        where = f"“{q[:30]}”"
        if len(q) < 3:
            problems.append({"where": where, "reason": "question too short"}); continue
        if not 2 <= len(opts) <= 10 or not all(opts):
            problems.append({"where": where, "reason": "needs 2–10 options"}); continue
        if not isinstance(c, int) or not 0 <= c < len(opts):
            problems.append({"where": where, "reason": "answer missing or doesn't match an option"}); continue
        d = {"question": q[:280], "options": [_clamp_option(o) for o in opts], "correct_answer": c}
        if it.get("explanation"):
            d["explanation"] = str(it["explanation"]).strip()[:190]
        out.append(d)
    return out, problems


class MyQuizMixin:

    # ── state ────────────────────────────────────────────────────────────

    @staticmethod
    def _mq_draft(context) -> Optional[Dict]:
        ud = getattr(context, "user_data", None)
        return ud.get("mq_draft") if ud else None

    async def _mq_db(self, fn: str, *args, default=None):
        if not self.db or not hasattr(self.db, fn):
            return default
        try:
            return await asyncio.to_thread(getattr(self.db, fn), *args)
        except Exception as e:
            logger.warning(f"[MYQUIZ] {fn} failed: {e}")
            return default

    # ── views ────────────────────────────────────────────────────────────

    def _mq_collect_view(self, d: Dict):
        n = len(d["qs"])
        text = "📝 <b>CREATE YOUR QUIZ</b>\n"
        if n or d.get("problems") or d.get("note"):
            text += f"<blockquote>✅ Questions added › {n}</blockquote>\n"
            if d.get("note"):
                text += f"⚠️ {html.escape(d['note'])}\n"
            probs = d.get("problems") or []
            if probs:
                text += f"⚠️ Skipped {len(probs)}:\n" + "\n".join(
                    f"• {html.escape(p['where'])} — {html.escape(p['reason'])}" for p in probs[-5:]) + "\n"
            text += "<i>Send more, or tap Done.</i>"
        else:
            text += HELP
        kb = InlineKeyboardMarkup([[_btn(f"✅ Done ({n})", "mq_done", "success")],
                                   [_btn("❌ Cancel", "mq_cancel", "danger")]])
        return text, kb

    def _mq_list_view(self, sets: List[Dict]):
        rows = [[_btn(f"📝 {s.get('title', 'Quiz')[:28]} · {s.get('count', 0)}Q", f"mq_open:{s['id']}")]
                for s in sets]
        if sets:
            text = f"📚 <b>MY QUIZZES</b> ({len(sets)}/{MAX_SETS})\n<i>Open a quiz to battle with it.</i>"
        else:
            text = "📚 <b>MY QUIZZES</b>\n<i>No quizzes yet. Make one from your own questions.</i>"
        if len(sets) < MAX_SETS:
            rows.insert(0, [_btn("📝 Create Own Quiz", "mq_new", "success")])
        return text, InlineKeyboardMarkup(rows)

    @staticmethod
    def _mq_card_view(qs: Dict, saved: bool = False):
        from src.bot.battle import _tlabel
        text = (("✅ <b>Saved!</b>\n" if saved else "") +
                f"📝 <b>{html.escape(qs.get('title', 'Quiz'))}</b>\n"
                f"<blockquote>❓ Questions › {qs.get('count', 0)}\n"
                f"⏱ Timer › {_tlabel(int(qs.get('secs') or 20))} each\n"
                f"📅 Created › {str(qs.get('created_at', ''))[:10]}</blockquote>")
        kb = InlineKeyboardMarkup([
            [_btn("⚔️ Battle with this quiz", f"mq_bt:{qs['id']}", "success")],
            [_btn("🗑 Delete", f"mq_del:{qs['id']}", "danger")],
            [_btn("⬅️ Back", "mq_list", "danger")],
        ])
        return text, kb

    # ── entry points ─────────────────────────────────────────────────────

    async def cmd_myquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat and chat.type != "private":
            link = f"https://t.me/{context.bot.username}?start=mq"
            await self._reply(update, "📚 <b>Your quizzes live in my DM.</b>",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                  "📚 Open My Quizzes", url=link, style="primary")]]))
            return
        await self.mq_open_list(update, context)

    async def mq_open_list(self, update: Update, context, query=None):
        user = update.effective_user
        sets = await self._mq_db("list_quiz_sets", user.id, default=[])
        text, kb = self._mq_list_view(sets if isinstance(sets, list) else [])
        if query:
            await self._bt_edit(query, text, kb)
        else:
            await self._reply(update, text, reply_markup=kb)

    async def mq_start_draft(self, query, context, user, update: Optional[Update] = None):
        """Begin collecting questions. From a button (query) the same message
        becomes the panel; from /start mq_new a new one is sent."""
        n = await self._mq_db("count_quiz_sets", user.id, default=0)
        if isinstance(n, int) and n >= MAX_SETS:
            text = f"⚠️ <b>You already have {MAX_SETS} quizzes.</b>\n<i>Delete one to make a new quiz.</i>"
            kb = InlineKeyboardMarkup([[_btn("📚 My Quizzes", "mq_list")]])
            if query:
                await self._bt_edit(query, text, kb)
            else:
                await self._reply(update, text, reply_markup=kb)
            return
        d = {"qs": [], "seen": set(), "problems": [], "note": "", "stage": "collect", "fname": ""}
        text, kb = self._mq_collect_view(d)
        if query:
            await self._bt_edit(query, text, kb)
            d["chat"], d["msg"] = query.message.chat.id, query.message.message_id
        else:
            sent = await self._reply(update, text, reply_markup=kb)
            d["chat"], d["msg"] = update.effective_chat.id, getattr(sent, "message_id", None)
        context.user_data["mq_draft"] = d
        context.user_data.pop("bt_await", None)

    async def _mq_show(self, context, d: Dict, text: str, kb) -> None:
        """Fresh panel under the user's last message; the old one is removed."""
        old = d.get("msg")
        try:
            sent = await context.bot.send_message(d["chat"], text, parse_mode=ParseMode.HTML, reply_markup=kb)
            d["msg"] = sent.message_id
        except Exception as e:
            logger.debug(f"[MYQUIZ] panel send failed: {e}")
            return
        if old:
            try:
                await context.bot.delete_message(d["chat"], old)
            except Exception:
                pass

    # ── input: text / file / forwarded poll ──────────────────────────────

    async def handle_myquiz_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Group -4 (DM only): feeds the open draft. Does nothing without one."""
        d = self._mq_draft(context)
        msg, user = update.effective_message, update.effective_user
        if not d or not msg or not user:
            return
        if d["stage"] == "name":
            title = " ".join((msg.text or "").split())[:40]
            if title:
                await self._mq_save(context, user, d, title)
            raise ApplicationHandlerStop

        items, text, note = [], "", ""
        if msg.poll:
            p = msg.poll
            if p.type == "quiz" and p.correct_option_id is not None:
                items = [{"question": p.question, "options": [o.text for o in p.options],
                          "correct_answer": p.correct_option_id, "explanation": p.explanation or ""}]
            else:
                note = "Only quiz polls with a visible answer can be added."
        elif msg.document:
            doc = msg.document
            if doc.file_size and doc.file_size > MAX_FILE:
                note = "File is too big (max 10 MB)."
            else:
                try:
                    from src.bot.file_extract import extract
                    f = await context.bot.get_file(doc.file_id, read_timeout=60)
                    data = bytes(await f.download_as_bytearray(read_timeout=60))
                    items, text, note = await asyncio.to_thread(extract, doc.file_name or "", data)
                    if not d.get("fname") and doc.file_name:
                        d["fname"] = doc.file_name.rsplit(".", 1)[0].replace("_", " ")[:40]
                except Exception as e:
                    logger.warning(f"[MYQUIZ] file read failed: {e}")
                    note = "Couldn't read that file. Try another format (TXT, DOCX, PDF)."
        elif msg.text:
            text = msg.text
        else:
            note = "Send text, a file, or forward a quiz poll."

        qs, probs = (await asyncio.to_thread(parse_questions, text, items)) if (text or items) else ([], [])
        added = 0
        for q in qs:
            key = q["question"].lower()
            if key in d["seen"]:
                continue
            if len(d["qs"]) >= MAX_Q_PER_SET:
                note = f"A quiz can have up to {MAX_Q_PER_SET} questions — the rest were skipped."
                break
            d["seen"].add(key)
            d["qs"].append(q)
            added += 1
        d["problems"] = (d["problems"] + probs)[-50:]
        if not added and not note:
            note = ("No new questions in that (already added)." if qs else
                    "No questions found in that. Use a layout like the example below.")
        d["note"] = note
        text_out, kb = self._mq_collect_view(d)
        if note.startswith("No questions found"):
            text_out += "\n" + EXAMPLE
        await self._mq_show(context, d, text_out, kb)
        raise ApplicationHandlerStop

    async def _mq_save(self, context, user, d: Dict, title: str) -> None:
        n = await self._mq_db("count_quiz_sets", user.id, default=0)
        if isinstance(n, int) and n >= MAX_SETS:
            context.user_data.pop("mq_draft", None)
            await self._mq_show(context, d, f"⚠️ <b>You already have {MAX_SETS} quizzes.</b>",
                                InlineKeyboardMarkup([[_btn("📚 My Quizzes", "mq_list")]]))
            return
        sid = await self._mq_db("create_quiz_set", user.id, title, d["qs"], 20)
        context.user_data.pop("mq_draft", None)
        if not sid:
            await self._mq_show(context, d, "⚠️ <b>Couldn't save the quiz. Please try again.</b>", None)
            return
        qs = await self._mq_db("get_quiz_set", sid) or {"id": sid, "title": title, "count": len(d["qs"]), "secs": 20}
        await self._mq_show(context, d, *self._mq_card_view(qs, saved=True))
        logger.info(f"[MYQUIZ] {user.id} saved {sid} ({len(d['qs'])} questions)")

    # ── buttons ──────────────────────────────────────────────────────────

    async def handle_myquiz_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query, user = update.callback_query, update.effective_user
        if not query or not user or not query.data:
            return
        action, _, arg = query.data.partition(":")
        d = self._mq_draft(context)

        if action in ("mq_done", "mq_name", "mq_cancel"):
            if not d:
                await query.answer("This quiz draft has ended.", show_alert=True)
                return
            if action == "mq_cancel":
                context.user_data.pop("mq_draft", None)
                await query.answer("Cancelled.")
                await self._bt_edit(query, "❌ <b>Quiz not saved.</b>",
                                    InlineKeyboardMarkup([[_btn("📚 My Quizzes", "mq_list")]]))
                return
            if action == "mq_done":
                if len(d["qs"]) < MIN_Q:
                    await query.answer(f"Add at least {MIN_Q} questions first.", show_alert=True)
                    return
                n = await self._mq_db("count_quiz_sets", user.id, default=0)
                d["stage"] = "name"
                d["default"] = d.get("fname") or f"My Quiz {(n if isinstance(n, int) else 0) + 1}"
                d["msg"] = query.message.message_id
                await query.answer()
                await self._bt_edit(
                    query,
                    f"✏️ <b>NAME YOUR QUIZ</b>\n<blockquote>✅ Questions › {len(d['qs'])}</blockquote>\n"
                    f"<i>Type a name, or tap below.</i>",
                    InlineKeyboardMarkup([[_btn(f"📝 Use “{d['default'][:28]}”", "mq_name", "success")],
                                          [_btn("❌ Cancel", "mq_cancel", "danger")]]))
                return
            await query.answer()
            if d["stage"] == "name":
                d["msg"] = query.message.message_id      # replaced by the saved-quiz card
                await self._mq_save(context, user, d, d.get("default") or "My Quiz")
            return

        if action == "mq_list":
            await query.answer()
            await self.mq_open_list(update, context, query)
            return
        if action == "mq_new":
            await query.answer()
            await self.mq_start_draft(query, context, user)
            return

        qs = await self._mq_db("get_quiz_set", arg) if arg else None
        if not isinstance(qs, dict) or qs.get("owner") != user.id:
            await query.answer("That quiz is no longer available.", show_alert=True)
            return
        if action == "mq_open":
            await query.answer()
            await self._bt_edit(query, *self._mq_card_view(qs))
        elif action == "mq_del":
            await query.answer()
            await self._bt_edit(
                query, f"🗑 <b>Delete “{html.escape(qs.get('title', 'Quiz'))}”?</b>\n<i>This can't be undone.</i>",
                InlineKeyboardMarkup([[_btn("🗑 Yes, delete", f"mq_delok:{qs['id']}", "danger")],
                                      [_btn("⬅️ Back", f"mq_open:{qs['id']}", "danger")]]))
        elif action == "mq_delok":
            await self._mq_db("delete_quiz_set", qs["id"], user.id)
            await query.answer("Deleted.")
            await self.mq_open_list(update, context, query)
        elif action == "mq_bt":
            await self._mq_battle(query, context, user, qs)
        else:
            await query.answer()

    async def _mq_battle(self, query, context, user, qs: Dict) -> None:
        """'Battle with this quiz' from the quiz card (DM) → challenge card."""
        from src.bot.battle import Battle
        if query.message.chat.type != "private":
            await query.answer("Open this in my DM.", show_alert=True)
            return
        if self._bt_busy(user.id):
            await query.answer("You're already in a battle — finish it first.", show_alert=True)
            return
        await self._bt_drop_open(user.id, context)
        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode="dm", p1=user.id, p1_name=self._bt_name(user), state="setup")
        self._bt_apply_set(b, qs)
        err = await self._bt_create(b)
        if err:
            await query.answer(err, show_alert=True)
            return
        b.msg_id = query.message.message_id
        self._bt_state()[bid] = b
        await self._bt_save(b)
        await query.answer("Challenge created! ⚔️")
        await self._bt_edit(query, *self._bt_challenge_view(b, context.bot.username, self._bt_inline_on(context)))
