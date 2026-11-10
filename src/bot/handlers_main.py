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
    BotCommand
)
from telegram.ext import (
    Application, CommandHandler, PollAnswerHandler,
    CallbackQueryHandler, MessageHandler, ChatMemberHandler, InlineQueryHandler,
    filters, ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest

from src.bot.ui import (
    UI, OWNER_ID, COMMUNITY, _NO_PREVIEW, get_thread_id
)
from src.bot.tracking import TrackingMixin
from src.bot.poll_manager import PollMixin
from src.bot.commands.user_cmds import UserCommandsMixin
from src.bot.commands.quiz_cmds import QuizCommandsMixin
from src.bot.commands.admin_cmds import AdminCommandsMixin
from src.bot.battle import BattleMixin
from src.bot.admin_panel import AdminPanelMixin
from src.bot.my_quiz import MyQuizMixin
from src.bot.group_quiz import GroupQuizMixin

logger = logging.getLogger(__name__)


class TelegramQuizBot(
    PollMixin,
    TrackingMixin,
    UserCommandsMixin,
    QuizCommandsMixin,
    AdminCommandsMixin,
    BattleMixin,
    MyQuizMixin,
    GroupQuizMixin,
    AdminPanelMixin,
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
        """Live question count from in-memory cache (always in sync with DB)."""
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
        """Returns [Home (Blue), Back (Red)] button row."""
        row = [InlineKeyboardButton("🏠 Home", callback_data="nav_home", style="primary")]
        if back_screen:
            row.append(InlineKeyboardButton("⬅️ Back", callback_data="nav_back", style="danger"))
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
        elif screen == "categories":
            await self.cmd_categories(update, context, edit_msg=edit_msg)
        elif screen == "info":
            await self.cmd_info(update, context, edit_msg=edit_msg)
        elif screen == "botstats":
            await self.cmd_botstats(update, context, edit_msg=edit_msg)
        else:
            await self.cmd_start(update, context, edit_msg=edit_msg)

    # ─── Initialization ──────────────────────────────────────

    @staticmethod
    def _build_application(token: str) -> Application:
        """Longer timeouts than PTB's defaults (5s connect) so slow or flaky
        networks (WSL, mobile hotspot, far-away servers) don't kill startup."""
        from telegram.request import HTTPXRequest
        from src.bot.premium import make_premium_bot_class, make_rate_limiter
        # PremiumExtBot turns every emoji in outgoing messages/buttons into
        # premium emoji when PREMIUM_EMOJI=1 (no-op otherwise).
        bot = make_premium_bot_class()(
            token,
            request=HTTPXRequest(connect_timeout=20, read_timeout=30,
                                 write_timeout=30, pool_timeout=30),
            get_updates_request=HTTPXRequest(connect_timeout=20, read_timeout=40),
            rate_limiter=make_rate_limiter(),
        )
        # Process up to 16 updates at once: a slow handler (DB, Telegram API)
        # no longer makes everyone else wait in line.
        return Application.builder().bot(bot).concurrent_updates(16).build()

    async def _initialize_with_retry(self, attempts: int = 3) -> None:
        from telegram.error import NetworkError, TimedOut
        for i in range(1, attempts + 1):
            try:
                await self.application.initialize()
                return
            except (TimedOut, NetworkError) as e:
                if i == attempts:
                    logger.critical(
                        "❌ Cannot reach api.telegram.org after %d tries (%s). "
                        "Check internet / DNS / VPN, then retry.", attempts, e)
                    raise
                logger.warning(f"Telegram connect attempt {i}/{attempts} failed: {e} — retrying in {i*5}s")
                await asyncio.sleep(i * 5)

    async def initialize(self, token: str):
        self.application = self._build_application(token)
        self._register_handlers()
        await self._initialize_with_retry()
        await self._set_commands()
        logger.info("✅ Bot initialized — polling mode")
        from src.bot.premium import premium_on
        logger.info(f"✨ Premium emoji: {'ON' if premium_on() else 'OFF (set PREMIUM_EMOJI=1 in .env)'}")

    async def initialize_webhook(self, token: str, webhook_url: str):
        from src.core.config import webhook_secret
        self.application = self._build_application(token)
        self._register_handlers()
        await self._initialize_with_retry()
        await self.application.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret(token),
        )
        await self._set_commands()
        logger.info("✅ Bot initialized — webhook configured")

    # ─── Startup tasks (called after application.start()) ────

    def run_startup_tasks(self):
        """Schedule startup broadcast + owner alert as background tasks."""
        loop = asyncio.get_running_loop()
        loop.create_task(self._send_owner_alert())
        if self.application is not None:                       # group quizzes running before a restart
            loop.create_task(self.gq_resume_all(self.application.bot))
        loop.create_task(self._send_startup_broadcast())

    async def _send_owner_alert(self):
        """Send system-status report to owner (and developers)."""
        now = datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")
        total_questions = self._q_count()
        total_users = total_groups = 0
        if self.db:
            try:
                total_users  = await asyncio.to_thread(
                    self.db.users_col.count_documents, {})
                total_groups = await asyncio.to_thread(
                    self.db.groups_col.count_documents, {})
            except Exception:
                pass
        text = (
            f"🎓  <b>MISS QUIZ</b>  ·  System Status\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"  ✅  Bot is live and operational.\n\n"
            f"  🕒  <b>{now}</b>\n"
            f"  📚  Questions  ›  <b>{total_questions}</b>\n"
            f"  👥  Users      ›  <b>{total_users}</b>\n"
            f"  💬  Groups     ›  <b>{total_groups}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  ⚡  All systems online  ·  /start → Admin Panel"
        )
        recipients = {OWNER_ID}
        if self.db:
            try:
                devs = await asyncio.to_thread(self.db.get_all_developers)
                for dev in devs:
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
        users = await asyncio.to_thread(self.db.get_pm_accessible_users)
        if not users:
            logger.info("[STARTUP] No PM-accessible users — skipping broadcast")
            return
        bot_inline = "Miss Quiz 🎓"

        kb = self._start_kb()

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
        from src.bot.premium import e
        q_display = UI.fmt_num(self._q_count())
        return (
            f"<b>╭──── ❨ 🎓 MISS QUIZ STUDIO ❩ ────╮</b>\n\n"
            f"<b>{e('hi')} Welcome, {user_mention}!</b>\n\n"
            f"<blockquote><b>1️⃣ CUSTOM QUIZ STUDIO\n"
            f"• Create quizzes from PDF, DOCX, TXT &amp; Polls\n"
            f"• Attach Study Material, Flashcards &amp; Exam Paper\n"
            f"• Commands: /myquiz · /clone · /mix · /mywords</b></blockquote>\n"
            f"<blockquote><b>2️⃣ LIVE 1v1 BATTLE &amp; PRACTICE\n"
            f"• {q_display} Questions · Instant 1v1 Duel &amp; Solo Practice\n"
            f"• Commands: /battle · /quiz · /categories</b></blockquote>\n"
            f"<blockquote><b>3️⃣ GROUP TOURNAMENTS &amp; ANALYTICS\n"
            f"• Live I'm Ready! Lobby · Top-3 Winner Certificates\n"
            f"• Commands: /startquiz · /schedule · /result · /compare</b></blockquote>\n"
            f"<b>╰──────────────────────────╯</b>\n"
            f"<b>{e('play')} <i>Choose an option below to get started:</i></b>"
        )

    async def _cb_start_battle(self, update: Update, context):
        """'Battle' button on /start → same as typing /battle."""
        await update.callback_query.answer()
        await self.cmd_battle(update, context)

    def _start_kb(self, show_admin: bool = False) -> InlineKeyboardMarkup:
        """Start-screen buttons using focal-point color psychology:
        Green = Primary CTA (Create Quiz, Host in Group), Red = Admin Panel,
        Default dark glass (None) = middle menu items."""
        from src.bot.premium import btn
        from src.bot.ui import COMMUNITY_URL
        username = getattr(getattr(self.application, "bot", None), "username", "") or ""
        rows = [
            # Row 1: Primary Creator CTA (Green)
            [btn("Create New Quiz", "plus", "success", callback_data="mq_new")],

            # Row 2: Custom Quizzes & Live 1v1 Battle (Clean Dark Glass)
            [btn("My Quizzes", "folder", None, callback_data="mq_list"),
             btn("1v1 Battle", "fight", None, callback_data="start_battle")],

            # Row 3: Quick Practice Quiz & Topic Categories (Clean Dark Glass)
            [btn("Start Quiz", "play", None, callback_data="play_quiz"),
             btn("Categories", "book", None, callback_data="categories")],

            # Row 4: Profile/Analytics & All Commands Guide (Clean Dark Glass)
            [btn("My Profile", "medal", None, callback_data="my_profile"),
             btn("All Commands", "question", None, callback_data="help")],

            # Row 5: Community (Dark Glass) & Host in Group (Green CTA)
            [btn("Community", "users", None, url=COMMUNITY_URL)]
            + ([btn("Host in Group", "plus", "success",
                    url=f"https://t.me/{username}?startgroup&admin=delete_messages+pin_messages")]
               if username else []),
        ]
        if show_admin:
            rows.append([btn("Admin Panel", "key", "danger", callback_data="ap:open")])
        return InlineKeyboardMarkup(rows)

    def _register_handlers(self):
        app = self.application

        # User commands
        app.add_handler(CommandHandler("start",       self.cmd_start))
        app.add_handler(CommandHandler("help",        self.cmd_help))
        app.add_handler(CommandHandler("quiz",        self.cmd_quiz))
        app.add_handler(CommandHandler("score",        self.cmd_score))
        app.add_handler(CommandHandler("stats",        self.cmd_stats))
        app.add_handler(CommandHandler("achievements", self.cmd_achievements))
        app.add_handler(CommandHandler("botstats",     self.cmd_botstats))
        app.add_handler(CommandHandler("categories",  self.cmd_categories))
        app.add_handler(CommandHandler("ping",        self.cmd_ping))
        app.add_handler(CommandHandler("info",        self.cmd_info))
        app.add_handler(CommandHandler("battle",      self.cmd_battle))
        app.add_handler(CommandHandler("myquiz",      self.cmd_myquiz))
        app.add_handler(CommandHandler("startquiz",   self.cmd_startquiz))
        app.add_handler(CommandHandler("stopquiz",    self.cmd_stopquiz))
        app.add_handler(CommandHandler("slow",        self.cmd_slow))
        app.add_handler(CommandHandler("fast",        self.cmd_fast))
        app.add_handler(CommandHandler("normal",      self.cmd_normal))
        app.add_handler(CommandHandler("result",      self.cmd_result))
        app.add_handler(CommandHandler("compare",     self.cmd_compare))
        app.add_handler(CommandHandler("mywords",     self.cmd_mywords))
        app.add_handler(CommandHandler("schedule",    self.cmd_schedule))
        app.add_handler(CommandHandler("clone",       self.cmd_clone))
        app.add_handler(CommandHandler("mix",         self.cmd_mix))

        # Admin commands
        app.add_handler(CommandHandler("addquiz",     self.cmd_addquiz))
        app.add_handler(CommandHandler("importquiz",  self.cmd_importquiz))
        app.add_handler(CommandHandler("delquiz",     self.cmd_delquiz))
        app.add_handler(CommandHandler("editquiz",    self.cmd_editquiz))
        app.add_handler(CommandHandler("dev",         self.cmd_dev))
        app.add_handler(CommandHandler("broadcast",     self.cmd_broadcast))
        app.add_handler(CommandHandler("delbroadcast",  self.cmd_delbroadcast))
        app.add_handler(CommandHandler("reload",        self.cmd_reload))
        app.add_handler(CommandHandler("restart",     self.cmd_restart))
        app.add_handler(CommandHandler("admin",       self.cmd_admin))
        app.add_handler(CommandHandler("cancel",      self.cmd_cancel))
        app.add_handler(CommandHandler("check",       self.cmd_check))
        app.add_handler(CommandHandler("cleanup",     self.cmd_cleanup))
        app.add_handler(CommandHandler("addadmin",    self.cmd_addadmin))

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
        app.add_handler(InlineQueryHandler(self.handle_battle_inline))   # battle invite card
        app.add_handler(CallbackQueryHandler(
            self._cb_delquiz, pattern=r"^dq_"))
        app.add_handler(CallbackQueryHandler(
            self.handle_battle_callback, pattern=r"^bt_(acc|dec|q|t|m|ch|go|rm|p|my|s|new):"))
        app.add_handler(CallbackQueryHandler(
            self.handle_group_quiz_callback, pattern=r"^gq\|"))
        app.add_handler(CallbackQueryHandler(
            self.handle_my_quiz_callback, pattern=r"^mq_"))
        app.add_handler(CallbackQueryHandler(
            self.handle_admin_panel_callback, pattern=r"^ap:"))
        app.add_handler(CallbackQueryHandler(
            self._cb_start_battle, pattern=r"^start_battle$"))
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Bulk import: .txt file
        app.add_handler(MessageHandler(
            filters.Document.ALL & filters.ChatType.PRIVATE,   # any file type, DM only
            self.handle_document))

        # Dev module
        try:
            from src.bot.dev_commands import DeveloperCommands
            if self.db:
                self._dev = DeveloperCommands(self.db, self.quiz_manager)
                app.add_handler(CommandHandler("broadcast_confirm",  self._dev.broadcast_confirm))
                app.add_handler(CommandHandler("delbroadcast_confirm", self._dev.delbroadcast_confirm))
                app.add_handler(CallbackQueryHandler(
                    self._dev.handle_edit_quiz_callback, pattern="^edit_quiz_"))
                app.add_handler(MessageHandler(
                    filters.TEXT & ~filters.COMMAND, self._dev.handle_text_input))
                logger.info("DeveloperCommands ✅")
        except Exception as e:
            logger.error(
                f"[CRITICAL] DeveloperCommands failed to load — /editquiz callbacks "
                f"and text-input handlers are DISABLED. Error: {e}",
                exc_info=True,
            )

        # Admin forwards a quiz poll to the DM → find that question
        app.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.POLL, self.handle_forwarded_poll), group=-2)

        # Create Own Quiz: questions sent while a draft is open (DM) — before everything else
        app.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & ((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL
                                        | filters.POLL | filters.PHOTO),
            self.handle_myquiz_input), group=-4)

        # Battle setup: challenger types a question count / time
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.handle_battle_input), group=-3)

        # Admin input (start photo) runs before every other handler group
        app.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            self.handle_admin_input), group=-1)

        # Global error handler — catches any unhandled exception in any handler
        app.add_error_handler(self._global_error_handler)

    async def _set_commands(self):
        user_cmds = [
            BotCommand("start",        "🏠 Home & Main Studio"),
            BotCommand("myquiz",       "📚 Create & Manage Your Quizzes"),
            BotCommand("battle",       "⚔️ 1v1 Live Quiz Battle"),
            BotCommand("quiz",         "▶️ Instant Practice Question"),
            BotCommand("startquiz",    "👥 Start Group Tournament"),
            BotCommand("stopquiz",     "⏹ Stop Group Tournament"),
            BotCommand("schedule",     "⏰ Schedule Group Quiz"),
            BotCommand("result",       "📩 Wrong-Answer Review in DM"),
            BotCommand("compare",      "🆚 Compare Stats with a Player"),
            BotCommand("score",        "🏆 Your XP, Level & Rank"),
            BotCommand("stats",        "📊 Subject-wise Analytics"),
            BotCommand("achievements", "🏅 Badges & Milestones"),
            BotCommand("categories",   "📖 Browse Quiz Topics"),
            BotCommand("clone",        "📋 Copy a Quiz by ID"),
            BotCommand("mix",          "🔀 Combine Quizzes into One"),
            BotCommand("mywords",      "🧹 Safai Auto-Clean Filter"),
            BotCommand("help",         "❓ Full Commands & Features Guide"),
        ]
        try:
            await self.application.bot.set_my_commands(user_cmds)
        except Exception as e:
            logger.warning(f"set_my_commands: {e}")

        from telegram import BotCommandScopeChat
        from src.core.config import AUTHORIZED_USERS
        admin_cmds = user_cmds + [
            BotCommand("admin",        "🔑 Open Admin Control Panel"),
            BotCommand("check",        "🩺 Bot & System Health Monitor"),
            BotCommand("cleanup",      "🧹 Sanitize DB & Clear Stale Quizzes"),
            BotCommand("addadmin",     "👑 Quick 1-Line Admin Promotion"),
            BotCommand("addquiz",      "➕ Add Questions to Bot Bank"),
            BotCommand("editquiz",     "✏️ Edit Bot Question Bank"),
            BotCommand("delquiz",      "🗑 Delete Question from Bank"),
            BotCommand("broadcast",    "📡 Broadcast Message to All"),
            BotCommand("reload",       "🔄 Reload Questions from DB"),
            BotCommand("restart",      "♻️ Restart Bot Process"),
        ]
        admin_ids = set(AUTHORIZED_USERS)
        if self.db:
            try:
                devs = await asyncio.to_thread(self.db.get_all_developers)
                admin_ids |= {d["user_id"] for d in devs if d.get("user_id")}
            except Exception as e:
                logger.warning(f"set_my_commands: developers lookup failed: {e}")
        for uid in admin_ids:
            try:
                await self.application.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=uid))
            except Exception as e:
                logger.debug(f"set_my_commands for admin {uid}: {e}")

    # ─── Core helpers ─────────────────────────────────────────

    def _is_owner(self, uid: int) -> bool:
        return uid == OWNER_ID

    async def _is_authorized(self, uid: int) -> bool:
        if self._is_owner(uid):
            return True
        if self.db:
            try:
                devs = await asyncio.to_thread(self.db.get_all_developers)
                return any(d.get("user_id") == uid for d in devs)
            except Exception:
                pass
        return False

    async def _reply(self, update: Update, text: str,
                     parse_mode=ParseMode.HTML,
                     reply_markup=None, **kw) -> Optional[Any]:
        """Smart reply — routes to an open forum topic (or Main topic if all closed)."""
        chat   = update.effective_chat
        tid    = get_thread_id(update)
        if chat and chat.type in ("group", "supergroup") and self.db and hasattr(self.db, "get_group_open_thread"):
            try:
                res_tid = await asyncio.to_thread(self.db.get_group_open_thread, chat.id, tid)
                if isinstance(res_tid, int) or res_tid is None:
                    tid = res_tid
            except Exception:
                pass
        kwargs = {"parse_mode": parse_mode, "link_preview_options": _NO_PREVIEW}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        if tid:
            kwargs["message_thread_id"] = tid
        kwargs.update(kw)
        try:
            if chat and getattr(update.effective_message, "is_topic_message", False) and tid != getattr(update.effective_message, "message_thread_id", None):
                return await self.application.bot.send_message(chat_id=chat.id, text=text, **kwargs)
            return await update.effective_message.reply_text(text, **kwargs)
        except TelegramError as e:
            if any(w in str(e).lower() for w in ("topic", "thread", "closed")) and chat:
                next_tid = None
                if tid and self.db and hasattr(self.db, "mark_group_topic_closed"):
                    try:
                        next_tid = await asyncio.to_thread(self.db.mark_group_topic_closed, chat.id, tid)
                    except Exception:
                        next_tid = None
                if next_tid:
                    kwargs["message_thread_id"] = next_tid
                else:
                    kwargs.pop("message_thread_id", None)
                try:
                    return await self.application.bot.send_message(chat_id=chat.id, text=text, **kwargs)
                except Exception:
                    kwargs.pop("message_thread_id", None)
                    try:
                        return await self.application.bot.send_message(chat_id=chat.id, text=text, **kwargs)
                    except Exception:
                        pass
            logger.error(f"_reply error: {e}")
        return None

    async def _edit(self, msg, text: str, reply_markup=None):
        """Safe message edit (falls back to reply_text if msg is a photo)."""
        kwargs = {"parse_mode": ParseMode.HTML, "link_preview_options": _NO_PREVIEW}
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        try:
            return await msg.edit_text(text, **kwargs)
        except Exception as e:
            if "not modified" in str(e).lower():
                return msg
            if hasattr(msg, "reply_text"):
                try:
                    return await msg.reply_text(text, **kwargs)
                except Exception:
                    pass
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

    # ─── CALLBACK HANDLER ─────────────────────────────────────

    async def _global_error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Catch-all PTB error handler — logs every unhandled exception."""
        logger.error(
            f"[PTB] Unhandled exception for update {type(update).__name__}: "
            f"{context.error}",
            exc_info=context.error,
        )
        if not isinstance(update, Update):
            return
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Please try again in a moment."
                )
        except Exception:
            pass

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        try:
            await query.answer()
        except Exception:
            pass  # answer() can fail if the query is too old
        data = query.data
        if not data:
            return
        try:
            await self._dispatch_callback(update, context, query, data)
        except Exception as e:
            logger.error(
                f"[CALLBACK] Error handling data={data!r} "
                f"user={update.effective_user.id if update.effective_user else '?'}: "
                f"{e}",
                exc_info=True,
            )
            try:
                await query.message.reply_text(
                    "⚠️ An error occurred. Please try again."
                )
            except Exception:
                pass

    async def _dispatch_callback(self, update, context, query, data):
        """Inner callback dispatch — separated so handle_callback can wrap it."""
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

        elif data == "my_profile":
            if uid: self._nav_push(uid, "stats")
            await self.cmd_stats(update, context, edit_msg=query.message)

        elif data == "achievements":
            if uid: self._nav_push(uid, "achievements")
            await self.cmd_achievements(update, context, edit_msg=query.message)

        elif data == "categories":
            if uid: self._nav_push(uid, "categories")
            await self.cmd_categories(update, context, edit_msg=query.message)

        elif data == "score":
            if uid: self._nav_push(uid, "score")
            await self.cmd_score(update, context, edit_msg=query.message)

        elif data == "help":
            if uid: self._nav_push(uid, "help")
            await self.cmd_help(update, context, edit_msg=query.message)

        elif data == "info":
            if uid: self._nav_push(uid, "info")
            await self.cmd_info(update, context, edit_msg=query.message)

        elif data == "nav_home":
            if hasattr(self, "_mq_clear_modes"):
                self._mq_clear_modes(context)
            if getattr(context, "user_data", None) is not None:
                context.user_data.pop("mq_draft", None)
                context.user_data.pop("bt_await", None)
            if uid: self._nav_clear(uid)
            await self.cmd_start(update, context, edit_msg=query.message)

        elif data == "nav_back":
            prev = self._nav_pop(uid) if uid else "home"
            await self._render_screen(update, context, prev, edit_msg=query.message)

        elif data.startswith("bs_"):
            if uid: self._nav_push(uid, "botstats")
            await self.cmd_botstats(update, context, edit_msg=query.message)
        else:
            logger.debug(f"[CALLBACK] Unrecognised data={data!r} — ignored")
