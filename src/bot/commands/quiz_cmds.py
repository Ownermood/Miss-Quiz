"""
QuizCommandsMixin — /quiz /score /stats /achievements /botstats
"""

import asyncio
import logging
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

        # ── Delete previous quiz in this chat before sending ─────
        if self.db:
            try:
                prev = await asyncio.to_thread(self.db.get_active_quiz_state, track_id)
                if prev and prev.get("message_id"):
                    try:
                        await context.bot.delete_message(
                            chat_id=chat.id, message_id=prev["message_id"])
                        logger.info(
                            f"cmd_quiz: deleted previous quiz "
                            f"msg_id={prev['message_id']} in {chat.id}"
                        )
                    except Exception as de:
                        logger.warning(f"cmd_quiz: could not delete previous quiz: {de}")
            except Exception as e:
                logger.warning(f"cmd_quiz: prev-quiz lookup failed: {e}")

        poll_kwargs = dict(
            question          = f"{cat_emoji} {question['question']}",
            options           = options,
            type              = Poll.QUIZ,
            correct_option_id = correct_idx,
            is_anonymous      = False,
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

            # Persist poll mapping (primary: MongoDB, secondary: pickle)
            if self.db and q_id:
                await asyncio.to_thread(
                    self.db.save_poll_mapping, str(poll_id), q_id, poll_entry)
            self._pickle_save(f"poll_{poll_id}", poll_entry)
            self._poll_stats["stored"] += 1

            # Track active quiz so next /quiz call can clean it up (works in DMs and groups)
            if self.db:
                try:
                    await asyncio.to_thread(
                        self.db.save_active_quiz,
                        chat_id=track_id,
                        message_id=poll_msg.message_id,
                        quiz_id=q_id,
                        poll_id=str(poll_id),
                    )
                except Exception as e:
                    logger.warning(f"cmd_quiz: save_active_quiz failed: {e}")

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
                db_doc = await asyncio.to_thread(self.db.get_user, user.id) or {}
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
                db_doc = await asyncio.to_thread(self.db.get_user, user.id) or {}
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
                earned_list = await asyncio.to_thread(
                    self.db.get_user_achievements, user.id) or []
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
                           edit_msg=None):
        if update.effective_user and not await self._is_authorized(update.effective_user.id):
            await self._unauthorized(update)
            return

        # Always show a loading state — on first call reply, on refresh edit-in-place
        if edit_msg is None:
            wait = await self._reply(update, "📊 <i>Loading analytics...</i>")
        else:
            wait = edit_msg
            try:
                await edit_msg.edit_text("📊 <i>Loading analytics...</i>",
                                         parse_mode="HTML")
            except Exception:
                pass
        await asyncio.sleep(0.3)

        q_total = self._q_count()
        ud = gd = dbs = cd = {}
        if self.db:
            try:
                ud, gd, cd, dbs = await asyncio.gather(
                    asyncio.to_thread(self.db.get_user_analytics),
                    asyncio.to_thread(self.db.get_group_analytics),
                    asyncio.to_thread(self.db.get_content_analytics),
                    asyncio.to_thread(self.db.get_db_stats),
                )
            except Exception as e:
                logger.error(f"cmd_botstats: {e}")
        d = {**ud, **gd, **cd}

        u_total    = d.get("u_total",    0)
        u_pm       = d.get("u_pm",       0)
        u_active_d = d.get("u_active_d", 0)
        u_active_w = d.get("u_active_w", 0)
        u_new_d    = d.get("u_new_d",    0)
        u_new_w    = d.get("u_new_w",    0)
        u_new_m    = d.get("u_new_m",    0)
        g_total    = d.get("g_total",    0)
        g_admin    = d.get("g_admin",    0)
        g_new_d    = d.get("g_new_d",    0)
        g_new_w    = d.get("g_new_w",    0)
        g_new_m    = d.get("g_new_m",    0)
        q_cats     = d.get("q_cats",     0)

        LINE = "━" * 38
        text = (
            f"📊  <b>𝐂𝐋𝐀𝐓 𝐕𝐈𝐒𝐈𝐎𝐍  •  𝐁𝐎𝐓 𝐀𝐍𝐀𝐋𝐘𝐓𝐈𝐂𝐒</b>\n"
            f"{LINE}\n\n"

            f"👥  <b>𝐔𝐒𝐄𝐑  𝐒𝐓𝐀𝐓𝐈𝐒𝐓𝐈𝐂𝐒</b>\n"
            f"╭──────────────────────────────────────╮\n"
            f"│  Total Users      ›  <b>{u_total:,}</b>\n"
            f"│  Reachable Users  ›  <b>{u_pm:,}</b>\n"
            f"│  Active Today     ›  <b>{u_active_d:,}</b>\n"
            f"│  Active This Week ›  <b>{u_active_w:,}</b>\n"
            f"│  New Today        ›  <b>+{u_new_d}</b>\n"
            f"│  New This Week    ›  <b>+{u_new_w}</b>\n"
            f"│  New This Month   ›  <b>+{u_new_m}</b>\n"
            f"╰──────────────────────────────────────╯\n\n"

            f"💬  <b>𝐆𝐑𝐎𝐔𝐏  𝐍𝐄𝐓𝐖𝐎𝐑𝐊</b>\n"
            f"╭──────────────────────────────────────╮\n"
            f"│  Total Groups     ›  <b>{g_total:,}</b>\n"
            f"│  Admin Groups     ›  <b>{g_admin:,}</b>\n"
            f"│  New Today        ›  <b>+{g_new_d}</b>\n"
            f"│  New This Week    ›  <b>+{g_new_w}</b>\n"
            f"│  New This Month   ›  <b>+{g_new_m}</b>\n"
            f"╰──────────────────────────────────────╯\n\n"

            f"📚  <b>𝐂𝐎𝐍𝐓𝐄𝐍𝐓  𝐋𝐈𝐁𝐑𝐀𝐑𝐘</b>\n"
            f"╭──────────────────────────────────────╮\n"
            f"│  Questions        ›  <b>{q_total:,}</b>\n"
            f"│  Categories       ›  <b>{q_cats:,}</b>\n"
            f"│  Collections      ›  <b>{dbs.get('collections', 0):,}</b>\n"
            f"│  Documents        ›  <b>{dbs.get('objects', 0):,}</b>\n"
            f"╰──────────────────────────────────────╯\n\n"

            f"{LINE}\n"
            f"⚡  Powered by {COMMUNITY}\n"
            f"{LINE}"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="bs_refresh"),
             InlineKeyboardButton("🏠 Home",    callback_data="nav_home")],
        ])

        await self._edit(wait, text, kb)
