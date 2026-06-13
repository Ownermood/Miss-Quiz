"""
Quiz Manager — CLAT Vision Quiz Bot
Fixed: category field in all question loads, reload_data, get_random_question formatting.
Clean in-memory caching + full MongoDB persistence.
"""

import json
import random
import logging
import traceback
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from collections import defaultdict, deque
from src.core.database import DatabaseManager
from src.core.exceptions import QuestionNotFoundError, ValidationError, DatabaseError

logger = logging.getLogger(__name__)


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
        "options":        options,
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

        # Cache
        self._cached_questions       = None
        self._cached_leaderboard     = None
        self._leaderboard_cache_time = None
        self._cache_duration         = timedelta(minutes=5)

        # Tracking
        self.recent_questions  = defaultdict(lambda: deque(maxlen=50))
        self.last_question_time = defaultdict(dict)
        self.available_questions = defaultdict(list)

        self._load_questions()

    # ─── Internal helpers ────────────────────────────────────────────────────

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

    def add_questions(self, questions: List[Dict]) -> Dict:
        added, db_saved = 0, 0
        duplicates, errors = [], []

        for q in questions:
            question = q.get("question", "").strip()
            options  = q.get("options", [])
            correct  = q.get("correct_answer", 0)
            category = q.get("category", "General")

            # Duplicate check
            existing = [ex["question"].strip() for ex in self.questions]
            if question in existing:
                duplicates.append(question)
                continue

            try:
                new_id = self.db.add_question(question, options, correct, category)
                if new_id is not None:
                    new_q = _fmt_question({
                        "id": new_id, "question": question,
                        "options": options, "correct_answer": correct, "category": category
                    })
                    self.questions.append(new_q)
                    added    += 1
                    db_saved += 1
                else:
                    errors.append(f"DB insert failed for: {question[:40]}")
            except Exception as e:
                errors.append(str(e))

        return {
            "added": added, "db_saved": db_saved,
            "rejected": {"duplicates": len(duplicates)},
            "errors": errors,
        }

    def delete_question_by_db_id(self, db_id: int) -> bool:
        try:
            if not self.db.delete_question(db_id):
                return False
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
                data.get("correct_answer", 0)
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
        """Reload questions from MongoDB, fix cache."""
        try:
            self._cached_questions       = None
            self._cached_leaderboard     = None
            self._leaderboard_cache_time = None
            self.recent_questions.clear()
            self.last_question_time.clear()
            self.available_questions.clear()

            raw = self.db.get_all_questions()
            self.questions = [_fmt_question(q) for q in raw]  # ← BUG FIX: category included

            logger.info(f"Reload complete: {len(self.questions)} questions")
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

            if s["last_correct_date"] == today:
                s["current_streak"] += 1
            elif s.get("last_correct_date") == (
                datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
                s["current_streak"] += 1
            else:
                s["current_streak"] = 1

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

    # ─── Leaderboard ─────────────────────────────────────────────────────────

    def get_leaderboard(self, limit: int = 10) -> List[Dict]:
        try:
            now = datetime.now()
            if (self._cached_leaderboard and self._leaderboard_cache_time
                    and now - self._leaderboard_cache_time < self._cache_duration):
                return self._cached_leaderboard[:limit]

            entries = []
            for uid, score in sorted(self.scores.items(), key=lambda x: x[1], reverse=True)[:limit]:
                uid_str = str(uid)
                s       = self.stats.get(uid_str, {})
                total   = s.get("total_quizzes", 0)
                acc     = round(score / total * 100, 1) if total > 0 else 0
                entries.append({
                    "user_id":        uid,
                    "score":          score,
                    "correct_answers": score,
                    "total_attempts": total,
                    "accuracy":       acc,
                })

            self._cached_leaderboard     = entries
            self._leaderboard_cache_time = now
            return entries
        except Exception as e:
            logger.error(f"get_leaderboard: {e}")
            return []

    def get_group_leaderboard(self, chat_id: int) -> Dict:
        try:
            gid       = str(chat_id)
            entries   = []
            total_att = 0
            total_cor = 0

            for uid_str, s in self.stats.items():
                g = s.get("groups", {}).get(gid)
                if not g:
                    continue
                t, c = g.get("total", 0), g.get("correct", 0)
                if t == 0:
                    continue
                total_att += t
                total_cor += c
                entries.append({
                    "user_id":         int(uid_str),
                    "correct_answers": c,
                    "total_attempts":  t,
                    "accuracy":        round(c / t * 100, 1),
                })

            entries.sort(key=lambda x: x["correct_answers"], reverse=True)
            group_acc = round(total_cor / total_att * 100, 1) if total_att else 0
            return {
                "leaderboard":    entries[:10],
                "total_quizzes":  total_att,
                "group_accuracy": group_acc,
            }
        except Exception as e:
            logger.error(f"get_group_leaderboard: {e}")
            return {"leaderboard": [], "total_quizzes": 0, "group_accuracy": 0}

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
