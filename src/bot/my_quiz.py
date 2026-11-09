"""
Users' own quizzes ("Create Own Quiz" / /myquiz).

A user sends questions in the bot's DM — pasted text, any file the importer
reads, or forwarded quiz polls — taps Done, names it, and gets a private quiz
set (`quiz_sets` collection) to battle with. These questions never go into the
bot's own pool or the auto quiz.

Includes:
- Safai Filter (clean counters, @usernames, URLs, custom words)
- Quiz Editor (rename, change timer)
- Practice Mode in DM (instant explanation per question)
"""

import asyncio
import html
import logging
import secrets
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.utils.cleaner import clean_text

logger = logging.getLogger(__name__)

MIN_Q         = 3                   # questions needed to save
MAX_Q_PER_SET = 5000                # safety only (MongoDB document size)
MAX_FILE      = 20 * 1024 * 1024    # Telegram: bots can't download files over 20 MB
PER_PAGE      = 8                   # quizzes per list page

HELP = ("<blockquote>📄 Paste questions as text\n"
        "📎 Send any file: PDF, DOCX, PPTX, TXT, CSV, XLSX, JSON, HTML, ZIP…\n"
        "🗳 Forward quiz polls (auto-cleaned!)\n"
        "🧹 Auto-cleans [1/100], @channel_names, and custom words</blockquote>\n"
        "<i>Each question needs options and the answer. Send as many as you like, then tap Done.</i>")
EXAMPLE = ("<blockquote>1. Capital of India?\nA) Mumbai\nB) Delhi\nC) Pune\nAnswer: B</blockquote>")


