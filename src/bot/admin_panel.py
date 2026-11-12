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

ADDQ_HELP = (
    "➕ ADD QUESTIONS\n"
    "{line}\n"
    "Send any of these — as many as you want:\n"
    "<blockquote>• Type / paste questions (one or hundreds)\n"
    "• A file: .txt .docx .pdf .csv .xlsx .json .html .md .odt .pptx .rtf\n"
    "• Forward Telegram quiz polls (answer comes with them)</blockquote>\n\n"
    "Example (any similar layout works):\n"
    "<blockquote>1. Which Article abolishes untouchability?\n"
    "A) Article 14\nB) Article 17\nC) Article 19\nD) Article 21\n"
    "Answer: B\n"
    "Explanation: Article 17 abolishes untouchability.\n"
    "Category: Polity</blockquote>\n\n"
    "<blockquote>Also fine: ✅ after the right option · answer key at the end (1-B 2-A) · "
    "Hindi (प्रश्न / उत्तर) · CSV/Excel columns question, A, B, C, D, answer</blockquote>\n\n"
    "<i>Tap ✅ Done when finished.</i>"
)


class _MsgProxy:
    """A message stand-in: same as `base`, but with chosen text / reply_to.

    edit=True: reply_text() EDITS `base` (the panel message) instead of sending
    a new message, so panel actions stay in one message (clean chat). A Back
    button to the panel is added to every screen shown this way."""

    def __init__(self, base, text="", reply_to=None, edit=False):
        self._base = base
        self.text = text
        self.reply_to_message = reply_to
        self._edit_mode = edit

    def __getattr__(self, name):
        return getattr(self._base, name)

    async def reply_text(self, text, **kw):
        if not self._edit_mode:
            return await self._base.reply_text(text, **kw)
        kb = kw.get("reply_markup")
        back = [btn("Back", "back", RED, callback_data="ap:home")]
        rows = list(kb.inline_keyboard) if isinstance(kb, InlineKeyboardMarkup) else []
        if not any(b.callback_data == "ap:home" for r in rows for b in r):
            rows.append(back)
        try:
            res = await self._base.edit_text(
                text, parse_mode=kw.get("parse_mode"), reply_markup=InlineKeyboardMarkup(rows),
                link_preview_options=kw.get("link_preview_options"))
            # later edits of this message (e.g. "Syncing…" → result) keep the Back button
            return _KeepBack(res if hasattr(res, "edit_text") else self._base)
        except Exception as ex:
            if "not modified" in str(ex).lower():
                return _KeepBack(self._base)
            # photo message or too old to edit: fall back to a new message
            kw["reply_markup"] = InlineKeyboardMarkup(rows)
            kw.pop("message_thread_id", None)
            return await self._base.reply_text(text, **kw)


