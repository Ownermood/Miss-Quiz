"""
admin_panel.py — /admin: red-button control panel inside Telegram.

Only owner / WIFU / developers (self._is_authorized). Works in DM only so
stats never leak into groups. Every button re-checks authorization.
"""

import asyncio
import logging
import re

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.bot.premium import btn, e
from src.bot.ui import UI

logger = logging.getLogger(__name__)

RED  = "danger"    # only for the entry button and dangerous actions
BLUE = "primary"   # panel buttons
PHOTO_KEY = "start_photo"

GUIDES = {
    "bc": (
        f"<b>BROADCAST</b>\n{UI.THIN}\n"
        "• <code>/broadcast your text</code> — bold, italic, quote, spoiler, links all kept\n"
        "• Reply <code>/broadcast</code> to a photo/video/file/any message to send it\n"
        "• Buttons at the end: <code>[[\"Join\",\"https://t.me/x\"]]</code>\n"
        "• Placeholders: <code>{first_name}</code> <code>{username}</code> <code>{chat_title}</code>\n"
        "• Then <code>/broadcast_confirm</code>  ·  undo: <code>/delbroadcast</code>"
    ),
    "q": (
        f"<b>QUESTIONS</b>\n{UI.THIN}\n"
        "• <code>/addquiz</code> — add one question\n"
        "• <code>/importquiz</code> then send a .txt file — bulk import\n"
        "• <code>/editquiz</code> — edit with buttons\n"
        "• <code>/delquiz</code> — delete"
    ),
    "team": (
        f"<b>TEAM</b>\n{UI.THIN}\n"
        "• <code>/dev</code> — list / add / remove developers\n"
        "• <code>/restart</code> — owner only"
    ),
}