def _btn(text: str, data: str, style: Optional[str] = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


def page_rows(pages: int, current: int, data) -> List[List[InlineKeyboardButton]]:
    if pages <= 1:
        return []
    start = max(1, min(current - 4, pages - 9))
    nums = list(range(start, min(pages, start + 9) + 1))
    btns = [_btn(f"·{n}·" if n == current else str(n), data(n), "success" if n == current else "primary")
            for n in nums]
    return [btns[k:k + 5] for k in range(0, len(btns), 5)]


async def load_page(db_call, count_call, owner: int, page: int):
    total = await count_call(owner)
    total = total if isinstance(total, int) else 0
    pages = max(1, -(-total // PER_PAGE))
    page = max(1, min(page, pages))
    sets = await db_call(owner, PER_PAGE, (page - 1) * PER_PAGE)
    return (sets if isinstance(sets, list) else []), page, pages


def parse_questions(text: str, items: Optional[List[Dict]] = None,
                    custom_words: Optional[List[str]] = None) -> Tuple[List[Dict], List[Dict]]:
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
        raw_q = " ".join(str(it.get("question") or "").split())
        q = clean_text(raw_q, custom_words)
        opts = [clean_text(str(o).strip(), custom_words) for o in it.get("options") or []]
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
            d["explanation"] = clean_text(str(it["explanation"]).strip(), custom_words)[:190]
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
            logger.warning(f"[MYQUIZ] db {fn} failed: {e}")
            return default

    # ── views ────────────────────────────────────────────────────────────

    @staticmethod
    def _mq_collect_view(d: Dict) -> Tuple[str, InlineKeyboardMarkup]:
        q_count = len(d["qs"])
        body = (
            f"✏️ <b>CREATE OWN QUIZ</b>\n"
            f"<blockquote>✅ Questions added › <b>{q_count}</b>" +
            (f"\n📎 From › {html.escape(d['fname'])}" if d.get("fname") else "") +
            "</blockquote>\n"
        )
        if d.get("note"):
            body += f"💡 <i>{html.escape(d['note'])}</i>\n\n"
        else:
            body += HELP + "\n\n"
        ready = q_count >= MIN_Q
        top = [
            _btn(f"✅ Done ({q_count}Q)" if ready else f"Add {MIN_Q - q_count} more…",
                 "mq_done", "success" if ready else "primary"),
            _btn("❌ Cancel", "mq_cancel", "danger"),
        ]
        return body, InlineKeyboardMarkup([top])

    @staticmethod
    def _mq_list_view(sets: List[Dict], page: int, pages: int) -> Tuple[str, InlineKeyboardMarkup]:
        from src.bot.battle import _tlabel
        head = "📚 <b>YOUR QUIZZES</b>\n"
        if not sets:
            body = head + "<i>You haven't created any quizzes yet. Make your first one below!</i>"
            return body, InlineKeyboardMarkup([[_btn("➕ Create New Quiz", "mq_new", "success")]])
        lines = []
        for i, s in enumerate(sets, 1 + (page - 1) * PER_PAGE):
            q_cnt = s.get("count") or (len(s["questions"]) if "questions" in s else 0)
            t_lbl = _tlabel(int(s.get("secs") or 20))
            lines.append(f"{i}. <b>{html.escape(s.get('title', 'Quiz')[:30])}</b> · {q_cnt}Q · {t_lbl}")
        body = head + "<blockquote>" + "\n".join(lines) + "</blockquote>\n<i>Tap a quiz to play, edit, or practice.</i>"
        item_rows = [[_btn(f"📝 {s.get('title', 'Quiz')[:26]}", f"mq_open:{s['id']}")]
                     for s in sets]
        nav_rows = page_rows(pages, page, lambda p: f"mq_list:{p}")
        foot = [_btn("➕ Create New Quiz", "mq_new", "success")]
        return body, InlineKeyboardMarkup(item_rows + nav_rows + [foot])

    async def _mq_page(self, owner: int, page: int = 1):
        call = lambda fn: (lambda *a: self._mq_db(fn, *a))
        return await load_page(call("list_quiz_sets"), call("count_quiz_sets"), owner, page)

    @staticmethod
    def _mq_card_view(qs: Dict, saved: bool = False, bot_username: str = ""):
        from src.bot.battle import _tlabel
        text = (("✅ <b>Saved!</b>\n" if saved else "") +
                f"📝 <b>{html.escape(qs.get('title', 'Quiz'))}</b>\n"
                f"<blockquote>❓ Questions › {qs.get('count', 0)}\n"
                f"⏱ Timer › {_tlabel(int(qs.get('secs') or 20))} each\n"
                f"📅 Created › {str(qs.get('created_at', ''))[:10]}</blockquote>")
        rows = [
            [_btn("⚔️ Battle with this quiz", f"mq_bt:{qs['id']}", "success")],
            [_btn("🎯 Practice in DM", f"mq_prac:{qs['id']}", "primary")],
        ]
        if bot_username:
            rows.append([InlineKeyboardButton("👥 Start in Group", style="primary",
                                              url=f"https://t.me/{bot_username}?startgroup=gq_{qs['id']}")])
        rows.append([_btn("✏️ Rename", f"mq_rename:{qs['id']}", "primary"),
                     _btn("⏱️ Timer", f"mq_timer:{qs['id']}", "primary")])
        rows.append([_btn("🗑 Delete", f"mq_del:{qs['id']}", "danger"), _btn("⬅️ Back", "mq_list", "danger")])
        return text, InlineKeyboardMarkup(rows)

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

    async def mq_open_list(self, update: Update, context, query=None, page: int = 1):
        user = update.effective_user
        text, kb = self._mq_list_view(*await self._mq_page(user.id, page))
        if query:
            await self._bt_edit(query, text, kb)
        else:
            await self._reply(update, text, reply_markup=kb)

    async def mq_start_draft(self, query, context, user, update: Optional[Update] = None):
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
        msg, user = update.effective_message, update.effective_user
        if not msg or not user:
            return

        # Check if renaming a quiz
        renaming_id = context.user_data.get("mq_renaming")
        if renaming_id and msg.text:
            new_title = " ".join(msg.text.strip().split())[:40]
            if new_title:
                context.user_data.pop("mq_renaming", None)
                if self.db and hasattr(self.db, "update_quiz_set"):
                    await asyncio.to_thread(self.db.update_quiz_set, renaming_id, user.id, {"title": new_title})
                qs = await self._mq_db("get_quiz_set", renaming_id)
                await self._reply(update, f"✅ <b>Quiz renamed to:</b> <code>{html.escape(new_title)}</code>")
                if qs:
                    await self._reply(update, *self._mq_card_view(qs, bot_username=context.bot.username))
                raise ApplicationHandlerStop

        d = self._mq_draft(context)
        if not d:
            return
        lock = d.setdefault("lock", asyncio.Lock())
        async with lock:
            if self._mq_draft(context) is d:
                await self._mq_take(context, user, d, msg)
        raise ApplicationHandlerStop

    async def _mq_take(self, context, user, d: Dict, msg) -> None:
        if d["stage"] == "name":
            plain = (msg.text or "").strip()
            if plain and "\n" not in plain and not msg.document and not msg.poll and not msg.photo:
                await self._mq_save(context, user, d, " ".join(plain.split())[:40])
                return
            d["stage"] = "collect"

        items, text, note = [], "", ""
        if msg.poll:
            p = msg.poll
            if p.type == "quiz" and p.correct_option_id is not None:
                items = [{"question": p.question, "options": [o.text for o in p.options],
                          "correct_answer": p.correct_option_id, "explanation": p.explanation or ""}]
            else:
                note = "Only quiz polls with a visible answer can be added."
        elif msg.document or msg.photo:
            doc = msg.document or msg.photo[-1]
            fname = getattr(doc, "file_name", None) or "photo.jpg"
            if doc.file_size and doc.file_size > MAX_FILE:
                note = "File is too big — Telegram lets bots read files up to 20 MB."
            else:
                try:
                    from src.bot.file_extract import extract
                    f = await context.bot.get_file(doc.file_id, read_timeout=60)
                    data = bytes(await f.download_as_bytearray(read_timeout=60))
                    items, text, note = await asyncio.to_thread(extract, fname, data)
                    if not d.get("fname") and msg.document and doc.file_name:
                        d["fname"] = doc.file_name.rsplit(".", 1)[0].replace("_", " ")[:40]
                except Exception as e:
                    logger.warning(f"[MYQUIZ] file read failed: {e}")
                    note = "Couldn't download that file. Please send it again."
            if msg.caption and not (text or items):
                text = msg.caption
        elif msg.text:
            text = msg.text
        else:
            note = "Send text, a file, or forward a quiz poll."

        user_words = []
        if self.db and hasattr(self.db, "get_user_custom_words"):
            user_words = await asyncio.to_thread(self.db.get_user_custom_words, user.id)

        qs, probs = (await asyncio.to_thread(parse_questions, text, items, user_words)) if (text or items) else ([], [])
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
        d["note"] = (f"Cleaned & Added: +{added}. " if added and (msg.document or msg.photo or msg.poll) else "") + note
        text_out, kb = self._mq_collect_view(d)
        if note.startswith("No questions found"):
            text_out += "\n" + EXAMPLE
        await self._mq_show(context, d, text_out, kb)

    async def _mq_save(self, context, user, d: Dict, title: str) -> None:
        qs_list = list(d["qs"])
        context.user_data.pop("mq_draft", None)
        sid = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")
        doc = {"id": sid, "owner": user.id, "title": title[:60], "count": len(qs_list),
               "questions": qs_list, "secs": 20}
        await self._mq_db("save_quiz_set", doc)
        logger.info(f"[MYQUIZ] saved {sid} '{title}' ({len(qs_list)} Q) for {user.id}")
        card_text, kb = self._mq_card_view(doc, saved=True, bot_username=context.bot.username)
        msg_id = d.get("msg")
        if msg_id:
            try:
                await context.bot.edit_message_text(card_text, chat_id=d["chat"], message_id=msg_id,
                                                    parse_mode=ParseMode.HTML, reply_markup=kb)
                return
            except Exception:
                pass
        await context.bot.send_message(d["chat"], card_text, parse_mode=ParseMode.HTML, reply_markup=kb)

    # ── callbacks ────────────────────────────────────────────────────────

    async def handle_my_quiz_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query, user = update.callback_query, update.effective_user
        if not query or not user or not query.data:
            return
        action, _, arg = query.data.partition(":")
        d = self._mq_draft(context)

        # Practice mode responses
        if action == "mq_p_ans":
            parts = query.data.split(":")
            if len(parts) >= 4:
                sid, q_idx, chosen = parts[1], int(parts[2]), int(parts[3])
                await self._mq_practice_answer(query, context, user, sid, q_idx, chosen)
                return
        if action == "mq_p_next":
            prac = context.user_data.get("mq_prac")
            if prac:
                await query.answer()
                await self._mq_practice_show_q(query, context, prac)
                return

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
                d["msg"] = query.message.message_id
                await self._mq_save(context, user, d, d.get("default") or "My Quiz")
            return

        if action == "mq_list":
            await query.answer()
            await self.mq_open_list(update, context, query, int(arg) if arg.isdigit() else 1)
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
            await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username))
        elif action == "mq_prac":
            await self._mq_practice_start(query, context, user, qs)
        elif action == "mq_rename":
            context.user_data["mq_renaming"] = qs["id"]
            await query.answer("Send new title in chat!")
            await self._bt_edit(
                query,
                f"✏️ <b>RENAME QUIZ</b>\n"
                f"Current: <code>{html.escape(qs.get('title', 'Quiz'))}</code>\n\n"
                f"<i>Type and send the new title as a message now.</i>",
                InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_open:{qs['id']}", "danger")]])
            )
        elif action == "mq_timer":
            await query.answer()
            rows = [
                [_btn("10s", f"mq_settimer:{qs['id']}:10"), _btn("15s", f"mq_settimer:{qs['id']}:15"), _btn("20s", f"mq_settimer:{qs['id']}:20")],
                [_btn("30s", f"mq_settimer:{qs['id']}:30"), _btn("45s", f"mq_settimer:{qs['id']}:45"), _btn("60s", f"mq_settimer:{qs['id']}:60")],
                [_btn("⬅️ Back", f"mq_open:{qs['id']}", "danger")]
            ]
            await self._bt_edit(
                query,
                f"⏱️ <b>SELECT TIMER</b>\nQuiz: <code>{html.escape(qs.get('title', 'Quiz'))}</code>\n"
                f"Current: <b>{qs.get('secs', 20)}s per question</b>\n<i>Choose new duration:</i>",
                InlineKeyboardMarkup(rows)
            )
        elif action == "mq_settimer":
            parts = query.data.split(":")
            if len(parts) >= 3 and parts[2].isdigit():
                new_secs = int(parts[2])
                if self.db and hasattr(self.db, "update_quiz_set"):
                    await asyncio.to_thread(self.db.update_quiz_set, qs["id"], user.id, {"secs": new_secs})
                qs["secs"] = new_secs
                await query.answer(f"Timer set to {new_secs}s!")
                await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username))
        elif action == "mq_del":
            await query.answer()
            await self._bt_edit(
                query, f"🗑 <b>Delete “{html.escape(qs.get('title', 'Quiz'))}”?</b>\n<i>This can't be undone.</i>",
                InlineKeyboardMarkup([[_btn("🗑 Yes, delete", f"mq_delok:{qs['id']}", "danger"),
                                       _btn("⬅️ Back", f"mq_open:{qs['id']}", "danger")]]))
        elif action == "mq_delok":
            await self._mq_db("delete_quiz_set", qs["id"], user.id)
            await query.answer("Deleted.")
            await self.mq_open_list(update, context, query)
        elif action == "mq_bt":
            await self._mq_battle(query, context, user, qs)
        else:
            await query.answer()

    # ── practice mode (DM) ────────────────────────────────────────────────

    async def _mq_practice_start(self, query, context, user, qs: Dict):
        questions = list(qs.get("questions") or [])
        if not questions:
            await query.answer("This quiz has no questions.", show_alert=True)
            return
        prac = {
            "set_id": qs["id"],
            "title": qs.get("title", "Practice"),
            "questions": questions,
            "idx": 0,
            "correct": 0,
            "wrong": 0
        }
        context.user_data["mq_prac"] = prac
        await query.answer("Starting Practice Mode! 🎯")
        await self._mq_practice_show_q(query, context, prac)

    async def _mq_practice_show_q(self, query_or_update, context, prac: Dict, explanation_note: str = ""):
        idx = prac["idx"]
        total = len(prac["questions"])
        if idx >= total:
            cor = prac["correct"]
            wrg = prac["wrong"]
            acc = round(100 * cor / total) if total else 0
            text = (
                f"🎉 <b>PRACTICE COMPLETED!</b>\n"
                f"<b>Quiz:</b> {html.escape(prac['title'])}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✅ Correct: <b>{cor}</b>\n"
                f"❌ Wrong: <b>{wrg}</b>\n"
                f"📊 Accuracy: <b>{acc}%</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Great practice session! Keep going."
            )
            kb = InlineKeyboardMarkup([
                [_btn("🔁 Practice Again", f"mq_prac:{prac['set_id']}", "success")],
                [_btn("📚 My Quizzes", "mq_list", "primary")]
            ])
            if hasattr(query_or_update, "edit_message_text"):
                await query_or_update.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await self._reply(query_or_update, text, reply_markup=kb)
            return

        q = prac["questions"][idx]
        q_text = html.escape(str(q.get("question") or ""))
        opts = [str(o) for o in (q.get("options") or [])]

        head = f"🎯 <b>PRACTICE MODE</b> [Q{idx+1}/{total}]\n<b>{html.escape(prac['title'])}</b>\n━━━━━━━━━━━━━━━━━━\n"
        body = f"❓ <b>{q_text}</b>\n\n"
        if explanation_note:
            body += explanation_note + "\n\n"

        rows = []
        for i, opt in enumerate(opts):
            label = f"{chr(65+i)}) {opt[:40]}"
            rows.append([_btn(label, f"mq_p_ans:{prac['set_id']}:{idx}:{i}", "primary")])
        rows.append([_btn("❌ Exit Practice", f"mq_open:{prac['set_id']}", "danger")])

        kb = InlineKeyboardMarkup(rows)
        full_text = head + body
        if hasattr(query_or_update, "edit_message_text"):
            try:
                await query_or_update.edit_message_text(full_text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                await query_or_update.message.reply_text(full_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await self._reply(query_or_update, full_text, reply_markup=kb)

    async def _mq_practice_answer(self, query, context, user, set_id: str, idx: int, chosen: int):
        prac = context.user_data.get("mq_prac")
        if not prac or prac.get("set_id") != set_id or prac.get("idx") != idx:
            await query.answer("Question expired or already answered.", show_alert=True)
            return

        q = prac["questions"][idx]
        correct = int(q.get("correct_answer", 0))
        opts = [str(o) for o in (q.get("options") or [])]
        exp = str(q.get("explanation") or "").strip()

        is_right = (chosen == correct)
        if is_right:
            prac["correct"] += 1
            verdict = "✅ <b>Correct!</b>"
        else:
            prac["wrong"] += 1
            cor_letter = chr(65 + correct) if 0 <= correct < 26 else str(correct + 1)
            cor_text = opts[correct] if 0 <= correct < len(opts) else ""
            verdict = f"❌ <b>Incorrect!</b> Correct was <b>{cor_letter}) {html.escape(cor_text)}</b>"

        exp_note = f"<blockquote>{verdict}" + (f"\n💡 <i>{html.escape(exp)}</i>" if exp else "") + "</blockquote>"
        prac["idx"] += 1

        total = len(prac["questions"])
        next_btn = _btn("Next Question ➡️", f"mq_p_next:{set_id}", "success") if prac["idx"] < total else _btn("View Results 🏁", f"mq_p_next:{set_id}", "success")
        kb = InlineKeyboardMarkup([
            [next_btn],
            [_btn("❌ Exit Practice", f"mq_open:{set_id}", "danger")]
        ])

        q_text = html.escape(str(q.get("question") or ""))
        text = (
            f"🎯 <b>PRACTICE MODE</b> [Q{idx+1}/{total}]\n"
            f"<b>{html.escape(prac['title'])}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"❓ <b>{q_text}</b>\n\n"
            f"{exp_note}"
        )
        await query.answer("Correct! 🎉" if is_right else "Wrong! ❌")
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def _mq_battle(self, query, context, user, qs: Dict) -> None:
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
