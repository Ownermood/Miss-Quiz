"""
UserCommandsMixin — /start /help /ping /info /categories
"""

import asyncio
import html
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.bot.ui import UI, COMMUNITY, get_thread_id

logger = logging.getLogger(__name__)


class UserCommandsMixin(object):

    # ─── /start ──────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                        edit_msg=None):
        user    = update.effective_user
        name    = UI.display_name(user)
        mention = UI.mention(user.id, name)
        chat    = update.effective_chat
        is_pm   = chat.type == "private"

        # Deep link in a group: /start gq_<quiz id> (quiz card → Start in Group)
        if not is_pm and context.args and context.args[0].startswith("gq_") and hasattr(self, "gq_start_from_link"):
            await self.gq_start_from_link(update, context, context.args[0][3:])
            return

        # Deep link: /start mq / mq_new → own quizzes
        if is_pm and context.args and context.args[0] in ("mq", "mq_new") and hasattr(self, "mq_start_draft"):
            if context.args[0] == "mq_new":
                await self.mq_start_draft(None, context, user, update)
            else:
                await self.mq_open_list(update, context)
            return

        # Deep link: /start bt_<id> joins a DM battle
        if is_pm and context.args and context.args[0].startswith("bt_") \
                and hasattr(self, "battle_join_from_link"):
            await self.battle_join_from_link(update, context, context.args[0][3:])
            return

        # Reset nav history to home
        if user:
            self._nav_clear(user.id)

        # Track groups via central pipeline
        if not is_pm:
            self.ensure_group_registered(update, context, source="cmd-start")

        bot_inline = "Miss Quiz 🎓"

        # Admin Panel button only in an admin's own DM
        show_admin = bool(is_pm and user and await self._is_authorized(user.id))
        kb = self._start_kb(show_admin=show_admin)

        text = self._build_greeting(mention, bot_inline)
        photo = await self._get_start_photo() if hasattr(self, "_get_start_photo") else None

        if photo and not edit_msg:
            try:
                result = await update.effective_message.reply_photo(
                    photo, caption=text, parse_mode="HTML", reply_markup=kb,
                    message_thread_id=get_thread_id(update))
            except Exception as ex:   # bad/expired photo: fall back to text
                logger.warning(f"start photo failed: {ex}")
                result = await self._reply(update, text, reply_markup=kb)
        elif edit_msg:
            # Navigating back to home — edit existing message
            result = await self._smart_edit(update, text, kb, edit_msg=edit_msg)
        elif is_pm:
            # Reveal animation for fresh /start in PM
            result = await self._reply(update, "🌸")
            await asyncio.sleep(0.3)
            await self._edit(result, "🎓  <b>𝐌𝐈𝐒𝐒 𝐐𝐔𝐈𝐙</b>  🎓")
            await asyncio.sleep(0.35)
            await self._edit(result, text, kb)
        else:
            result = await self._reply(update, text, reply_markup=kb)

        # Store as active message for this user
        if user and result:
            self._active_msg[user.id] = result

        # Register user in DB (is_pm is known here from chat.type check above)
        if self.db:
            try:
                self.ensure_user_registered(user, is_pm=is_pm if is_pm else None, source="cmd-start")
            except Exception as e:
                logger.error(f"upsert_user: {e}")

    # ─── /help ───────────────────────────────────────────────

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                       edit_msg=None):
        from src.bot.premium import btn, e
        text = (
            f"{e('question')} <b>HELP · MISS QUIZ</b>\n\n"
            f"<blockquote><b>{e('play')} PLAY\n"
            f"/quiz — get a question\n"
            f"/battle — 1v1 with a friend\n"
            f"/myquiz — make your own quiz\n"
            f"/categories — all topics</b></blockquote>\n"
            f"<blockquote><b>{e('stats')} YOUR PROGRESS\n"
            f"/score — scorecard &amp; rank\n"
            f"/stats — full analytics\n"
            f"/achievements — badges</b></blockquote>\n"
            f"<blockquote><b>{e('users')} IN GROUPS\n"
            f"Make me admin → a new quiz every hour.\n"
            f"/startquiz — run a full quiz (admins)\n"
            f"/quiz and /battle work without admin too.</b></blockquote>\n"
            f"<b>{e('signal')} Need help? {COMMUNITY}</b>"
        )
        from src.bot.ui import COMMUNITY_URL
        kb = InlineKeyboardMarkup([
            [btn("Play Quiz", "play", "primary", callback_data="play_quiz"),
             btn("Battle", "fight", "primary", callback_data="start_battle")],
            [btn("Community", "users", "success", url=COMMUNITY_URL)],
            self._nav_row(back_screen="home"),
        ])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)

    # ─── /categories ─────────────────────────────────────────

    async def cmd_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                              edit_msg=None):
        DIV = "═" * 32
        cat_lines = "\n".join(
            f"•  {html.escape(name)}  {emoji}" for name, emoji in UI.QUIZ_CATEGORIES
        )
        text = (
            f"📚  <b>𝗩𝗜𝗘𝗪  𝗖𝗔𝗧𝗘𝗚𝗢𝗥𝗜𝗘𝗦</b>\n"
            f"{DIV}\n\n"
            f"📑  <b>𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘  𝗤𝗨𝗜𝗭  𝗖𝗔𝗧𝗘𝗚𝗢𝗥𝗜𝗘𝗦</b>\n\n"
            f"{cat_lines}\n\n"
            f"{DIV}\n"
            f"🎯  Stay tuned!  More quizzes coming soon!\n"
            f"🛠  Need help?  Use /help for more commands!\n"
            f"{DIV}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Start Quiz", callback_data="play_quiz"),
             InlineKeyboardButton("🎓 Commands",   callback_data="help")],
            self._nav_row(back_screen="home"),
        ])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)

    # ─── /ping ───────────────────────────────────────────────

    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        t0  = time.time()
        msg = await self._reply(update, "🏓 <i>Measuring latency...</i>")
        ms  = int((time.time() - t0) * 1000)
        if not msg:
            return

        q_count = self._q_count()
        bar     = UI.bar(min(100, ms / 10))
        if   ms < 100: status = "⚡ Blazing fast"
        elif ms < 300: status = "✅ Fast"
        elif ms < 600: status = "🟡 Normal"
        else:          status = "🔴 Slow"

        text = (
            f"🏓 <b>PONG</b>\n"
            f"{UI.LINE}\n\n"
            f"  Latency   ›  <code>{ms} ms</code>\n"
            f"  [{bar}]\n"
            f"  Status    ›  <b>{status}</b>\n\n"
            f"  Questions ›  <b>{q_count}</b> loaded\n"
            f"  Bot       ›  🟢 Online\n\n"
            f"{UI.LINE}\n"
            f"  <i>Miss Quiz Bot</i>"
        )
        await self._edit(msg, text)

    # ─── /info ───────────────────────────────────────────────

    async def cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                       edit_msg=None):
        chat    = update.effective_chat
        q_count = self._q_count()

        _type_map = {
            "private":    "DM",
            "group":      "Group",
            "supergroup":  "Supergroup",
            "channel":    "Channel",
        }
        chat_type = _type_map.get(chat.type, chat.type)

        from src.bot.ui import OWNER_LINK
        text = (
            f"ℹ️  <b>𝐁𝐎𝐓  𝐈𝐍𝐅𝐎𝐑𝐌𝐀𝐓𝐈𝐎𝐍</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🤖  <b>𝐁𝐎𝐓</b>\n"
            f"<blockquote>"
            f"📛  Name        ›  Miss Quiz Bot\n"
            f"📚  Questions   ›  <b>{q_count}</b>\n"
            f"🗄  Database    ›  MongoDB ✅\n"
            f"👑  Owner       ›  {OWNER_LINK}\n"
            f"</blockquote>\n\n"
            f"💬  <b>𝐓𝐇𝐈𝐒  𝐂𝐇𝐀𝐓</b>\n"
            f"<blockquote>"
            f"🆔  Chat ID     ›  <code>{chat.id}</code>\n"
            f"📌  Type        ›  {chat_type}\n"
            f"</blockquote>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡  {COMMUNITY}"
        )
        kb = InlineKeyboardMarkup([self._nav_row(back_screen="home")])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)


    # ─── /mywords (Safai Filter Custom Words) ──────────────────

    async def cmd_mywords(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/mywords [add|del|clear] [word] - manage custom blacklist/filter words."""
        user = update.effective_user
        if not user:
            return

        words = []
        if self.db and hasattr(self.db, "get_user_custom_words"):
            words = await asyncio.to_thread(self.db.get_user_custom_words, user.id)

        args = context.args or []
        if not args:
            w_list = ", ".join(f"<code>{html.escape(w)}</code>" for w in words) if words else "<i>No custom words set yet.</i>"
            text = (
                "🧹 <b>SAFAI FILTER — CUSTOM WORDS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Polls and text imported into your quizzes will automatically filter out:\n"
                "• <code>[1/100]</code> counters\n"
                "• <code>@channel_names</code> and invite links\n"
                "• Your custom words below:\n\n"
                f"📝 <b>Your Words:</b>\n{w_list}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "👉 <b>How to use:</b>\n"
                "• <code>/mywords add &lt;word&gt;</code> — add word to blacklist\n"
                "• <code>/mywords del &lt;word&gt;</code> — remove word\n"
                "• <code>/mywords clear</code> — clear all custom words"
            )
            await self._reply(update, text)
            return

        sub = args[0].lower()
        if sub == "clear":
            if self.db and hasattr(self.db, "set_user_custom_words"):
                await asyncio.to_thread(self.db.set_user_custom_words, user.id, [])
            await self._reply(update, "🧹 <b>Cleared!</b> All your custom filter words have been removed.")
            return

        target = " ".join(args[1:]).strip() if len(args) > 1 else ""
        if not target:
            await self._reply(update, f"⚠️ Please specify a word: <code>/mywords {sub} &lt;word&gt;</code>")
            return

        if sub == "add":
            if target.lower() not in [w.lower() for w in words]:
                words.append(target)
                if self.db and hasattr(self.db, "set_user_custom_words"):
                    await asyncio.to_thread(self.db.set_user_custom_words, user.id, words)
                await self._reply(update, f"✅ Added <code>{html.escape(target)}</code> to your auto-clean filter!")
            else:
                await self._reply(update, f"ℹ️ <code>{html.escape(target)}</code> is already in your list.")
        elif sub in ("del", "remove", "delete"):
            new_words = [w for w in words if w.lower() != target.lower()]
            if len(new_words) != len(words):
                if self.db and hasattr(self.db, "set_user_custom_words"):
                    await asyncio.to_thread(self.db.set_user_custom_words, user.id, new_words)
                await self._reply(update, f"🗑️ Removed <code>{html.escape(target)}</code> from your filter list.")
            else:
                await self._reply(update, f"⚠️ <code>{html.escape(target)}</code> was not found in your list.")
        else:
            await self._reply(update, "👉 Usage: <code>/mywords add &lt;word&gt;</code> or <code>/mywords del &lt;word&gt;</code>")


    # ─── /clone & /mix (Quiz Management) ──────────────────────

    async def cmd_clone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/clone <quiz_id> - duplicate any quiz to your own collection."""
        user = update.effective_user
        if not user:
            return

        args = context.args or []
        if not args:
            await self._reply(update, "👉 <b>Usage:</b> <code>/clone &lt;quiz_id&gt;</code>\nExample: <code>/clone gq_abc123</code>")
            return

        sid = args[0].removeprefix("gq_").strip()
        qs = None
        if self.db and hasattr(self.db, "get_quiz_set"):
            qs = await asyncio.to_thread(self.db.get_quiz_set, sid)

        if not qs or not qs.get("questions"):
            await self._reply(update, "⚠️ <b>Quiz not found.</b> Please check the quiz ID.")
            return

        import secrets
        new_sid = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")
        new_title = f"Copy of {qs.get('title', 'Quiz')}"[:60]
        new_doc = {
            "id": new_sid,
            "owner": user.id,
            "title": new_title,
            "count": len(qs["questions"]),
            "questions": list(qs["questions"]),
            "secs": int(qs.get("secs") or 20)
        }
        if self.db and hasattr(self.db, "save_quiz_set"):
            await asyncio.to_thread(self.db.save_quiz_set, new_doc)

        await self._reply(
            update,
            f"🎉 <b>Quiz Cloned Successfully!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Title:</b> <code>{html.escape(new_title)}</code>\n"
            f"❓ <b>Questions:</b> {len(qs['questions'])}\n"
            f"🆔 <b>New ID:</b> <code>{new_sid}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>You can now edit, battle, or run this quiz from /myquiz!</i>"
        )

    async def cmd_mix(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/mix <id1> <id2> ... - combine multiple quizzes into one mega quiz."""
        user = update.effective_user
        if not user:
            return

        args = context.args or []
        if len(args) < 2:
            await self._reply(update, "👉 <b>Usage:</b> <code>/mix &lt;id1&gt; &lt;id2&gt; [id3...]</code>\nCombines 2 or more quizzes into a mega quiz.")
            return

        combined_qs = []
        seen = set()
        titles = []

        for raw_id in args:
            sid = raw_id.removeprefix("gq_").strip()
            qs = None
            if self.db and hasattr(self.db, "get_quiz_set"):
                qs = await asyncio.to_thread(self.db.get_quiz_set, sid)
            if qs and qs.get("questions"):
                titles.append(qs.get("title", "Quiz")[:15])
                for q in qs["questions"]:
                    key = str(q.get("question") or "").strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        combined_qs.append(q)

        if len(combined_qs) < 3:
            await self._reply(update, "⚠️ <b>Not enough valid questions found</b> across the specified IDs.")
            return

        import secrets
        new_sid = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")
        mixed_title = f"Mixed: {' + '.join(titles[:2])}"[:50]
        new_doc = {
            "id": new_sid,
            "owner": user.id,
            "title": mixed_title,
            "count": len(combined_qs),
            "questions": combined_qs,
            "secs": 20
        }
        if self.db and hasattr(self.db, "save_quiz_set"):
            await asyncio.to_thread(self.db.save_quiz_set, new_doc)

        await self._reply(
            update,
            f"🔀 <b>Mega Quiz Created!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Title:</b> <code>{html.escape(mixed_title)}</code>\n"
            f"❓ <b>Total Questions:</b> {len(combined_qs)}\n"
            f"🆔 <b>Quiz ID:</b> <code>{new_sid}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Saved in /myquiz! Ready to play or run in groups.</i>"
        )
