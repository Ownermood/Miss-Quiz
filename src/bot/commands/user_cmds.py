"""
UserCommandsMixin — /start /help /ping /info /categories
"""

import asyncio
import logging
import time
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.bot.ui import UI, COMMUNITY, _NO_PREVIEW

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

        # Reset nav history to home
        if user:
            self._nav_clear(user.id)

        # Track groups via central pipeline
        if not is_pm:
            self.ensure_group_registered(update, context, source="cmd-start")

        bot_inline = "Miss Quiz 🎓"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Start Quiz",       callback_data="play_quiz"),
             InlineKeyboardButton("🎓 My Profile",         callback_data="my_profile")],
            [InlineKeyboardButton("❓ Help",              callback_data="help")],
            [InlineKeyboardButton("🎓 Join CLAT Vision",  url="https://t.me/CLAT_Vision")],
        ])

        text = self._build_greeting(mention, bot_inline)

        if edit_msg:
            # Navigating back to home — edit existing message
            result = await self._smart_edit(update, text, kb, edit_msg=edit_msg)
        elif is_pm:
            # Reveal animation for fresh /start in PM
            result = await self._reply(update, "🌸")
            await asyncio.sleep(0.3)
            await self._edit(result, "🎓  <b>𝐂𝐋𝐀𝐓 𝐕𝐈𝐒𝐈𝐎𝐍</b>  🎓")
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
        is_authorized = await self._is_authorized(update.effective_user.id) if update.effective_user else False

        text = (
            f"╔══════════════════════════════════════════╗\n"
            f"║   🎓  <b>𝐂𝐋𝐀𝐓 𝐕𝐈𝐒𝐈𝐎𝐍</b>  ·  Command Guide   ║\n"
            f"╚══════════════════════════════════════════╝\n\n"

            f"🎯  <b>𝐐𝐔𝐈𝐙  𝐂𝐄𝐍𝐓𝐄𝐑</b>\n"
            f"╭──────────────────────────────────────────╮\n"
            f"│  /quiz              ›  Start a quiz\n"
            f"│  /q                  ›  Quick shortcut\n"
            f"│  /categories      ›  Browse all topics\n"
            f"╰──────────────────────────────────────────╯\n\n"

            f"📊  <b>𝐏𝐑𝐎𝐆𝐑𝐄𝐒𝐒  𝐂𝐄𝐍𝐓𝐄𝐑</b>\n"
            f"╭──────────────────────────────────────────╮\n"
            f"│  /score           ›  Scorecard &amp; rank\n"
            f"│  /stats            ›  Full analytics\n"
            f"│  /achievements  ›  Badges &amp; milestones\n"
            f"╰──────────────────────────────────────────╯\n\n"

            f"🔧  <b>𝐒𝐘𝐒𝐓𝐄𝐌</b>\n"
            f"╭──────────────────────────────────────────╮\n"
            f"│  /ping    ›  Latency check\n"
            f"│  /info     ›  Bot information\n"
            f"│  /start   ›  Dashboard\n"
            f"╰──────────────────────────────────────────╯\n"
        )

        if is_authorized:
            text += (
                f"\n👑  <b>𝐀𝐃𝐌𝐈𝐍  𝐂𝐄𝐍𝐓𝐄𝐑</b>  · Owner &amp; Devs only\n"
                f"╭──────────────────────────────────────────╮\n"
                f"│  /dev           ›  Admin panel\n"
                f"│  /addquiz      ›  Add question\n"
                f"│  /editquiz     ›  Edit question\n"
                f"│  /delquiz      ›  Delete question\n"
                f"│  /importquiz  ›  Bulk import (.txt)\n"
                f"│  /broadcast      ›  Message everyone\n"
                f"│  /bc                 ›  Broadcast shortcut\n"
                f"│  /delbroadcast  ›  Delete last broadcast\n"
                f"│  /botstats    ›  Platform analytics\n"
                f"│  /reload        ›  Sync from database\n"
                f"│  /restart       ›  Restart bot\n"
                f"╰──────────────────────────────────────────╯\n"
            )

        text += (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡  {COMMUNITY}  ·  CLAT 2027"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Play Quiz",   callback_data="play_quiz"),
             InlineKeyboardButton("ℹ️ Bot Info",    callback_data="info")],
            self._nav_row(back_screen="home"),
        ])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)

    # ─── /categories ─────────────────────────────────────────

    async def cmd_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                              edit_msg=None):
        DIV = "═" * 32
        cat_lines = "\n".join(
            f"•  {name}  {emoji}" for name, emoji in UI.QUIZ_CATEGORIES
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
            f"  <i>CLAT Vision Quiz Bot</i>"
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
            f"╭──────────────────────────────────────╮\n"
            f"│  📛  Name        ›  CLAT Vision Quiz Bot\n"
            f"│  📚  Questions   ›  <b>{q_count}</b>\n"
            f"│  🗄  Database    ›  MongoDB Atlas ✅\n"
            f"│  👑  Owner       ›  {OWNER_LINK}\n"
            f"╰──────────────────────────────────────╯\n\n"
            f"💬  <b>𝐓𝐇𝐈𝐒  𝐂𝐇𝐀𝐓</b>\n"
            f"╭──────────────────────────────────────╮\n"
            f"│  🆔  Chat ID     ›  <code>{chat.id}</code>\n"
            f"│  📌  Type        ›  {chat_type}\n"
            f"╰──────────────────────────────────────╯\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡  {COMMUNITY}  ·  CLAT 2027"
        )
        kb = InlineKeyboardMarkup([self._nav_row(back_screen="home")])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)
