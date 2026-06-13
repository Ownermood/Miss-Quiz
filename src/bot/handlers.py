"""
CLAT VISION QUIZ BOT — PREMIUM HANDLER ENGINE
Ultra-premium redesign: modern, elegant, professional.
"""

import html
import logging
import asyncio
import os
import re
import time
from typing import Optional, Any
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, Poll, LinkPreviewOptions
)
from telegram.ext import (
    Application, CommandHandler, PollAnswerHandler,
    CallbackQueryHandler, MessageHandler, ChatMemberHandler,
    filters, ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest

logger   = logging.getLogger(__name__)
OWNER_ID   = int(os.environ.get("OWNER_ID", "8403136097"))
OWNER_NAME = "🌷 𝐂𝐋𝐀𝐓 𝐎𝐖𝐍𝐄𝐑 🌷"
OWNER_LINK = OWNER_NAME  # plain text only — tg profile links expose user bios
COMMUNITY  = "@CLAT_Vision"
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ══════════════════════════════════════════════════════════════
#  PREMIUM DESIGN SYSTEM
# ══════════════════════════════════════════════════════════════

class UI:
    """Central design token system — all visual constants live here."""

    LINE  = "━" * 30
    THIN  = "─" * 26
    DOT   = "·"

    # ── Progress bar ──────────────────────────────────────────
    @staticmethod
    def bar(pct: float, width: int = 10) -> str:
        filled = max(0, min(width, int(float(pct) / 100 * width)))
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def mini_bar(pct: float, width: int = 5) -> str:
        filled = max(0, min(width, int(float(pct) / 100 * width)))
        return "▰" * filled + "▱" * (width - filled)

    # ── Rank tier system (based on correct answers) ───────────
    @staticmethod
    def rank(score: int) -> tuple:
        """Returns (rank_label, grade_letter)."""
        if   score >= 500: return "👑 LEGEND",   "S"
        elif score >= 200: return "🔱 MASTER",   "A+"
        elif score >= 100: return "⚔️  EXPERT",  "A"
        elif score >= 50:  return "🎯 ADVANCED", "B"
        elif score >= 20:  return "📈 RISING",   "C"
        elif score >= 5:   return "🌱 BEGINNER", "D"
        else:              return "🎲 ROOKIE",   "E"

    # ── XP Level system (score × 10 = XP) ────────────────────
    @staticmethod
    def level(score: int) -> str:
        xp = score * 10
        if   xp >= 10000: return "💠 Legendary"
        elif xp >= 5000:  return "💎 Diamond"
        elif xp >= 2500:  return "🔷 Platinum"
        elif xp >= 1000:  return "🥇 Gold"
        elif xp >= 500:   return "🥈 Silver"
        else:             return "🥉 Bronze"

    @staticmethod
    def xp_bar(score: int) -> str:
        """Show progress within current level."""
        breakpoints = [0, 50, 100, 250, 500, 1000]
        for i, bp in enumerate(breakpoints):
            if score < bp:
                prev = breakpoints[i - 1] if i > 0 else 0
                pct  = (score - prev) / (bp - prev) * 100 if bp > prev else 100
                return UI.mini_bar(pct)
        return "▰▰▰▰▰"

    # ── Medal & ranking display ───────────────────────────────
    MEDALS = ["🥇", "🥈", "🥉"] + ["🏅"] * 20

    @staticmethod
    def rank_badge(pos: int) -> str:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        return medals.get(pos, f"  {pos}.")

    # ── Category system ───────────────────────────────────────
    CATS = {
        "legal":     ("⚖️",  "Legal Reasoning"),
        "english":   ("📖",  "English"),
        "gk":        ("🌐",  "General Knowledge"),
        "current":   ("📰",  "Current Affairs"),
        "polity":    ("🏛️",  "Polity"),
        "math":      ("🔢",  "Mathematics"),
        "reasoning": ("🧠",  "Logical Reasoning"),
        "history":   ("📜",  "History"),
        "default":   ("📚",  "General"),
    }

    @staticmethod
    def cat_emoji(cat: str) -> str:
        cat_lower = (cat or "").lower()
        for k, (emoji, _) in UI.CATS.items():
            if k in cat_lower:
                return emoji
        return UI.CATS["default"][0]

    # ── Inline mention ────────────────────────────────────────
    @staticmethod
    def mention(user_id: int, name: str) -> str:
        """Return a bold display name. No tg:// link — avoids profile card previews."""
        return f'<b>{html.escape(str(name))}</b>'

    # ── Display name (HTML-safe) ──────────────────────────────
    @staticmethod
    def display_name(user) -> str:
        """Returns HTML-safe display name: first_name > full name > @username > 'User'"""
        if not user:
            return "User"
        name = (user.first_name or "").strip()
        if not name and user.last_name:
            name = user.last_name.strip()
        if not name and user.username:
            name = f"@{user.username}"
        if not name:
            name = "User"
        return name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ── Streak display ────────────────────────────────────────
    @staticmethod
    def streak_display(n: int) -> str:
        if n == 0:
            return "— No streak yet"
        fires = "🔥" * min(n, 5)
        suffix = " 🔥" if n > 5 else ""
        return f"{fires}{suffix} <b>{n} days</b>"

    # ── Number formatter ──────────────────────────────────────
    @staticmethod
    def fmt_num(n: int) -> str:
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000:     return f"{n/1_000:.1f}K"
        return str(n)


# ══════════════════════════════════════════════════════════════
#  FORUM / TOPIC HELPERS
# ══════════════════════════════════════════════════════════════

def get_thread_id(update: Update) -> Optional[int]:
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False):
        return msg.message_thread_id
    return None

def get_tracking_id(chat_id: int, thread_id: Optional[int]) -> int:
    return int(f"{abs(chat_id)}{thread_id}") if thread_id else chat_id


# ══════════════════════════════════════════════════════════════
#  MAIN BOT CLASS
# ══════════════════════════════════════════════════════════════

