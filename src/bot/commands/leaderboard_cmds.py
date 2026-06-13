"""
LeaderboardMixin — /leaderboard + pagination helpers
"""

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.bot.ui import UI

logger = logging.getLogger(__name__)


class LeaderboardMixin(object):

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
