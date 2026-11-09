"""
Auto Quiz Scheduler — per-group timestamp-driven delivery.

Architecture
------------
APScheduler fires a lightweight poll every 1 minute.
Each poll issues a single indexed query:

    groups WHERE next_quiz_due_at <= NOW() AND active_status != inactive

Only the groups whose delivery window has arrived receive a quiz.
After a successful delivery (or an unrecoverable skip), the group's
next_quiz_due_at is advanced by `interval` minutes.  This means:

  • Groups added at different times get quizzes at different times.
  • No global delivery wave — each group follows its own clock.
  • Restart-safe: all schedule state lives in MongoDB, never in memory.
  • Scales to thousands of groups without hot-spots.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.error import RetryAfter, NetworkError, BadRequest

logger = logging.getLogger(__name__)


class AutoQuizScheduler:

    def __init__(self, bot, quiz_manager, db_manager=None, interval_minutes: int = 60):
        self.bot          = bot
        self.quiz_manager = quiz_manager
        self.db           = db_manager
        self.interval     = interval_minutes
        self.scheduler    = AsyncIOScheduler()
        # In-memory cache: chat_id -> {message_id, quiz_id, poll_id, sent_time}
        self._active_quiz: dict = {}
        self._load_persisted_state()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_persisted_state(self):
        """Load per-group active quiz state from MongoDB on startup."""
        if not self.db:
            return
        try:
            docs = self.db.get_all_active_quiz_states()
            for doc in docs:
                chat_id = doc.get("chat_id")
                if chat_id:
                    self._active_quiz[chat_id] = {
                        "message_id": doc.get("message_id"),
                        "quiz_id":    doc.get("quiz_id"),
                        "poll_id":    doc.get("poll_id"),
                        "sent_time":  doc.get("sent_time"),
                    }
            if docs:
                logger.info(f"[SCHEDULER] Restored active-quiz state for {len(docs)} group(s)")
        except Exception as e:
            logger.warning(f"[SCHEDULER] Could not load persisted state: {e}")

    def _save_active_quiz(self, chat_id: int, message_id: int,
                          quiz_id=None, poll_id: str = None):
        state = {
            "message_id": message_id,
            "quiz_id":    quiz_id,
            "poll_id":    poll_id,
            "sent_time":  datetime.utcnow().isoformat(),
        }
        self._active_quiz[chat_id] = state
        if self.db:
            self.db.save_active_quiz(
                chat_id=chat_id,
                message_id=message_id,
                quiz_id=quiz_id,
                poll_id=poll_id,
            )

    def _clear_active_quiz(self, chat_id: int):
        self._active_quiz.pop(chat_id, None)
        if self.db:
            self.db.clear_active_quiz(chat_id)

    # ── Scheduler lifecycle ───────────────────────────────────────────────────

    def start(self):
        # Backfill any existing groups that predate per-group scheduling.
        # Sets next_quiz_due_at = NOW() for them so they are served in the
        # first poll; going forward each group keeps its own schedule.
        if self.db:
            try:
                n = self.db.backfill_group_schedules(self.interval)
                if n:
                    logger.info(
                        f"[SCHEDULER] Backfilled {n} legacy group(s) "
                        f"— they will receive a quiz in the first poll"
                    )
            except Exception as e:
                logger.warning(f"[SCHEDULER] backfill_group_schedules failed: {e}")

        # Poll every 1 minute; delivery is governed by next_quiz_due_at per group
        self.scheduler.add_job(
            self._send_auto_quiz,
            trigger="interval",
            minutes=1,
            id="auto_quiz",
            replace_existing=True,
            next_run_time=datetime.now(),   # first poll fires immediately
        )
        # Daily housekeeping: old answer logs, poll mappings, in-memory polls
        self.scheduler.add_job(
            self._housekeeping,
            trigger="interval",
            hours=24,
            id="housekeeping",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=10),
        )

        # "Make me admin" alerts for groups where auto quiz is paused
        self.scheduler.add_job(
            self._send_admin_reminders,
            trigger="interval",
            minutes=self.ADMIN_ALERT_EVERY_MIN,
            id="admin_reminders",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=2),  # after first admin checks
        )
        self.scheduler.start()
        logger.info(
            f"[SCHEDULER] Started — poll: 1 min | "
            f"quiz interval per group: {self.interval} min"
        )

    def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("[SCHEDULER] Stopped")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def _delete_previous_quiz(self, chat_id: int):
        """Delete the previously sent quiz poll for a group. Non-fatal.

        Checks in-memory cache first; falls back to DB so quizzes sent
        via /quiz command (which updates only DB) are also cleaned up.
        """
        state = self._active_quiz.get(chat_id)
        if not state and self.db:
            try:
                state = await asyncio.to_thread(self.db.get_active_quiz_state, chat_id) or {}
            except Exception:
                state = {}

        msg_id = state.get("message_id") if state else None
        if not msg_id:
            return
        try:
            await self.bot.application.bot.delete_message(
                chat_id=chat_id, message_id=msg_id)
            logger.info(
                f"[SCHEDULER] Deleted previous quiz "
                f"msg_id={msg_id} in chat {chat_id}"
            )
        except Exception as e:
            logger.warning(
                f"[SCHEDULER] Could not delete previous quiz "
                f"msg_id={msg_id} in chat {chat_id}: {e}"
            )

    async def _mark_group_inactive(self, chat_id: int):
        """Flag a group as inactive when the bot is blocked or removed."""
        if self.db:
            try:
                self.db.groups_col.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"active_status": "inactive", "bot_blocked": True,
                              "bot_blocked_at": datetime.utcnow().isoformat()}}
                )
                logger.info(f"[SCHEDULER] Marked group {chat_id} as inactive")
            except Exception as e:
                logger.warning(
                    f"[SCHEDULER] Could not update inactive status "
                    f"for {chat_id}: {e}"
                )
        # Clear from the bot's in-memory seen-groups cache so that if the bot
        # is re-added to this group, ensure_group_registered() will run the
        # upsert and restore active_status="active" instead of skipping.
        try:
            self.bot._seen_groups.discard(chat_id)
        except Exception:
            pass

    # ── Delivery ──────────────────────────────────────────────────────────────

    TRANSIENT_RETRY_MIN   = 2    # minutes to wait after a network error/timeout
    ADMIN_ALERT_EVERY_MIN = 60   # how often to look for groups needing an alert
    ADMIN_ALERT_GAP_H     = 24   # one alert per day per group
    ADMIN_ALERT_MAX       = None # no cap: daily until promoted, kicked or inactive

    _INACTIVE_ERRORS = (
        "forbidden",
        "bot was blocked",
        "bot was kicked",
        "bot is not a member",
        "chat not found",
        "have no rights",
        "group chat was deactivated",
        "chat has been deleted",
        "user is deactivated",
        "need administrator rights",
    )

    _TOPIC_GONE = ("thread not found", "topic_closed", "topic closed", "topic_deleted",
                   "topic deleted", "forum topic is closed", "topic_id_invalid")

    async def _send_in_topic(self, send, chat: int, thread_id, **kw):
        """Topic groups: post in the group's open topic (the last one people
        wrote in). If that topic was closed or deleted, post in the main
        (General) topic instead and forget the dead topic.
        Returns (message, thread_id actually used)."""
        if thread_id:
            try:
                return await send(**kw, message_thread_id=thread_id), thread_id
            except BadRequest as e:
                if not any(w in str(e).lower() for w in self._TOPIC_GONE):
                    raise
                logger.info(f"[SCHEDULER] topic {thread_id} in {chat} is closed/gone ({e}) — using main topic")
                if self.db:
                    try:
                        self.db.clear_group_thread(chat, thread_id)
                    except Exception as ex:
                        logger.warning(f"[SCHEDULER] clear_group_thread failed for {chat}: {ex}")
        return await send(**kw), None

    async def _bot_is_admin(self, chat_id: int, known) -> bool:
        """Auto quiz only runs where the bot is an admin.

        known=True/False comes from groups.bot_is_admin (kept up to date by
        my_chat_member). If unknown (older groups), ask Telegram once and store
        it; a stored False removes the group from get_groups_due_for_quiz()
        until the bot is promoted. On API errors, return True so the normal
        send path and its error handling (inactive marking, retries) apply."""
        if known is True:
            return True
        if known is False:
            return False
        tg = self.bot.application.bot
        try:
            member = await tg.get_chat_member(chat_id, tg.id)
        except Exception as e:
            logger.warning(f"[SCHEDULER] admin check failed for {chat_id}: {e}")
            return True
        is_admin = member.status in ("administrator", "creator")
        if self.db:
            try:
                self.db.update_group_admin_status(chat_id, is_admin)
            except Exception as e:
                logger.warning(f"[SCHEDULER] could not store admin status for {chat_id}: {e}")
        return is_admin

    POLL_KEEP_DAYS = 7
    ACTIVITY_KEEP_DAYS = 30

    async def _housekeeping(self):
        """Keep storage and RAM flat: purge old DB rows, forget old polls."""
        try:
            if self.db:
                n = await asyncio.to_thread(self.db.purge_old_data,
                                            self.ACTIVITY_KEEP_DAYS, self.POLL_KEEP_DAYS)
                logger.info(f"[HOUSEKEEPING] purged {n}")
            bd = self.bot.application.bot_data
            cutoff = time.time() - self.POLL_KEEP_DAYS * 86400
            old = [k for k, v in list(bd.items())
                   if k.startswith("poll_") and isinstance(v, dict) and v.get("ts", cutoff + 1) < cutoff]
            for k in old:
                bd.pop(k, None)
            logger.info(f"[HOUSEKEEPING] dropped {len(old)} old polls from memory")
        except Exception as e:
            logger.error(f"[HOUSEKEEPING] failed: {e}", exc_info=True)

    async def _send_admin_reminders(self):
        """Ask non-admin groups to promote the bot: one alert every
        ADMIN_ALERT_GAP_H hours (ADMIN_ALERT_MAX caps it if set); reset when promoted."""
        if not self.db:
            return
        try:
            groups = self.db.get_groups_needing_admin_reminder(
                self.ADMIN_ALERT_GAP_H, self.ADMIN_ALERT_MAX)
        except Exception as e:
            logger.error(f"[SCHEDULER] get_groups_needing_admin_reminder failed: {e}")
            return
        if not groups:
            return
        from src.bot.ui import ADMIN_ALERT_TEXT, make_admin_keyboard
        tg = self.bot.application.bot
        kb = make_admin_keyboard(getattr(tg, "username", ""))
        sent = 0
        for g in groups:
            chat_id = g.get("chat_id")
            if not chat_id:
                continue
            kw = {"chat_id": chat_id, "text": ADMIN_ALERT_TEXT, "parse_mode": "HTML", "reply_markup": kb}
            try:
                await self._send_in_topic(tg.send_message, chat_id, g.get("message_thread_id"), **kw)
                self.db.mark_admin_reminder(chat_id)
                sent += 1
            except RetryAfter as e:
                logger.warning(f"[SCHEDULER] admin alerts: flood limit, stopping this round ({e})")
                break
            except Exception as e:
                err = str(e).lower()
                if any(kw_ in err for kw_ in self._INACTIVE_ERRORS):
                    await self._mark_group_inactive(chat_id)
                else:
                    # can't post (e.g. members can't send messages): count it so we don't retry forever
                    logger.warning(f"[SCHEDULER] admin alert to {chat_id} failed: {e}")
                    try:
                        self.db.mark_admin_reminder(chat_id)
                    except Exception:
                        pass
            await asyncio.sleep(0.05)   # stay well under 30 msg/sec
        logger.info(f"[SCHEDULER] Admin alerts sent: {sent}/{len(groups)}")

    async def _send_auto_quiz(self):
        """Poll entry point — called by APScheduler every minute.
        Outer guard keeps the job alive if _do_send_auto_quiz raises."""
        try:
            await self._do_send_auto_quiz()
        except Exception as e:
            logger.error(f"[SCHEDULER] Unhandled error in delivery poll: {e}", exc_info=True)

    async def _do_send_auto_quiz(self):
        """Query groups whose time has come, send one quiz to each."""
        if self.db:
            try:
                groups = await asyncio.to_thread(self.db.get_groups_due_for_quiz)
            except Exception as e:
                logger.error(f"[SCHEDULER] get_groups_due_for_quiz() failed: {e}")
                return

            group_targets = [
                (g["chat_id"], g.get("message_thread_id"), g.get("bot_is_admin"))
                for g in groups
                if g.get("chat_id")
            ]
        else:
            # No DB (dev/polling mode): fall back to all active chats.
            # Schedule tracking requires DB; without it every poll delivers.
            group_targets = [(cid, None, None) for cid in self.quiz_manager.active_chats]

        if not group_targets:
            return  # Nothing due — silent; most 1-min polls are empty

        total_q = len(self.quiz_manager.questions) if self.quiz_manager else 0
        logger.info(
            f"[SCHEDULER] {len(group_targets)} group(s) due | "
            f"question bank: {total_q}"
        )
        sent = failed = skipped = 0

        from telegram import Poll
        for chat_id, thread_id, known_admin in group_targets:
            try:
                if not await self._bot_is_admin(chat_id, known_admin):
                    logger.info(f"[SCHEDULER] ⏸ {chat_id}: bot is not admin — auto quiz paused")
                    skipped += 1
                    continue

                question = self.quiz_manager.get_random_question(chat_id=chat_id)
                if not question:
                    logger.warning(
                        f"[SCHEDULER] No question available for {chat_id} — skipping"
                    )
                    skipped += 1
                    # Advance schedule so we don't retry every minute
                    if self.db:
                        await asyncio.to_thread(self.db.update_group_quiz_schedule, chat_id, self.interval)
                    continue

                options     = question.get("options", [])
                correct_idx = question.get("correct_answer", 0)
                category    = question.get("category", "General Knowledge")
                q_id        = question.get("id")

                # Clamp options to Telegram's 100-char limit (belt-and-suspenders;
                # _fmt_question already sanitizes but protects against stale cache).
                _clamped = []
                for _opt in options:
                    _s = str(_opt)
                    if len(_s) > 100:
                        logger.warning(
                            f"[SCHEDULER] Q#{q_id} option truncated at delivery "
                            f"({len(_s)} chars) for chat {chat_id}"
                        )
                        _s = _s[:97] + "…"
                    _clamped.append(_s)
                options = _clamped

                own = (question.get("explanation") or "").strip()
                tail = f"\n🆔 Q#{q_id}"   # always keep the ID: admins find/fix questions by it
                head = (f"✅ {options[correct_idx]}\n💡 {own}" if own
                        else f"✅ {options[correct_idx]}\n📚 {category}  ·")
                explanation = head[:200 - len(tail)] + tail

                send_kwargs = dict(
                    chat_id           = chat_id,
                    question          = question.get("question", "Quiz Question"),
                    options           = options,
                    type              = Poll.QUIZ,
                    correct_option_id = correct_idx,
                    explanation       = explanation[:200],
                    is_anonymous      = False,
                )
                msg, thread_id = await self._send_in_topic(
                    self.bot.application.bot.send_poll, chat_id, thread_id, **send_kwargs)
                poll_id_str = str(msg.poll.id)

                # Remove the previous quiz only AFTER the new one is out, so a
                # failed send never leaves the group empty until the next slot.
                # Must run before _save_active_quiz() overwrites the old state.
                await self._delete_previous_quiz(chat_id)

                # Advance this group's schedule immediately after delivery
                if self.db:
                    await asyncio.to_thread(self.db.update_group_quiz_schedule, chat_id, self.interval)

                await asyncio.to_thread(
                    self._save_active_quiz,
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    quiz_id=q_id,
                    poll_id=poll_id_str,
                )

                poll_entry = {
                    "chat_id":           chat_id,
                    "correct_option_id": correct_idx,
                    "category":          category,
                    "question_id":       q_id,
                    "thread_id":         thread_id,
                    "tracking_id":       chat_id,
                    "ts":                time.time(),
                }
                if self.db and q_id:
                    try:
                        await asyncio.to_thread(self.db.save_poll_mapping, poll_id_str, q_id, poll_data=poll_entry)
                    except Exception as e:
                        logger.warning(
                            f"[SCHEDULER] save_poll_mapping failed for {chat_id}: {e}"
                        )
                try:
                    self.bot.application.bot_data[f"poll_{poll_id_str}"] = poll_entry
                except Exception:
                    pass

                logger.info(
                    f"[SCHEDULER] ✅ {chat_id} — "
                    f"msg={msg.message_id}  Q#{q_id}  cat={category}  "
                    f"next_in={self.interval}min"
                )
                sent += 1

            except RetryAfter as e:
                # Flood control: retry as soon as Telegram allows, not in 1 hour
                wait_s = getattr(e, "retry_after", 60)
                wait_s = wait_s.total_seconds() if hasattr(wait_s, "total_seconds") else float(wait_s)
                logger.warning(f"[SCHEDULER] ⏳ Flood limit for {chat_id} — retry in {wait_s:.0f}s")
                if self.db:
                    try:
                        await asyncio.to_thread(self.db.update_group_quiz_schedule, chat_id, max(1.0, wait_s / 60 + 0.5))
                    except Exception:
                        pass
                failed += 1

            except Exception as e:
                err = str(e).lower()
                # BadRequest subclasses NetworkError in PTB, so exclude it:
                # "chat not found" etc. must still mark the group inactive.
                if isinstance(e, NetworkError) and not isinstance(e, BadRequest):
                    # Transient network problem: short retry, not a full interval
                    logger.warning(f"[SCHEDULER] 🌐 Network error for {chat_id}: {e} — "
                                   f"retry in {self.TRANSIENT_RETRY_MIN} min")
                    if self.db:
                        try:
                            await asyncio.to_thread(self.db.update_group_quiz_schedule, chat_id, self.TRANSIENT_RETRY_MIN)
                        except Exception:
                            pass
                elif any(kw in err for kw in self._INACTIVE_ERRORS):
                    logger.warning(
                        f"[SCHEDULER] ⛔ Bot removed/blocked from {chat_id} "
                        f"— marking inactive. Reason: {e}"
                    )
                    await self._mark_group_inactive(chat_id)
                    self._clear_active_quiz(chat_id)
                    # Do NOT advance schedule — group is now inactive
                else:
                    logger.error(f"[SCHEDULER] ❌ Failed for {chat_id}: {e}")
                    # Advance schedule so we don't retry every minute
                    if self.db:
                        try:
                            await asyncio.to_thread(self.db.update_group_quiz_schedule, chat_id, self.interval)
                        except Exception:
                            pass
                failed += 1

        logger.info(
            f"[SCHEDULER] ◀ Done — "
            f"✅ sent={sent}  ❌ failed={failed}  ⏭ skipped={skipped}"
        )
