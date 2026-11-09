"""
admin_panel.py — /admin: every admin command as a button.

Only owner / WIFU / developers (self._is_authorized). DM only, so stats never
leak into groups. Every button re-checks authorization.

Commands that need input (add quiz, broadcast, start photo, add/remove dev)
set context.user_data["ap_await"]; the next message from that admin is
consumed by handle_admin_input (handler group -1). /cancel or Back clears it.
Existing command handlers are reused through small proxies, so behaviour
stays identical to typing the command.
"""

import asyncio
import logging
import re

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.bot.premium import btn, e
from src.bot.ui import UI

logger = logging.getLogger(__name__)

RED   = "danger"    # Back, destructive actions, entry button on /start
BLUE  = "primary"
GREEN = "success"
PHOTO_KEY = "start_photo"

GUIDES = {
    "bc": (
        f"<b>BROADCAST</b>\n{UI.THIN}\n"
        "• <b>Broadcast</b> button → send any message (text, photo, video, file)\n"
        "• Bold, italic, quote, spoiler, links are kept\n"
        "• Buttons at the end: <code>[[\"Join\",\"https://t.me/x\"]]</code>\n"
        "• Placeholders: <code>{first_name}</code> <code>{username}</code> <code>{chat_title}</code>"
    ),
}

ADDQUIZ_HELP = (
    "<b>ADD QUESTION</b>\n{line}\n"
    "<b>Send in this format (one per line):</b>\n"
    "<code>Question text\nOption A\nOption B\nOption C\nOption D\n"
    "Correct (1-4)\nCategory</code>\n\n"
    "<b>Example:</b>\n<code>Which Article abolishes untouchability?\n"
    "Article 14\nArticle 17\nArticle 19\nArticle 21\n2\nPolity</code>\n\n"
    "<i>/cancel to stop</i>"
)


class _MsgProxy:
    """A message stand-in: same as `base`, but with chosen text / reply_to."""

    def __init__(self, base, text="", reply_to=None):
        self._base = base
        self.text = text
        self.reply_to_message = reply_to

    def __getattr__(self, name):
        return getattr(self._base, name)


class _UpdProxy:
    """An update stand-in whose message is `msg`; user/chat stay the real ones."""

    def __init__(self, update, msg):
        self._update = update
        self.message = msg
        self.effective_message = msg
        self.callback_query = None

    def __getattr__(self, name):
        return getattr(self._update, name)


