"""
Quiz Manager — Miss Quiz Bot
Fixed: category field in all question loads, reload_data, get_random_question formatting.
Clean in-memory caching + full MongoDB persistence.
"""

import json
import random
import logging
import traceback
import threading
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from collections import defaultdict, deque
from src.core.database import DatabaseManager
from src.core.exceptions import ValidationError, DatabaseError

logger = logging.getLogger(__name__)


TELEGRAM_OPTION_MAX = 100  # Telegram hard limit for poll option text length


def _sanitize_option(opt: str) -> str:
    """Truncate a poll option to Telegram's 100-char limit, marking the cut with an ellipsis."""
    opt = str(opt).strip()
    if len(opt) > TELEGRAM_OPTION_MAX:
        return opt[:97] + "…"
    return opt


def _fmt_question(q: Dict) -> Dict:
    """Normalize a raw DB question dict into a consistent format with all fields and clean PDF/header noise."""
    from src.utils.cleaner import clean_text
    options = q.get("options", [])
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except Exception:
            options = []
    raw_q = str(q.get("question", ""))
    clean_q = clean_text(raw_q, is_option=False) or raw_q.strip()
    clean_opts = [_sanitize_option(clean_text(str(o), is_option=True) or str(o).strip()) for o in options]
    raw_exp = str(q.get("explanation", "") or "")
    clean_exp = clean_text(raw_exp, is_option=True) if raw_exp else ""
    return {
        "id":             q.get("id"),
        "question":       clean_q,
        "options":        clean_opts,
        "correct_answer": q.get("correct_answer", 0),
        "category":       q.get("category", "General"),  # ← BUG FIX: was missing
        "explanation":    clean_exp,
    }


