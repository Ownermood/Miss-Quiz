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

import logging
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class AutoQuizScheduler:

    def __init__(self, bot, quiz_manager, db_manager=None, interval_minutes: int = 30):
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
                state = self.db.get_active_quiz_state(chat_id) or {}
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

    _INACTIVE_ERRORS = (
        "forbidden",
        "bot was blocked",
        "bot was kicked",
        "bot is not a member",
        "chat not found",
        "have no rights",
        "group chat was deactivated",
        "chat has been deleted",
        "forum topic is closed",
        "user is deactivated",
        "need administrator rights",
    )

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
                groups = self.db.get_groups_due_for_quiz()
            except Exception as e:
                logger.error(f"[SCHEDULER] get_groups_due_for_quiz() failed: {e}")
                return

            group_targets = [
                (g["chat_id"], g.get("message_thread_id"))
                for g in groups
                if g.get("chat_id")
            ]
        else:
            # No DB (dev/polling mode): fall back to all active chats.
            # Schedule tracking requires DB; without it every poll delivers.
            group_targets = [(cid, None) for cid in self.quiz_manager.active_chats]

        if not group_targets:
            return  # Nothing due — silent; most 1-min polls are empty

        total_q = len(self.quiz_manager.questions) if self.quiz_manager else 0
        logger.info(
            f"[SCHEDULER] {len(group_targets)} group(s) due | "
            f"question bank: {total_q}"
        )
        sent = failed = skipped = 0

        from telegram import Poll
        for chat_id, thread_id in group_targets:
            try:
                question = self.quiz_manager.get_random_question(chat_id=chat_id)
                if not question:
                    logger.warning(
                        f"[SCHEDULER] No question available for {chat_id} — skipping"
                    )
                    skipped += 1
                    # Advance schedule so we don't retry every minute
                    if self.db:
                        self.db.update_group_quiz_schedule(chat_id, self.interval)
                    continue

                await self._delete_previous_quiz(chat_id)

                options     = question.get("options", [])
                correct_idx = question.get("correct_answer", 0)
                category    = question.get("category", "General Knowledge")
                q_id        = question.get("id")
                explanation = f"✅ {options[correct_idx]}\n📚 {category}  ·  🆔 Q#{q_id}"

                send_kwargs = dict(
                    chat_id           = chat_id,
                    question          = question.get("question", "Quiz Question"),
                    options           = options,
                    type              = Poll.QUIZ,
                    correct_option_id = correct_idx,
                    explanation       = explanation[:200],
                    is_anonymous      = False,
                )
                if thread_id:
                    send_kwargs["message_thread_id"] = thread_id

                msg = await self.bot.application.bot.send_poll(**send_kwargs)
                poll_id_str = str(msg.poll.id)

                # Advance this group's schedule immediately after delivery
                if self.db:
                    self.db.update_group_quiz_schedule(chat_id, self.interval)

                self._save_active_quiz(
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
                }
                if self.db and q_id:
                    try:
                        self.db.save_poll_mapping(poll_id_str, q_id, poll_data=poll_entry)
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

            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in self._INACTIVE_ERRORS):
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
                            self.db.update_group_quiz_schedule(chat_id, self.interval)
                        except Exception:
                            pass
                failed += 1

        logger.info(
            f"[SCHEDULER] ◀ Done — "
            f"✅ sent={sent}  ❌ failed={failed}  ⏭ skipped={skipped}"
        )
