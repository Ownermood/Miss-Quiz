"""
Auto Quiz Scheduler — sends a new quiz every 30 minutes,
deletes the previous one. Persists poll IDs in MongoDB for restart safety.
"""

import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class AutoQuizScheduler:

    def __init__(self, bot, quiz_manager, db_manager=None, interval_minutes: int = 30):
        self.bot          = bot
        self.quiz_manager = quiz_manager
        self.db           = db_manager
        self.interval     = interval_minutes
        self.scheduler    = AsyncIOScheduler()
        self.last_poll_ids: dict = {}  # chat_id -> message_id (in-memory cache)
        self._load_persisted_poll_ids()

    def _load_persisted_poll_ids(self):
        """Load persisted auto-quiz poll IDs from MongoDB on startup."""
        if not self.db:
            return
        try:
            docs = list(self.db.db["auto_quiz_state"].find({}, {"_id": 0}))
            for doc in docs:
                self.last_poll_ids[doc["chat_id"]] = doc["message_id"]
            if docs:
                logger.info(f"Loaded {len(docs)} persisted auto-quiz poll IDs")
        except Exception as e:
            logger.warning(f"Could not load persisted poll IDs: {e}")

    def _persist_poll_id(self, chat_id: int, message_id: int):
        """Save auto-quiz poll ID to MongoDB for restart persistence."""
        if not self.db:
            return
        try:
            self.db.db["auto_quiz_state"].update_one(
                {"chat_id": chat_id},
                {"$set": {"chat_id": chat_id, "message_id": message_id}},
                upsert=True
            )
        except Exception as e:
            logger.warning(f"Could not persist poll ID for {chat_id}: {e}")

    def start(self):
        self.scheduler.add_job(
            self._send_auto_quiz,
            trigger="interval",
            minutes=self.interval,
            id="auto_quiz",
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info(f"✅ AutoQuizScheduler started — interval: {self.interval} min")

    def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("AutoQuizScheduler stopped")

    async def _send_auto_quiz(self):
        if self.db:
            groups = self.db.get_all_groups()
            chats = [g.get("chat_id") for g in groups if g.get("chat_id")]
        else:
            chats = list(self.quiz_manager.active_chats)
        if not chats:
            logger.info("No active groups — skipping auto quiz")
            return

        for chat_id in chats:
            try:
                question = self.quiz_manager.get_random_question(chat_id=chat_id)
                if not question:
                    logger.warning(f"No questions available for auto quiz in {chat_id}")
                    continue

                # Delete the previous auto-quiz poll
                old_msg_id = self.last_poll_ids.get(chat_id)
                if old_msg_id:
                    try:
                        await self.bot.application.bot.delete_message(
                            chat_id=chat_id,
                            message_id=old_msg_id
                        )
                    except Exception as e:
                        logger.warning(f"Could not delete old auto-quiz for {chat_id}: {e}")

                options     = question.get("options", [])
                correct_idx = question.get("correct_answer", 0)
                category    = question.get("category", "General")
                q_id        = question.get("id")
                explanation = f"✅ {options[correct_idx]}\n📚 {category}  ·  🆔 Q#{q_id}"

                from telegram import Poll
                msg = await self.bot.application.bot.send_poll(
                    chat_id          = chat_id,
                    question         = question.get("question", "Quiz Question"),
                    options          = options,
                    type             = Poll.QUIZ,
                    correct_option_id= correct_idx,
                    explanation      = explanation[:200],
                    is_anonymous     = False,
                )
                self.last_poll_ids[chat_id] = msg.message_id
                self._persist_poll_id(chat_id, msg.message_id)
                if self.db and q_id:
                    try:
                        poll_entry = {
                            "chat_id":           chat_id,
                            "correct_option_id": correct_idx,
                            "category":          category,
                            "question_id":       q_id,
                            "thread_id":         None,
                            "tracking_id":       chat_id,
                        }
                        self.db.save_poll_mapping(
                            str(msg.poll.id), q_id, poll_data=poll_entry)
                    except Exception:
                        pass
                # Also update runtime bot_data for immediate poll answer handling
                try:
                    self.bot.application.bot_data[f"poll_{msg.poll.id}"] = poll_entry
                except Exception:
                    pass
                logger.info(f"Auto quiz sent to {chat_id} — msg_id: {msg.message_id}")

            except Exception as e:
                logger.error(f"Auto quiz failed for {chat_id}: {e}")