class QuizManager:
    """
    Central coordinator for all quiz operations.
    Uses MongoDB for persistence, in-memory for speed.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.questions:      List[Dict] = []
        self.scores:         Dict       = {}
        self.active_chats:   set        = set()   # group chat ids (set: O(1) lookups)
        self.stats:          Dict       = {}

        self.db = db_manager if db_manager else DatabaseManager()
        logger.info("QuizManager: database connection ready")

        # Protects self.questions against concurrent import threads
        self._questions_lock = threading.Lock()

        # Tracking
        self.recent_questions  = defaultdict(lambda: deque(maxlen=50))
        self.last_question_time = defaultdict(dict)

        raw = self._load_questions()
        self._migrate_option_lengths(raw)   # reuse the same fetch — no 2nd full scan
        self.sanitize_existing_questions(raw)

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _migrate_option_lengths(self, all_q: Optional[List[Dict]] = None) -> None:
        """One-time startup scan: truncate any DB option > 100 chars in-place.

        Runs synchronously at init. Safe to fail — bot continues regardless.
        In-memory cache is already sanitized by _fmt_question; this only
        writes fixed values back to MongoDB so the DB stays permanently clean.
        """
        if not self.db:
            return
        try:
            if all_q is None:
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

    def sanitize_existing_questions(self, all_q: Optional[List[Dict]] = None) -> Dict[str, int]:
        """Scan all existing questions in MongoDB (questions + quiz_sets) and permanently
        remove PDF watermarks, coaching branch/phone headers, domains, and leaked option numbers.
        Returns {'questions_fixed': int, 'quiz_sets_fixed': int}."""
        stats = {"questions_fixed": 0, "quiz_sets_fixed": 0}
        if not self.db:
            return stats
        try:
            from src.utils.cleaner import clean_text
            if all_q is None:
                all_q = self.db.get_all_questions()

            for q in all_q:
                q_id = q.get("id")
                orig_q = str(q.get("question", "")).strip()
                opts = q.get("options", [])
                if isinstance(opts, str):
                    try:
                        opts = json.loads(opts)
                    except Exception:
                        continue
                orig_opts = [str(o).strip() for o in opts]
                orig_exp = str(q.get("explanation", "") or "").strip()

                new_q = clean_text(orig_q, is_option=False) or orig_q
                new_opts = [_sanitize_option(clean_text(o, is_option=True) or o) for o in orig_opts]
                new_exp = clean_text(orig_exp, is_option=True) if orig_exp else ""

                if new_q != orig_q or new_opts != orig_opts or new_exp != orig_exp:
                    try:
                        fields = {
                            "question": new_q,
                            "options": new_opts,
                            "correct_answer": q.get("correct_answer", 0),
                        }
                        if q.get("category") is not None:
                            fields["category"] = q.get("category")
                        if new_exp != orig_exp:
                            fields["explanation"] = new_exp
                        self.db.questions_col.update_one({"id": q_id}, {"$set": fields})
                        stats["questions_fixed"] += 1
                    except Exception as e:
                        logger.error(f"[SANITIZE] Failed to update Q#{q_id}: {e}")

            # Also sanitize user-created quiz_sets in MongoDB
            if hasattr(self.db, "quiz_sets_col"):
                for qs in self.db.quiz_sets_col.find({}, {"_id": 0, "id": 1, "questions": 1}):
                    sid = qs.get("id")
                    qs_list = qs.get("questions") or []
                    changed = False
                    new_qs_list = []
                    for item in qs_list:
                        if not isinstance(item, dict):
                            new_qs_list.append(item)
                            continue
                        oq = str(item.get("question", "")).strip()
                        oopts = [str(o).strip() for o in (item.get("options") or [])]
                        nq = clean_text(oq, is_option=False) or oq
                        nopts = [_sanitize_option(clean_text(o, is_option=True) or o) for o in oopts]
                        if nq != oq or nopts != oopts:
                            changed = True
                            item = dict(item, question=nq, options=nopts)
                        new_qs_list.append(item)
                    if changed and sid:
                        self.db.quiz_sets_col.update_one({"id": sid}, {"$set": {"questions": new_qs_list}})
                        stats["quiz_sets_fixed"] += 1

            if stats["questions_fixed"] or stats["quiz_sets_fixed"]:
                logger.info(
                    f"[SANITIZE] Cleaned {stats['questions_fixed']} question(s) in bank "
                    f"and {stats['quiz_sets_fixed']} custom quiz set(s) in MongoDB."
                )
        except Exception as e:
            logger.error(f"[SANITIZE] sanitize_existing_questions error: {e}")
        return stats

    def _load_questions(self) -> List[Dict]:
        """Load all questions from DB into memory, with up to 3 retries.
        Returns the raw DB documents so startup checks can reuse them."""
        import time as _time
        last_err = None
        for attempt in range(1, 4):
            try:
                raw = self.db.get_all_questions()
                self.questions = [_fmt_question(q) for q in raw]
                logger.info(f"Loaded {len(self.questions)} questions from MongoDB")
                return raw
            except Exception as e:
                last_err = e
                logger.warning(f"_load_questions attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    _time.sleep(attempt * 3)  # 3s, then 6s before final try
        logger.error(f"Failed to load questions after 3 attempts: {last_err}")
        raise DatabaseError(f"Failed to initialize questions: {last_err}") from last_err

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
        Uses in-memory cache — no DB call on every invocation.
        """
        if category is not None and not isinstance(category, str):
            raise ValidationError(f"category must be a string, got {type(category).__name__}")

        try:
            if not self.questions:
                return None

            # ── Category filter path (in-memory) ─────────────────────────
            if category and category.strip():
                cat  = category.strip()
                pool = [q for q in self.questions
                        if q.get("category", "").lower() == cat.lower()]
                if not pool:
                    logger.warning(f"No questions for category '{cat}'")
                    return None
            else:
                pool = self.questions

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
                    "explanation":    (q.get("explanation") or "")[:200],
                })
                existing.add(q_lower)   # block intra-batch duplicates

        # ── Phase 2: DB write — lock NOT held ─────────────────────────────
        added = db_saved = 0
        if batch:
            try:
                count, new_ids, errs = self.db.add_questions_batch(batch)

                # ── Phase 3: update in-memory cache under lock ────────────
                with self._questions_lock:
                    for i, new_id in new_ids:
                        self.questions.append(_fmt_question({
                            "id":             new_id,
                            "question":       batch[i]["question"],
                            "options":        batch[i]["options"],
                            "correct_answer": batch[i]["correct_answer"],
                            "category":       batch[i]["category"],
                            "explanation":    batch[i]["explanation"],
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
                            "options":        [_sanitize_option(o) for o in data.get("options", q["options"])],
                            "correct_answer": data.get("correct_answer", q["correct_answer"]),
                            "category":       data.get("category") or q.get("category", "General"),
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
            self.recent_questions.clear()
            self.last_question_time.clear()

            raw           = self.db.get_all_questions()         # outside lock (I/O)
            self.sanitize_existing_questions(raw)               # permanently clean DB records on reload
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
        self.active_chats.add(chat_id)

    def get_score(self, user_id: int) -> int:
        return self.scores.get(user_id, 0)

    # ─── Compatibility shims (used by dev_commands) ──────────────────────────

    def remove_active_chat(self, chat_id: int):
        """Remove a chat from active_chats (called when bot is kicked from group)."""
        self.active_chats.discard(chat_id)
