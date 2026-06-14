"""
Quiz Manager — CLAT Vision Quiz Bot
Fixed: category field in all question loads, reload_data, get_random_question formatting.
Clean in-memory caching + full MongoDB persistence.
"""

import json
import random
import logging
import traceback
import threading
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from collections import defaultdict, deque
from src.core.database import DatabaseManager
from src.core.exceptions import QuestionNotFoundError, ValidationError, DatabaseError

logger = logging.getLogger(__name__)


TELEGRAM_OPTION_MAX = 100  # Telegram hard limit for poll option text length


def _sanitize_option(opt: str) -> str:
    """Truncate a poll option to Telegram's 100-char limit, marking the cut with an ellipsis."""
    opt = str(opt).strip()
    if len(opt) > TELEGRAM_OPTION_MAX:
        return opt[:97] + "…"
    return opt


def _fmt_question(q: Dict) -> Dict:
    """Normalize a raw DB question dict into a consistent format with all fields."""
    options = q.get("options", [])
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except Exception:
            options = []
    return {
        "id":             q.get("id"),
        "question":       q.get("question", ""),
        "options":        [_sanitize_option(o) for o in options],
        "correct_answer": q.get("correct_answer", 0),
        "category":       q.get("category", "General"),  # ← BUG FIX: was missing
    }


