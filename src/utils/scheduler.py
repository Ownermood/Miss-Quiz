"""
Auto Quiz Scheduler — delivers a new quiz to every registered group every 30 minutes.
No quiz timer: polls stay open until replaced by the next delivery cycle.
Deletes the previous quiz before posting the new one.
Persists full state in MongoDB so delivery resumes correctly after restarts.
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
                logger.info(f"[SCHEDULER] Restored state for {len(docs)} groups on startup")
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
        self.scheduler.add_job(
            self._send_auto_quiz,
            trigger="interval",
            minutes=self.interval,
            id="auto_quiz",
            replace_existing=True,
            next_run_time=datetime.now(),   # fire immediately on start; then every N min
        )
        self.scheduler.start()
        job = self.scheduler.get_job("auto_quiz")
        next_run = job.next_run_time if job else "unknown"
        logger.info(
            f"[SCHEDULER] Started — interval: {self.interval} min | "
            f"first run: immediate | next after that: {next_run}"
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
            # /quiz command may have sent the last quiz — check DB
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
                f"[SCHEDULER] Cleaned up previous quiz "
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

    # ── Delivery ──────────────────────────────────────────────────────────────

    # Keywords that mean the bot is no longer able to post in the group.
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
        """Deliver one quiz to every active group. Called by APScheduler."""
        logger.info("[SCHEDULER] ▶ Delivery cycle started")
        try:
            await self._do_send_auto_quiz()
        except Exception as e:
            # Top-level guard: APScheduler silently disables jobs that raise;
            # catching here keeps the job alive and surfaces the error.
            logger.error(f"[SCHEDULER] Unhandled error in delivery cycle: {e}", exc_info=True)

    async def _do_send_auto_quiz(self):
        """Inner implementation — separated so the outer guard stays clean."""
        if self.db:
            try:
                groups = self.db.get_all_groups()
            except Exception as e:
                logger.error(f"[SCHEDULER] get_all_groups() failed: {e}")
                return

            group_targets = [
                (g["chat_id"], g.get("message_thread_id"))
                for g in groups
                if g.get("chat_id")
                and g.get("active_status") != "inactive"
            ]
        else:
            group_targets = [(cid, None) for cid in self.quiz_manager.active_chats]

        if not group_targets:
            logger.info("[SCHEDULER] No active groups — skipping delivery cycle")
            return

        total_q = len(self.quiz_manager.questions) if self.quiz_manager else 0
        logger.info(
            f"[SCHEDULER] Delivering to {len(group_targets)} group(s) | "
            f"question bank: {total_q} questions"
        )
        sent = failed = skipped = 0

        from telegram import Poll
        for chat_id, thread_id in group_targets:
            try:
                question = self.quiz_manager.get_random_question(chat_id=chat_id)
                if not question:
                    logger.warning(f"[SCHEDULER] No question available for {chat_id} — skipping")
                    skipped += 1
                    continue

                # Delete previous quiz before posting the new one
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
                        logger.warning(f"[SCHEDULER] save_poll_mapping failed for {chat_id}: {e}")
                try:
                    self.bot.application.bot_data[f"poll_{poll_id_str}"] = poll_entry
                except Exception:
                    pass

                logger.info(
                    f"[SCHEDULER] ✅ Sent to {chat_id} — "
                    f"msg_id={msg.message_id}  Q#{q_id}  cat={category}"
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
                else:
                    logger.error(f"[SCHEDULER] ❌ Failed to deliver to {chat_id}: {e}")
                failed += 1

        logger.info(
            f"[SCHEDULER] ◀ Cycle complete — "
            f"✅ sent={sent}  ❌ failed={failed}  ⏭ skipped={skipped}"
        )