class AdminPanelMixin:

    # ── screens ──────────────────────────────────────────────────────────

    def _panel_kb(self, is_owner: bool) -> InlineKeyboardMarkup:
        rows = [
            [btn("Bot Stats", "stats", BLUE, callback_data="ap:stats"),
             btn("Reload", "bolt", BLUE, callback_data="ap:reload")],
            [btn("Add Quiz", "plus", BLUE, callback_data="ap:addquiz"),
             btn("Import .txt", "folder", BLUE, callback_data="ap:import")],
            [btn("Edit Quiz", "edit", BLUE, callback_data="ap:edit"),
             btn("Delete Quiz", "trash", BLUE, callback_data="ap:delquiz")],
            [btn("Broadcast", "signal", BLUE, callback_data="ap:bc"),
             btn("Undo Broadcast", "back", BLUE, callback_data="ap:dbc")],
            [btn("Start Photo", "folder", BLUE, callback_data="ap:photo"),
             btn("Team", "users", BLUE, callback_data="ap:team")],
        ]
        if is_owner:
            rows.append([btn("Restart Bot", "warn", RED, callback_data="ap:restart")])
        rows.append([btn("Close", "cross", None, callback_data="ap:close")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _back_kb(extra=None) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup((extra or []) + [[btn("Back", "back", RED, callback_data="ap:home")]])

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
            f"<blockquote><b>{e('edit')} Questions  ›  {q}\n"
            f"{e('users')} Users  ›  {users}\n"
            f"{e('signal')} Groups  ›  {groups}</b></blockquote>\n"
            f"<blockquote><b>{e('check')} Auto quiz ON (bot admin)  ›  {admin_g}\n"
            f"{e('warn')} Paused (not admin)  ›  {groups - admin_g}</b></blockquote>\n"
            f"<b><i>Only you can see this. Tap a button.</i></b>"
        )

    def _proxy(self, update, text=""):
        q = update.callback_query
        base = q.message if q else update.effective_message
        return _UpdProxy(update, _MsgProxy(base, text=text))

    # ── /admin ───────────────────────────────────────────────────────────

    async def cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, chat = update.effective_user, update.effective_chat
        if not user or not await self._is_authorized(user.id):
            await self._unauthorized(update)
            return
        if chat.type != "private":
            await self._reply(update, f"{e('key')} <b>Open the admin panel in my DM: /admin</b>")
            return
        await self._reply(update, await self._panel_text(),
                          reply_markup=self._panel_kb(self._is_owner(user.id)))

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data is not None and context.user_data.pop("ap_await", None):
            await self._reply(update, f"{e('cross')} <b>Cancelled.</b>")

    # ── buttons ──────────────────────────────────────────────────────────

    async def handle_admin_panel_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q, user = update.callback_query, update.effective_user
        if not q or not user:
            return
        if not await self._is_authorized(user.id):
            await q.answer("⛔ Admins only.", show_alert=True)
            return
        data = q.data or ""
        owner = self._is_owner(user.id)
        ud = context.user_data if (context and context.user_data is not None) else {}
        dev = getattr(self, "_dev", None)

        async def ask(key, text, extra=None):
            ud["ap_await"] = key
            await q.answer()
            await self._edit(q.message, text, self._back_kb(extra))

        if data == "ap:open":            # red button on /start (message may be a photo)
            await q.answer()
            await self._reply(update, await self._panel_text(), reply_markup=self._panel_kb(owner))
        elif data == "ap:home":
            ud.pop("ap_await", None)
            await q.answer()
            await self._edit(q.message, await self._panel_text(), self._panel_kb(owner))
        elif data.startswith("ap:g:") and data[5:] in GUIDES:
            await q.answer()
            await self._edit(q.message, GUIDES[data[5:]], self._back_kb())

        # read-only / one-tap commands
        elif data == "ap:stats":
            await q.answer()
            await self.cmd_botstats(update, context)
        elif data == "ap:reload":
            await q.answer("Reloading…")
            await self.cmd_reload(update, context)
        elif data == "ap:import":
            await q.answer()
            await self.cmd_importquiz(self._proxy(update), context)
        elif data == "ap:delquiz":
            await q.answer()
            await self.cmd_delquiz(self._proxy(update), context)
        elif data == "ap:edit":
            await q.answer()
            if context is not None:
                context.args = []
            await self.cmd_editquiz(self._proxy(update, "/editquiz"), context)

        # commands that need the admin's next message
        elif data == "ap:addquiz":
            await ask("addquiz", ADDQUIZ_HELP.format(line=UI.THIN))
        elif data == "ap:bc":
            await ask("broadcast",
                      f"{e('signal')} <b>BROADCAST</b>\n{UI.THIN}\n"
                      "<b>Send me the message to broadcast</b> — text, photo, video, file, "
                      "anything. Formatting is kept.\n"
                      "Buttons at the end: <code>[[\"Join\",\"https://t.me/x\"]]</code>\n\n"
                      "<i>/cancel to stop</i>")
        elif data == "ap:photo":
            cur = await self._get_start_photo()
            await ask(PHOTO_KEY,
                      f"{e('folder')} <b>START PHOTO</b>\n{UI.THIN}\n"
                      f"<b>Status: {'set ✅' if cur else 'not set'}</b>\n\n"
                      "<b>Send me a photo, or paste an image link</b> "
                      "(https://… .jpg/.png from any image host).\n"
                      "<i>It shows on /start for everyone. /cancel to stop.</i>",
                      [[btn("Remove photo", "trash", RED, callback_data="ap:photo_del")]] if cur else None)
        elif data == "ap:photo_del":
            ud.pop("ap_await", None)
            await q.answer("Removed")
            await self._set_start_photo(None)
            await self._edit(q.message, await self._panel_text(), self._panel_kb(owner))
        elif data == "ap:team":
            await q.answer()
            if dev and context is not None:
                context.args = ["list"]
                await dev.dev(self._proxy(update, "/dev list"), context)
            await self._reply(update, f"{e('users')} <b>TEAM</b>", reply_markup=InlineKeyboardMarkup([
                [btn("Add Developer", "plus", GREEN, callback_data="ap:dev_add"),
                 btn("Remove Developer", "trash", RED, callback_data="ap:dev_rm")],
                [btn("Back", "back", RED, callback_data="ap:home")]]))
        elif data in ("ap:dev_add", "ap:dev_rm"):
            verb = "add" if data == "ap:dev_add" else "remove"
            await ask(f"dev_{verb}",
                      f"{e('users')} <b>{verb.upper()} DEVELOPER</b>\n{UI.THIN}\n"
                      "<b>Send the user's numeric Telegram ID.</b>\n<i>/cancel to stop</i>")

        # broadcast confirm / undo
        elif data == "ap:bc_yes" and dev:
            await q.answer("Sending…")
            await dev.broadcast_confirm(self._proxy(update, "/broadcast_confirm"), context)
        elif data == "ap:bc_no":
            for k in [k for k in ud if str(k).startswith("broadcast_")]:
                ud.pop(k, None)
            await q.answer("Cancelled")
            await self._edit(q.message, f"{e('cross')} <b>Broadcast cancelled.</b>", self._back_kb())
        elif data == "ap:dbc" and dev:
            await q.answer()
            await dev.delbroadcast(self._proxy(update, "/delbroadcast"), context)
            await self._reply(update, f"{e('warn')} <b>Delete the last broadcast everywhere?</b>",
                              reply_markup=InlineKeyboardMarkup([
                                  [btn("Yes, delete it", "trash", RED, callback_data="ap:dbc_yes")],
                                  [btn("Back", "back", RED, callback_data="ap:home")]]))
        elif data == "ap:dbc_yes" and dev:
            await q.answer("Deleting…")
            await dev.delbroadcast_confirm(self._proxy(update, "/delbroadcast_confirm"), context)

        # owner only
        elif data == "ap:restart" and owner:
            await q.answer()
            await self._edit(q.message, f"{e('warn')} <b>Restart the bot?</b>", InlineKeyboardMarkup([
                [btn("Yes, restart now", "warn", RED, callback_data="ap:restart_yes")],
                [btn("Back", "back", RED, callback_data="ap:home")]]))
        elif data == "ap:restart_yes" and owner:
            await q.answer("Restarting…")
            await self.cmd_restart(update, context)
        elif data == "ap:close":
            ud.pop("ap_await", None)
            await q.answer()
            try:
                await q.message.delete()
            except Exception:
                pass
        else:
            await q.answer("⛔ Not allowed.", show_alert=True)

    # ── the admin's next message ─────────────────────────────────────────

    async def handle_admin_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Group -1 handler: consumes the next message of an admin who pressed a
        button that needs input. Does nothing for everyone else."""
        ud = context.user_data if context.user_data is not None else {}
        key = ud.get("ap_await")
        if not key:
            return
        user, msg = update.effective_user, update.effective_message
        if not user or not msg or not await self._is_authorized(user.id):
            ud.pop("ap_await", None)
            return

        if key == PHOTO_KEY:
            await self._save_start_photo_from(update, msg, ud)
        elif key == "addquiz":
            if not msg.text:
                await self._reply(update, f"{e('warn')} <b>Send the question as text.</b>")
            else:
                ud.pop("ap_await", None)
                await self.cmd_addquiz(update, context)
        elif key == "broadcast":
            ud.pop("ap_await", None)
            dev = getattr(self, "_dev", None)
            if dev:
                if msg.text:
                    context.args = msg.text.split()
                    await dev.broadcast(update, context)
                else:   # media / anything else: treat it as the replied-to message
                    context.args = []
                    await dev.broadcast(_UpdProxy(update, _MsgProxy(msg, "/broadcast", reply_to=msg)), context)
                await self._reply(update, f"{e('signal')} <b>Send this broadcast?</b>",
                                  reply_markup=InlineKeyboardMarkup([
                                      [btn("Send now", "check", GREEN, callback_data="ap:bc_yes"),
                                       btn("Cancel", "cross", RED, callback_data="ap:bc_no")]]))
        elif key in ("dev_add", "dev_remove"):
            uid = (msg.text or "").strip()
            if not uid.isdigit():
                await self._reply(update, f"{e('warn')} <b>Send a numeric Telegram ID.</b>")
            else:
                ud.pop("ap_await", None)
                dev = getattr(self, "_dev", None)
                if dev:
                    context.args = [key[4:], uid]
                    await dev.dev(update, context)
        raise ApplicationHandlerStop

    # ── start photo (stored in settings, cached in memory) ───────────────

    async def _save_start_photo_from(self, update, msg, ud) -> None:
        source = None
        if msg.photo:
            source = msg.photo[-1].file_id
        elif msg.text and re.match(r"^https?://\S+$", msg.text.strip()):
            source = msg.text.strip()
        else:
            await self._reply(update, f"{e('warn')} <b>Send a photo or an https:// image link.</b>")
            return
        try:
            # Send it back as a preview: for links this makes Telegram fetch the
            # image once, and we keep its file_id so a dead link can't break /start.
            sent = await msg.reply_photo(source, caption=f"{e('check')} <b>Start photo saved.</b>",
                                         parse_mode="HTML")
            await self._set_start_photo(sent.photo[-1].file_id)
            ud.pop("ap_await", None)
        except Exception as ex:
            logger.warning(f"[ADMIN PANEL] start photo rejected: {ex}")
            await self._reply(update, f"{e('cross')} <b>Telegram could not load that image. "
                                      "Try another photo or a direct image link.</b>")

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