class QuizManager:
    """
    Central coordinator for all quiz operations.
    Uses MongoDB for persistence, in-memory for speed.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.questions:      List[Dict] = []
        self.scores:         Dict       = {}
        self.active_chats:   List       = []
        self.stats:          Dict       = {}

        self.db = db_manager if db_manager else DatabaseManager()
        logger.info("QuizManager: database connection ready")

        # Protects self.questions against concurrent import threads
        self._questions_lock = threading.Lock()

        # Cache
        self._cached_questions       = None

        # Tracking
        self.recent_questions  = defaultdict(lambda: deque(maxlen=50))
        self.last_question_time = defaultdict(dict)
        self.available_questions = defaultdict(list)

        self._load_questions()
        self._migrate_option_lengths()

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _migrate_option_lengths(self) -> None:
        """One-time startup scan: truncate any DB option > 100 chars in-place.

        Runs synchronously at init. Safe to fail — bot continues regardless.
        In-memory cache is already sanitized by _fmt_question; this only
        writes fixed values back to MongoDB so the DB stays permanently clean.
        """
        if not self.db:
            return
        try:
            all_q = self.db.get_all_questions()
            to_fix = []
            for q in all_q:
                opts = q.get("options", [])
                if isinstance(opts, str):
                    try:
                        opts = json.loads(opts)
                    except Exception:
                        continue
                raw = [str(o).strip() for o in opts]
                if any(len(o) > TELEGRAM_OPTION_MAX for o in raw):
                    to_fix.append((q, raw))

            if not to_fix:
                logger.info(
                    f"[MIGRATION] Option length check: all {len(all_q)} question(s) "
                    f"comply with Telegram's {TELEGRAM_OPTION_MAX}-char limit"
                )
                return

            logger.warning(
                f"[MIGRATION] {len(to_fix)} question(s) have option(s) > "
                f"{TELEGRAM_OPTION_MAX} chars — fixing DB records..."
            )
            fixed = 0
            for q, raw_opts in to_fix:
                q_id     = q.get("id")
                over     = [(i, len(o)) for i, o in enumerate(raw_opts) if len(o) > TELEGRAM_OPTION_MAX]
                new_opts = [_sanitize_option(o) for o in raw_opts]
                logger.warning(
                    f"[MIGRATION] Q#{q_id}: truncating option(s) at "
                    f"index(es) {[i for i, _ in over]}, "
                    f"original length(s) {[l for _, l in over]}"
                )
                try:
                    self.db.update_question(
                        q_id,
                        q.get("question", ""),
                        new_opts,
                        q.get("correct_answer", 0),
                        category=q.get("category"),
                    )
                    fixed += 1
                except Exception as e:
                    logger.error(f"[MIGRATION] Failed to fix Q#{q_id}: {e}")

            logger.info(
                f"[MIGRATION] Fixed {fixed}/{len(to_fix)} question(s) in DB. "
                f"In-memory already sanitized via _fmt_question."
            )
        except Exception as e:
            logger.error(f"[MIGRATION] _migrate_option_lengths error: {e}")

    def _load_questions(self):
        """Load all questions from DB into memory (with category fix)."""
        try:
            raw = self.db.get_all_questions()
            self.questions = [_fmt_question(q) for q in raw]
            logger.info(f"Loaded {len(self.questions)} questions from MongoDB")
        except Exception as e:
            logger.error(f"Failed to load questions: {e}")
            raise DatabaseError(f"Failed to initialize questions: {e}") from e

    def _init_user_stats(self, user_id: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self.stats[user_id] = {
            "total_quizzes":       0,
            "correct_answers":     0,
            "current_streak":      0,
            "longest_streak":      0,
            "last_correct_date":   None,
            "category_scores":     {},
            "daily_activity":      {today: {"attempts": 0, "correct": 0}},
            "last_quiz_date":      today,
            "last_activity_date":  today,
            "join_date":           today,
            "groups":              {},
            "private_chat_activity": {"total_messages": 0, "last_active": today},
        }

    # ─── Question selection ──────────────────────────────────────────────────

    def get_random_question(self, chat_id: int = 0, category: str = "") -> Optional[Dict]:
        """
        Return a random question, optionally filtered by category.
        Avoids recently-asked questions per chat.
        """
        if category is not None and not isinstance(category, str):
            raise ValidationError(f"category must be a string, got {type(category).__name__}")

        try:
            if not self.questions and not self.db:
                return None

            # ── Category filter path ─────────────────────────────────────
            if category and category.strip():
                cat = category.strip()
                raw = self.db.get_questions_by_category(cat)
                if not raw:
                    logger.warning(f"No questions for category '{cat}'")
                    return None

                pool = [_fmt_question(q) for q in raw]  # ← BUG FIX: use _fmt_question

                if chat_id == 0:
                    return random.choice(pool)

                recent = self.recent_questions[chat_id]
                available = [q for q in pool if q["question"] not in recent]
                if not available:
                    available = pool
                    logger.info(f"Reset recent questions for category '{cat}' chat {chat_id}")

                selected = random.choice(available)
                self.recent_questions[chat_id].append(selected["question"])
                self.last_question_time[chat_id][selected["question"]] = datetime.now()
                return selected

            # ── No category path ─────────────────────────────────────────
            # Always fetch from DB so IDs are accurate for /delquiz
            raw = self.db.get_all_questions()
            if not raw:
                return random.choice(self.questions) if self.questions else None

            pool = [_fmt_question(q) for q in raw]  # ← BUG FIX: use _fmt_question

            if chat_id == 0:
                return random.choice(pool)

            recent    = self.recent_questions[chat_id]
            available = [q for q in pool if q["question"] not in recent]
            if not available:
                available = pool
                logger.info(f"Reset recent questions for chat {chat_id}")

            selected = random.choice(available)
            self.recent_questions[chat_id].append(selected["question"])
            self.last_question_time[chat_id][selected["question"]] = datetime.now()
            return selected

        except Exception as e:
            logger.error(f"get_random_question error: {e}\n{traceback.format_exc()}")
            return random.choice(self.questions) if self.questions else None

    # ─── Question management ─────────────────────────────────────────────────

    def add_questions(self, questions: List[Dict],
                      _existing: Optional[set] = None) -> Dict:
        """Thread-safe bulk insert.

        Phase 1 (dedup) and Phase 3 (cache append) hold _questions_lock.
        Phase 2 (DB insert) runs without the lock so the bot stays
        responsive during long writes.

        _existing: pre-built dedup set from the caller (bulk_import).
        We merge it with the current self.questions snapshot under the
        lock to catch any questions added by a concurrent import between
        when the caller built the set and when we enter the lock.
        """
        dup_count = 0
        errors    = []

        # ── Phase 1: build authoritative batch under lock ─────────────────
        with self._questions_lock:
            if _existing is not None:
                # Start from caller's set and fold in anything added since
                # it was built (handles concurrent-import races)
                existing = set(_existing)
                for q in self.questions:
                    existing.add(q["question"].strip().lower())
            else:
                existing = {q["question"].strip().lower() for q in self.questions}

            batch = []
            for q in questions:
                question = q.get("question", "").strip()
                q_lower  = question.lower()
                if q_lower in existing:
                    dup_count += 1
                    continue
                batch.append({
                    "question":       question,
                    "options":        q.get("options", []),
                    "correct_answer": q.get("correct_answer", 0),
                    "category":       q.get("category", "General"),
                })
                existing.add(q_lower)   # block intra-batch duplicates

        # ── Phase 2: DB write — lock NOT held ─────────────────────────────
        added = db_saved = 0
        if batch:
            try:
                count, new_ids, errs = self.db.add_questions_batch(batch)

                # ── Phase 3: update in-memory cache under lock ────────────
                with self._questions_lock:
                    for i in range(count):
                        self.questions.append(_fmt_question({
                            "id":             new_ids[i],
                            "question":       batch[i]["question"],
                            "options":        batch[i]["options"],
                            "correct_answer": batch[i]["correct_answer"],
                            "category":       batch[i]["category"],
                        }))
                added    = count
                db_saved = count
                errors   = errs
            except Exception as e:
                errors.append(str(e))

        return {
            "added":    added,
            "db_saved": db_saved,
            "rejected": {"duplicates": dup_count},
            "errors":   errors,
        }

    def delete_question_by_db_id(self, db_id: int) -> bool:
        try:
            if not self.db.delete_question(db_id):
                return False
            with self._questions_lock:
                before = len(self.questions)
                self.questions = [q for q in self.questions if q.get("id") != db_id]
                logger.info(f"Deleted Q#{db_id}, removed {before - len(self.questions)} from cache")
            return True
        except Exception as e:
            logger.error(f"delete_question_by_db_id: {e}")
            raise DatabaseError(f"Failed to delete question {db_id}: {e}") from e

    def edit_question_by_db_id(self, db_id: int, data: Dict) -> bool:
        try:
            ok = self.db.update_question(
                db_id,
                data.get("question", ""),
                data.get("options", []),
                data.get("correct_answer", 0),
                category=data.get("category") or None
            )
            if ok:
                for q in self.questions:
                    if q.get("id") == db_id:
                        q.update({
                            "question":       data.get("question", q["question"]),
                            "options":        data.get("options", q["options"]),
                            "correct_answer": data.get("correct_answer", q["correct_answer"]),
                        })
                        break
            return ok
        except Exception as e:
            logger.error(f"edit_question_by_db_id: {e}")
            return False

    def reload_data(self):
        """Reload questions from MongoDB.
        DB fetch and list-build run outside the lock; only the pointer
        swap that makes the new list visible is locked."""
        try:
            self._cached_questions = None
            self.recent_questions.clear()
            self.last_question_time.clear()
            self.available_questions.clear()

            raw           = self.db.get_all_questions()         # outside lock (I/O)
            new_questions = [_fmt_question(q) for q in raw]    # outside lock (CPU)

            with self._questions_lock:
                self.questions = new_questions                  # atomic pointer swap

            logger.info(f"Reload complete: {len(new_questions)} questions")
            return True
        except Exception as e:
            logger.error(f"reload_data: {e}\n{traceback.format_exc()}")
            raise DatabaseError(f"Failed to reload data: {e}") from e

    # ─── Scoring ─────────────────────────────────────────────────────────────

    def record_attempt(self, user_id: int, is_correct: bool, category: str = ""):
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValidationError(f"Invalid user_id: {user_id}")

        uid = str(user_id)
        if uid not in self.stats:
            self._init_user_stats(uid)

        s     = self.stats[uid]
        today = datetime.now().strftime("%Y-%m-%d")

        s["total_quizzes"] += 1
        if today not in s["daily_activity"]:
            s["daily_activity"][today] = {"attempts": 0, "correct": 0}
        s["daily_activity"][today]["attempts"] += 1

        if is_correct:
            s["correct_answers"]  += 1
            s["daily_activity"][today]["correct"] += 1
            self.scores[user_id]   = self.scores.get(user_id, 0) + 1

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            last_cd = s.get("last_correct_date")
            if last_cd == today:
                pass  # already played today — streak unchanged
            elif last_cd == yesterday:
                s["current_streak"] += 1  # consecutive day
            else:
                s["current_streak"] = 1   # gap or first time

            s["last_correct_date"] = today
            if s["current_streak"] > s["longest_streak"]:
                s["longest_streak"] = s["current_streak"]

            if category:
                s["category_scores"][category] = s["category_scores"].get(category, 0) + 1
        else:
            s["current_streak"] = 0

        if user_id not in self.active_chats:
            self.active_chats.append(user_id)

    def record_group_attempt(self, user_id: int, chat_id: int, is_correct: bool):
        uid = str(user_id)
        if uid not in self.stats:
            self._init_user_stats(uid)
        if "groups" not in self.stats[uid]:
            self.stats[uid]["groups"] = {}
        gid = str(chat_id)
        if gid not in self.stats[uid]["groups"]:
            self.stats[uid]["groups"][gid] = {"total": 0, "correct": 0}
        self.stats[uid]["groups"][gid]["total"]   += 1
        if is_correct:
            self.stats[uid]["groups"][gid]["correct"] += 1
        if chat_id not in self.active_chats:
            self.active_chats.append(chat_id)

    def get_score(self, user_id: int) -> int:
        return self.scores.get(user_id, 0)

    def get_user_stats(self, user_id: int) -> Dict:
        try:
            uid = str(user_id)
            if uid not in self.stats:
                self._init_user_stats(uid)

            s     = self.stats[uid]
            total = s.get("total_quizzes", 0)
            corr  = s.get("correct_answers", 0)
            score = self.get_score(user_id)
            rate  = round((corr / total * 100), 1) if total > 0 else 0

            today = datetime.now().strftime("%Y-%m-%d")
            week_start  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            month_start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

            today_q = s.get("daily_activity", {}).get(today, {}).get("attempts", 0)
            week_q  = sum(
                v.get("attempts", 0) for k, v in s.get("daily_activity", {}).items()
                if k >= week_start
            )
            month_q = sum(
                v.get("attempts", 0) for k, v in s.get("daily_activity", {}).items()
                if k >= month_start
            )

            return {
                "total_quizzes":    total,
                "correct_answers":  corr,
                "success_rate":     rate,
                "current_score":    score,
                "today_quizzes":    today_q,
                "week_quizzes":     week_q,
                "month_quizzes":    month_q,
                "current_streak":   s.get("current_streak", 0),
                "longest_streak":   s.get("longest_streak", 0),
            }
        except Exception as e:
            logger.error(f"get_user_stats: {e}")
            return {
                "total_quizzes": 0, "correct_answers": 0, "success_rate": 0,
                "current_score": 0, "today_quizzes": 0, "week_quizzes": 0,
                "month_quizzes": 0, "current_streak": 0, "longest_streak": 0,
            }

    # ─── Compatibility shims (used by dev_commands) ──────────────────────────

    def get_group_last_activity(self, chat_id: str) -> Optional[str]:
        for uid_str, s in self.stats.items():
            g = s.get("groups", {}).get(str(chat_id))
            if g:
                return s.get("last_activity_date")
        return None

    def get_quiz_stats(self) -> Dict:
        """Return basic quiz stats used by dev_commands after deletion."""
        db_count = 0
        try:
            db_count = self.db.questions_col.count_documents({})
        except Exception:
            db_count = len(self.questions)
        status = "synced" if db_count == len(self.questions) else "out_of_sync"
        return {
            "total_quizzes":    len(self.questions),
            "db_count":         db_count,
            "integrity_status": status,
        }

    def remove_active_chat(self, chat_id: int):
        """Remove a chat from active_chats (called when bot is kicked from group)."""
        try:
            self.active_chats.remove(chat_id)
        except ValueError:
            pass

    def _initialize_available_questions(self, chat_id: int):
        self.available_questions[chat_id] = list(range(len(self.questions)))
        random.shuffle(self.available_questions[chat_id])
