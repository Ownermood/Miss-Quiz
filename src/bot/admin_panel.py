"""
admin_panel.py — /admin: red-button control panel inside Telegram.

Only owner / WIFU / developers (self._is_authorized). Works in DM only so
stats never leak into groups. Every button re-checks authorization.
"""

import asyncio
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.bot.premium import btn, e
from src.bot.ui import UI

logger = logging.getLogger(__name__)

RED = "danger"

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
            [btn("Bot Stats", "stats", RED, callback_data="ap:stats"),
             btn("Reload", "bolt", RED, callback_data="ap:reload")],
            [btn("Broadcast", "signal", RED, callback_data="ap:g:bc"),
             btn("Questions", "edit", RED, callback_data="ap:g:q")],
            [btn("Team", "users", RED, callback_data="ap:g:team")],
        ]
        if is_owner:
            rows.append([btn("Restart Bot", "warn", RED, callback_data="ap:restart")])
        rows.append([btn("Close", "cross", RED, callback_data="ap:close")])
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
        back = InlineKeyboardMarkup([[btn("Back", "back", RED, callback_data="ap:home")]])

        if data == "ap:home":
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
