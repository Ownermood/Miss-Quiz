"""
handlers_main.py — TelegramQuizBot: assembles all mixins into the final bot class.
"""

import asyncio
import logging
import time
from typing import Optional, Any
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, LinkPreviewOptions
)
from telegram.ext import (
    Application, CommandHandler, PollAnswerHandler,
    CallbackQueryHandler, MessageHandler, ChatMemberHandler,
    filters, ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest

from src.bot.ui import (
    UI, OWNER_ID, OWNER_LINK, COMMUNITY, _NO_PREVIEW,
    get_thread_id, get_tracking_id
)
from src.bot.tracking import TrackingMixin
from src.bot.poll_manager import PollMixin
from src.bot.commands.user_cmds import UserCommandsMixin
from src.bot.commands.quiz_cmds import QuizCommandsMixin
from src.bot.commands.leaderboard_cmds import LeaderboardMixin
from src.bot.commands.admin_cmds import AdminCommandsMixin

logger = logging.getLogger(__name__)


class TelegramQuizBot(
    PollMixin,
    TrackingMixin,
    UserCommandsMixin,
    QuizCommandsMixin,
    LeaderboardMixin,
    AdminCommandsMixin,
):

    def __init__(self, quiz_manager, db_manager=None):
        self.quiz_manager             = quiz_manager
        self.db                       = db_manager
        self.application: Optional[Application] = None
        self._dev                     = None
        self._del_page: dict          = {}
        self._active_msg: dict        = {}
        self._nav_history: dict       = {}
        self._broadcast_sent: list    = []
        self._start_ts: float         = time.time()
        self._seen_groups: set        = set()   # perf cache: groups upserted this session
        self._seen_users: dict        = {}      # perf cache: {user_id: epoch} for time-bound dedup
        self._poll_stats: dict = {
            "stored":           0,
            "recovered_db":     0,
            "recovered_pickle": 0,
            "lost":             0,
        }

    def _q_count(self) -> int:
        """Live question count — DB first, in-memory fallback."""
        if self.db:
            n = self.db.get_question_count()
            if n:
                return n
        return len(self.quiz_manager.questions) if self.quiz_manager else 0

    # ─── Navigation helpers ───────────────────────────────────

    def _nav_push(self, user_id: int, screen: str):
        """Push current screen to history before navigating away."""
        hist = self._nav_history.setdefault(user_id, [])
        if not hist or hist[-1] != screen:
            hist.append(screen)
        if len(hist) > 10:
            hist.pop(0)

    def _nav_pop(self, user_id: int) -> str:
        """Pop and return previous screen, or 'home' if empty."""
        hist = self._nav_history.get(user_id, [])
        if len(hist) > 1:
            hist.pop()          # remove current
            return hist[-1]     # return previous (don't pop it, so Back works repeatedly)
        return "home"

    def _nav_clear(self, user_id: int):
        """Clear history (for Home button)."""
        self._nav_history[user_id] = ["home"]

    async def _smart_edit(self, update, text: str, kb, edit_msg=None):
        """
        Edit edit_msg if provided. Otherwise try active_msg for user.
        If all fails, send new message and store it.
        """
        from telegram.error import BadRequest as BR
        user   = update.effective_user
        target = edit_msg

        if target is None and user:
            target = self._active_msg.get(user.id)

        if target:
            try:
                await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                       link_preview_options=_NO_PREVIEW)
                if user:
                    self._active_msg[user.id] = target
                return target
            except BR as e:
                if "not modified" in str(e).lower():
                    return target      # same content, fine
                # message deleted/too old — fall through to send new
            except Exception:
                pass   # fall through

        msg = await self._reply(update, text, reply_markup=kb)
        if msg and user:
            self._active_msg[user.id] = msg
        return msg

    @staticmethod
    def _nav_row(back_screen: str = None) -> list:
        """Returns [Home, Back] button row."""
        row = [InlineKeyboardButton("🏠 Home", callback_data="nav_home")]
        if back_screen:
            row.append(InlineKeyboardButton("⬅️ Back", callback_data="nav_back"))
        return row

    async def _render_screen(self, update, context, screen: str, edit_msg=None):
        """Render a named screen into edit_msg."""
        if screen in ("home", "start"):
            await self.cmd_start(update, context, edit_msg=edit_msg)
        elif screen == "stats":
            await self.cmd_stats(update, context, edit_msg=edit_msg)
        elif screen == "score":
            await self.cmd_score(update, context, edit_msg=edit_msg)
        elif screen == "help":
            await self.cmd_help(update, context, edit_msg=edit_msg)
        elif screen == "achievements":
            await self.cmd_achievements(update, context, edit_msg=edit_msg)
        elif screen == "leaderboard":
            await self._show_leaderboard(update, context, mode="global", page=1, edit_msg=edit_msg)
        elif screen == "categories":
            await self.cmd_categories(update, context, edit_msg=edit_msg)
        elif screen == "info":
            await self.cmd_info(update, context, edit_msg=edit_msg)
        elif screen == "botstats":
            await self.cmd_botstats(update, context, edit_msg=edit_msg)
        else:
            await self.cmd_start(update, context, edit_msg=edit_msg)

    # ─── Initialization ──────────────────────────────────────

    async def initialize(self, token: str):
        self.application = Application.builder().token(token).build()
        self._register_handlers()
        await self.application.initialize()
        await self._set_commands()
        logger.info("✅ Bot initialized — polling mode")

    async def initialize_webhook(self, token: str, webhook_url: str):
        import os as _os
        self.application = Application.builder().token(token).build()
        self._register_handlers()
        await self.application.initialize()
        _secret = _os.environ.get("WEBHOOK_SECRET_TOKEN", "")
        await self.application.bot.set_webhook(
            url=webhook_url,
            secret_token=_secret if _secret else None,
        )
        await self._set_commands()
        logger.info("✅ Bot initialized — webhook configured")

    # ─── Startup tasks (called after application.start()) ────

    def run_startup_tasks(self):
        """Schedule startup broadcast + owner alert as background tasks."""
        loop = asyncio.get_running_loop()
        loop.create_task(self._send_owner_alert())
        loop.create_task(self._send_startup_broadcast())

    async def _send_owner_alert(self):
        """Send system-status report to owner (and developers)."""
        now = datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")
        total_questions = self._q_count()
        total_users = total_groups = 0
        if self.db:
            try:
                total_users  = self.db.users_col.count_documents({})
                total_groups = self.db.groups_col.count_documents({})
            except Exception:
                pass
        text = (
            f"🎓  <b>CLAT VISION</b>  ·  System Status\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  ✅  Bot is live and operational.\n\n"
            f"  🕒  <b>{now}</b>\n"
            f"  📚  Questions  ›  <b>{total_questions}</b>\n"
            f"  👥  Users      ›  <b>{total_users}</b>\n"
            f"  💬  Groups     ›  <b>{total_groups}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  ⚡  All systems online  ·  /dev for controls"
        )
        recipients = {OWNER_ID}
        if self.db:
            try:
                for dev in self.db.get_all_developers():
                    uid = dev.get("user_id")
                    if uid:
                        recipients.add(uid)
            except Exception:
                pass
        for uid in recipients:
            try:
                await self.application.bot.send_message(
                    chat_id=uid, text=text, parse_mode=ParseMode.HTML,
                    link_preview_options=_NO_PREVIEW)
            except Exception as e:
                logger.warning(f"[STARTUP] Owner alert to {uid} failed: {e}")

    async def _send_startup_broadcast(self):
        """Send greeting to all PM-accessible users on startup.
        Disabled by default — set STARTUP_BROADCAST=1 to enable."""
        import os as _os
        if _os.environ.get("STARTUP_BROADCAST", "0") != "1":
            return
        if not self.db:
            return
        users = self.db.get_pm_accessible_users()
        if not users:
            logger.info("[STARTUP] No PM-accessible users — skipping broadcast")
            return
        bot_inline = "Miss Quiz 🎓"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Start Quiz",      callback_data="play_quiz"),
             InlineKeyboardButton("🎓 My Profile",        callback_data="my_profile")],
            [InlineKeyboardButton("🎓 Leaderboard",     callback_data="leaderboard"),
             InlineKeyboardButton("❓ Help",             callback_data="help")],
            [InlineKeyboardButton("🎓 Join CLAT Vision", url="https://t.me/CLAT_Vision")],
        ])

        logger.info(f"[STARTUP] Broadcasting to {len(users)} users")
        sent = []
        for user in users:
            uid = user.get("user_id")
            if not uid:
                continue
            try:
                name    = user.get("name") or user.get("username") or "User"
                mention = UI.mention(uid, name)
                text    = self._build_greeting(mention, bot_inline)
                msg     = await self.application.bot.send_message(
                    chat_id=uid, text=text,
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                    link_preview_options=_NO_PREVIEW)
                sent.append((uid, msg.message_id))
            except (Forbidden, BadRequest):
                pass
            except Exception as e:
                logger.warning(f"[STARTUP] Send to {uid} failed: {e}")
            await asyncio.sleep(0.05)

        logger.info(f"[STARTUP] Sent {len(sent)}")

    # ─── Greeting builder (shared by /start and broadcast) ───

    def _build_greeting(self, user_mention: str, bot_inline: str = "Miss Quiz 🎓") -> str:
        q_count   = self._q_count()
        q_display = UI.fmt_num(q_count)
        return (
            f"╔══════════════════════════════════════╗\n"
            f"║       🎓  <b>𝐂𝐋𝐀𝐓  𝐕𝐈𝐒𝐈𝐎𝐍</b>  🎓        ║\n"
            f"║          🌸 {user_mention} 🌸          ║\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"🌷  ᴏʜ ᴍʏ, ʟᴏᴏᴋ ᴡʜᴏ'ꜱ ʜᴇʀᴇ!  🌷\n\n"
            f"ʜɪɪɪɪ ᴅᴀʀʟɪɴɢ! 💕\n\n"
            f"💞 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ {bot_inline}\n"
            f"ʏᴏᴜʀ ꜱᴜᴘᴇʀ ᴀᴅᴏʀᴀʙʟᴇ ᴘʀᴇᴍɪᴜᴍ ᴄʟᴀᴛ ᴄᴏᴍᴘᴀɴɪᴏɴ! 💞\n\n"
            f"☘️ ɪ'ᴍ ꜱᴏ ᴛʜʀɪʟʟᴇᴅ ʏᴏᴜ'ʀᴇ ʜᴇʀᴇ!\n\n"
            f"🍁 ʟᴇᴛ'ꜱ ᴍᴀᴋᴇ ᴇᴠᴇʀʏ ꜱᴇꜱꜱɪᴏɴ ᴍᴀɢɪᴄᴀʟ —\n"
            f"🍁 ᴇᴠᴇʀʏ Qᴜᴇꜱᴛɪᴏɴ ᴀ ꜱᴘᴀʀᴋʟᴇ,\n"
            f"🍁 ᴇᴠᴇʀʏ ᴀɴꜱᴡᴇʀ ᴀ ꜱᴡᴇᴇᴛ ᴠɪᴄᴛᴏʀʏ!\n\n"
            f"🎓 ʀᴇᴀᴅʏ ᴛᴏ ɢʟᴏᴡ? 🎓\n\n"
            f"🎓 ᴊᴜꜱᴛ ᴛʏᴘᴇ /quiz ᴀɴᴅ ʟᴇᴛ'ꜱ ᴄʀᴇᴀᴛᴇ ꜱᴏᴍᴇ ʙʀɪʟʟɪᴀɴᴄᴇ ᴛᴏɢᴇᴛʜᴇʀ! ❤️\n\n"
            f"🥰 ʏᴏᴜʀ ʟᴏᴠɪɴɢ Qᴜɪᴢ ʙᴜᴅᴅʏ ɪꜱ ᴀʟʟ ʏᴏᴜʀꜱ ~ 🥰\n\n"
            f"🎓 ꜰᴏʀ ᴍᴏʀᴇ ᴄᴏᴍᴍᴀɴᴅꜱ: /help\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📚 {q_display} Qᴜᴇꜱᴛɪᴏɴꜱ • ⚡ ᴏɴʟɪɴᴇ\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    def _register_handlers(self):
        app = self.application

        # User commands
        app.add_handler(CommandHandler("start",       self.cmd_start))
        app.add_handler(CommandHandler("help",        self.cmd_help))
        app.add_handler(CommandHandler("quiz",        self.cmd_quiz))
        app.add_handler(CommandHandler("q",           self.cmd_quiz))
        app.add_handler(CommandHandler("score",        self.cmd_score))
        app.add_handler(CommandHandler("stats",        self.cmd_stats))
        app.add_handler(CommandHandler("achievements", self.cmd_achievements))
        app.add_handler(CommandHandler("botstats",     self.cmd_botstats))
        app.add_handler(CommandHandler("leaderboard", self.cmd_leaderboard))
        app.add_handler(CommandHandler("lb",          self.cmd_leaderboard))
        app.add_handler(CommandHandler("categories",  self.cmd_categories))
        app.add_handler(CommandHandler("ping",        self.cmd_ping))
        app.add_handler(CommandHandler("info",        self.cmd_info))

        # Admin commands
        app.add_handler(CommandHandler("addquiz",     self.cmd_addquiz))
        app.add_handler(CommandHandler("importquiz",  self.cmd_importquiz))
        app.add_handler(CommandHandler("delquiz",     self.cmd_delquiz))
        app.add_handler(CommandHandler("editquiz",    self.cmd_editquiz))
        app.add_handler(CommandHandler("dev",         self.cmd_dev))
        app.add_handler(CommandHandler("broadcast",     self.cmd_broadcast))
        app.add_handler(CommandHandler("bc",            self.cmd_broadcast))
        app.add_handler(CommandHandler("delbroadcast",  self.cmd_delbroadcast))
        app.add_handler(CommandHandler("reload",        self.cmd_reload))
        app.add_handler(CommandHandler("restart",     self.cmd_restart))

        # ── Group tracking — three complementary mechanisms ──────────
        # 1. my_chat_member: bot add/remove/promote/demote/restrict
        app.add_handler(ChatMemberHandler(
            self.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

        # 2. Group migration (group → supergroup): preserves tracking across ID change
        app.add_handler(MessageHandler(
            filters.StatusUpdate.MIGRATE, self._handle_group_migration))

        # 3. Auto-track in handler group 1: runs alongside ALL group message
        #    handlers in group 0, auto-registering the group AND the sender.
        app.add_handler(
            MessageHandler(filters.ChatType.GROUPS, self._auto_track),
            group=1
        )

        # Poll + Callbacks
        app.add_handler(PollAnswerHandler(self.handle_poll_answer))
        app.add_handler(CallbackQueryHandler(
            self._cb_delquiz, pattern=r"^dq_"))
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Bulk import: .txt file
        app.add_handler(MessageHandler(
            filters.Document.TXT | filters.Document.TEXT,
            self.handle_document))

        # Dev module
        try:
            from src.bot.dev_commands import DeveloperCommands
            if self.db:
                self._dev = DeveloperCommands(self.db, self.quiz_manager)
                app.add_handler(CommandHandler("devstats",           self._dev.devstats))
                app.add_handler(CommandHandler("activity",           self._dev.activity))
                app.add_handler(CommandHandler("performance",        self._dev.performance_stats))
                app.add_handler(CommandHandler("broadcast_confirm",  self._dev.broadcast_confirm))
                app.add_handler(CommandHandler("delbroadcast_confirm", self._dev.delbroadcast_confirm))
                app.add_handler(CallbackQueryHandler(
                    self._dev.handle_edit_quiz_callback, pattern="^eq_"))
                app.add_handler(MessageHandler(
                    filters.TEXT & ~filters.COMMAND, self._dev.handle_text_input))
                logger.info("DeveloperCommands ✅")
        except Exception as e:
            logger.warning(f"DeveloperCommands skip: {e}")

    async def _set_commands(self):
        try:
            await self.application.bot.set_my_commands([
                BotCommand("quiz",         "🎯 Get a quiz question"),
                BotCommand("score",        "🏆 Your personal score"),
                BotCommand("stats",        "📈 Your detailed stats"),
                BotCommand("achievements", "🏅 Badges & milestones"),
                BotCommand("botstats",     "📊 Bot-wide statistics"),
                BotCommand("leaderboard",  "🔱 Global leaderboard"),
                BotCommand("categories",   "📚 Browse quiz categories"),
                BotCommand("help",         "📖 Command center"),
                BotCommand("start",        "🚀 Welcome screen"),
                BotCommand("ping",         "🏓 Connection test"),
            ])
        except Exception as e:
            logger.warning(f"set_my_commands: {e}")

    # ─── Core helpers ─────────────────────────────────────────

    def _is_owner(self, uid: int) -> bool:
        return uid == OWNER_ID

    async def _is_authorized(self, uid: int) -> bool:
        if self._is_owner(uid):
            return True
        if self.db:
            try:
                return any(d.get("user_id") == uid
                           for d in self.db.get_all_developers())
            except Exception:
                pass
        return False

    async def _reply(self, update: Update, text: str,
                     parse_mode=ParseMode.HTML,
                     reply_markup=None, **kw) -> Optional[Any]:
        """Smart reply — auto-injects thread_id for forum topics."""
        tid    = get_thread_id(update)
        kwargs = {"parse_mode": parse_mode, "link_preview_options": _NO_PREVIEW}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        if tid:
            kwargs["message_thread_id"] = tid
        kwargs.update(kw)
        try:
            return await update.effective_message.reply_text(text, **kwargs)
        except TelegramError as e:
            if any(w in str(e).lower() for w in ("topic", "thread", "closed")):
                kwargs.pop("message_thread_id", None)
                try:
                    return await update.effective_message.reply_text(text, **kwargs)
                except Exception:
                    pass
            logger.error(f"_reply error: {e}")
        return None

    async def _edit(self, msg, text: str, reply_markup=None):
        """Safe message edit."""
        try:
            kwargs = {"parse_mode": ParseMode.HTML, "link_preview_options": _NO_PREVIEW}
            if reply_markup:
                kwargs["reply_markup"] = reply_markup
            await msg.edit_text(text, **kwargs)
        except Exception as e:
            logger.error(f"_edit error: {e}")

    async def _unauthorized(self, update: Update):
        """Professional access denied — auto-deletes after 7s."""
        user    = update.effective_user
        mention = UI.mention(user.id, UI.display_name(user))
        text = (
            f"🔒 <b>ACCESS RESTRICTED</b>\n"
            f"{UI.LINE}\n\n"
            f"  {mention}, this command requires\n"
            f"  elevated privileges.\n\n"
            f"  ◈ Owner or Developer access only.\n\n"
            f"{UI.THIN}\n"
            f"  <i>Contact {COMMUNITY} for access.</i>"
        )
        msg = await self._reply(update, text)
        await asyncio.sleep(7)
        try:
            if msg:
                await msg.delete()
            await update.effective_message.delete()
        except Exception:
            pass

    def _get_user_rank_position(self, user_id: int) -> Optional[int]:
        """Return global rank position (1-indexed) or None — always from DB."""
        if self.db:
            try:
                info = self.db.get_user_rank_in_period(user_id, days=36500)
                rank = info.get("rank", 0)
                return rank if rank > 0 else None
            except Exception:
                pass
        return None

    # ─── CALLBACK HANDLER ─────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data  = query.data
        # Callback queries bypass the group-1 MessageHandler; register both
        # group and user here via the central pipelines.
        self.ensure_group_registered(update, context, source="callback-query")
        cb_user = update.effective_user
        if cb_user:
            cb_chat = update.effective_chat
            cb_pm   = cb_chat.type == "private" if cb_chat else False
            self.ensure_user_registered(cb_user, is_pm=cb_pm if cb_pm else None,
                                        source="callback-query")
        uid   = cb_user.id if cb_user else None

        if data == "play_quiz":
            # Quiz sends a poll — do NOT edit the current message
            await self.cmd_quiz(update, context)

        elif data == "leaderboard":
            await self._show_leaderboard(update, context, mode="global", page=1,
                                         edit_msg=query.message)

        elif data == "my_profile":
            if uid: self._nav_push(uid, "stats")
            await self.cmd_stats(update, context, edit_msg=query.message)

        elif data == "lb_noop":
            pass  # disabled nav button — already answered

        elif data and data.startswith("lb_myrank_"):
            parts = data.split("_")
            mode  = parts[2] if len(parts) > 2 else "global"
            if mode not in ("global", "weekly", "monthly"):
                mode = "global"
            await self._show_my_rank(update, context, mode=mode, edit_msg=query.message)

        elif data and data.startswith("lbp_"):
            parts = data.split("_")
            mode  = parts[1] if len(parts) > 1 else "global"
            try:
                pg = int(parts[2]) if len(parts) > 2 else 1
            except ValueError:
                pg = 1
            if mode not in ("global", "weekly", "monthly", "group"):
                mode = "global"
            await self._show_leaderboard(update, context, mode=mode, page=pg,
                                         edit_msg=query.message)

        # ── Legacy aliases (kept for old messages) ──
        elif data == "lb_global":
            await self._show_leaderboard(update, context, mode="global", page=1,
                                         edit_msg=query.message)
        elif data == "lb_weekly":
            await self._show_leaderboard(update, context, mode="weekly", page=1,
                                         edit_msg=query.message)
        elif data == "lb_monthly":
            await self._show_leaderboard(update, context, mode="monthly", page=1,
                                         edit_msg=query.message)

        elif data == "achievements":
            if uid: self._nav_push(uid, "achievements")
            await self.cmd_achievements(update, context, edit_msg=query.message)

        elif data == "help":
            if uid: self._nav_push(uid, "help")
            await self.cmd_help(update, context, edit_msg=query.message)

        elif data == "info":
            if uid: self._nav_push(uid, "info")
            await self.cmd_info(update, context, edit_msg=query.message)

        elif data == "back_start":
            if uid: self._nav_clear(uid)
            await self.cmd_start(update, context, edit_msg=query.message)

        elif data == "nav_home":
            if uid: self._nav_clear(uid)
            await self.cmd_start(update, context, edit_msg=query.message)

        elif data == "nav_back":
            prev = self._nav_pop(uid) if uid else "home"
            await self._render_screen(update, context, prev, edit_msg=query.message)

        elif data.startswith("bs_"):
            parts = data.split("_", 2)
            if len(parts) >= 2 and parts[1] == "refresh":
                page = parts[2] if len(parts) > 2 else "overview"
                await self.cmd_botstats(update, context, edit_msg=query.message, page=page)
            else:
                page = parts[1] if len(parts) > 1 else "overview"
                if uid: self._nav_push(uid, "botstats")
                await self.cmd_botstats(update, context, edit_msg=query.message, page=page)
