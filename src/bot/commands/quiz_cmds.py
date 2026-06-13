"""
QuizCommandsMixin — /quiz /score /stats /achievements /botstats
"""

import asyncio
import logging
import time
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from src.bot.ui import UI, COMMUNITY, _NO_PREVIEW, get_thread_id, get_tracking_id

logger = logging.getLogger(__name__)


class QuizCommandsMixin(object):

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

        text = (
            f"🏆  <b>𝐒𝐂𝐎𝐑𝐄𝐂𝐀𝐑𝐃</b>\n"
            f"{UI.LINE}\n\n"
            f"👤  {mention}\n\n"
            f"{UI.LINE}\n\n"
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
            [InlineKeyboardButton("🎓 Achievements",   callback_data="achievements")],
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
             InlineKeyboardButton("🎓 My Profile",    callback_data="my_profile")],
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
             _tab("🎯 Quiz",     "quiz")],
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
