"""
AdminCommandsMixin — /addquiz /delquiz /editquiz /importquiz /dev /broadcast
                     /delbroadcast /reload /restart + handle_document
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, Forbidden, BadRequest, RetryAfter
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from src.bot.ui import UI, OWNER_ID, OWNER_LINK, COMMUNITY, _NO_PREVIEW

logger = logging.getLogger(__name__)


class AdminCommandsMixin(object):

    # ─── /addquiz ────────────────────────────────────────────

    async def cmd_addquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        text  = update.effective_message.text or ""
        raw   = [l.strip() for l in text.strip().split("\n")]
        if raw: raw[0] = raw[0].replace("/addquiz", "").strip()
        lines = [l for l in raw if l]

        USAGE = (
            f"➕ <b>ADD QUESTION</b>\n"
            f"{UI.LINE}\n\n"
            "<b>Format:</b>\n"
            "<code>/addquiz\n"
            "Question text\n"
            "Option A\nOption B\nOption C\nOption D\n"
            "Correct (1-4)\n"
            "Category</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/addquiz\n"
            "Which Article abolishes untouchability?\n"
            "Article 14\nArticle 17\nArticle 19\nArticle 21\n"
            "2\nLegal Reasoning</code>"
        )

        if len(lines) < 6:
            await self._reply(update, USAGE)
            return

        question = lines[0]
        options  = lines[1:5]
        try:
            correct = int(lines[5]) - 1
        except (ValueError, IndexError):
            await self._reply(update, "❌ Correct answer must be a number: 1, 2, 3 or 4")
            return

        if not (0 <= correct <= 3):
            await self._reply(update, "❌ Correct answer must be <b>1, 2, 3 or 4</b>")
            return

        # Enforce Telegram's 100-char poll option limit
        _TMAX   = 100
        options = [
            (o[:97] + "…" if len(str(o).strip()) > _TMAX else str(o).strip())
            for o in options
        ]

        category = lines[6].strip() if len(lines) > 6 else "General"
        msg = await self._reply(update, "⏳ <i>Saving to database...</i>")
        await asyncio.sleep(0.3)

        result = self.quiz_manager.add_questions([{
            "question": question, "options": options,
            "correct_answer": correct, "category": category,
        }])

        added   = result.get("added", 0)
        dups    = result.get("rejected", {}).get("duplicates", 0)
        total   = self._q_count()
        mention = UI.mention(user.id, UI.display_name(user))

        if added > 0:
            text = (
                f"✅ <b>QUESTION ADDED</b>\n"
                f"{UI.LINE}\n\n"
                f"  Added by {mention}\n\n"
                f"<b>PREVIEW</b>\n"
                f"{UI.THIN}\n"
                f"  {question[:65]}{'…' if len(question) > 65 else ''}\n\n"
                f"  A: {options[0]}\n  B: {options[1]}\n"
                f"  C: {options[2]}\n  D: {options[3]}\n\n"
                f"  ✅ Answer   ›  Option {correct+1} — <b>{options[correct]}</b>\n"
                f"  📂 Category ›  <b>{category}</b>\n\n"
                f"{UI.LINE}\n"
                f"  📦 Total in bank: <b>{total}</b>"
            )
        elif dups:
            text = (
                f"⚠️ <b>DUPLICATE</b>\n"
                f"{UI.LINE}\n\n"
                "  This question already exists.\n"
                "  Use /editquiz to modify it."
            )
        else:
            err = (result.get("errors") or ["Unknown"])[0]
            text = (
                f"❌ <b>FAILED</b>\n"
                f"{UI.LINE}\n\n"
                f"  Error: <code>{err}</code>"
            )

        if msg: await self._edit(msg, text)
        else:   await self._reply(update, text)

    # ─── /delquiz ────────────────────────────────────────────

    async def cmd_delquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        # Reply-to-poll detection
        reply = update.effective_message.reply_to_message
        if reply and reply.poll:
            poll_id   = reply.poll.id
            poll_data = context.bot_data.get(f"poll_{poll_id}", {})
            q_id      = poll_data.get("question_id")

            if q_id is None and self.db:
                try:
                    q_id = await asyncio.to_thread(
                        self.db.get_quiz_id_from_poll, str(poll_id))
                except Exception:
                    pass

            if q_id is None:
                poll_q       = reply.poll.question or ""
                poll_q_clean = re.sub(r"^\S+\s+", "", poll_q.strip())
                for q in self.quiz_manager.questions:
                    if q.get("question", "").strip() == poll_q_clean.strip():
                        q_id = q.get("id")
                        break

            if q_id is not None:
                mention   = UI.mention(user.id, UI.display_name(user))
                q_info    = next((q for q in self.quiz_manager.questions
                                  if q.get("id") == q_id), {})
                q_preview = q_info.get("question", f"#{q_id}")[:55]

                msg = await self._reply(update, f"🗑️ <i>Deleting Q#{q_id}...</i>")
                await asyncio.sleep(0.3)
                success = self.quiz_manager.delete_question_by_db_id(q_id)

                if success:
                    remaining = len(self.quiz_manager.questions)
                    text = (
                        f"✅ <b>DELETED</b>\n"
                        f"{UI.LINE}\n\n"
                        f"  By {mention}\n"
                        f"  <code>#{q_id}</code> — {q_preview}…\n\n"
                        f"  📦 Remaining: <b>{remaining}</b> questions"
                    )
                else:
                    text = (
                        f"❌ <b>Not Found</b>\n"
                        f"{UI.LINE}\n\n"
                        f"  Q#{q_id} not found in database."
                    )
                if msg:
                    await self._edit(msg, text)
                return
            else:
                await self._reply(update,
                    f"⚠️ <b>Cannot Identify Question</b>\n"
                    f"{UI.LINE}\n\n"
                    "  Could not match this poll to any question.\n"
                    "  Use /delquiz without reply to pick from list."
                )
                return

        questions = self.quiz_manager.questions
        if not questions:
            await self._reply(update,
                f"📭 <b>No Questions</b>\n{UI.LINE}\n\nDatabase is empty.")
            return

        page = 0
        self._del_page[user.id] = page
        await self._reply(
            update,
            self._delquiz_text(questions, page),
            reply_markup=self._delquiz_kb(questions, page, user.id)
        )

    def _delquiz_text(self, questions: list, page: int) -> str:
        total = len(questions)
        per   = 8
        start = page * per
        end   = min(start + per, total)
        pages = (total + per - 1) // per

        lines = [
            f"🗑️ <b>DELETE QUESTION</b>\n"
            f"{UI.LINE}\n"
            f"  Page <b>{page+1}</b> / <b>{pages}</b>  ·  Total: <b>{total}</b>\n"
            f"{UI.LINE}\n\n"
            f"Select a question to delete:\n"
        ]
        for q in questions[start:end]:
            qid   = q.get("id", "?")
            qtext = q.get("question", "")[:40]
            cat   = q.get("category", "General")
            emoji = UI.cat_emoji(cat)
            lines.append(f"  {emoji} <code>#{qid}</code>  {qtext}{'…' if len(q.get('question',''))>40 else ''}")

        return "\n".join(lines)

    def _delquiz_kb(self, questions: list, page: int, user_id: int) -> InlineKeyboardMarkup:
        per   = 8
        start = page * per
        total = len(questions)
        pages = (total + per - 1) // per
        rows  = []

        chunk = questions[start:start+per]
        for i in range(0, len(chunk), 2):
            row = []
            for q in chunk[i:i+2]:
                qid   = q.get("id", "?")
                qtext = q.get("question", "")[:18]
                row.append(InlineKeyboardButton(
                    f"🗑 #{qid} {qtext}…",
                    callback_data=f"dq_del_{qid}_{user_id}"
                ))
            rows.append(row)

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("🎓 ◀ Prev", callback_data=f"dq_page_{page-1}_{user_id}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶ 🎓", callback_data=f"dq_page_{page+1}_{user_id}"))
        if nav:
            rows.append(nav)

        rows.append([InlineKeyboardButton("🎓 Cancel", callback_data=f"dq_cancel_{user_id}")])
        return InlineKeyboardMarkup(rows)

    async def _cb_delquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        data  = query.data
        actor = query.from_user

        try:
            parts  = data.split("_")
            action = parts[1]

            try:
                owner_uid = int(parts[-1])
            except (ValueError, IndexError):
                owner_uid = 0

            if actor.id != owner_uid:
                await query.answer("❌ Not your menu!", show_alert=True)
                return

            questions = self.quiz_manager.questions

            if action == "cancel":
                try:
                    await query.message.delete()
                except Exception as e:
                    logger.debug(f"[DELQUIZ] delete failed (ok if already gone): {e}")
                return

            if action == "page":
                try:
                    page = int(parts[2])
                except (ValueError, IndexError):
                    logger.warning(f"[DELQUIZ] bad page in {data!r}")
                    return
                self._del_page[actor.id] = page
                try:
                    await query.message.edit_text(
                        self._delquiz_text(questions, page),
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._delquiz_kb(questions, page, actor.id)
                    )
                except Exception as e:
                    logger.debug(f"[DELQUIZ] page edit failed: {e}")
                return

            if action == "del":
                try:
                    qid = int(parts[2])
                except (ValueError, IndexError):
                    logger.warning(f"[DELQUIZ] bad qid in {data!r}")
                    return
                q_info  = next((q for q in questions if q.get("id") == qid), None)
                preview = q_info.get("question", "")[:55] if q_info else f"#{qid}"

                success = self.quiz_manager.delete_question_by_db_id(qid)
                mention = UI.mention(actor.id, UI.display_name(actor))

                if success:
                    remaining = len(self.quiz_manager.questions)
                    text = (
                        f"✅ <b>DELETED</b>\n"
                        f"{UI.LINE}\n\n"
                        f"  By {mention}\n"
                        f"  <code>#{qid}</code> — {preview}…\n\n"
                        f"  📦 Remaining: <b>{remaining}</b> questions"
                    )
                else:
                    text = (
                        f"❌ <b>Not Found</b>\n{UI.LINE}\n\n"
                        f"Question #{qid} not found."
                    )
                try:
                    await query.message.edit_text(text, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.debug(f"[DELQUIZ] result edit failed: {e}")

        except Exception as e:
            logger.error(
                f"[DELQUIZ] Unhandled error data={data!r} user={actor.id}: {e}",
                exc_info=True,
            )

    # ─── /editquiz ───────────────────────────────────────────

    async def cmd_editquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        if self._dev and hasattr(self._dev, "editquiz"):
            await self._dev.editquiz(update, context)
            return

        msg = await self._reply(update, "📋 <i>Loading question bank...</i>")
        await asyncio.sleep(0.3)

        questions = self.quiz_manager.questions
        if not questions:
            text = (
                f"📭 <b>EMPTY BANK</b>\n"
                f"{UI.LINE}\n\n"
                "  No questions in database.\n"
                "  Use /addquiz to add some."
            )
            if msg: await self._edit(msg, text)
            return

        total = len(questions)
        lines = [f"📋 <b>QUESTION BANK</b>  ·  {total} questions\n{UI.LINE}\n"]

        for q in questions[:20]:
            qid   = q.get("id", "?")
            qtext = q.get("question", "")[:45]
            cat   = q.get("category", "General")
            emoji = UI.cat_emoji(cat)
            lines.append(
                f"  {emoji} <code>#{qid}</code>  {qtext}"
                + ("…" if len(q.get("question", "")) > 45 else "")
            )

        if total > 20:
            lines.append(f"\n  <i>… and {total-20} more questions</i>")

        lines.append(
            f"\n{UI.LINE}\n"
            f"  /delquiz   Delete a question\n"
            f"  /addquiz   Add a question\n"
            f"  /reload    Sync from database"
        )

        if msg: await self._edit(msg, "\n".join(lines))

    # ─── /dev ────────────────────────────────────────────────

    async def cmd_dev(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        if self._dev and hasattr(self._dev, "dev"):
            await self._dev.dev(update, context)
            return

        msg = await self._reply(update, "🛠️ <i>Loading developer panel...</i>")
        await asyncio.sleep(0.3)

        mention = OWNER_LINK if self._is_owner(user.id) else UI.mention(user.id, UI.display_name(user))
        q_count = self._q_count()
        users = groups = 0
        if self.db:
            try:
                stats  = await asyncio.to_thread(self.db.get_user_engagement_stats)
                users  = stats.get('total_users', 0)
                groups_list = await asyncio.to_thread(self.db.get_active_groups)
                groups = len(groups_list)
            except Exception:
                pass

        text = (
            f"🛠️ <b>DEVELOPER PANEL</b>\n"
            f"{UI.LINE}\n\n"
            f"  {mention}\n\n"
            f"<b>LIVE STATS</b>\n"
            f"{UI.THIN}\n"
            f"  Questions  ›  <b>{q_count}</b>\n"
            f"  Users      ›  <b>{users}</b>\n"
            f"  Groups     ›  <b>{groups}</b>\n\n"
            f"<b>COMMANDS</b>\n"
            f"{UI.THIN}\n"
            f"  /addquiz   /delquiz   /editquiz\n"
            f"  /broadcast /reload    /restart\n\n"
            f"{UI.LINE}\n"
            f"  Owner ID: <code>{OWNER_ID}</code>"
        )
        if msg: await self._edit(msg, text)

    # ─── /broadcast ──────────────────────────────────────────

    async def cmd_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self._is_owner(user.id):
            await self._unauthorized(update)
            return

        if self._dev and hasattr(self._dev, "broadcast"):
            await self._dev.broadcast(update, context)
            return

        raw = (update.effective_message.text or "")\
            .replace("/broadcast", "").replace("/bc", "").strip()

        if not raw:
            await self._reply(update,
                f"📡 <b>BROADCAST</b>\n"
                f"{UI.LINE}\n\n"
                "<b>Usage:</b>\n"
                "  <code>/broadcast Your message here</code>\n\n"
                "Supports HTML: <code>&lt;b&gt;</code> <code>&lt;i&gt;</code>\n"
                "Alias: <code>/bc</code>"
            )
            return

        if not self.db:
            await self._reply(update, "❌ Database not available.")
            return

        users, groups = await asyncio.gather(
            asyncio.to_thread(self.db.get_pm_accessible_users),
            asyncio.to_thread(self.db.get_active_groups),
        )
        total  = len(users) + len(groups)

        owner_mention = OWNER_LINK
        status = await self._reply(update,
            f"📡 <b>BROADCASTING</b>\n"
            f"{UI.LINE}\n\n"
            f"  By {owner_mention}\n\n"
            f"  Users  ›  <b>{len(users)}</b>\n"
            f"  Groups ›  <b>{len(groups)}</b>\n"
            f"  Total  ›  <b>{total}</b>\n\n"
            f"  <i>Sending...</i>"
        )

        self._broadcast_sent.clear()
        sent = failed = 0
        for u in users:
            try:
                m = await context.bot.send_message(
                    chat_id=u["user_id"], text=raw, parse_mode=ParseMode.HTML,
                    link_preview_options=_NO_PREVIEW)
                self._broadcast_sent.append((u["user_id"], m.message_id))
                sent += 1
                await asyncio.sleep(0.05)
            except RetryAfter as e:
                wait = e.retry_after + 1
                logger.warning(f"BC rate-limited — sleeping {wait}s")
                await asyncio.sleep(wait)
                try:
                    m = await context.bot.send_message(
                        chat_id=u["user_id"], text=raw, parse_mode=ParseMode.HTML,
                        link_preview_options=_NO_PREVIEW)
                    self._broadcast_sent.append((u["user_id"], m.message_id))
                    sent += 1
                except Exception:
                    failed += 1
            except (Forbidden, BadRequest):
                failed += 1
            except Exception as e:
                logger.error(f"BC user {u['user_id']}: {e}")
                failed += 1

        for g in groups:
            tid = g.get("message_thread_id")
            try:
                kwargs = {"chat_id": g["chat_id"], "text": raw, "parse_mode": ParseMode.HTML,
                          "link_preview_options": _NO_PREVIEW}
                if tid: kwargs["message_thread_id"] = tid
                gm = await context.bot.send_message(**kwargs)
                self._broadcast_sent.append((g["chat_id"], gm.message_id))
                sent += 1
                await asyncio.sleep(0.05)
            except RetryAfter as e:
                wait = e.retry_after + 1
                logger.warning(f"BC group rate-limited — sleeping {wait}s")
                await asyncio.sleep(wait)
                try:
                    kwargs2 = {"chat_id": g["chat_id"], "text": raw,
                               "parse_mode": ParseMode.HTML,
                               "link_preview_options": _NO_PREVIEW}
                    gm = await context.bot.send_message(**kwargs2)
                    self._broadcast_sent.append((g["chat_id"], gm.message_id))
                    sent += 1
                except Exception:
                    failed += 1
            except TelegramError as e:
                if any(w in str(e).lower() for w in ("topic", "closed", "thread")):
                    try:
                        gm = await context.bot.send_message(
                            chat_id=g["chat_id"], text=raw, parse_mode=ParseMode.HTML,
                            link_preview_options=_NO_PREVIEW)
                        self._broadcast_sent.append((g["chat_id"], gm.message_id))
                        sent += 1
                    except Exception:
                        failed += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"BC group {g.get('chat_id')}: {e}")
                failed += 1

        rate = int(sent / total * 100) if total else 0
        if status:
            await self._edit(status,
                f"✅ <b>BROADCAST COMPLETE</b>\n"
                f"{UI.LINE}\n\n"
                f"  Sent    ›  <b>{sent}</b>\n"
                f"  Failed  ›  <b>{failed}</b>\n"
                f"  Rate    ›  [{UI.bar(rate)}] <b>{rate}%</b>"
            )

    # ─── /delbroadcast ───────────────────────────────────────

    async def cmd_delbroadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self._is_owner(user.id):
            await self._unauthorized(update)
            return

        if self._dev and hasattr(self._dev, "delbroadcast"):
            await self._dev.delbroadcast(update, context)
            return

        if not self._broadcast_sent:
            await self._reply(update,
                "📭 <b>Nothing to delete</b>\n"
                f"{UI.LINE}\n\n"
                "No broadcast messages are tracked.\n"
                "Send a broadcast first with /broadcast."
            )
            return

        total_to_del = len(self._broadcast_sent)
        msg = await self._reply(update,
            f"🗑 <i>Deleting {total_to_del} broadcast messages...</i>"
        )

        deleted = failed = 0
        for chat_id, msg_id in self._broadcast_sent:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                deleted += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        self._broadcast_sent.clear()
        if msg:
            await self._edit(msg,
                f"✅ <b>BROADCAST DELETED</b>\n"
                f"{UI.LINE}\n\n"
                f"  Deleted  ›  <b>{deleted}</b>\n"
                f"  Failed   ›  <b>{failed}</b>"
            )

    # ─── /reload ─────────────────────────────────────────────

    async def cmd_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        mention = OWNER_LINK if self._is_owner(user.id) else UI.mention(user.id, UI.display_name(user))
        msg = await self._reply(update, "🔄 <i>Syncing from MongoDB...</i>")
        await asyncio.sleep(0.4)

        try:
            old = len(self.quiz_manager.questions)
            self.quiz_manager.reload_data()
            new  = len(self.quiz_manager.questions)
            diff = new - old
            sign = "+" if diff >= 0 else ""
            text = (
                f"✅ <b>RELOAD COMPLETE</b>\n"
                f"{UI.LINE}\n\n"
                f"  By {mention}\n\n"
                f"  Questions ›  <b>{new}</b>  <i>({sign}{diff})</i>\n"
                f"  Source    ›  MongoDB Atlas\n"
                f"  Cache     ›  ✅ Refreshed"
            )
        except Exception as e:
            text = (
                f"❌ <b>RELOAD FAILED</b>\n"
                f"{UI.LINE}\n\n"
                f"  Error: <code>{e}</code>"
            )
        if msg: await self._edit(msg, text)

    # ─── /restart ────────────────────────────────────────────

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self._is_owner(user.id):
            await self._unauthorized(update)
            return

        await self._reply(update,
            f"🔄 <b>RESTARTING</b>\n"
            f"{UI.LINE}\n\n"
            f"  Initiated by {OWNER_LINK}\n"
            f"  Shutting down gracefully...\n"
            f"  ✅ Back online in seconds!"
        )
        import sys
        os.makedirs("data", exist_ok=True)
        open("data/.restart_flag", "w").close()
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ─── /importquiz ─────────────────────────────────────────

    async def cmd_importquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        text = (
            f"📥 <b>BULK IMPORT</b>\n"
            f"{UI.LINE}\n\n"
            f"  Send a <b>.txt file</b> to this chat.\n\n"
            f"<b>AUTO-DETECTED FORMATS</b>\n"
            f"{UI.THIN}\n"
            f"  ◈ Numbered  —  1. / Q1:\n"
            f"  ◈ Options   —  A) B) C) D)\n"
            f"  ◈ Answer    —  Answer: B / Ans: 2\n"
            f"  ◈ Inline    —  All on same line\n"
            f"  ◈ Asterisk  —  C) opt *\n\n"
            f"<b>PROTECTION</b>\n"
            f"{UI.THIN}\n"
            f"  ✅ Duplicate detection\n"
            f"  ✅ Format validation\n"
            f"  ✅ Auto category tagging\n"
            f"  ✅ Import report\n\n"
            f"{UI.LINE}\n"
            f"  <i>Send your .txt file to begin →</i>"
        )
        await self._reply(update, text)

    # ─── DOCUMENT HANDLER — Bulk .txt import ─────────────────
    #
    # Architecture: handle_document() validates, downloads, and decodes
    # the file, then fires a background asyncio Task and RETURNS immediately.
    # The bot is fully responsive while the import runs.  The background
    # task edits the status message when the import completes.
    #
    # _import_users (event-loop-only set) prevents a single admin from
    # accidentally queuing multiple concurrent imports of the same file.

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user

        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        doc = update.effective_message.document
        if not doc:
            return

        mention = UI.mention(user.id, UI.display_name(user))
        fname   = doc.file_name or ""
        is_txt  = (
            fname.lower().endswith(".txt") or
            (doc.mime_type or "").startswith("text/")
        )
        if not is_txt:
            await self._reply(update,
                f"⚠️ <b>Wrong File Type</b>\n"
                f"{UI.LINE}\n\n"
                f"  Please send a <b>.txt</b> file.\n"
                f"  Use /importquiz to see the format guide."
            )
            return

        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await self._reply(update,
                f"❌ <b>File Too Large</b>\n"
                f"{UI.LINE}\n\n"
                f"  Maximum size: 20 MB.\n"
                f"  Split into smaller files."
            )
            return

        # ── Per-user concurrent-import guard ──────────────────────────────
        # Accessed only from the event loop — no threading lock needed.
        if not hasattr(self, '_import_users'):
            self._import_users: set = set()

        if user.id in self._import_users:
            await self._reply(update,
                f"⏳ <b>Import Already Running</b>\n"
                f"{UI.LINE}\n\n"
                f"  Your previous import is still in progress.\n"
                f"  Please wait for it to finish before sending another file."
            )
            return
        self._import_users.add(user.id)

        # ── Download & decode (fast async — does not block event loop) ────
        msg = await self._reply(update,
            f"📥 <b>IMPORT QUEUED</b>\n"
            f"{UI.LINE}\n\n"
            f"  By {mention}\n"
            f"  📄 <code>{fname}</code>\n"
            f"  Size: <code>{doc.file_size or 0:,} bytes</code>\n\n"
            f"  ⏳ Downloading..."
        )

        try:
            file_obj  = await context.bot.get_file(
                doc.file_id, read_timeout=60, write_timeout=60, connect_timeout=60)
            raw_bytes = await file_obj.download_as_bytearray(read_timeout=60)
        except Exception as e:
            logger.error(f"File download error: {e}")
            self._import_users.discard(user.id)
            if msg:
                await self._edit(msg,
                    f"❌ <b>Download Failed</b>\n"
                    f"{UI.LINE}\n\n"
                    f"  Error: <code>{e}</code>"
                )
            return

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw_bytes.decode("latin-1")
            except Exception:
                self._import_users.discard(user.id)
                if msg:
                    await self._edit(msg,
                        f"❌ <b>Encoding Error</b>\n"
                        f"{UI.LINE}\n\n"
                        f"  Please save the file as UTF-8 and retry."
                    )
                return

        line_count = len(text.splitlines())
        if msg:
            await self._edit(msg,
                f"📥 <b>IMPORT IN PROGRESS</b>\n"
                f"{UI.LINE}\n\n"
                f"  By {mention}\n"
                f"  📄 <code>{fname}</code>\n"
                f"  📊 {line_count:,} lines detected\n\n"
                f"  ⚙️ Parsing &amp; importing in background...\n"
                f"  <i>Bot is fully active. You will be notified when done.</i>"
            )

        # ── Fire background task — handler returns immediately ─────────────
        task = asyncio.create_task(
            self._run_import_background(text, fname, mention, user.id, msg),
            name=f"import-uid{user.id}",
        )
        # Surface unhandled task exceptions to the log
        task.add_done_callback(
            lambda t: logger.error(
                f"[IMPORT] Background task raised: {t.exception()}"
            ) if not t.cancelled() and t.exception() else None
        )

    async def _run_import_background(
        self,
        text:    str,
        fname:   str,
        mention: str,
        user_id: int,
        msg,
    ):
        """Background task: parse → batch-insert → report.
        Runs parse+insert in asyncio.to_thread() so the event loop stays
        free to handle /quiz, callbacks, broadcasts, etc. simultaneously."""
        try:
            t0 = time.monotonic()
            from src.bot.quiz_parser import bulk_import
            result  = await asyncio.to_thread(bulk_import, text, self.quiz_manager)
            elapsed = time.monotonic() - t0

            detected = result.get("total_detected", 0)
            imported = result.get("imported", 0)
            skipped  = result.get("skipped", 0)
            failed   = result.get("failed", 0)
            errors   = result.get("errors", [])
            total_q  = self._q_count()

            rate = int(imported / max(detected, 1) * 100)
            bar  = UI.bar(rate)

            text_out = (
                f"📊 <b>IMPORT REPORT</b>\n"
                f"{UI.LINE}\n\n"
                f"  By {mention}\n"
                f"  📄 <code>{fname}</code>\n\n"
                f"<b>RESULTS</b>\n"
                f"{UI.THIN}\n"
                f"  Detected  ›  <b>{detected}</b>\n"
                f"  Imported  ›  <b>{imported}</b>\n"
                f"  Skipped   ›  <b>{skipped}</b>  <i>(duplicates)</i>\n"
                f"  Failed    ›  <b>{failed}</b>\n\n"
                f"  Success   ›  [{bar}] <b>{rate}%</b>\n\n"
                f"  📦 Total in DB: <b>{total_q}</b>\n"
                f"  ⏱ Time      ›  <b>{elapsed:.1f}s</b>\n"
            )
            if errors:
                text_out += f"\n<b>ERRORS (first {min(len(errors), 3)}):</b>\n"
                for err in errors[:3]:
                    text_out += f"  <code>{str(err)[:65]}</code>\n"
            text_out += f"\n{UI.LINE}\n  <i>Use /quiz to test your new questions!</i>"

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎓 Play Quiz", callback_data="play_quiz"),
            ]])
            if msg:
                await self._edit(msg, text_out, kb)

        except Exception as e:
            logger.error(f"[IMPORT] _run_import_background failed: {e}", exc_info=True)
            if msg:
                try:
                    await self._edit(msg,
                        f"❌ <b>Import Failed</b>\n"
                        f"{UI.LINE}\n\n"
                        f"  Error: <code>{str(e)[:120]}</code>"
                    )
                except Exception:
                    pass
        finally:
            if hasattr(self, '_import_users'):
                self._import_users.discard(user_id)