class TelegramQuizBot:

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
        self.application = Application.builder().token(token).build()
        self._register_handlers()
        await self.application.initialize()
        await self.application.bot.set_webhook(url=webhook_url)
        await self._set_commands()
        logger.info(f"✅ Bot initialized — webhook: {webhook_url}")

    # ── Poll mapping persistence ─────────────────────────────
    _PICKLE_PATH = "data/poll_cache.pkl"

    def _pickle_save(self, key: str, data: dict) -> None:
        """Append one entry to standalone pickle backup. Non-fatal on failure."""
        import pickle as _pkl
        try:
            existing: dict = {}
            if os.path.exists(self._PICKLE_PATH):
                try:
                    with open(self._PICKLE_PATH, "rb") as f:
                        existing = _pkl.load(f)
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            existing[key] = data
            with open(self._PICKLE_PATH, "wb") as f:
                _pkl.dump(existing, f)
        except Exception as e:
            logger.warning(f"[POLL CACHE] Pickle save failed (non-fatal): {e}")

    def _pickle_load(self) -> dict:
        """Load standalone pickle backup. Returns empty dict on any error."""
        import pickle as _pkl
        try:
            if not os.path.exists(self._PICKLE_PATH):
                return {}
            with open(self._PICKLE_PATH, "rb") as f:
                data = _pkl.load(f)
            if not isinstance(data, dict):
                logger.warning("[POLL CACHE] Pickle corrupt — ignored, starting fresh")
                return {}
            return data
        except Exception as e:
            logger.warning(f"[POLL CACHE] Pickle load failed (non-fatal): {e}")
            return {}

    async def restore_poll_mappings_to_bot_data(self) -> dict:
        """Restore poll→answer mappings into bot_data from MongoDB (primary)
        and pickle backup (secondary). Called once at startup, before polling.

        Priority: MongoDB > pickle > nothing.
        Bot never crashes regardless of pickle state.
        """
        stats = {"recovered_db": 0, "recovered_pickle": 0, "total": 0}

        # ── 1. Load from MongoDB (authoritative) ──────────────────
        db_mappings: dict = {}
        if self.db:
            try:
                db_mappings = self.db.get_active_poll_mappings()
                stats["recovered_db"] = len(db_mappings)
                logger.info(
                    f"[POLL RECOVERY] Mappings loaded from MongoDB: {stats['recovered_db']}"
                )
            except Exception as e:
                logger.warning(f"[POLL RECOVERY] MongoDB load failed (non-fatal): {e}")

        # ── 2. Load from pickle backup (secondary) ─────────────────
        pickle_mappings = self._pickle_load()
        # Count entries that MongoDB didn't already cover
        pickle_only = {k: v for k, v in pickle_mappings.items()
                       if k not in db_mappings}
        stats["recovered_pickle"] = len(pickle_only)
        if stats["recovered_pickle"]:
            logger.info(
                f"[POLL RECOVERY] Additional mappings from pickle: "
                f"{stats['recovered_pickle']}"
            )

        # ── 3. Merge (MongoDB wins on conflict) ────────────────────
        merged = {**pickle_only, **db_mappings}

        # ── 4. Populate bot_data ───────────────────────────────────
        bot_data = self.application.bot_data
        for key, val in merged.items():
            if key.startswith("poll_"):
                bot_data[key] = val

        stats["total"] = len(merged)
        # Update session-level stats
        self._poll_stats["recovered_db"]     = stats["recovered_db"]
        self._poll_stats["recovered_pickle"] = stats["recovered_pickle"]

        logger.info(
            f"[POLL RECOVERY] ✅ Restored {stats['total']} mappings into bot_data "
            f"(MongoDB: {stats['recovered_db']}, pickle-only: {stats['recovered_pickle']})"
        )
        return stats

    # ─── Startup tasks (called after application.start()) ────

    def run_startup_tasks(self):
        """Schedule startup broadcast + owner alert as background tasks."""
        loop = asyncio.get_event_loop()
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
        """Send greeting to all PM-accessible users on startup."""
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
            [InlineKeyboardButton("🎓 Leaderboard",      callback_data="leaderboard"),
             InlineKeyboardButton("❓ Help",              callback_data="help")],
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
                self.ensure_user_registered(user, is_pm=is_pm, source="cmd-start")
            except Exception as e:
                logger.error(f"upsert_user: {e}")

    # ══════════════════════════════════════════════════════════════
    #  GROUP TRACKING — SINGLE PIPELINE
    #
    #  ALL group events funnel through ensure_group_registered().
    #  This is the ONLY place that calls register_group_interaction.
    #
    #  _seen_groups is a PERFORMANCE CACHE ONLY.
    #  It prevents redundant MongoDB upserts for groups already seen
    #  this session.  It is NEVER used for counts or analytics.
    #  All counts come exclusively from the database.
    #
    #  Sources that call ensure_group_registered:
    #    • my_chat_member  — bot added/promoted/restricted
    #    • passive message handler (group 1) — any group message
    #    • handle_callback — inline button presses from groups
    #    • handle_poll_answer — quiz answers (chat recovered from bot_data)
    #    • cmd_start / cmd_quiz — explicit entry points
    #    • _handle_group_migration — group→supergroup ID transfer
    # ══════════════════════════════════════════════════════════════

    def ensure_group_registered(
        self,
        update: Update,
        context=None,
        source: str = "unknown",
    ) -> None:
        """Central group registration pipeline — call from every handler.

        DB is authoritative.  _seen_groups is a write-dedup cache only;
        never read it for analytics or broadcast targeting.
        """
        if not self.db:
            return

        chat = update.effective_chat

        # poll_answer has no effective_chat — recover chat_id from bot_data
        if chat is None and context is not None and update.poll_answer:
            poll_id  = update.poll_answer.poll_id
            data     = context.bot_data.get(f"poll_{poll_id}", {})
            chat_id  = data.get("chat_id")
            if chat_id and isinstance(chat_id, int) and chat_id < 0:
                if chat_id not in self._seen_groups:
                    try:
                        self.db.register_group_interaction(
                            chat_id  = chat_id,
                            title    = data.get("chat_title", ""),
                            username = ""
                        )
                        self._seen_groups.add(chat_id)
                        logger.info(
                            f"[GROUP REGISTERED] id={chat_id} source={source}"
                        )
                    except Exception as e:
                        logger.error(
                            f"ensure_group_registered poll {chat_id}: {e}"
                        )
            return

        if not chat or chat.type not in ("group", "supergroup"):
            return

        if chat.id in self._seen_groups:
            return  # already upserted this session — skip redundant DB write

        try:
            self.db.register_group_interaction(
                chat_id  = chat.id,
                thread_id= get_thread_id(update),
                title    = chat.title or "",
                username = getattr(chat, "username", "") or ""
            )
            self._seen_groups.add(chat.id)
            logger.info(
                f"[GROUP REGISTERED] id={chat.id} title={chat.title!r} "
                f"source={source}"
            )
        except Exception as e:
            logger.error(
                f"ensure_group_registered {chat.id} ({source}): {e}"
            )

    # ══════════════════════════════════════════════════════════════
    #  USER TRACKING — SINGLE PIPELINE
    #
    #  ALL user observations funnel through ensure_user_registered().
    #  _seen_users is a time-bounded PERFORMANCE CACHE ONLY (5-min TTL).
    #  Database is the sole source of truth for all user counts.
    # ══════════════════════════════════════════════════════════════

    _USER_CACHE_TTL = 300  # seconds

    def ensure_user_registered(
        self,
        user,
        is_pm: bool = None,
        source: str = "unknown",
    ) -> None:
        """Central user registration pipeline.

        Upserts the user record in MongoDB with current metadata.
        is_pm=True sets pm_accessible so broadcast can reach them.
        is_pm=None leaves pm_accessible unchanged (already-set values persist).
        """
        if not self.db or not user:
            return

        now_ts = time.time()
        cached = self._seen_users.get(user.id)
        if cached and (now_ts - cached) < self._USER_CACHE_TTL:
            return  # already upserted within TTL — skip redundant write

        try:
            data: dict = {
                "user_id":   user.id,
                "username":  user.username or "",
                "name":      UI.display_name(user),
                "last_seen": datetime.utcnow().isoformat(),
                "active_status": "active",
            }
            if is_pm is not None:
                data["pm_accessible"] = is_pm
            self.db.upsert_user(user.id, data)
            self._seen_users[user.id] = now_ts
            logger.debug(f"[USER REGISTERED] id={user.id} source={source}")
        except Exception as e:
            logger.error(f"ensure_user_registered {user.id} ({source}): {e}")

    # ─── Bot membership change handler ───────────────────────

    async def handle_my_chat_member(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Full lifecycle: add / remove / promote / demote / restrict / rejoin."""
        member = update.my_chat_member
        if not member:
            return
        chat = member.chat
        if chat.type not in ("group", "supergroup"):
            return

        new_status = member.new_chat_member.status
        old_status = member.old_chat_member.status

        if new_status in ("member", "administrator", "restricted"):
            # Bot present (active or restricted) — register / refresh metadata
            self.ensure_group_registered(update, context, source="my_chat_member")

            # Track admin status changes
            if self.db:
                try:
                    is_admin = (new_status == "administrator")
                    perms = None
                    if is_admin:
                        cm = member.new_chat_member
                        perms = {
                            "can_delete_messages":  getattr(cm, "can_delete_messages",  False),
                            "can_restrict_members": getattr(cm, "can_restrict_members", False),
                            "can_pin_messages":     getattr(cm, "can_pin_messages",     False),
                            "can_manage_chat":      getattr(cm, "can_manage_chat",      False),
                        }
                    self.db.update_group_admin_status(chat.id, is_admin, perms)
                except Exception as e:
                    logger.error(f"update_group_admin_status {chat.id}: {e}")

            action = {
                "member":        "added",
                "administrator": "promoted",
                "restricted":    "restricted",
            }.get(new_status, new_status)
            if old_status in ("left", "kicked", "banned"):
                logger.info(
                    f"[GROUP REACTIVATED] id={chat.id} title={chat.title!r}"
                )
            else:
                logger.info(
                    f"[GROUP {action.upper()}] id={chat.id} title={chat.title!r}"
                )

        elif new_status in ("left", "kicked", "banned"):
            # Bot removed — purge from DB so counts stay accurate
            if self.db:
                try:
                    self.db.remove_inactive_group(chat.id)
                    self._seen_groups.discard(chat.id)
                    logger.info(
                        f"[GROUP REMOVED] id={chat.id} title={chat.title!r} "
                        f"status={new_status}"
                    )
                except Exception as e:
                    logger.error(
                        f"handle_my_chat_member remove {chat.id}: {e}"
                    )

    async def _handle_group_migration(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Group → supergroup migration: move DB record to new chat_id."""
        msg  = update.effective_message
        chat = update.effective_chat
        if not msg or not self.db or not chat:
            return

        new_id = getattr(msg, "migrate_to_chat_id", None)
        old_id = getattr(msg, "migrate_from_chat_id", None)

        if new_id:
            # Message from the OLD group chat — transfer record to new ID
            try:
                self.db.remove_inactive_group(chat.id)
                self._seen_groups.discard(chat.id)
            except Exception as e:
                logger.error(f"migration remove old {chat.id}: {e}")
            # Register new supergroup ID using same title/username
            try:
                self.db.register_group_interaction(
                    chat_id  = new_id,
                    title    = chat.title or "",
                    username = getattr(chat, "username", "") or ""
                )
                self._seen_groups.add(new_id)
                logger.info(
                    f"[GROUP MIGRATION] old={chat.id} → new={new_id} "
                    f"title={chat.title!r}"
                )
            except Exception as e:
                logger.error(f"migration register new {new_id}: {e}")

        elif old_id:
            # Message from the NEW supergroup — ensure it is registered
            self.ensure_group_registered(update, context, source="migration-new-id")

    async def _auto_track(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handler group 1 — fires alongside every group message.
        Registers both the group and the sending user in a single pass."""
        self.ensure_group_registered(update, context, source="passive-message")
        user = update.effective_user
        if user:
            chat = update.effective_chat
            is_pm = chat.type == "private" if chat else False
            self.ensure_user_registered(user, is_pm=is_pm if is_pm else None,
                                        source="passive-message")

    async def recover_groups_from_history(self) -> None:
        """Startup recovery: find every group chat_id in activity history that is
        not yet in the groups collection, call getChat() for each, and register it.

        This runs ONCE at startup and immediately fixes the gap where the bot is
        in 22+ groups but only 5 are in the DB — with zero manual intervention.
        """
        if not self.db or not self.application:
            return
        try:
            known_ids   = self.db.get_known_group_ids_from_history()
            registered  = self.db.get_registered_group_ids()
            missing_ids = known_ids - registered

            if not missing_ids:
                logger.info("[STARTUP RECOVERY] No unregistered groups found in history")
                return

            logger.info(
                f"[STARTUP RECOVERY] {len(missing_ids)} group IDs in history but "
                f"not in DB — fetching metadata from Telegram API"
            )

            recovered = skipped = failed = 0
            for chat_id in missing_ids:
                try:
                    chat = await self.application.bot.get_chat(chat_id)
                    if chat.type in ("group", "supergroup"):
                        self.db.register_group_interaction(
                            chat_id  = chat.id,
                            title    = chat.title or "",
                            username = getattr(chat, "username", "") or ""
                        )
                        self._seen_groups.add(chat.id)
                        logger.info(
                            f"[GROUP RECOVERED] id={chat_id} title={chat.title!r}"
                        )
                        recovered += 1
                    else:
                        skipped += 1
                except Forbidden:
                    logger.info(
                        f"[GROUP SKIP] id={chat_id} — bot no longer a member"
                    )
                    skipped += 1
                except BadRequest as e:
                    logger.warning(f"[GROUP SKIP] id={chat_id} — {e}")
                    skipped += 1
                except Exception as e:
                    logger.error(f"[GROUP RECOVER FAIL] id={chat_id}: {e}")
                    failed += 1

            logger.info(
                f"[STARTUP RECOVERY] Done — recovered={recovered} "
                f"skipped={skipped} failed={failed}"
            )
        except Exception as e:
            logger.error(f"recover_groups_from_history: {e}")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                       edit_msg=None):
        is_owner = self._is_owner(update.effective_user.id) if update.effective_user else False

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

            f"🏆  <b>𝐂𝐎𝐌𝐏𝐄𝐓𝐈𝐓𝐈𝐎𝐍</b>\n"
            f"╭──────────────────────────────────────────╮\n"
            f"│  /leaderboard  ›  Global rankings\n"
            f"│  /lb                  ›  Quick shortcut\n"
            f"╰──────────────────────────────────────────╯\n\n"

            f"🔧  <b>𝐒𝐘𝐒𝐓𝐄𝐌</b>\n"
            f"╭──────────────────────────────────────────╮\n"
            f"│  /ping    ›  Latency check\n"
            f"│  /info     ›  Bot information\n"
            f"│  /start   ›  Dashboard\n"
            f"╰──────────────────────────────────────────╯\n"
        )

        if is_owner:
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
                f"│  /devstats    ›  Developer metrics\n"
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
             InlineKeyboardButton("🎓 Leaderboard", callback_data="leaderboard"),
             InlineKeyboardButton("ℹ️ Bot Info",    callback_data="info")],
            self._nav_row(back_screen="home"),
        ])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)

    # ─── /categories ─────────────────────────────────────────

    async def cmd_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                              edit_msg=None):
        # Live category list + question counts straight from the database
        cats = []
        if self.db:
            cats = self.db.get_category_counts()
        if not cats and self.quiz_manager:
            counts: dict = {}
            for q in self.quiz_manager.questions:
                c = q.get("category") or "General"
                counts[c] = counts.get(c, 0) + 1
            cats = [{"_id": c, "count": n}
                    for c, n in sorted(counts.items(), key=lambda x: -x[1])]

        total_q = sum(c.get("count", 0) for c in cats)
        lines = [
            f"📚  <b>𝗩𝗜𝗘𝗪 𝗖𝗔𝗧𝗘𝗚𝗢𝗥𝗜𝗘𝗦</b>",
            f"══════════════════",
            f"",
            f"📑  <b>𝗔𝗩𝗔𝗜𝗟𝗔𝗕𝗟𝗘 𝗤𝗨𝗜𝗭 𝗖𝗔𝗧𝗘𝗚𝗢𝗥𝗜𝗘𝗦</b>",
            f"",
        ]
        if cats:
            for c in cats:
                name  = (c.get("_id") or "General")
                safe  = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                emoji = UI.cat_emoji(name)
                lines.append(f"{emoji}  {safe}  ›  <b>{UI.fmt_num(c.get('count', 0))}</b>")
            lines += [
                f"",
                f"📚  Total  ›  <b>{UI.fmt_num(total_q)}</b> questions",
                f"",
                f"🎯  Use <code>/quiz &lt;category&gt;</code> to play a topic!",
            ]
        else:
            lines += [
                f"📭  No categories yet — question bank is empty.",
                f"",
                f"🛠  Use /addquiz or /importquiz to add questions.",
            ]
        text = "\n".join(lines)
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

    # ─── /quiz ───────────────────────────────────────────────

    async def cmd_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat      = update.effective_chat
        thread_id = get_thread_id(update)
        track_id  = get_tracking_id(chat.id, thread_id)
        category  = " ".join(context.args).strip() if context.args else ""

        question = self.quiz_manager.get_random_question(
            chat_id=track_id, category=category)

        if not question:
            cat_e = UI.cat_emoji(category)
            text  = (
                f"📭 <b>No Questions Found</b>\n"
                f"{UI.LINE}\n\n"
                + (f"  {cat_e} Category: <b>{category}</b>\n\n" if category else "")
                + "  The question bank is empty.\n\n"
                "  Use /addquiz to add questions."
            )
            await self._reply(update, text, reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎓 Try Again", callback_data="play_quiz")
            ]]))
            return

        options = question.get("options", [])
        if not isinstance(options, list) or len(options) < 2:
            await self._reply(update, "⚠️ Question data error. Try /quiz again.")
            return

        correct_idx = question.get("correct_answer", 0)
        if not isinstance(correct_idx, int) or not (0 <= correct_idx < len(options)):
            correct_idx = 0

        cat       = question.get("category", "General")
        cat_emoji = UI.cat_emoji(cat)
        q_id      = question.get("id")

        poll_kwargs = dict(
            question          = f"{cat_emoji} {question['question']}",
            options           = options,
            type              = Poll.QUIZ,
            correct_option_id = correct_idx,
            is_anonymous      = False,
            open_period       = 30,
            explanation       = (
                f"✅ {options[correct_idx]}\n"
                f"📚 {cat}  ·  🆔 Q#{q_id}"
            )
        )
        if thread_id:
            poll_kwargs["message_thread_id"] = thread_id

        try:
            poll_msg = await update.effective_message.reply_poll(**poll_kwargs)
            poll_id  = poll_msg.poll.id

            poll_entry = {
                "question_id":       q_id,
                "question":          question["question"],
                "correct_option_id": correct_idx,
                "chat_id":           chat.id,
                "thread_id":         thread_id,
                "tracking_id":       track_id,
                "category":          cat,
            }
            context.bot_data[f"poll_{poll_id}"] = poll_entry

            # Persist to MongoDB (primary) and pickle backup (secondary)
            if self.db and q_id:
                self.db.save_poll_mapping(str(poll_id), q_id, poll_data=poll_entry)
            self._pickle_save(f"poll_{poll_id}", poll_entry)
            self._poll_stats["stored"] += 1

            if chat.type in ("group", "supergroup"):
                try:
                    self.ensure_group_registered(update, context, source="cmd-quiz")
                except Exception as eg:
                    logger.error(f"register_group: {eg}")

        except TelegramError as e:
            err = str(e).lower()
            logger.error(f"send_poll error: {e}")
            text = (
                "⚠️ <b>Topic Restricted</b>\n\nThis topic is closed."
                if any(w in err for w in ("topic", "thread", "closed"))
                else f"⚠️ Could not send quiz:\n<code>{e}</code>"
            )
            await self._reply(update, text)

    # ─── POLL ANSWER HANDLER ─────────────────────────────────

    async def handle_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # poll_answer has no effective_chat; ensure_group_registered recovers
        # chat_id from bot_data using the poll_id.
        self.ensure_group_registered(update, context, source="poll-answer")

        answer     = update.poll_answer
        if answer.user:
            self.ensure_user_registered(answer.user, source="poll-answer")
        user_id    = answer.user.id
        poll_id    = answer.poll_id
        option_ids = answer.option_ids

        data       = context.bot_data.get(f"poll_{poll_id}", {})
        correct_id = data.get("correct_option_id")
        chat_id    = data.get("chat_id", 0)
        thread_id  = data.get("thread_id")
        track_id   = data.get("tracking_id", chat_id)

        if correct_id is None or not option_ids:
            # mapping missing — track it
            self._poll_stats["lost"] += 1
            return

        is_correct = (option_ids[0] == correct_id)

        try:
            self.quiz_manager.record_attempt(user_id, is_correct)
            if chat_id and chat_id != user_id:
                self.quiz_manager.record_group_attempt(user_id, chat_id, is_correct)
        except Exception as e:
            logger.error(f"record_attempt: {e}")

        if self.db:
            try:
                self.db.log_activity("quiz_answer",
                    user_id=user_id, chat_id=chat_id,
                    thread_id=thread_id, poll_id=poll_id,
                    is_correct=is_correct,
                    category=data.get("category", ""))
                self.db.upsert_user(user_id, {
                    "user_id":   user_id,
                    "last_seen": datetime.utcnow().isoformat(),
                })
            except Exception as e:
                logger.error(f"DB poll_answer: {e}")

            # Record quiz result for Progress Center
            try:
                self.db.record_quiz_result(user_id, {
                    "correct":  1 if is_correct else 0,
                    "wrong":    0 if is_correct else 1,
                    "skipped":  0,
                    "total":    1,
                    "score":    1 if is_correct else 0,
                    "category": data.get("category", "General"),
                })
            except Exception as e:
                logger.error(f"record_quiz_result: {e}")

    # ─── /score ──────────────────────────────────────────────

    async def cmd_score(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                        edit_msg=None):
        import math as _math
        user    = update.effective_user
        mention = UI.mention(user.id, UI.display_name(user))

        # Load from DB if available, fallback to quiz_manager
        db_doc = {}
        if self.db:
            try:
                db_doc = self.db.get_user(user.id) or {}
            except Exception as e:
                logger.error(f"cmd_score get_user: {e}")

        correct        = db_doc.get("correct_answers",  0)
        wrong          = db_doc.get("wrong_answers",    0)
        total_q        = db_doc.get("total_questions",  0)
        total_marks    = db_doc.get("total_marks",      0)
        best_score     = db_doc.get("best_score",       0)
        xp             = db_doc.get("xp",               0)
        level          = db_doc.get("level",            1)
        streak         = db_doc.get("current_streak",   0)

        accuracy = round(correct / max(total_q, 1) * 100, 1) if total_q else 0
        avg_score = round(total_marks / max(db_doc.get("quizzes_completed", 0) or 1, 1), 1)

        # XP bar within current level
        xp_for_level    = level * level * 100
        xp_for_next     = (level + 1) * (level + 1) * 100
        xp_in_level     = xp - xp_for_level
        xp_needed       = max(xp_for_next - xp_for_level, 1)
        xp_pct          = min(100, int(xp_in_level / xp_needed * 100))
        xp_bar          = UI.mini_bar(xp_pct)

        # Global rank
        global_rank  = 0
        total_users  = 0
        if self.db:
            try:
                rank_info   = self.db.get_user_rank(user.id)
                global_rank = rank_info.get("global_rank", 0)
                total_users = rank_info.get("total_users", 0)
            except Exception as e:
                logger.error(f"cmd_score get_user_rank: {e}")

        rank_str = f"#{global_rank}  of  {total_users}" if global_rank else "—"

        text = (
            f"🏆  <b>𝐒𝐂𝐎𝐑𝐄𝐂𝐀𝐑𝐃</b>\n"
            f"{UI.LINE}\n\n"
            f"👤  {mention}\n\n"
            f"{UI.LINE}\n\n"
            f"🥇  Rank        ›  {rank_str}\n"
            f"📈  Accuracy    ›  {accuracy}%\n"
            f"🎯  Avg Score   ›  {avg_score}\n\n"
            f"🔥  Streak      ›  {streak} days\n"
            f"⚡  XP          ›  {xp}\n"
            f"🏅  Level       ›  {level}\n\n"
            f"{xp_bar}  {xp_pct}% to next level\n\n"
            f"{UI.LINE}\n\n"
            f"📚  Questions   ›  {total_q}\n"
            f"✅  Correct     ›  {correct}\n"
            f"❌  Wrong       ›  {wrong}\n\n"
            f"🏆  Best Score  ›  {best_score}\n"
            f"📝  Total Marks ›  {total_marks}\n\n"
            f"{UI.LINE}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Play Quiz",      callback_data="play_quiz"),
             InlineKeyboardButton("🎓 My Profile",     callback_data="my_profile")],
            [InlineKeyboardButton("🎓 Achievements",   callback_data="achievements"),
             InlineKeyboardButton("🎓 Leaderboard",    callback_data="leaderboard")],
            self._nav_row(back_screen="home"),
        ])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)

    # ─── /stats ──────────────────────────────────────────────

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                        edit_msg=None):
        user    = update.effective_user
        mention = UI.mention(user.id, UI.display_name(user))

        if edit_msg:
            # Edit the existing message with loading indicator
            try:
                await edit_msg.edit_text("📊 <i>Crunching your analytics...</i>",
                                         parse_mode=ParseMode.HTML)
            except Exception:
                pass
            msg = edit_msg
        else:
            msg = await self._reply(update, "📊 <i>Crunching your analytics...</i>")
        await asyncio.sleep(0.4)

        db_doc = {}
        if self.db:
            try:
                db_doc = self.db.get_user(user.id) or {}
            except Exception as e:
                logger.error(f"cmd_stats get_user: {e}")

        quizzes_completed = db_doc.get("quizzes_completed", 0)
        correct           = db_doc.get("correct_answers",   0)
        total_q           = db_doc.get("total_questions",   0)
        total_marks       = db_doc.get("total_marks",       0)
        streak            = db_doc.get("current_streak",    0)
        subject_stats     = db_doc.get("subject_stats",     {})
        if not isinstance(subject_stats, dict):
            subject_stats = {}

        accuracy  = round(correct / max(total_q, 1) * 100, 1) if total_q else 0
        avg_score = round(total_marks / max(quizzes_completed or 1, 1), 1)

        text = (
            f"📊  <b>𝐀𝐍𝐀𝐋𝐘𝐓𝐈𝐂𝐒</b>\n"
            f"{UI.LINE}\n\n"
            f"👤  {mention}\n\n"
            f"<b>𝐎𝐕𝐄𝐑𝐀𝐋𝐋  𝐏𝐄𝐑𝐅𝐎𝐑𝐌𝐀𝐍𝐂𝐄</b>\n"
            f"╭──────────────────────────────╮\n"
            f"│  Quizzes      ›  {quizzes_completed}\n"
            f"│  Accuracy     ›  {accuracy}%\n"
            f"│  Avg Score    ›  {avg_score}\n"
            f"│  Total Marks  ›  {total_marks}\n"
            f"╰──────────────────────────────╯\n\n"
        )

        # Subject breakdown
        if subject_stats:
            text += (
                f"<b>𝐒𝐔𝐁𝐉𝐄𝐂𝐓  𝐁𝐑𝐄𝐀𝐊𝐃𝐎𝐖𝐍</b>\n"
                f"╭──────────────────────────────╮\n"
            )
            for subj, sdata in subject_stats.items():
                if not isinstance(sdata, dict):
                    continue
                s_attempted = sdata.get("attempted", 0)
                s_correct   = sdata.get("correct",   0)
                s_acc       = round(s_correct / max(s_attempted, 1) * 100, 1) if s_attempted else 0
                text += f"│  {subj[:18]}   ›  {s_acc}%  ({s_correct}/{s_attempted})\n"
            text += f"╰──────────────────────────────╯\n\n"

        # Insights
        insights = []
        if quizzes_completed == 0:
            insights.append("Start your first quiz with /quiz!")
        else:
            # Weakest subject
            weak_subject = None
            weak_acc     = 100.0
            best_subject = None
            best_acc     = 0.0
            for subj, sdata in subject_stats.items():
                if not isinstance(sdata, dict):
                    continue
                s_attempted = sdata.get("attempted", 0)
                s_correct   = sdata.get("correct",   0)
                if s_attempted < 3:
                    continue
                s_acc = s_correct / max(s_attempted, 1) * 100
                if s_acc < weak_acc:
                    weak_acc     = s_acc
                    weak_subject = subj
                if s_acc > best_acc:
                    best_acc     = s_acc
                    best_subject = subj

            if weak_subject and weak_acc < 60:
                insights.append(f"Your {weak_subject} needs improvement.")
            if best_subject:
                insights.append(f"Your strongest subject is {best_subject} at {round(best_acc, 1)}%.")
            if streak > 5:
                insights.append(f"🔥 You're on a {streak}-day streak! Keep going!")

        if insights:
            text += "<b>𝐈𝐍𝐒𝐈𝐆𝐇𝐓𝐒</b>\n"
            for ins in insights:
                text += f"• {ins}\n"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Play Quiz",     callback_data="play_quiz"),
             InlineKeyboardButton("🎓 Leaderboard",   callback_data="leaderboard")],
            self._nav_row(back_screen="home"),
        ])
        if edit_msg:
            await self._smart_edit(update, text, kb, edit_msg=edit_msg)
        elif msg:
            await self._edit(msg, text, kb)
        else:
            await self._smart_edit(update, text, kb)

    # ─── /achievements ───────────────────────────────────────

    async def cmd_achievements(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                edit_msg=None):
        user    = update.effective_user
        mention = UI.mention(user.id, UI.display_name(user))

        earned_list = []
        if self.db:
            try:
                earned_list = self.db.get_user_achievements(user.id) or []
            except Exception as e:
                logger.error(f"cmd_achievements get_user_achievements: {e}")

        # Normalise stored achievements (could be dicts or strings)
        earned_keys = set()
        earned_display = []
        for a in earned_list:
            if isinstance(a, dict):
                earned_keys.add(a.get("key", ""))
                label     = a.get("label", a.get("key", "?"))
                earned_at = a.get("earned_at", "")
                date_str  = earned_at[:10] if earned_at else "—"
                earned_display.append(f"  {label}  <i>({date_str})</i>")
            else:
                earned_keys.add(str(a))
                earned_display.append(f"  {a}")

        # Locked achievements
        all_keys = list(self.db.ACHIEVEMENTS.keys()) if self.db else []
        locked_display = []
        for key in all_keys:
            if key not in earned_keys:
                ach = self.db.ACHIEVEMENTS[key]
                locked_display.append(f"  🔒  ???  <i>({ach['label']})</i>")

        count = len(earned_keys)
        total = len(all_keys)

        earned_text = "\n".join(earned_display) if earned_display else "  None yet — play /quiz to earn some!"
        locked_text = "\n".join(locked_display[:10]) if locked_display else "  All achievements unlocked! 🎉"
        if len(locked_display) > 10:
            locked_text += f"\n  <i>… and {len(locked_display) - 10} more</i>"

        text = (
            f"🏅  <b>𝐀𝐂𝐇𝐈𝐄𝐕𝐄𝐌𝐄𝐍𝐓𝐒</b>\n"
            f"{UI.LINE}\n\n"
            f"👤  {mention}\n\n"
            f"🔓  <b>𝐄𝐀𝐑𝐍𝐄𝐃</b>  ({count}/{total})\n"
            f"{earned_text}\n\n"
            f"🔒  <b>𝐋𝐎𝐂𝐊𝐄𝐃</b>\n"
            f"{locked_text}\n\n"
            f"{UI.LINE}\n"
            f"Keep playing to unlock more! 🎓"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Play Quiz",  callback_data="play_quiz"),
             InlineKeyboardButton("🎓 My Profile",   callback_data="my_profile")],
            self._nav_row(back_screen="home"),
        ])
        await self._smart_edit(update, text, kb, edit_msg=edit_msg)

    # ─── /botstats ───────────────────────────────────────────

    async def cmd_botstats(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                           edit_msg=None, page: str = "overview"):
        # Loading indicator (only on fresh call, not on navigation)
        if edit_msg is None:
            wait = await self._reply(update, "📊 <i>Loading analytics...</i>")
            await asyncio.sleep(0.35)
        else:
            wait = None

        q_total = self._q_count()
        d = {}
        if self.db:
            try:
                d = self.db.get_analytics_data()
            except Exception as e:
                logger.error(f"cmd_botstats: {e}")

        def _acc(c, t):
            return f"{round(c/t*100,1)}%" if t else "—"

        def _qs(s):
            att = s.get("attempts", 0)
            cor = s.get("correct", 0)
            return att, cor, _acc(cor, att), s.get("players", 0)

        # ── Build page text ───────────────────────────────────
        if page == "users":
            u  = d.get("u_total", 0)
            pm = d.get("u_pm", 0)
            ad = d.get("u_active_d", 0)
            aw = d.get("u_active_w", 0)
            nd = d.get("u_new_d", 0)
            nw = d.get("u_new_w", 0)
            nm = d.get("u_new_m", 0)
            er = d.get("engage_rate", 0)
            tu = d.get("top_user") or {}
            tu_name  = tu.get("name") or tu.get("username") or "—"
            tu_uid   = tu.get("user_id")
            tu_pts   = tu.get("total_marks", 0)
            tu_ref   = UI.mention(tu_uid, tu_name[:18]) if tu_uid else "—"
            text = (
                f"📊  <b>𝐁𝐎𝐓  𝐀𝐍𝐀𝐋𝐘𝐓𝐈𝐂𝐒</b>\n"
                f"{'━'*38}\n\n"
                f"👥  <b>𝐔𝐒𝐄𝐑𝐒  —  𝐃𝐄𝐓𝐀𝐈𝐋</b>\n"
                f"╭──────────────────────────────────────╮\n"
                f"│  Total           ›  <b>{UI.fmt_num(u)}</b>\n"
                f"│  Broadcast Reach ›  <b>{pm}</b>  (DM-accessible)\n"
                f"│  Active 24h      ›  <b>{ad}</b>\n"
                f"│  Active 7d       ›  <b>{aw}</b>\n"
                f"│  New Today       ›  <b>+{nd}</b>\n"
                f"│  New This Week   ›  <b>+{nw}</b>\n"
                f"│  New This Month  ›  <b>+{nm}</b>\n"
                f"│  Engagement Rate ›  <b>{er}%</b>  (24h)\n"
                f"│  Top User        ›  {tu_ref}  ⭐{tu_pts:,}\n"
                f"╰──────────────────────────────────────╯\n\n"
                f"{'━'*38}\n"
                f"⚡  {COMMUNITY}  ·  CLAT Vision Analytics"
            )

        elif page == "quiz":
            qd_a, qd_c, qd_acc, qd_p = _qs(d.get("qs_d", {}))
            qw_a, qw_c, qw_acc, qw_p = _qs(d.get("qs_w", {}))
            qm_a, qm_c, qm_acc, qm_p = _qs(d.get("qs_m", {}))
            qa_a, qa_c, qa_acc, _     = _qs(d.get("qs_a", {}))
            subj = d.get("subj_stats", [])
            lines = [
                f"📊  <b>𝐁𝐎𝐓  𝐀𝐍𝐀𝐋𝐘𝐓𝐈𝐂𝐒</b>",
                f"{'━'*38}",
                f"",
                f"🎯  <b>𝐐𝐔𝐈𝐙  𝐀𝐂𝐓𝐈𝐕𝐈𝐓𝐘</b>",
                f"",
                f"24h   ›  <b>{qd_a}</b> attempts  ·  <b>{qd_c}</b> correct"
                f"  ·  <b>{qd_acc}</b>  ·  <b>{qd_p}</b> players",
                f"7d    ›  <b>{qw_a}</b> attempts  ·  <b>{qw_c}</b> correct"
                f"  ·  <b>{qw_acc}</b>  ·  <b>{qw_p}</b> players",
                f"30d   ›  <b>{qm_a}</b> attempts  ·  <b>{qm_c}</b> correct"
                f"  ·  <b>{qm_acc}</b>  ·  <b>{qm_p}</b> players",
                f"All   ›  <b>{qa_a}</b> attempts  ·  <b>{qa_c}</b> correct"
                f"  ·  <b>{qa_acc}</b>",
                f"",
                f"{'━'*38}",
            ]
            if subj:
                lines.append(f"📂  <b>𝐒𝐔𝐁𝐉𝐄𝐂𝐓  𝐁𝐑𝐄𝐀𝐊𝐃𝐎𝐖𝐍</b>")
                lines.append(f"╭──────────────────────────────────────╮")
                for s in subj:
                    cat  = (s.get("_id") or "General")[:18]
                    att  = s.get("attempts", 0)
                    cor  = s.get("correct", 0)
                    sacc = _acc(cor, att)
                    lines.append(f"│  {cat:<18}  ›  <b>{att}</b> att  <b>{sacc}</b>")
                lines.append(f"╰──────────────────────────────────────╯")
            lines += [f"{'━'*38}", f"⚡  {COMMUNITY}  ·  CLAT Vision Analytics"]
            text = "\n".join(lines)

        elif page == "top":
            top5 = []
            if self.db:
                try:
                    top5 = list(self.db.users_col.find(
                        {}, {"user_id": 1, "name": 1, "username": 1,
                             "total_marks": 1, "correct_answers": 1,
                             "quizzes_completed": 1, "xp": 1})
                        .sort(self.db._LB_SORT)
                        .limit(5))
                except Exception:
                    pass
            lines = [
                f"📊  <b>𝐁𝐎𝐓  𝐀𝐍𝐀𝐋𝐘𝐓𝐈𝐂𝐒</b>",
                f"{'━'*38}",
                f"",
                f"🏆  <b>𝐓𝐎𝐏  𝟓  𝐏𝐋𝐀𝐘𝐄𝐑𝐒</b>",
                f"╭──────────────────────────────────────╮",
            ]
            medals = ["🥇", "🥈", "🥉", "  4.", "  5."]
            for i, u in enumerate(top5):
                uid   = u.get("user_id")
                nm    = (u.get("name") or u.get("username") or f"User{str(uid)[-4:]}")[:16]
                men   = UI.mention(uid, nm) if uid else "—"
                pts   = u.get("total_marks", 0)
                cor   = u.get("correct_answers", 0)
                qz    = u.get("quizzes_completed", 0)
                lines.append(f"│  {medals[i]}  {men}  ⭐{pts:,}  ✅{cor:,}  🎯{qz}")
            if not top5:
                lines.append(f"│  No players yet — be the first!")
            lines += [
                f"╰──────────────────────────────────────╯",
                f"",
                f"{'━'*38}",
                f"⚡  {COMMUNITY}  ·  CLAT Vision Analytics",
            ]
            text = "\n".join(lines)

        else:  # overview (default — matches example exactly)
            u_total    = d.get("u_total", 0)
            u_pm       = d.get("u_pm", 0)
            u_active_d = d.get("u_active_d", 0)
            u_active_w = d.get("u_active_w", 0)
            u_new_d    = d.get("u_new_d", 0)
            u_new_w    = d.get("u_new_w", 0)
            u_new_m    = d.get("u_new_m", 0)
            g_total    = d.get("g_total", 0)
            g_admin    = d.get("g_admin", 0)
            g_new_d    = d.get("g_new_d", 0)
            g_new_w    = d.get("g_new_w", 0)
            g_new_m    = d.get("g_new_m", 0)
            q_fmt      = UI.fmt_num(q_total)
            q_cats     = d.get("q_cats", 0)
            qd_a, qd_c, qd_acc, qd_p = _qs(d.get("qs_d", {}))
            qw_a, qw_c, qw_acc, qw_p = _qs(d.get("qs_w", {}))
            qm_a, qm_c, qm_acc, qm_p = _qs(d.get("qs_m", {}))
            qa_a, qa_c, qa_acc, _     = _qs(d.get("qs_a", {}))

            # Live system metrics
            dbs = {}
            bc_total = 0
            if self.db:
                try:
                    dbs      = self.db.get_db_stats()
                    bc_total = self.db.broadcasts_col.count_documents({})
                except Exception as e:
                    logger.error(f"botstats system metrics: {e}")
            up = int(time.time() - self._start_ts)
            if up >= 86400:
                uptime_str = f"{up // 86400}d {(up % 86400) // 3600}h"
            elif up >= 3600:
                uptime_str = f"{up // 3600}h {(up % 3600) // 60}m"
            else:
                uptime_str = f"{up // 60}m {up % 60}s"

            text = (
                f"📊  <b>𝐁𝐎𝐓  𝐀𝐍𝐀𝐋𝐘𝐓𝐈𝐂𝐒</b>\n"
                f"{'━'*38}\n\n"

                f"👥  <b>𝐔𝐒𝐄𝐑𝐒</b>\n"
                f"╭──────────────────────────────────────╮\n"
                f"│  Total           ›  <b>{UI.fmt_num(u_total)}</b>\n"
                f"│  Broadcast Reach ›  <b>{u_pm}</b>  (DM-accessible)\n"
                f"│  Active 24h      ›  <b>{u_active_d}</b>\n"
                f"│  Active 7d       ›  <b>{u_active_w}</b>\n"
                f"│  New Today       ›  <b>+{u_new_d}</b>\n"
                f"│  New This Week   ›  <b>+{u_new_w}</b>\n"
                f"│  New This Month  ›  <b>+{u_new_m}</b>\n"
                f"╰──────────────────────────────────────╯\n\n"

                f"💬  <b>𝐆𝐑𝐎𝐔𝐏𝐒</b>\n"
                f"╭──────────────────────────────────────╮\n"
                f"│  Total           ›  <b>{UI.fmt_num(g_total)}</b>\n"
                f"│  Bot Is Admin    ›  <b>{g_admin}</b>\n"
                f"│  New Today       ›  <b>+{g_new_d}</b>\n"
                f"│  New This Week   ›  <b>+{g_new_w}</b>\n"
                f"│  New This Month  ›  <b>+{g_new_m}</b>\n"
                f"╰──────────────────────────────────────╯\n\n"

                f"📚  <b>𝐐𝐔𝐄𝐒𝐓𝐈𝐎𝐍  𝐁𝐀𝐍𝐊</b>  ›  <b>{q_fmt}</b> questions"
                f"  ·  <b>{q_cats}</b> categories\n\n"
                f"{'━'*38}\n\n"

                f"🎯  QUIZ ACTIVITY\n\n"
                f"24h   ›  <b>{qd_a}</b> attempts  ·  <b>{qd_c}</b> correct"
                f"  ·  <b>{qd_acc}</b>  ·  <b>{qd_p}</b> players\n"
                f"7d    ›  <b>{qw_a}</b> attempts  ·  <b>{qw_c}</b> correct"
                f"  ·  <b>{qw_acc}</b>  ·  <b>{qw_p}</b> players\n"
                f"30d   ›  <b>{qm_a}</b> attempts  ·  <b>{qm_c}</b> correct"
                f"  ·  <b>{qm_acc}</b>  ·  <b>{qm_p}</b> players\n"
                f"All   ›  <b>{qa_a}</b> attempts  ·  <b>{qa_c}</b> correct"
                f"  ·  <b>{qa_acc}</b>\n\n"

                f"{'━'*38}\n\n"

                f"🗄  <b>𝐒𝐘𝐒𝐓𝐄𝐌</b>\n"
                f"╭──────────────────────────────────────╮\n"
                f"│  Collections     ›  <b>{dbs.get('collections', '—')}</b>\n"
                f"│  Documents       ›  <b>{UI.fmt_num(dbs.get('objects', 0))}</b>\n"
                f"│  Data Size       ›  <b>{dbs.get('data_mb', 0)} MB</b>\n"
                f"│  Storage Size    ›  <b>{dbs.get('storage_mb', 0)} MB</b>\n"
                f"│  Broadcasts Sent ›  <b>{bc_total}</b>\n"
                f"│  Uptime          ›  <b>{uptime_str}</b>\n"
                f"╰──────────────────────────────────────╯\n\n"

                f"{'━'*38}\n"
                f"⚡  {COMMUNITY}  ·  CLAT Vision Analytics"
            )

        # ── Navigation keyboard ───────────────────────────────
        def _tab(label, p):
            mark = " ✓" if page == p else ""
            return InlineKeyboardButton(label + mark, callback_data=f"bs_{p}")

        kb = InlineKeyboardMarkup([
            [_tab("📊 Overview", "overview"),
             _tab("👥 Users",    "users"),
             _tab("🎯 Quiz",     "quiz"),
             _tab("🏆 Top",      "top")],
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"bs_refresh_{page}"),
             InlineKeyboardButton("🏠 Home",    callback_data="nav_home")],
        ])

        target = edit_msg or wait
        if target:
            await self._edit(target, text, kb)
        else:
            msg = await self._reply(update, text, reply_markup=kb)
            if msg and update.effective_user:
                self._active_msg[update.effective_user.id] = msg

    # ─── /leaderboard ────────────────────────────────────────
    #  Paginated Top-50 leaderboard — 10 per page, 5 pages.

    LB_PAGE_SIZE = 10
    LB_MAX_RANKS = 50

    async def cmd_leaderboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._show_leaderboard(update, context, mode="global", page=1)

    # ── Period mapping ─────────────────────────────────────────
    _LB_PERIOD = {"global": 36500, "weekly": 7, "monthly": 30}
    _LB_LABEL  = {"global": "All-Time", "weekly": "This Week", "monthly": "This Month"}

    # Number emojis for positions 1-10
    _LB_NUM = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
               6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟"}

    @staticmethod
    def _lb_badge(pos: int) -> str:
        if pos == 1:     return "👑"
        elif pos <= 3:   return "🥇"
        elif pos <= 10:  return "🏅"
        elif pos <= 20:  return "⭐"
        else:            return "✨"

    def _lb_clip(self, name: str, width: int = 20) -> str:
        name = (name or "").replace("\n", " ").strip()
        return name[:width - 1] + "…" if len(name) > width else name

    def _lb_fetch(self, mode: str, chat_id: int) -> list:
        """Always fetch live from the database — no caching."""
        if mode == "group":
            data = self.quiz_manager.get_group_leaderboard(chat_id)
            return data.get("leaderboard", [])
        if self.db:
            days = self._LB_PERIOD.get(mode, 36500)
            return self.db.get_leaderboard_by_period(days=days, limit=self.LB_MAX_RANKS)
        return self.quiz_manager.get_leaderboard(limit=self.LB_MAX_RANKS)

    def _lb_resolve_names(self, uids: list) -> dict:
        names: dict = {}
        if not uids or not self.db:
            return names
        try:
            for doc in self.db.users_col.find(
                    {"user_id": {"$in": uids}},
                    {"user_id": 1, "name": 1, "username": 1}):
                n = (doc.get("name") or doc.get("username") or "").strip()
                if n:
                    names[doc["user_id"]] = n
        except Exception as e:
            logger.error(f"_lb_resolve_names error: {e}")
        return names

    async def _show_my_rank(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                            mode: str = "global", edit_msg=None):
        req_user = update.effective_user
        if not req_user or not self.db:
            await self._smart_edit(update,
                                   "❌ Could not fetch your rank.", None, edit_msg)
            return

        SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        days = self._LB_PERIOD.get(mode, 36500)
        try:
            info   = self.db.get_user_rank_in_period(req_user.id, days)
            streak = self.quiz_manager.get_user_stats(
                req_user.id).get("current_streak", 0)
        except Exception as e:
            logger.error(f"_show_my_rank: {e}")
            await self._smart_edit(update, "❌ Could not fetch rank data.", None, edit_msg)
            return

        rank    = info.get("rank", 0)
        correct = info.get("correct", 0)
        total   = info.get("total", 0)
        acc     = info.get("accuracy", 0)
        above   = info.get("above_correct")
        mention = UI.mention(req_user.id, UI.display_name(req_user))

        if rank == 1:     badge = "👑 Champion"
        elif rank <= 3:   badge = "🥇 Elite"
        elif rank <= 10:  badge = "🏅 Top 10"
        elif rank <= 20:  badge = "⭐ Top 20"
        elif rank <= 50:  badge = "✨ Top 50"
        elif rank > 0:    badge = "🎯 Ranked"
        else:             badge = "🎯 Unranked"

        label   = self._LB_LABEL.get(mode, "All-Time")
        lines   = [
            f"📍  <b>𝐘𝐎𝐔𝐑 𝐑𝐀𝐍𝐊𝐈𝐍𝐆</b>",
            f"",
            SEP,
            f"",
            f"👤 {mention}",
            f"",
        ]

        if rank > 0:
            lines += [
                f"🏅 Rank <b>#{rank}</b>  •  {badge}",
                f"",
                f"⭐ <b>{correct}</b> Points",
                f"",
                f"🎯 <b>{acc}%</b> Accuracy",
                f"",
                f"🔥 <b>{streak}</b> Streak",
                f"",
            ]
            if above is not None and rank > 1:
                gap = max(0, above - correct)
                lines += [
                    f"📈 Need <b>{gap}</b> More Point{'s' if gap != 1 else ''} For Rank <b>#{rank - 1}</b>",
                    f"",
                ]
        else:
            lines += [
                f"<i>No activity yet for {label}.</i>",
                f"Play a quiz to appear on the board!",
                f"",
            ]

        lines.append(SEP)
        text = "\n".join(lines)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Play Quiz",   callback_data="play_quiz"),
             InlineKeyboardButton("🏆 Leaderboard", callback_data=f"lbp_{mode}_1")],
            [InlineKeyboardButton("🏠 Home", callback_data="back_start")],
        ])
        await self._smart_edit(update, text, kb, edit_msg)

    async def _show_leaderboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                mode: str = "global", page: int = 1, edit_msg=None):
        chat     = update.effective_chat
        is_group = chat.type in ("group", "supergroup")
        req_user = update.effective_user

        if is_group and mode in ("global", "weekly", "monthly"):
            mode = "group"

        if edit_msg is None:
            wait_msg = await self._reply(update, "🏆  <i>Loading leaderboard…</i>")
        else:
            wait_msg = None

        lb    = self._lb_fetch(mode, chat.id)
        label = self._LB_LABEL.get(mode, "All-Time")
        SEP   = "━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        # ── Empty state ───────────────────────────────────────
        if not lb:
            text = (
                f"🏆  <b>𝐂𝐋𝐀𝐓 𝐕𝐈𝐒𝐈𝐎𝐍 • 𝐋𝐄𝐀𝐃𝐄𝐑𝐁𝐎𝐀𝐑𝐃</b>\n"
                f"📅 {label} • Top {self.LB_MAX_RANKS} Players\n\n"
                f"{SEP}\n\n"
                f"🥇  No champions yet!\n\n"
                f"Be the first to top the board.\n"
                f"Tap <b>Play Quiz</b> to begin. 🚀"
            )
            kb = self._build_lb_keyboard(mode, 1, 1, is_group)
            target = edit_msg or wait_msg
            if target:
                await self._edit(target, text, kb)
            else:
                await self._reply(update, text, reply_markup=kb)
            if wait_msg and req_user:
                self._active_msg[req_user.id] = wait_msg
            return

        # ── Pagination ────────────────────────────────────────
        total       = min(len(lb), self.LB_MAX_RANKS)
        total_pages = max(1, (total + self.LB_PAGE_SIZE - 1) // self.LB_PAGE_SIZE)
        page        = max(1, min(page, total_pages))
        start       = (page - 1) * self.LB_PAGE_SIZE
        end         = min(start + self.LB_PAGE_SIZE, total)
        page_slice  = lb[start:end]

        # ── Batch-fetch names for this page ───────────────────
        page_uids = [e.get("user_id") for e in page_slice]
        names     = self._lb_resolve_names(page_uids)

        def _mention(entry):
            uid  = entry.get("user_id")
            raw  = names.get(uid) or f"User{str(uid)[-4:]}"
            disp = self._lb_clip(raw, 20)
            return UI.mention(uid, disp) if uid else disp

        # ── Build message ─────────────────────────────────────
        lines = [
            f"🏆  <b>𝐂𝐋𝐀𝐓 𝐕𝐈𝐒𝐈𝐎𝐍 • 𝐋𝐄𝐀𝐃𝐄𝐑𝐁𝐎𝐀𝐑𝐃</b>",
            f"📅 {label} • Top {total} Players",
            f"",
            SEP,
        ]

        if page == 1:
            # Champions (positions 1-3)
            top3 = page_slice[:3]
            rest = page_slice[3:]

            lines += ["", "👑  <b>𝐂𝐇𝐀𝐌𝐏𝐈𝐎𝐍𝐒</b>", ""]
            for i, entry in enumerate(top3):
                pts = entry.get("correct_answers", entry.get("score", 0))
                lines += [f"{'🥇🥈🥉'[i]} {_mention(entry)} ⭐ <b>{pts}</b> Points", ""]

            if rest:
                lines += [SEP, "", "🏅  <b>𝐓𝐎𝐏 𝐑𝐀𝐍𝐊𝐈𝐍𝐆𝐒</b>", ""]
                for i, entry in enumerate(rest):
                    pos = 4 + i
                    pts = entry.get("correct_answers", entry.get("score", 0))
                    num = self._LB_NUM.get(pos, f"<b>{pos}.</b>")
                    lines += [f"{num} {_mention(entry)} ⭐ <b>{pts}</b> Points", ""]
        else:
            lines += ["", f"🏅  <b>𝐑𝐀𝐍𝐊𝐈𝐍𝐆𝐒</b>  •  <i>#{start + 1}–#{end}</i>", ""]
            for i, entry in enumerate(page_slice):
                pos   = start + i + 1
                pts   = entry.get("correct_answers", entry.get("score", 0))
                badge = self._lb_badge(pos)
                lines += [f"{badge} <b>#{pos}</b>  {_mention(entry)} ⭐ <b>{pts}</b> Points", ""]

        # ── Badge legend + page indicator ─────────────────────
        lines += [
            SEP,
            "",
            "👑 Champion • 🥇 Elite • 🏅 Top 10 • ⭐ Top 20 • ✨ Top 50",
            "",
            SEP,
            "",
            f"📄 Page {page} / {total_pages}",
            f"⚡ Updated Live From Database",
        ]
        text = "\n".join(lines)

        kb = self._build_lb_keyboard(mode, page, total_pages, is_group)
        target = edit_msg or wait_msg
        if target:
            await self._edit(target, text, kb)
            if req_user:
                self._active_msg[req_user.id] = target
        else:
            result = await self._reply(update, text, reply_markup=kb)
            if result and req_user:
                self._active_msg[req_user.id] = result

    def _build_lb_keyboard(self, mode: str, page: int, total_pages: int,
                           is_group: bool) -> InlineKeyboardMarkup:
        has_prev = page > 1
        has_next = page < total_pages

        rows = []
        # Mode tabs (not shown in group mode)
        if not is_group and mode != "group":
            rows.append([
                InlineKeyboardButton(
                    "🌐 All-Time" + (" ✓" if mode == "global"  else ""),
                    callback_data="lbp_global_1"),
                InlineKeyboardButton(
                    "📅 Weekly"   + (" ✓" if mode == "weekly"  else ""),
                    callback_data="lbp_weekly_1"),
                InlineKeyboardButton(
                    "🗓 Monthly"  + (" ✓" if mode == "monthly" else ""),
                    callback_data="lbp_monthly_1"),
            ])
        # Navigation row
        rows.append([
            InlineKeyboardButton(
                "⬅️ Previous" if has_prev else "⬅️",
                callback_data=f"lbp_{mode}_{page - 1}" if has_prev else "lb_noop"),
            InlineKeyboardButton(
                "📍 My Rank",
                callback_data=f"lb_myrank_{mode}"),
            InlineKeyboardButton(
                "🏠 Home",
                callback_data="back_start"),
            InlineKeyboardButton(
                "➡️ Next" if has_next else "➡️",
                callback_data=f"lbp_{mode}_{page + 1}" if has_next else "lb_noop"),
        ])
        return InlineKeyboardMarkup(rows)

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
                    q_id = self.db.get_quiz_id_from_poll(str(poll_id))
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
        await query.answer()
        data  = query.data
        actor = query.from_user

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
            try: await query.message.delete()
            except Exception: pass
            return

        if action == "page":
            page = int(parts[2])
            self._del_page[actor.id] = page
            try:
                await query.message.edit_text(
                    self._delquiz_text(questions, page),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._delquiz_kb(questions, page, actor.id)
                )
            except Exception:
                pass
            return

        if action == "del":
            qid     = int(parts[2])
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
                try:
                    await query.message.edit_text(text, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            else:
                try:
                    await query.message.edit_text(
                        f"❌ <b>Not Found</b>\n{UI.LINE}\n\nQuestion #{qid} not found.",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

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
                users  = self.db.get_user_engagement_stats().get('total_users', 0)
                groups = len(self.db.get_all_groups())
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
            f"  /broadcast /reload    /restart\n"
            f"  /devstats  /activity  /performance\n\n"
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

        users  = self.db.get_pm_accessible_users()
        groups = self.db.get_all_groups()
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

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user

        if not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return

        doc = update.effective_message.document
        if not doc:
            return

        mention = UI.mention(user.id, UI.display_name(user))

        fname  = doc.file_name or ""
        is_txt = (
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

        if doc.file_size and doc.file_size > 2 * 1024 * 1024:
            await self._reply(update,
                f"❌ <b>File Too Large</b>\n"
                f"{UI.LINE}\n\n"
                f"  Maximum size: 2 MB.\n"
                f"  Split into smaller files."
            )
            return

        msg = await self._reply(update,
            f"📥 <b>IMPORT STARTED</b>\n"
            f"{UI.LINE}\n\n"
            f"  By {mention}\n"
            f"  📄 <code>{fname}</code>\n"
            f"  Size: <code>{doc.file_size or 0:,} bytes</code>\n\n"
            f"  ⏳ Parsing questions..."
        )

        try:
            file_obj  = await context.bot.get_file(doc.file_id,
                read_timeout=60, write_timeout=60, connect_timeout=60)
            raw_bytes = await file_obj.download_as_bytearray(read_timeout=60)
        except Exception as e:
            logger.error(f"File download error: {e}")
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
                if msg:
                    await self._edit(msg,
                        f"❌ <b>Encoding Error</b>\n"
                        f"{UI.LINE}\n\n"
                        f"  Please save the file as UTF-8 and retry."
                    )
                return

        if msg:
            await self._edit(msg,
                f"📥 <b>IMPORT STARTED</b>\n"
                f"{UI.LINE}\n\n"
                f"  By {mention}\n"
                f"  📄 <code>{fname}</code>\n\n"
                f"  🔍 Analyzing {len(text.splitlines())} lines..."
            )

        try:
            from src.bot.quiz_parser import bulk_import
            result = bulk_import(text, self.quiz_manager)
        except Exception as e:
            logger.error(f"bulk_import error: {e}")
            if msg:
                await self._edit(msg,
                    f"❌ <b>Import Failed</b>\n"
                    f"{UI.LINE}\n\n"
                    f"  Error: <code>{e}</code>"
                )
            return

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
        else:
            await self._reply(update, text_out, reply_markup=kb)

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
