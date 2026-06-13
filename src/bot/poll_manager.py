"""
PollMixin — poll persistence and handle_poll_answer.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from src.bot.ui import get_thread_id, get_tracking_id

logger = logging.getLogger(__name__)


class PollMixin(object):

    # ── Poll mapping persistence ─────────────────────────────
    _PICKLE_PATH = "data/poll_cache.pkl"

    def _pickle_save(self, key: str, data: dict) -> None:
        """Append one entry to standalone pickle backup. Non-fatal on failure."""
        import pickle as _pkl
        try:
            existing: dict = {}
            if os.path.exists(self._PICKLE_PATH):
                try:
                    with open(self._PICKLE_PATH, "rb") as f:
                        existing = _pkl.load(f)
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            existing[key] = data
            with open(self._PICKLE_PATH, "wb") as f:
                _pkl.dump(existing, f)
        except Exception as e:
            logger.warning(f"[POLL CACHE] Pickle save failed (non-fatal): {e}")

    def _pickle_load(self) -> dict:
        """Load standalone pickle backup. Returns empty dict on any error."""
        import pickle as _pkl
        try:
            if not os.path.exists(self._PICKLE_PATH):
                return {}
            with open(self._PICKLE_PATH, "rb") as f:
                data = _pkl.load(f)
            if not isinstance(data, dict):
                logger.warning("[POLL CACHE] Pickle corrupt — ignored, starting fresh")
                return {}
            return data
        except Exception as e:
            logger.warning(f"[POLL CACHE] Pickle load failed (non-fatal): {e}")
            return {}

    async def restore_poll_mappings_to_bot_data(self) -> dict:
        """Restore poll→answer mappings into bot_data from MongoDB (primary)
        and pickle backup (secondary). Called once at startup, before polling.

        Priority: MongoDB > pickle > nothing.
        Bot never crashes regardless of pickle state.
        """
        stats = {"recovered_db": 0, "recovered_pickle": 0, "total": 0}

        # ── 1. Load from MongoDB (authoritative) ──────────────────
        db_mappings: dict = {}
        if self.db:
            try:
                db_mappings = self.db.get_active_poll_mappings()
                stats["recovered_db"] = len(db_mappings)
                logger.info(
                    f"[POLL RECOVERY] Mappings loaded from MongoDB: {stats['recovered_db']}"
                )
            except Exception as e:
                logger.warning(f"[POLL RECOVERY] MongoDB load failed (non-fatal): {e}")

        # ── 2. Load from pickle backup (secondary) ─────────────────
        pickle_mappings = self._pickle_load()
        # Count entries that MongoDB didn't already cover
        pickle_only = {k: v for k, v in pickle_mappings.items()
                       if k not in db_mappings}
        stats["recovered_pickle"] = len(pickle_only)
        if stats["recovered_pickle"]:
            logger.info(
                f"[POLL RECOVERY] Additional mappings from pickle: "
                f"{stats['recovered_pickle']}"
            )

        # ── 3. Merge (MongoDB wins on conflict) ────────────────────
        merged = {**pickle_only, **db_mappings}

        # ── 4. Populate bot_data ───────────────────────────────────
        bot_data = self.application.bot_data
        for key, val in merged.items():
            if key.startswith("poll_"):
                bot_data[key] = val

        stats["total"] = len(merged)
        # Update session-level stats
        self._poll_stats["recovered_db"]     = stats["recovered_db"]
        self._poll_stats["recovered_pickle"] = stats["recovered_pickle"]

        logger.info(
            f"[POLL RECOVERY] ✅ Restored {stats['total']} mappings into bot_data "
            f"(MongoDB: {stats['recovered_db']}, pickle-only: {stats['recovered_pickle']})"
        )
        return stats

    async def handle_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # poll_answer has no effective_chat; ensure_group_registered recovers
        # chat_id from bot_data using the poll_id.
        self.ensure_group_registered(update, context, source="poll-answer")

        answer     = update.poll_answer
        if answer.user:
            self.ensure_user_registered(answer.user, source="poll-answer")
        user_id    = answer.user.id
        poll_id    = answer.poll_id
        option_ids = answer.option_ids

        data       = context.bot_data.get(f"poll_{poll_id}", {})
        correct_id = data.get("correct_option_id")
        chat_id    = data.get("chat_id", 0)
        thread_id  = data.get("thread_id")
        track_id   = data.get("tracking_id", chat_id)

        if correct_id is None or not option_ids:
            # mapping missing — track it
            self._poll_stats["lost"] += 1
            return

        is_correct = (option_ids[0] == correct_id)

        try:
            self.quiz_manager.record_attempt(user_id, is_correct)
            if chat_id and chat_id != user_id:
                self.quiz_manager.record_group_attempt(user_id, chat_id, is_correct)
        except Exception as e:
            logger.error(f"record_attempt: {e}")

        if self.db:
            try:
                self.db.log_activity("quiz_answer",
                    user_id=user_id, chat_id=chat_id,
                    thread_id=thread_id, poll_id=poll_id,
                    is_correct=is_correct,
                    category=data.get("category", ""))
                self.db.upsert_user(user_id, {
                    "user_id":   user_id,
                    "last_seen": datetime.utcnow().isoformat(),
                })
            except Exception as e:
                logger.error(f"DB poll_answer: {e}")

            # Record quiz result for Progress Center
            try:
                self.db.record_quiz_result(user_id, {
                    "correct":  1 if is_correct else 0,
                    "wrong":    0 if is_correct else 1,
                    "skipped":  0,
                    "total":    1,
                    "score":    1 if is_correct else 0,
                    "category": data.get("category", "General"),
                })
            except Exception as e:
                logger.error(f"record_quiz_result: {e}")