class _KeepBack:
    """Panel message whose edit_text always keeps a Back-to-panel button
    (a plain edit without reply_markup would remove all buttons)."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    async def edit_text(self, text, **kw):
        kb = kw.get("reply_markup")
        rows = list(kb.inline_keyboard) if isinstance(kb, InlineKeyboardMarkup) else []
        if not any(b.callback_data == "ap:home" for r in rows for b in r):
            rows.append([btn("Back", "back", RED, callback_data="ap:home")])
        kw["reply_markup"] = InlineKeyboardMarkup(rows)
        return await self._base.edit_text(text, **kw)


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
        admin_row = [btn("Admins List", "users", None, callback_data="ap:team")]
        if is_owner:
            admin_row.append(btn("Restart Bot", "warn", GREEN, callback_data="ap:restart"))

        rows = [
            # 1. Primary Question Bank Action (Green)
            [btn("Add Questions to Bank", "plus", GREEN, callback_data="ap:addq")],

            # 2. Question Bank Management (Clean Dark Glass)
            [btn("Edit Quiz Bank", "edit", None, callback_data="ap:edit"),
             btn("Find Question", "search", None, callback_data="ap:find")],

            # 3. Analytics & Live Health Monitor (Clean Dark Glass)
            [btn("Bot Stats", "stats", None, callback_data="ap:stats"),
             btn("Health Check", "gear", None, callback_data="ap:check")],

            # 4. DB Sanitizer & Reload (Clean Dark Glass)
            [btn("Clean & Sanitize DB", "check", None, callback_data="ap:cleanup"),
             btn("Reload Cache", "bolt", None, callback_data="ap:reload")],

            # 5. Broadcast Tools (Clean Dark Glass)
            [btn("Broadcast", "signal", None, callback_data="ap:bc"),
             btn("Undo Broadcast", "back", None, callback_data="ap:dbc")],

            # 6. Media & Emoji Testing (Clean Dark Glass)
            [btn("Start Photo", "folder", None, callback_data="ap:photo"),
             btn("Emoji Test", "star", None, callback_data="ap:emoji:0")],

            # 7. Team Management & Restart (Red for Restart)
            admin_row,

            # 8. Delete Question (Red) & Exit to Home (Blue)
            [btn("Delete Question", "trash", RED, callback_data="ap:delquiz"),
             btn("Home", "door", BLUE, callback_data="ap:close")],
        ]
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
            f"{e('key')}  <b>ADMIN CONTROL PANEL</b>\n\n"
            f"<blockquote><b>1️⃣ LIVE SYSTEM OVERVIEW\n"
            f"• Question Bank  ›  {q:,}\n"
            f"• Total Users    ›  {users:,}\n"
            f"• Total Groups   ›  {groups:,}  (Active: {admin_g} · Paused: {groups - admin_g})</b></blockquote>\n"
            f"<blockquote><b>2️⃣ ADMIN COMMAND SHORTCUTS\n"
            f"• /check — Live RAM, CPU &amp; Uptime Health\n"
            f"• /cleanup — Sanitize DB &amp; Clear Stale Loops\n"
            f"• /addadmin — 1-Line Admin Grant (Name Emoji ID)\n"
            f"• /addquiz · /editquiz · /delquiz · /reload\n"
            f"• /broadcast · /delbroadcast · /restart</b></blockquote>\n"
            f"<i>Select an action below (visible only in your DM):</i>"
        )

    def _proxy(self, update, text=""):
        """Run a command handler from a panel button: its replies edit the panel."""
        q = update.callback_query
        base = q.message if q else update.effective_message
        return _UpdProxy(update, _MsgProxy(base, text=text, edit=bool(q)))

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
        if context.user_data is not None:
            context.user_data.pop("mq_renaming", None)
            context.user_data.pop("mq_attaching_file", None)
            context.user_data.pop("mq_prac", None)
            if context.user_data.pop("mq_draft", None):
                await self._reply(update, f"{e('cross')} <b>Quiz not saved.</b>")
                return
            if context.user_data.pop("ap_await", None):
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
            await self._edit(q.message, await self._panel_text(), self._panel_kb(owner))
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
            await self.cmd_botstats(update, context, edit_msg=q.message)
        elif data == "ap:check":
            await q.answer("Checking system health… 🩺")
            await self.cmd_check(self._proxy(update, "/check"), context)
        elif data == "ap:cleanup":
            await q.answer("Sanitizing DB & cleaning stale sessions… 🧹")
            await self.cmd_cleanup(self._proxy(update, "/cleanup"), context)
        elif data == "ap:reload":
            await q.answer("Reloading…")
            await self.cmd_reload(self._proxy(update, "/reload"), context)
        elif data == "ap:delquiz":
            await q.answer()
            await self.cmd_delquiz(self._proxy(update), context)
        elif data == "ap:edit":
            await q.answer()
            if context is not None:
                context.args = []
            await self.cmd_editquiz(self._proxy(update, "/editquiz"), context)

        # commands that need the admin's next message
        elif data.startswith("ap:emoji:"):
            await q.answer()
            try:
                page = int(data.split(":")[2])
            except (ValueError, IndexError):
                page = 0
            await self._edit(q.message, *self._emoji_test_page(page))
        elif data == "ap:find":
            await ask("find",
                      f"🔎 <b>FIND QUESTION</b>\n{UI.THIN}\n"
                      "<b>Send a question ID (like 125) or any word from the question.</b>\n"
                      "You'll get Edit / Delete buttons for each match.\n<i>/cancel to stop</i>")
        elif data in ("ap:addq", "ap:addquiz", "ap:import"):     # old buttons land here too
            await ask("addq", ADDQ_HELP.format(line=UI.THIN),
                      [[btn("Done", "check", GREEN, callback_data="ap:home")]])
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
            ud.pop("ap_await", None)
            await self._edit(q.message, *(await self._admins_view(user.id)))
        elif data == "ap:adm_add":
            if not self._can_manage_admins(user.id):
                await q.answer("⛔ Only the owner can add admins.", show_alert=True); return
            ud["adm_draft"] = {}
            await ask("adm_id",
                      f"{e('plus')} <b>ADD ADMIN (1-Step or Guided)</b>\n{UI.THIN}\n"
                      "<b>Option A — 1-Step Quick Add (one line):</b>\n"
                      "<code>&lt;Custom Name&gt; &lt;Emoji&gt; &lt;User_ID&gt;</code>\n"
                      "• Example: <code>Quiz Master 👑 1234567890</code>\n\n"
                      "<b>Option B — Step-by-Step:</b>\n"
                      "• Send just their <b>User ID</b> or <b>forward a message</b> from them.\n\n"
                      "<i>Send /cancel to abort.</i>")
        elif data == "ap:adm_skip":
            if not self._can_manage_admins(user.id):
                await q.answer("⛔", show_alert=True); return
            await q.answer()
            step = ud.get("ap_await")
            if step == "adm_title":
                await self._admin_step_emoji(q.message, ud, edit=True)
            elif step == "adm_emoji":
                ud.pop("ap_await", None)
                await self._admin_finish(update, context, ud, q.message)
        elif data == "ap:adm_rm":
            if not self._can_manage_admins(user.id):
                await q.answer("⛔ Only the owner can remove admins.", show_alert=True); return
            await q.answer()
            devs = await asyncio.to_thread(self.db.get_all_developers) if self.db else []
            rows = [[btn(f"Remove {(d.get('title') or d.get('name') or d.get('user_id'))}"[:40], "trash", RED,
                         callback_data=f"ap:adm_rmc:{d.get('user_id')}")] for d in devs if d.get("user_id")]
            await self._edit(q.message, f"{e('trash')} <b>REMOVE ADMIN</b>\n<b>Tap the admin to remove.</b>"
                             if rows else f"{e('users')} <b>No admins to remove.</b>",
                             InlineKeyboardMarkup(rows + [[btn("Back", "back", RED, callback_data="ap:team")]]))
        elif data.startswith(("ap:adm_rmc:", "ap:adm_rmy:")):
            if not self._can_manage_admins(user.id):
                await q.answer("⛔ Only the owner can remove admins.", show_alert=True); return
            try:
                target = int(data.rsplit(":", 1)[1])
            except ValueError:
                await q.answer(); return
            await q.answer()
            if data.startswith("ap:adm_rmc:"):
                await self._edit(q.message, f"{e('warn')} <b>Remove admin <code>{target}</code>?</b>",
                                 InlineKeyboardMarkup([[btn("Yes, remove", "trash", RED, callback_data=f"ap:adm_rmy:{target}")],
                                                       [btn("Back", "back", RED, callback_data="ap:adm_rm")]]))
            else:
                removed = await asyncio.to_thread(self.db.remove_developer, target) if self.db else False
                if removed:
                    await self._notify_admin(context, target,
                        f"{e('warn')} <b>You are no longer an admin of Miss Quiz Bot.</b>")
                text, kb = await self._admins_view(user.id)
                await self._edit(q.message, (f"{e('check')} <b>Removed.</b>\n\n" if removed
                                             else f"{e('cross')} <b>Not found.</b>\n\n") + text, kb)

        # broadcast confirm / undo
        elif data == "ap:bc_yes" and dev:
            await q.answer("Sending…")
            await self._edit(q.message, f"{e('signal')} <b>Sending broadcast…</b>")
            await dev.broadcast_confirm(self._proxy(update, "/broadcast_confirm"), context)
        elif data == "ap:bc_no":
            for k in [k for k in ud if str(k).startswith("broadcast_")]:
                ud.pop(k, None)
            await q.answer("Cancelled")
            await self._edit(q.message, f"{e('cross')} <b>Broadcast cancelled.</b>", self._back_kb())
        elif data == "ap:dbc" and dev:
            await q.answer()
            await self._edit(q.message, f"{e('warn')} <b>Delete the last broadcast from every chat?</b>",
                             InlineKeyboardMarkup([
                                 [btn("Yes, delete it", "trash", RED, callback_data="ap:dbc_yes")],
                                 [btn("Back", "back", RED, callback_data="ap:home")]]))
        elif data == "ap:dbc_yes" and dev:
            await q.answer("Deleting…")
            await dev.delbroadcast(self._proxy(update, "/delbroadcast"), context)          # finds the last one
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
                if hasattr(self, "cmd_start"):
                    await self.cmd_start(update, context, edit_msg=q.message)
                else:
                    await q.message.delete()
            except Exception:
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
        elif key == "addq":
            await self._addq_input(update, context, msg)      # stays in this mode until Done/Back
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
                                      [btn("Cancel", "cross", RED, callback_data="ap:bc_no"),
                                       btn("Send now", "check", GREEN, callback_data="ap:bc_yes")]]))
        elif key == "find":
            ud.pop("ap_await", None)
            f_text, f_kb = self._find_results((msg.text or "").strip(), user.id)
            await self._reply(update, f_text, reply_markup=f_kb)
        elif key.startswith("adm_"):
            if not self._can_manage_admins(user.id):
                ud.pop("ap_await", None)
            else:
                await self._admin_input_step(update, context, ud, key, msg)
        raise ApplicationHandlerStop

    # ── admins: add with custom title + premium emoji, notify them ───────

    @staticmethod
    def _can_manage_admins(uid: int) -> bool:
        from src.core.config import AUTHORIZED_USERS   # owner (+ WIFU)
        return uid in AUTHORIZED_USERS

    @staticmethod
    def _admin_badge(d: dict) -> str:
        """Admin's emoji (premium if saved) + bold title, HTML-safe."""
        import html as _h
        from src.bot.premium import premium_on
        emo = d.get("emoji") or ""
        if emo and d.get("emoji_id") and premium_on():
            emo = f'<tg-emoji emoji-id="{d["emoji_id"]}">{_h.escape(emo)}</tg-emoji>'
        else:
            emo = _h.escape(emo)
        title = _h.escape(d.get("title") or "")
        return " ".join(x for x in (emo, f"<b>{title}</b>" if title else "") if x)

    async def _admins_view(self, viewer: int):
        import html as _h
        devs = []
        if self.db:
            try:
                devs = await asyncio.to_thread(self.db.get_all_developers)
            except Exception as ex:
                logger.warning(f"[ADMIN PANEL] admins list failed: {ex}")
        lines = [f"{e('users')} <b>ADMINS</b> · {len(devs)}\n{UI.THIN}"]
        for d in devs:
            name = d.get("name") or d.get("username") or str(d.get("user_id"))
            badge = self._admin_badge(d)
            lines.append(f"{badge + ' — ' if badge else '• '}{UI.mention(d.get('user_id'), _h.unescape(str(name)))}"
                         f"  <code>{d.get('user_id')}</code>")
        if not devs:
            lines.append("<b>No admins yet.</b>")
        rows = []
        if self._can_manage_admins(viewer):
            rows.append([btn("Remove Admin", "trash", RED, callback_data="ap:adm_rm"),
                         btn("Add Admin", "plus", GREEN, callback_data="ap:adm_add")])
        rows.append([btn("Back", "back", RED, callback_data="ap:home")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def _admin_step_title(self, target_msg, ud):
        ud["ap_await"] = "adm_title"
        text = (f"{e('edit')} <b>ADD ADMIN · step 2/3</b>\n{UI.THIN}\n"
                "<b>Send a custom title for them</b> (e.g. <code>Quiz Master</code>), or tap Skip.")
        kb = InlineKeyboardMarkup([[btn("Skip", "play", BLUE, callback_data="ap:adm_skip")],
                                   [btn("Back", "back", RED, callback_data="ap:team")]])
        await target_msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

    async def _admin_step_emoji(self, target_msg, ud, edit=False):
        ud["ap_await"] = "adm_emoji"
        text = (f"{e('star')} <b>ADD ADMIN · step 3/3</b>\n{UI.THIN}\n"
                "<b>Send ONE emoji for them</b> — premium emoji work too.")
        kb = InlineKeyboardMarkup([[btn("Skip", "play", BLUE, callback_data="ap:adm_skip")],
                                   [btn("Back", "back", RED, callback_data="ap:team")]])
        if edit:
            await self._edit(target_msg, text, kb)
        else:
            await target_msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

    async def _admin_input_step(self, update, context, ud, key, msg):
        draft = ud.setdefault("adm_draft", {})
        origin = getattr(msg, "forward_origin", None)
        sender = getattr(origin, "sender_user", None) if origin else None
        raw_text = (msg.text or msg.caption or "").strip()

        # Step 2 of 3: custom title
        if key == "adm_title":
            draft["title"] = raw_text[:32]
            await self._admin_step_emoji(msg, ud, edit=False)
            return

        # Step 3 of 3: emoji (standard or Telegram custom emoji)
        if key == "adm_emoji":
            emo = raw_text.split()[0][:8] if raw_text else ""
            emo_id = ""
            for ent in list(msg.entities or []) + list(msg.caption_entities or []):
                if ent.type == "custom_emoji":
                    emo_id = ent.custom_emoji_id
                    break
            draft["emoji"] = emo
            draft["emoji_id"] = emo_id
            ud.pop("ap_await", None)
            await self._admin_finish(update, context, ud, None)
            return

        # Step 1 (adm_id / adm_quick): supports BOTH 1-step ("Quiz Master 👑 1234567890")
        # and step-by-step (just "4455" or a forwarded message)!
        tokens = raw_text.split()
        uid = sender.id if sender else None
        name = sender.full_name if sender else None
        emoji = ""
        emoji_id = ""
        custom_emo_str = ""

        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        for ent in entities:
            if ent.type == "custom_emoji":
                emoji_id = ent.custom_emoji_id
                try:
                    custom_emo_str = msg.parse_entity(ent) if msg.text else msg.parse_caption_entity(ent)
                except Exception:
                    custom_emo_str = ""
                if custom_emo_str:
                    emoji = custom_emo_str
                break

        # If forwarded message or single bare number -> guided step-by-step flow
        if sender or (len(tokens) == 1 and tokens[0].isdigit()):
            if uid is None:
                uid = int(tokens[0])
            if self._can_manage_admins(uid):
                await self._reply(update, f"{e('warn')} <b>That user is already the owner.</b>")
                ud.pop("ap_await", None)
                return
            if not name:
                try:
                    chat = await context.bot.get_chat(uid)
                    name = chat.full_name or chat.first_name
                except Exception:
                    name = f"User {uid}"
            draft.update({"user_id": uid, "name": name, "title": "", "emoji": "", "emoji_id": ""})
            await self._admin_step_title(msg, ud)
            return

        # 1-Step Quick Add: "<Custom Name> <Emoji> <User_ID>" in any order
        title_tokens = []
        for tok in tokens:
            if tok.isdigit():
                if len(tok) >= 13 and not emoji_id:
                    emoji_id = tok
                    continue
                elif uid is None:
                    uid = int(tok)
                    continue
            if custom_emo_str and tok == custom_emo_str:
                continue
            if len(tok) <= 4 and not any(ch.isalnum() for ch in tok):
                if not emoji or not custom_emo_str:
                    emoji = tok
                continue
            title_tokens.append(tok)

        if not uid:
            await self._reply(
                update,
                f"{e('warn')} <b>Could not find a valid User ID!</b>\n"
                "Send either:\n"
                "• 1-Step: <code>&lt;Custom Name&gt; &lt;Emoji&gt; &lt;User_ID&gt;</code>\n"
                "• Or just the <code>User_ID</code> / a forwarded message."
            )
            return

        if self._can_manage_admins(uid):
            await self._reply(update, f"{e('warn')} <b>That user is already the owner.</b>")
            ud.pop("ap_await", None)
            return

        title = " ".join(title_tokens)[:32] if title_tokens else "Admin"
        if not emoji:
            emoji = "⭐️"
        if not name:
            try:
                chat = await context.bot.get_chat(uid)
                name = chat.full_name or chat.first_name
            except Exception:
                name = title or f"User {uid}"

        draft.update({
            "user_id": uid,
            "name": name,
            "title": title,
            "emoji": emoji,
            "emoji_id": emoji_id,
        })
        ud.pop("ap_await", None)
        await self._admin_finish(update, context, ud, None)

    async def _admin_finish(self, update, context, ud, edit_msg):
        draft = ud.pop("adm_draft", {}) or {}
        uid = draft.get("user_id")
        if not uid or not self.db:
            await self._reply(update, f"{e('cross')} <b>Nothing to save — start again.</b>")
            return
        await asyncio.to_thread(self.db.add_developer, uid, "", draft.get("name", ""),
                                added_by=update.effective_user.id)
        await asyncio.to_thread(self.db.set_admin_profile, uid, draft.get("title", ""),
                                draft.get("emoji", ""), draft.get("emoji_id", ""))
        badge = self._admin_badge(draft)
        dm_ok = await self._notify_admin(context, uid,
            f"{e('party')} <b>You've been promoted to Admin of Miss Quiz Bot!</b>\n\n"
            + (f"<blockquote><b>Your title: {badge}</b></blockquote>\n" if badge else "")
            + "<b>Open /start → Admin Panel to manage the bot.</b>")
        text, kb = await self._admins_view(update.effective_user.id)
        head = (f"{e('check')} <b>{UI.mention(uid, draft.get('name', str(uid)))} is now an admin"
                f"{' — ' + badge if badge else ''}.</b>\n"
                + ("<b>They got a message from the bot.</b>" if dm_ok else
                   f"{e('warn')} <b>Couldn't message them — they must open the bot and press Start once.</b>")
                + "\n\n")
        if edit_msg is not None:
            await self._edit(edit_msg, head + text, kb)
        else:
            await self._reply(update, head + text, reply_markup=kb)

    async def _notify_admin(self, context, uid: int, text: str) -> bool:
        try:
            await context.bot.send_message(uid, text, parse_mode="HTML")
            return True
        except Exception as ex:
            logger.info(f"[ADMIN PANEL] could not DM {uid}: {ex}")
            return False

    # ── emoji test (see every premium emoji the bot can send) ────────────

    EMOJI_PER_PAGE = 40   # stay well under Telegram's custom-emoji-per-message limit

    def _emoji_test_page(self, page: int):
        from src.bot.premium import EXACT, EMOJI_ID, premium_on
        lookalike = sorted(k for k in EMOJI_ID if k not in EXACT)
        exact = sorted(EXACT)
        items = [("≈", k) for k in lookalike] + [("", k) for k in exact]
        pages = max(1, (len(items) + self.EMOJI_PER_PAGE - 1) // self.EMOJI_PER_PAGE)
        page = max(0, min(page, pages - 1))
        start = page * self.EMOJI_PER_PAGE
        chunk = items[start:start + self.EMOJI_PER_PAGE]
        rows = []
        for i in range(0, len(chunk), 5):
            rows.append("   ".join(f"{start + i + j + 1}.{mark}{k}" for j, (mark, k) in enumerate(chunk[i:i + 5])))
        head = (f"🧪 <b>EMOJI TEST</b> · page {page + 1}/{pages}\n"
                f"<b>{len(EXACT)} exact · {len(lookalike)} look-alike (≈)</b>\n"
                + ("" if premium_on() else "<b>⚠️ PREMIUM_EMOJI is OFF — showing normal emoji</b>\n")
                + "<i>Wrong-looking one? Send its number to the developer.</i>\n\n")
        nav = []
        if page > 0:
            nav.append(btn("Prev", "back", BLUE, callback_data=f"ap:emoji:{page - 1}"))
        if page < pages - 1:
            nav.append(btn("Next", "play", BLUE, callback_data=f"ap:emoji:{page + 1}"))
        kb = InlineKeyboardMarkup(([nav] if nav else []) + [[btn("Back", "back", RED, callback_data="ap:home")]])
        return head + "\n".join(rows), kb

    # ── add questions: text, any file, forwarded quiz polls ──────────────

    async def _addq_input(self, update, context, msg):
        from src.bot.quiz_parser import bulk_import
        if msg.document:
            await self.handle_document(update, context)          # full import report
            return
        items, text, src = [], "", ""
        if msg.poll:
            p = msg.poll
            if p.type != "quiz" or p.correct_option_id is None:
                await self._reply(update, f"{e('warn')} <b>Only quiz polls with a visible answer can be added.</b>")
                return
            items = [{"question": p.question, "options": [o.text for o in p.options],
                      "correct_answer": p.correct_option_id, "explanation": p.explanation or ""}]
            src = "poll"
        elif msg.text:
            text, src = msg.text, "text"
        else:
            await self._reply(update, f"{e('warn')} <b>Send text, a file, or forward a quiz poll.</b>")
            return
        res = await asyncio.to_thread(bulk_import, text, self.quiz_manager, items)
        if src == "text" and not res["imported"] and not res["duplicates"] and not res["problems"]:
            # nothing recognised: maybe the old 6-line /addquiz layout
            await self.cmd_addquiz(update, context)
            return
        import html as _h
        lines = [f"{e('check')} <b>Added {res['imported']}</b>"
                 + (f" · {res['duplicates']} duplicate" if res["duplicates"] else "")
                 + (f" · {len(res['problems'])} not understood" if res["problems"] else "")
                 + f"  ·  total {self._q_count()}"]
        for d in res.get("dup_list", [])[:3]:
            lines.append(f"♻️ {_h.escape(d['where'])} — {_h.escape(d['reason'])}")
        for pr in res.get("problems", [])[:5]:
            lines.append(f"⚠️ {_h.escape(pr['where'])} — {_h.escape(pr['reason'])}")
        lines.append("<i>Send more, or tap ✅ Done.</i>")
        await self._reply(update, "\n".join(lines),
                          reply_markup=InlineKeyboardMarkup([[btn("Done", "check", GREEN, callback_data="ap:home")]]))

    # ── find question ────────────────────────────────────────────────────

    async def handle_forwarded_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin forwards any quiz poll to the bot's DM → Edit / Delete buttons
        for that exact question. A forwarded poll keeps its poll id, so the
        saved poll→question mapping identifies it; text match is the fallback."""
        user, msg = update.effective_user, update.effective_message
        if not user or not msg or not msg.poll or not await self._is_authorized(user.id):
            return
        if (getattr(context, "user_data", None) or {}).get("ap_await") == "addq":
            return          # adding questions: handle_admin_input imports the poll instead
        poll = msg.poll
        qid = (context.bot_data.get(f"poll_{poll.id}", {}) or {}).get("question_id")
        if qid is None and self.db:
            try:
                qid = await asyncio.to_thread(self.db.get_quiz_id_from_poll, str(poll.id))
            except Exception:
                qid = None
        if qid is not None:
            text, kb = self._find_results(str(qid), user.id)
        else:
            # strip prefixes the bot adds ('📚 ', '⚔️ 1/5  ') and match the text
            ptxt = re.sub(r"^\W*(?:\d+/\d+)?\W*", "", poll.question or "").strip()
            text, kb = self._find_results(ptxt, user.id)
        await self._reply(update, text, reply_markup=kb)
        raise ApplicationHandlerStop

    def _find_results(self, query: str, user_id: int, limit: int = 8):
        """Questions matching an ID or words → (text, keyboard with Edit/Delete per hit)."""
        import html as _h
        qs = list(getattr(self.quiz_manager, "questions", []) or [])
        q = re.sub(r"^(?:q#|#|q)\s*", "", query.strip(), flags=re.IGNORECASE).strip()
        if q.isdigit():
            hits = [x for x in qs if x.get("id") == int(q)]
        else:
            words = [w for w in q.lower().split() if w]

            def blob(x):
                return ((x.get("question") or "") + " " + " ".join(map(str, x.get("options") or []))).lower()
            hits = [x for x in qs if words and all(w in blob(x) for w in words)]
            if not hits and words:
                # typo-tolerant fallback: closest question texts
                import difflib
                texts = {(x.get("question") or "").lower(): x for x in qs}
                close = difflib.get_close_matches(q.lower(), list(texts), n=limit, cutoff=0.55)
                hits = [texts[c] for c in close]
        again = [btn("Search again", "bolt", BLUE, callback_data="ap:find")]
        back = [btn("Back", "back", RED, callback_data="ap:home")]
        if not hits:
            return (f"🔎 <b>No question matches “{_h.escape(query[:40])}”.</b>",
                    InlineKeyboardMarkup([again, back]))
        lines, rows = [f"🔎 <b>{len(hits)} match{'es' if len(hits) != 1 else ''}"
                       f"{f' (showing {limit})' if len(hits) > limit else ''}</b>"], []
        for x in hits[:limit]:
            qid = x.get("id")
            lines.append(f"<b>#{qid}</b> {_h.escape((x.get('question') or '')[:70])}")
            rows.append([btn(f"Delete #{qid}", "trash", RED, callback_data=f"dq_ask_{qid}_{user_id}"),
                         btn(f"Edit #{qid}", "edit", BLUE, callback_data=f"edit_quiz_select_{qid}")])
        return "\n".join(lines), InlineKeyboardMarkup(rows + [again, back])

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