class AdminPanelMixin:

    def _panel_kb(self, is_owner: bool) -> InlineKeyboardMarkup:
        rows = [
            [btn("Bot Stats", "stats", BLUE, callback_data="ap:stats"),
             btn("Reload", "bolt", BLUE, callback_data="ap:reload")],
            [btn("Broadcast", "signal", BLUE, callback_data="ap:g:bc"),
             btn("Questions", "edit", BLUE, callback_data="ap:g:q")],
            [btn("Start Photo", "folder", BLUE, callback_data="ap:photo"),
             btn("Team", "users", BLUE, callback_data="ap:g:team")],
        ]
        if is_owner:
            rows.append([btn("Restart Bot", "warn", RED, callback_data="ap:restart")])
        rows.append([btn("Close", "cross", None, callback_data="ap:close")])
        return InlineKeyboardMarkup(rows)

    async def _panel_text(self) -> str:
        q = self._q_count()
        users = groups = admin_g = 0
        if self.db:
            try:
                st = await asyncio.to_thread(self.db.get_user_engagement_stats)
                users = st.get("total_users", 0)
                c = await asyncio.to_thread(self.db.count_groups_by_admin)
                groups, admin_g = c["total"], c["admin"]
            except Exception as ex:
                logger.warning(f"[ADMIN PANEL] stats failed: {ex}")
        return (
            f"{e('key')} <b>ADMIN PANEL</b>\n{UI.LINE}\n\n"
            f"{e('edit')} Questions  ›  <b>{q}</b>\n"
            f"{e('users')} Users      ›  <b>{users}</b>\n"
            f"{e('signal')} Groups     ›  <b>{groups}</b>\n"
            f"{e('check')} Auto quiz ON (bot admin)  ›  <b>{admin_g}</b>\n"
            f"{e('warn')} Paused (not admin)  ›  <b>{groups - admin_g}</b>\n\n"
            f"<i>Only you can see this.</i>"
        )

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, chat = update.effective_user, update.effective_chat
        if not user or not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return
        if chat.type != "private":
            await self._reply(update, f"{e('key')} Open the admin panel in my DM: /admin")
            return
        await self._reply(update, await self._panel_text(),
                          reply_markup=self._panel_kb(self._is_owner(user.id)))

    async def handle_admin_panel_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q, user = update.callback_query, update.effective_user
        if not q or not user:
            return
        if not await self._is_authorized(user.id):
            await q.answer("⛔ Admins only.", show_alert=True)
            return
        data = q.data or ""
        owner = self._is_owner(user.id)
        back = InlineKeyboardMarkup([[btn("Back", "back", BLUE, callback_data="ap:home")]])

        if data == "ap:open":            # red button on /start (message may be a photo)
            await q.answer()
            await self._reply(update, await self._panel_text(), reply_markup=self._panel_kb(owner))
        elif data == "ap:photo":
            await q.answer()
            context.user_data["ap_await"] = PHOTO_KEY
            cur = await self._get_start_photo()
            kb = InlineKeyboardMarkup(
                ([[btn("Remove photo", "trash", RED, callback_data="ap:photo_del")]] if cur else [])
                + [[btn("Back", "back", BLUE, callback_data="ap:home")]])
            await self._edit(q.message,
                f"{e('folder')} <b>START PHOTO</b>\n{UI.THIN}\n"
                f"Status: <b>{'set ✅' if cur else 'not set'}</b>\n\n"
                "Now send me <b>a photo</b>, or paste an <b>image link</b> "
                "(https://… .jpg/.png from any image host).\n"
                "It will show on /start for everyone.", kb)
        elif data == "ap:photo_del":
            await q.answer("Removed")
            context.user_data.pop("ap_await", None)
            await self._set_start_photo(None)
            await self._edit(q.message, await self._panel_text(), self._panel_kb(owner))
        elif data == "ap:home":
            context.user_data.pop("ap_await", None)
            await q.answer()
            await self._edit(q.message, await self._panel_text(), self._panel_kb(owner))
        elif data.startswith("ap:g:") and data[5:] in GUIDES:
            await q.answer()
            await self._edit(q.message, GUIDES[data[5:]], back)
        elif data == "ap:stats":
            await q.answer()
            await self.cmd_botstats(update, context)
        elif data == "ap:reload":
            await q.answer("Reloading…")
            await self.cmd_reload(update, context)
        elif data == "ap:restart" and owner:
            await q.answer()
            kb = InlineKeyboardMarkup([
                [btn("Yes, restart now", "warn", RED, callback_data="ap:restart_yes")],
                [btn("Cancel", "back", RED, callback_data="ap:home")],
            ])
            await self._edit(q.message, f"{e('warn')} <b>Restart the bot?</b>", kb)
        elif data == "ap:restart_yes" and owner:
            await q.answer("Restarting…")
            await self.cmd_restart(update, context)
        elif data == "ap:close":
            await q.answer()
            try:
                await q.message.delete()
            except Exception:
                pass
        else:
            await q.answer("⛔ Not allowed.", show_alert=True)

    # ── start photo (stored in settings, cached in memory) ───────────────

    async def _get_start_photo(self):
        if not hasattr(self, "_start_photo_cache"):
            val = None
            if self.db:
                try:
                    val = await asyncio.to_thread(self.db.get_setting, PHOTO_KEY)
                except Exception as ex:
                    logger.warning(f"[ADMIN PANEL] read start photo failed: {ex}")
                    return None
            self._start_photo_cache = val
        return self._start_photo_cache

    async def _set_start_photo(self, file_id) -> None:
        if self.db:
            await asyncio.to_thread(self.db.set_setting, PHOTO_KEY, file_id)
        self._start_photo_cache = file_id

    async def handle_admin_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Group -1 handler: consumes the next photo / image link from an admin
        who pressed 'Start Photo'. Does nothing for everyone else."""
        ud = context.user_data if context.user_data is not None else {}
        if ud.get("ap_await") != PHOTO_KEY:
            return
        user, msg = update.effective_user, update.effective_message
        if not user or not msg or not await self._is_authorized(user.id):
            ud.pop("ap_await", None)
            return
        source = None
        if msg.photo:
            source = msg.photo[-1].file_id
        elif msg.text and re.match(r"^https?://\S+$", msg.text.strip()):
            source = msg.text.strip()
        else:
            await self._reply(update, f"{e('warn')} Send a photo or an https:// image link.")
            raise ApplicationHandlerStop
        try:
            # Send it back as a preview: for links this makes Telegram fetch the
            # image once, and we keep its file_id so a dead link can't break /start.
            sent = await msg.reply_photo(source, caption=f"{e('check')} <b>Start photo saved.</b>",
                                         parse_mode="HTML")
            await self._set_start_photo(sent.photo[-1].file_id)
            ud.pop("ap_await", None)
        except Exception as ex:
            logger.warning(f"[ADMIN PANEL] start photo rejected: {ex}")
            await self._reply(update, f"{e('cross')} Telegram could not load that image. "
                                      "Try another photo or a direct image link.")
        raise ApplicationHandlerStop
