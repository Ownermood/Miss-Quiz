"""
MongoDB DatabaseManager for Telegram Quiz Bot
Owner ID: 8403136097
"""

import logging
import os
import math
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

try:
    from pymongo import MongoClient, ASCENDING, DESCENDING
    from pymongo.errors import PyMongoError
    PYMONGO_AVAILABLE = True
except ImportError:
    PYMONGO_AVAILABLE = False
    logger.error("pymongo not installed. Run: pip install pymongo")


class DatabaseManager:
    def __init__(self, mongo_url: Optional[str] = None):
        if not PYMONGO_AVAILABLE:
            raise RuntimeError("pymongo is required. Run: pip install pymongo")

        url = mongo_url or os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("MONGODB_DB", "quiz_bot")

        self.client = MongoClient(url, serverSelectionTimeoutMS=10000)
        # Ping to verify connection
        self.client.admin.command('ping')
        self.db = self.client[db_name]

        self.questions_col  = self.db["questions"]
        self.users_col      = self.db["users"]
        self.groups_col     = self.db["groups"]
        self.broadcasts_col = self.db["broadcasts"]
        self.activities_col = self.db["activities"]
        self.developers_col = self.db["developers"]
        self.poll_map_col   = self.db["poll_map"]
        self.performance_col= self.db["performance"]

        self._ensure_indexes()
        logger.info(f"✅ MongoDB connected: {url} / db={db_name}")

    def _ensure_indexes(self):
        try:
            self.questions_col.create_index("id", unique=True)
            self.questions_col.create_index("category")
            self.users_col.create_index([("user_id", ASCENDING)], unique=True)
            self.users_col.create_index([("xp", DESCENDING)])
            self.users_col.create_index([("total_marks", DESCENDING)])
            self.users_col.create_index([("correct_answers", DESCENDING)])
            self.users_col.create_index([("last_seen", DESCENDING)])
            self.users_col.create_index([("last_activity", DESCENDING)])
            self.users_col.create_index([("total_answers", DESCENDING)])
            # Compound index for leaderboard ranking query
            self.users_col.create_index([
                ("total_marks", DESCENDING),
                ("correct_answers", DESCENDING),
                ("quizzes_attempted", DESCENDING),
            ])
            self.groups_col.create_index("chat_id", unique=True)
            self.groups_col.create_index([("last_active", DESCENDING)])
            self.poll_map_col.create_index("poll_id", unique=True)
            # Compound indexes for time-based activity queries (critical for leaderboards)
            self.activities_col.create_index([("type", ASCENDING), ("timestamp", DESCENDING)])
            self.activities_col.create_index([("type", ASCENDING), ("is_correct", ASCENDING), ("timestamp", DESCENDING)])
            self.activities_col.create_index([("type", ASCENDING), ("user_id", ASCENDING), ("timestamp", DESCENDING)])
            self.activities_col.create_index([("timestamp", DESCENDING)])
            self.performance_col.create_index([("metric", ASCENDING), ("timestamp", DESCENDING)])
            self.performance_col.create_index([("timestamp", DESCENDING)])
            self.broadcasts_col.create_index([("created_at", DESCENDING)])
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")

    def _next_id(self, name: str) -> int:
        counter = self.db["_counters"].find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True
        )
        return counter["seq"]

    # ── Questions ─────────────────────────────────────────────────────────────

    def get_all_questions(self) -> List[Dict]:
        docs = list(self.questions_col.find({}, {"_id": 0}))
        for d in docs:
            if isinstance(d.get("options"), str):
                import json
                try:
                    d["options"] = json.loads(d["options"])
                except Exception:
                    pass
        return docs

    def get_question_by_id(self, qid: int) -> Optional[Dict]:
        return self.questions_col.find_one({"id": qid}, {"_id": 0})

    def get_question_count(self) -> int:
        """Live question count straight from the database."""
        try:
            return self.questions_col.count_documents({})
        except Exception as e:
            logger.error(f"get_question_count error: {e}")
            return 0

    def get_category_counts(self) -> List[Dict]:
        """Live per-category question counts, sorted by count descending."""
        try:
            return list(self.questions_col.aggregate([
                {"$group": {"_id": "$category", "count": {"$sum": 1}}},
                {"$sort": {"count": DESCENDING}},
            ]))
        except Exception as e:
            logger.error(f"get_category_counts error: {e}")
            return []

    def get_db_stats(self) -> Dict:
        """Live database metrics: collections, objects, data/storage size."""
        try:
            s = self.db.command("dbstats")
            return {
                "collections":  s.get("collections", 0),
                "objects":      s.get("objects", 0),
                "data_mb":      round(s.get("dataSize", 0) / 1024 / 1024, 2),
                "storage_mb":   round(s.get("storageSize", 0) / 1024 / 1024, 2),
                "indexes":      s.get("indexes", 0),
            }
        except Exception as e:
            logger.error(f"get_db_stats error: {e}")
            return {}

    def get_questions_by_category(self, category: str) -> List[Dict]:
        return list(self.questions_col.find({"category": category}, {"_id": 0}))

    def add_question(self, question: str, options: list, correct_answer: int,
                     category: str = "General") -> Optional[int]:
        """Add a question. Returns new DB id or None on failure."""
        try:
            new_id = self._next_id("questions")
            doc = {
                "id": new_id,
                "question": question,
                "options": options,
                "correct_answer": correct_answer,
                "category": category,
                "created_at": datetime.utcnow().isoformat()
            }
            self.questions_col.insert_one(doc)
            return new_id
        except Exception as e:
            logger.error(f"add_question error: {e}")
            return None

    def update_question(self, qid: int, question: str, options: list,
                        correct_answer: int, category: str = None) -> bool:
        try:
            fields = {"question": question, "options": options,
                      "correct_answer": correct_answer}
            if category is not None:
                fields["category"] = category
            result = self.questions_col.update_one(
                {"id": qid},
                {"$set": fields}
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"update_question error: {e}")
            return False

    def delete_question(self, qid: int) -> bool:
        try:
            result = self.questions_col.delete_one({"id": qid})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"delete_question error: {e}")
            return False

    # ── Poll → Question mapping ───────────────────────────────────────────────

    def save_poll_mapping(self, poll_id: str, quiz_id: int, chat_id: int = None,
                          poll_data: dict = None) -> None:
        """Store poll mapping. poll_data dict fields: correct_option_id, chat_id,
        thread_id, tracking_id, category, question_id, question (chat_title optional).
        MongoDB is authoritative; all fields stored here are source of truth."""
        try:
            data: dict = {
                "quiz_id":    quiz_id,
                "created_at": datetime.utcnow().isoformat(),
            }
            if chat_id and isinstance(chat_id, int) and chat_id < 0:
                data["chat_id"] = chat_id
            if poll_data:
                for field in ("correct_option_id", "thread_id", "tracking_id",
                              "category", "question_id", "question", "chat_title"):
                    if field in poll_data and poll_data[field] is not None:
                        data[field] = poll_data[field]
                # chat_id from poll_data overrides positional arg if negative integer
                pd_cid = poll_data.get("chat_id")
                if pd_cid and isinstance(pd_cid, int) and pd_cid < 0:
                    data["chat_id"] = pd_cid
            self.poll_map_col.update_one(
                {"poll_id": poll_id},
                {"$set": data},
                upsert=True
            )
        except Exception as e:
            logger.error(f"save_poll_mapping error: {e}")

    def get_quiz_id_from_poll(self, poll_id: str) -> Optional[int]:
        try:
            doc = self.poll_map_col.find_one({"poll_id": poll_id})
            return doc["quiz_id"] if doc else None
        except Exception:
            return None

    def get_active_poll_mappings(self, max_age_hours: int = 48) -> Dict[str, dict]:
        """Load recent poll mappings from MongoDB for bot_data restoration.
        Returns {f'poll_{poll_id}': bot_data_entry, ...}.
        max_age_hours=48 covers auto-quiz polls that stay open until deleted."""
        try:
            cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
            docs = list(self.poll_map_col.find(
                {"created_at": {"$gte": cutoff}, "correct_option_id": {"$exists": True}},
                {"_id": 0}
            ))
            result: Dict[str, dict] = {}
            for doc in docs:
                pid = doc.get("poll_id")
                if not pid:
                    continue
                result[f"poll_{pid}"] = {
                    "question_id":       doc.get("question_id"),
                    "question":          doc.get("question", ""),
                    "correct_option_id": doc.get("correct_option_id"),
                    "chat_id":           doc.get("chat_id", 0),
                    "thread_id":         doc.get("thread_id"),
                    "tracking_id":       doc.get("tracking_id", doc.get("chat_id", 0)),
                    "category":          doc.get("category", ""),
                }
            return result
        except Exception as e:
            logger.warning(f"get_active_poll_mappings error: {e}")
            return {}

    # ── Users ─────────────────────────────────────────────────────────────────

    def upsert_user(self, user_id: int, data: Dict):
        try:
            self.users_col.update_one(
                {"user_id": user_id},
                {
                    "$set": data,
                    "$setOnInsert": {
                        "joined_at":          datetime.utcnow().isoformat(),
                        "active_status":      "active",
                        "quizzes_attempted":  0,
                        "quizzes_completed":  0,
                        "total_questions":    0,
                        "correct_answers":    0,
                        "wrong_answers":      0,
                        "skipped_answers":    0,
                        "total_marks":        0,
                        "best_score":         0,
                        "xp":                 0,
                        "level":              1,
                        "current_streak":     0,
                        "highest_streak":     0,
                        "last_activity":      "",
                        "subject_stats":      {},
                        "achievements":       [],
                        "daily_activity":     [],
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"upsert_user error: {e}")

    # ── Progress Center — new methods ─────────────────────────────────────────

    # Achievement definitions (class-level constant)
    ACHIEVEMENTS = {
        "first_quiz":  {"label": "🎯 First Quiz",    "condition": lambda u: u.get("quizzes_completed", 0) >= 1},
        "quiz_10":     {"label": "📚 10 Quizzes",    "condition": lambda u: u.get("quizzes_completed", 0) >= 10},
        "quiz_50":     {"label": "🏅 50 Quizzes",    "condition": lambda u: u.get("quizzes_completed", 0) >= 50},
        "quiz_100":    {"label": "🏆 100 Quizzes",   "condition": lambda u: u.get("quizzes_completed", 0) >= 100},
        "quiz_500":    {"label": "👑 500 Quizzes",   "condition": lambda u: u.get("quizzes_completed", 0) >= 500},
        "acc_70":      {"label": "✅ 70% Accuracy",  "condition": lambda u: u.get("total_questions", 0) >= 10 and (u.get("correct_answers", 0) / max(u.get("total_questions", 1), 1)) * 100 >= 70},
        "acc_80":      {"label": "⭐ 80% Accuracy",  "condition": lambda u: u.get("total_questions", 0) >= 10 and (u.get("correct_answers", 0) / max(u.get("total_questions", 1), 1)) * 100 >= 80},
        "acc_90":      {"label": "💫 90% Accuracy",  "condition": lambda u: u.get("total_questions", 0) >= 10 and (u.get("correct_answers", 0) / max(u.get("total_questions", 1), 1)) * 100 >= 90},
        "acc_95":      {"label": "🌟 95% Accuracy",  "condition": lambda u: u.get("total_questions", 0) >= 10 and (u.get("correct_answers", 0) / max(u.get("total_questions", 1), 1)) * 100 >= 95},
        "streak_3":    {"label": "🔥 3-Day Streak",  "condition": lambda u: u.get("highest_streak", 0) >= 3},
        "streak_7":    {"label": "🔥 7-Day Streak",  "condition": lambda u: u.get("highest_streak", 0) >= 7},
        "streak_15":   {"label": "🔥 15-Day Streak", "condition": lambda u: u.get("highest_streak", 0) >= 15},
        "streak_30":   {"label": "🔥 30-Day Streak", "condition": lambda u: u.get("highest_streak", 0) >= 30},
        "streak_100":  {"label": "🔥 100-Day Streak","condition": lambda u: u.get("highest_streak", 0) >= 100},
        "q_100":       {"label": "📖 100 Questions", "condition": lambda u: u.get("total_questions", 0) >= 100},
        "q_500":       {"label": "📖 500 Questions", "condition": lambda u: u.get("total_questions", 0) >= 500},
        "q_1000":      {"label": "📖 1000 Questions","condition": lambda u: u.get("total_questions", 0) >= 1000},
        "q_5000":      {"label": "📖 5000 Questions","condition": lambda u: u.get("total_questions", 0) >= 5000},
        "lvl_5":       {"label": "⚡ Level 5",       "condition": lambda u: u.get("level", 1) >= 5},
        "lvl_10":      {"label": "⚡ Level 10",      "condition": lambda u: u.get("level", 1) >= 10},
        "lvl_25":      {"label": "⚡ Level 25",      "condition": lambda u: u.get("level", 1) >= 25},
        "lvl_50":      {"label": "⚡ Level 50",      "condition": lambda u: u.get("level", 1) >= 50},
    }

    def record_quiz_result(self, user_id: int, result_data: Dict):
        """Atomic update of all user stats after a quiz ends."""
        try:
            correct  = result_data.get("correct",  0)
            wrong    = result_data.get("wrong",    0)
            skipped  = result_data.get("skipped",  0)
            total    = result_data.get("total",    0)
            score    = result_data.get("score",    0)
            category = result_data.get("category", "General")

            # Fetch current user to calculate streak and XP-based level
            user_doc = self.users_col.find_one({"user_id": user_id}) or {}

            today_str    = datetime.utcnow().strftime("%Y-%m-%d")
            last_activity= user_doc.get("last_activity", "")

            # Streak calculation
            current_streak  = user_doc.get("current_streak", 0)
            highest_streak  = user_doc.get("highest_streak", 0)

            if last_activity == today_str:
                # Already played today, keep streak as-is
                pass
            elif last_activity:
                try:
                    last_date = datetime.strptime(last_activity, "%Y-%m-%d").date()
                    today_date = datetime.utcnow().date()
                    diff = (today_date - last_date).days
                    if diff == 1:
                        current_streak += 1
                    else:
                        current_streak = 1
                except Exception:
                    current_streak = 1
            else:
                current_streak = 1

            if current_streak > highest_streak:
                highest_streak = current_streak

            # XP calculation
            xp_gain = correct * 5
            if total > 0:
                xp_gain += 20  # quiz completed
            if total > 0 and correct == total:
                xp_gain += 50  # perfect quiz

            new_xp = user_doc.get("xp", 0) + xp_gain
            new_level = max(1, int(math.floor(math.sqrt(new_xp / 100))))

            # Subject stats update
            subject_stats = user_doc.get("subject_stats", {})
            if not isinstance(subject_stats, dict):
                subject_stats = {}
            subj = subject_stats.get(category, {"attempted": 0, "correct": 0})
            subj["attempted"] = subj.get("attempted", 0) + total
            subj["correct"]   = subj.get("correct",   0) + correct
            subject_stats[category] = subj

            # Daily activity: update or append today
            daily_activity = user_doc.get("daily_activity", [])
            if not isinstance(daily_activity, list):
                daily_activity = []
            updated = False
            for entry in daily_activity:
                if isinstance(entry, dict) and entry.get("date") == today_str:
                    entry["count"] = entry.get("count", 0) + total
                    updated = True
                    break
            if not updated:
                daily_activity.append({"date": today_str, "count": total})
            # Keep last 30 days only
            daily_activity = sorted(daily_activity, key=lambda x: x.get("date", ""))[-30:]

            inc_ops = {
                "quizzes_attempted": 1,
                "total_questions":   total,
                "correct_answers":   correct,
                "wrong_answers":     wrong,
                "skipped_answers":   skipped,
                "total_marks":       score,
            }
            if total > 0:
                inc_ops["quizzes_completed"] = 1

            set_ops = {
                "xp":              new_xp,
                "level":           new_level,
                "current_streak":  current_streak,
                "highest_streak":  highest_streak,
                "last_activity":   today_str,
                "subject_stats":   subject_stats,
                "daily_activity":  daily_activity,
            }

            self.users_col.update_one(
                {"user_id": user_id},
                {
                    "$inc": inc_ops,
                    "$max": {"best_score": score},
                    "$set": set_ops,
                },
                upsert=True
            )

            # Fetch updated doc for achievement check
            updated_doc = self.users_col.find_one({"user_id": user_id}) or {}
            self._check_achievements(user_id, updated_doc)

        except Exception as e:
            logger.error(f"record_quiz_result error: {e}")

    def _check_achievements(self, user_id: int, user_doc: Dict):
        """Check and award unlocked achievements."""
        try:
            existing_keys = {
                a["key"] if isinstance(a, dict) else a
                for a in user_doc.get("achievements", [])
            }
            now_iso = datetime.utcnow().isoformat()
            new_achievements = []
            for key, ach in self.ACHIEVEMENTS.items():
                if key not in existing_keys:
                    try:
                        if ach["condition"](user_doc):
                            new_achievements.append({
                                "key":       key,
                                "label":     ach["label"],
                                "earned_at": now_iso,
                            })
                    except Exception:
                        pass
            if new_achievements:
                for ach in new_achievements:
                    self.users_col.update_one(
                        {"user_id": user_id},
                        {"$addToSet": {"achievements": ach}}
                    )
        except Exception as e:
            logger.error(f"_check_achievements error: {e}")

    # Canonical leaderboard sort order
    _LB_SORT = [
        ("total_marks",      DESCENDING),
        ("correct_answers",  DESCENDING),
        ("quizzes_attempted", DESCENDING),
        ("last_activity",    DESCENDING),
    ]

    def get_user_rank(self, user_id: int) -> Dict:
        """Returns global_rank and total_users based on total_marks ranking."""
        try:
            user_doc = self.users_col.find_one(
                {"user_id": user_id},
                {"total_marks": 1, "correct_answers": 1, "quizzes_attempted": 1,
                 "xp": 1, "current_streak": 1, "total_questions": 1,
                 "quizzes_completed": 1, "name": 1, "username": 1}) or {}
            marks = user_doc.get("total_marks", 0)
            rank  = self.users_col.count_documents({"total_marks": {"$gt": marks}}) + 1
            total = self.users_col.count_documents({})
            return {
                "global_rank":       rank,
                "total_users":       total,
                "total_marks":       marks,
                "correct_answers":   user_doc.get("correct_answers", 0),
                "quizzes_completed": user_doc.get("quizzes_completed", 0),
                "total_questions":   user_doc.get("total_questions", 1),
                "current_streak":    user_doc.get("current_streak", 0),
                "xp":                user_doc.get("xp", 0),
            }
        except Exception as e:
            logger.error(f"get_user_rank error: {e}")
            return {"global_rank": 0, "total_users": 0}

    def get_neighbor_ranks(self, user_id: int) -> Dict:
        """Returns the users ranked just above and below the given user."""
        try:
            user_doc = self.users_col.find_one({"user_id": user_id},
                                               {"total_marks": 1}) or {}
            marks = user_doc.get("total_marks", 0)
            rank  = self.users_col.count_documents({"total_marks": {"$gt": marks}}) + 1

            above = below = None
            if rank > 1:
                above = self.users_col.find_one(
                    {"total_marks": {"$gt": marks}},
                    {"user_id": 1, "name": 1, "username": 1, "total_marks": 1},
                    sort=self._LB_SORT)
            below_doc = list(
                self.users_col.find(
                    {"total_marks": {"$lt": marks}},
                    {"user_id": 1, "name": 1, "username": 1, "total_marks": 1})
                .sort(self._LB_SORT)
                .limit(1))
            if below_doc:
                below = below_doc[0]
            return {"rank": rank, "above": above, "below": below}
        except Exception as e:
            logger.error(f"get_neighbor_ranks error: {e}")
            return {"rank": 0, "above": None, "below": None}

    def get_leaderboard_page(self, mode: str = "global", limit: int = 10, offset: int = 0) -> List[Dict]:
        """Return paginated leaderboard sorted by total_marks → correct_answers → quizzes_attempted → last_activity."""
        try:
            if mode in ("weekly", "monthly"):
                days   = 7 if mode == "weekly" else 30
                cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
                query  = {"last_activity": {"$gte": cutoff[:10]}}
            else:
                query = {}
            return list(
                self.users_col.find(query, {"_id": 0})
                .sort(self._LB_SORT)
                .skip(offset)
                .limit(limit)
            )
        except Exception as e:
            logger.error(f"get_leaderboard_page error: {e}")
            return []

    def get_user_achievements(self, user_id: int) -> List[Dict]:
        """Returns the user's achievements list."""
        try:
            doc = self.users_col.find_one({"user_id": user_id}, {"achievements": 1})
            if doc:
                return doc.get("achievements", [])
            return []
        except Exception as e:
            logger.error(f"get_user_achievements error: {e}")
            return []

    def get_all_users_stats(self) -> List[Dict]:
        return list(self.users_col.find({}, {"_id": 0}))

    def get_active_users_count(self, days: int = 7) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        return self.users_col.count_documents({"last_seen": {"$gte": cutoff}})

    def get_new_users(self, days: int = 7) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        return self.users_col.count_documents({"joined_at": {"$gte": cutoff}})

    def get_new_groups(self, days: int = 7) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        return self.groups_col.count_documents({"joined_at": {"$gte": cutoff}})

    def get_most_active_users(self, limit: int = 10) -> List[Dict]:
        return list(self.users_col.find({}, {"_id": 0})
                    .sort("total_answers", DESCENDING).limit(limit))

    def get_pm_accessible_users(self) -> List[Dict]:
        return list(self.users_col.find({"pm_accessible": True}, {"_id": 0}))

    def remove_inactive_user(self, user_id: int) -> bool:
        result = self.users_col.delete_one({"user_id": user_id})
        return result.deleted_count > 0

    # ── Groups ────────────────────────────────────────────────────────────────

    def upsert_group(self, chat_id: int, data: Dict):
        try:
            self.groups_col.update_one(
                {"chat_id": chat_id},
                {"$set": data, "$setOnInsert": {"joined_at": datetime.utcnow().isoformat()}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"upsert_group error: {e}")

    def get_all_groups(self) -> List[Dict]:
        return list(self.groups_col.find({}, {"_id": 0}))

    def get_registered_group_ids(self) -> set:
        """Return the set of chat_ids already in groups_col."""
        return {
            doc["chat_id"]
            for doc in self.groups_col.find({}, {"chat_id": 1, "_id": 0})
            if isinstance(doc.get("chat_id"), int)
        }

    def get_known_group_ids_from_history(self) -> set:
        """Scan activity history and auto_quiz_state for group chat_ids
        (negative integers) that have been seen before but may not be in
        groups_col — used for startup recovery."""
        ids: set = set()
        try:
            for cid in self.activities_col.distinct("chat_id"):
                if isinstance(cid, int) and cid < 0:
                    ids.add(cid)
        except Exception as e:
            logger.warning(f"history scan activities: {e}")
        try:
            for doc in self.db["auto_quiz_state"].find({}, {"chat_id": 1, "_id": 0}):
                cid = doc.get("chat_id")
                if isinstance(cid, int) and cid < 0:
                    ids.add(cid)
        except Exception as e:
            logger.warning(f"history scan auto_quiz_state: {e}")
        try:
            for doc in self.poll_map_col.find(
                {"chat_id": {"$exists": True}}, {"chat_id": 1, "_id": 0}
            ):
                cid = doc.get("chat_id")
                if isinstance(cid, int) and cid < 0:
                    ids.add(cid)
        except Exception as e:
            logger.warning(f"history scan poll_map: {e}")
        return ids

    def remove_inactive_group(self, chat_id: int) -> bool:
        result = self.groups_col.delete_one({"chat_id": chat_id})
        return result.deleted_count > 0

    # ── Developers ────────────────────────────────────────────────────────────

    def get_all_developers(self) -> List[Dict]:
        return list(self.developers_col.find({}, {"_id": 0}))

    def remove_developer(self, user_id: int) -> bool:
        result = self.developers_col.delete_one({"user_id": user_id})
        return result.deleted_count > 0

    # ── Broadcasts ────────────────────────────────────────────────────────────

    def save_broadcast(self, data: Dict) -> int:
        new_id = self._next_id("broadcasts")
        data["id"] = new_id
        data.setdefault("created_at", datetime.utcnow().isoformat())
        self.broadcasts_col.insert_one(data)
        return new_id

    def log_broadcast(self, data: Dict):
        self.save_broadcast(data)

    def get_latest_broadcast(self) -> Optional[Dict]:
        return self.broadcasts_col.find_one({}, {"_id": 0},
                                             sort=[("created_at", DESCENDING)])

    def get_broadcast_by_id(self, bid: int) -> Optional[Dict]:
        return self.broadcasts_col.find_one({"id": bid}, {"_id": 0})

    def delete_broadcast(self, bid: int) -> bool:
        result = self.broadcasts_col.delete_one({"id": bid})
        return result.deleted_count > 0

    # ── Activities ────────────────────────────────────────────────────────────

    def log_activity(self, activity_type: str = None, data: Dict = None, **kwargs):
        """Flexible log_activity: accepts log_activity(type, dict) or log_activity(activity_type=x, key=val)."""
        doc = {"type": activity_type, "timestamp": datetime.utcnow().isoformat()}
        if data and isinstance(data, dict):
            doc.update(data)
        if kwargs:
            doc.update(kwargs)
        try:
            self.activities_col.insert_one(doc)
        except Exception as e:
            logger.error(f"log_activity error: {e}")

    def get_recent_activities(self, limit: int = 50, activity_type: str = None) -> List[Dict]:
        query = {} if not activity_type else {"type": activity_type}
        return list(self.activities_col.find(query, {"_id": 0})
                    .sort("timestamp", DESCENDING).limit(limit))

    def get_user_engagement_stats(self) -> Dict:
        return {
            "total_users": self.users_col.count_documents({}),
            "active_7d": self.get_active_users_count(7),
            "active_30d": self.get_active_users_count(30)
        }

    # ── Performance ───────────────────────────────────────────────────────────

    def log_performance_metric(self, metric: str, value: float, extra: Dict = None):
        doc = {"metric": metric, "value": value,
               "timestamp": datetime.utcnow().isoformat(), **(extra or {})}
        self.performance_col.insert_one(doc)

    def get_response_time_trends(self, hours: int = 24) -> List[Dict]:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        return list(self.performance_col.find(
            {"metric": "response_time", "timestamp": {"$gte": cutoff}}, {"_id": 0}
        ).sort("timestamp", ASCENDING))

    def get_memory_usage_history(self, hours: int = 24) -> List[Dict]:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        return list(self.performance_col.find(
            {"metric": "memory", "timestamp": {"$gte": cutoff}}, {"_id": 0}
        ).sort("timestamp", ASCENDING))

    def get_api_call_counts(self, hours: int = 24) -> Dict:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        result = {}
        for row in self.activities_col.aggregate([
            {"$match": {"type": "api_call", "timestamp": {"$gte": cutoff}}},
            {"$group": {"_id": "$endpoint", "count": {"$sum": 1}}}
        ]):
            result[row["_id"]] = row["count"]
        return result

    # ── Metrics summary (used by /metrics endpoint) ───────────────────────────

    def get_metrics_summary(self) -> Dict:
        cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        cutoff_7d  = (datetime.utcnow() - timedelta(days=7)).isoformat()

        total_questions = self.questions_col.count_documents({})
        total_users     = self.users_col.count_documents({})
        total_groups    = self.groups_col.count_documents({})
        total_broadcasts= self.broadcasts_col.count_documents({})
        active_24h      = self.users_col.count_documents({"last_seen": {"$gte": cutoff_24h}})
        active_7d       = self.users_col.count_documents({"last_seen": {"$gte": cutoff_7d}})
        active_groups   = self.groups_col.count_documents({"last_active": {"$gte": cutoff_24h}})

        quiz_24h   = self.activities_col.count_documents(
            {"type": "quiz_answer", "timestamp": {"$gte": cutoff_24h}})
        correct_24h= self.activities_col.count_documents(
            {"type": "quiz_answer", "is_correct": True, "timestamp": {"$gte": cutoff_24h}})
        accuracy_24h = (correct_24h / quiz_24h * 100) if quiz_24h else 0

        commands_24h = self.activities_col.count_documents(
            {"type": "command", "timestamp": {"$gte": cutoff_24h}})
        errors_24h   = self.activities_col.count_documents(
            {"type": "error", "timestamp": {"$gte": cutoff_24h}})
        total_24h    = self.activities_col.count_documents({"timestamp": {"$gte": cutoff_24h}})
        error_rate   = (errors_24h / total_24h * 100) if total_24h else 0
        rate_limits  = self.activities_col.count_documents(
            {"type": "rate_limit", "timestamp": {"$gte": cutoff_24h}})

        # Average response time
        rt_docs = list(self.performance_col.find(
            {"metric": "response_time", "timestamp": {"$gte": cutoff_24h}}, {"value": 1}))
        avg_rt = (sum(d["value"] for d in rt_docs) / len(rt_docs)) if rt_docs else 0

        # Broadcast success rate
        bc_ok  = self.activities_col.count_documents(
            {"type": "broadcast_sent", "timestamp": {"$gte": cutoff_24h}})
        bc_fail= self.activities_col.count_documents(
            {"type": "broadcast_failed", "timestamp": {"$gte": cutoff_24h}})
        bc_total= bc_ok + bc_fail
        bc_rate = (bc_ok / bc_total * 100) if bc_total else 100

        return {
            "total_questions":       total_questions,
            "total_users":           total_users,
            "total_groups":          total_groups,
            "total_broadcasts":      total_broadcasts,
            "active_users_24h":      active_24h,
            "active_users_7d":       active_7d,
            "active_groups":         active_groups,
            "quiz_attempts_24h":     quiz_24h,
            "quiz_accuracy_24h":     round(accuracy_24h, 2),
            "commands_24h":          commands_24h,
            "error_rate_24h":        round(error_rate, 2),
            "rate_limit_violations_24h": rate_limits,
            "avg_response_time_24h": round(avg_rt, 2),
            "broadcast_success_rate": round(bc_rate, 2),
        }

    def get_analytics_data(self) -> Dict:
        """Single call that returns all stats needed for the analytics dashboard."""
        try:
            now   = datetime.utcnow()
            d_cut = (now - timedelta(hours=24)).isoformat()
            w_cut = (now - timedelta(days=7)).isoformat()
            m_cut = (now - timedelta(days=30)).isoformat()
            acts  = self.activities_col
            ucol  = self.users_col
            gcol  = self.groups_col

            # ── Users ─────────────────────────────────────────
            u_total    = ucol.count_documents({})
            u_pm       = ucol.count_documents({"pm_accessible": True})
            u_active_d = ucol.count_documents({"last_seen": {"$gte": d_cut}})
            u_active_w = ucol.count_documents({"last_seen": {"$gte": w_cut}})
            u_new_d    = ucol.count_documents({"joined_at": {"$gte": d_cut}})
            u_new_w    = ucol.count_documents({"joined_at": {"$gte": w_cut}})
            u_new_m    = ucol.count_documents({"joined_at": {"$gte": m_cut}})
            engage_rate = round(u_active_d / u_total * 100, 1) if u_total else 0

            # Most active user (by total_marks)
            top_user = ucol.find_one({}, {"name": 1, "username": 1, "user_id": 1,
                                          "total_marks": 1, "quizzes_completed": 1},
                                     sort=[("total_marks", DESCENDING)])

            # ── Groups ────────────────────────────────────────
            g_total = gcol.count_documents({})
            g_admin = gcol.count_documents({"bot_is_admin": True})
            g_new_d = gcol.count_documents({"joined_at": {"$gte": d_cut}})
            g_new_w = gcol.count_documents({"joined_at": {"$gte": w_cut}})
            g_new_m = gcol.count_documents({"joined_at": {"$gte": m_cut}})

            # Most active group (by last_active)
            top_group = gcol.find_one({}, {"title": 1, "chat_id": 1},
                                      sort=[("last_active", DESCENDING)])

            # ── Questions ─────────────────────────────────────
            q_total = self.questions_col.count_documents({})
            q_cats  = len(self.questions_col.distinct("category"))

            # ── Quiz activity (aggregation — one pass per period) ──
            def _quiz_stats(cut=None):
                match = {"type": "quiz_answer"}
                if cut:
                    match["timestamp"] = {"$gte": cut}
                pipeline = [
                    {"$match": match},
                    {"$group": {
                        "_id":       None,
                        "attempts":  {"$sum": 1},
                        "correct":   {"$sum": {"$cond": [{"$eq": ["$is_correct", True]}, 1, 0]}},
                        "players":   {"$addToSet": "$user_id"},
                    }},
                ]
                row = list(acts.aggregate(pipeline))
                if row:
                    r = row[0]
                    att = r["attempts"]
                    cor = r["correct"]
                    return {
                        "attempts": att,
                        "correct":  cor,
                        "wrong":    att - cor,
                        "accuracy": round(cor / att * 100, 1) if att else 0,
                        "players":  len(r["players"]),
                    }
                return {"attempts": 0, "correct": 0, "wrong": 0, "accuracy": 0, "players": 0}

            qs_d = _quiz_stats(d_cut)
            qs_w = _quiz_stats(w_cut)
            qs_m = _quiz_stats(m_cut)
            qs_a = _quiz_stats()

            # ── Subject breakdown ──────────────────────────────
            subj_pipe = [
                {"$match": {"type": "quiz_answer"}},
                {"$group": {
                    "_id":     "$category",
                    "attempts": {"$sum": 1},
                    "correct":  {"$sum": {"$cond": [{"$eq": ["$is_correct", True]}, 1, 0]}},
                }},
                {"$sort": {"attempts": DESCENDING}},
                {"$limit": 6},
            ]
            subj_stats = list(acts.aggregate(subj_pipe))

            return {
                "u_total": u_total, "u_pm": u_pm,
                "u_active_d": u_active_d, "u_active_w": u_active_w,
                "u_new_d": u_new_d, "u_new_w": u_new_w, "u_new_m": u_new_m,
                "engage_rate": engage_rate,
                "top_user": top_user,
                "g_total": g_total, "g_admin": g_admin,
                "g_new_d": g_new_d, "g_new_w": g_new_w, "g_new_m": g_new_m,
                "top_group": top_group,
                "q_total": q_total, "q_cats": q_cats,
                "qs_d": qs_d, "qs_w": qs_w, "qs_m": qs_m, "qs_a": qs_a,
                "subj_stats": subj_stats,
            }
        except Exception as e:
            logger.error(f"get_analytics_data error: {e}")
            return {}

    # ── Utility ───────────────────────────────────────────────────────────────


    def register_group_interaction(self, chat_id: int, thread_id=None,
                                    title: str = '', username: str = '') -> None:
        """Register/update a group. Called from every handler that observes a group.
        Sets active_status=active on every upsert."""
        now = datetime.utcnow().isoformat()
        data = {
            "chat_id":       chat_id,
            "title":         title,
            "username":      username,
            "last_active":   now,
            "last_seen":     now,
            "active_status": "active",
        }
        if thread_id:
            data["message_thread_id"] = thread_id
        self.groups_col.update_one(
            {"chat_id": chat_id},
            {"$set": data, "$setOnInsert": {"joined_at": now}},
            upsert=True
        )

    def update_group_admin_status(
        self, chat_id: int, is_admin: bool, permissions: dict = None
    ) -> None:
        """Store bot's admin status and permissions for a group."""
        data: dict = {
            "bot_is_admin":           is_admin,
            "last_permission_check":  datetime.utcnow().isoformat(),
        }
        if permissions is not None:
            data["bot_permissions"] = permissions
        self.groups_col.update_one({"chat_id": chat_id}, {"$set": data})
    def format_relative_time(self, timestamp_str: str) -> str:
        try:
            ts = datetime.fromisoformat(timestamp_str)
            delta = datetime.utcnow() - ts
            if delta.days > 0:
                return f"{delta.days}d ago"
            elif delta.seconds >= 3600:
                return f"{delta.seconds // 3600}h ago"
            elif delta.seconds >= 60:
                return f"{delta.seconds // 60}m ago"
            else:
                return "just now"
        except Exception:
            return timestamp_str

    # ── Compatibility wrappers (for dev_commands.py legacy calls) ─────────────

    def get_quiz_stats_by_period(self, period) -> Dict:
        """Accept 'today','week','month','all' strings or int days."""
        mapping = {'today': 1, 'week': 7, 'month': 30, 'all': 36500}
        if isinstance(period, str):
            days = mapping.get(period, 7)
        else:
            days = int(period) if period else 7
        cutoff  = (datetime.utcnow() - timedelta(days=days)).isoformat()
        count   = self.activities_col.count_documents(
            {"type": "quiz_answer", "timestamp": {"$gte": cutoff}})
        correct = self.activities_col.count_documents(
            {"type": "quiz_answer", "is_correct": True, "timestamp": {"$gte": cutoff}})
        return {"answers": count, "correct": correct, "period_days": days,
                "total_quizzes": count, "correct_answers": correct}

    def get_performance_summary(self, hours=24) -> Dict:
        """Accept optional hours param."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        result = {}
        for row in self.performance_col.aggregate([
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {"_id": "$metric", "avg": {"$avg": "$value"}, "count": {"$sum": 1}}}
        ]):
            result[row["_id"]] = {"avg": row["avg"], "count": row["count"]}
        return result

    def get_activity_stats(self, days=7) -> Dict:
        """Accepts int days."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        total  = self.activities_col.count_documents({"timestamp": {"$gte": cutoff}})
        by_type = {}
        for row in self.activities_col.aggregate([
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {"_id": "$type", "count": {"$sum": 1}}}
        ]):
            by_type[row["_id"]] = row["count"]
        return {"total": total, "by_type": by_type}

    def get_error_rate_stats(self, hours=24) -> Dict:
        """Accept hours param."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        errors = self.activities_col.count_documents(
            {"type": "error", "timestamp": {"$gte": cutoff}})
        total  = self.activities_col.count_documents({"timestamp": {"$gte": cutoff}})
        return {"errors": errors, "total": total,
                "rate": (errors / total * 100) if total else 0}

    def get_command_usage_stats(self, days=7) -> Dict:
        """Accept days param."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        result = {}
        for row in self.activities_col.aggregate([
            {"$match": {"type": "command", "timestamp": {"$gte": cutoff}}},
            {"$group": {"_id": "$command", "count": {"$sum": 1}}}
        ]):
            result[row["_id"]] = row["count"]
        return result

    def add_developer(self, user_id: int, username: str = "", name: str = "",
                      first_name: str = "", last_name: str = "",
                      added_by: int = None) -> bool:
        try:
            display_name = name or first_name or username or ""
            doc = {
                "username":   username,
                "name":       display_name,
                "first_name": first_name,
                "last_name":  last_name,
                "added_at":   datetime.utcnow().isoformat(),
            }
            if added_by:
                doc["added_by"] = added_by
            self.developers_col.update_one(
                {"user_id": user_id}, {"$set": doc}, upsert=True)
            return True
        except Exception as e:
            logger.error(f"add_developer error: {e}")
            return False

    def get_user(self, user_id: int) -> Optional[Dict]:
        """Get a single user document by user_id."""
        try:
            return self.users_col.find_one({"user_id": user_id}, {"_id": 0})
        except Exception as e:
            logger.error(f"get_user error: {e}")
            return None

    def get_leaderboard_by_period(self, days: int, limit: int = 10) -> List[Dict]:
        """Return top users from users_col sorted by correct_answers → quizzes_attempted → last_activity."""
        try:
            query = {}
            if days < 36500:
                cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
                query  = {"last_activity": {"$gte": cutoff}}
            sort = [
                ("correct_answers",  DESCENDING),
                ("quizzes_attempted", DESCENDING),
                ("last_activity",    DESCENDING),
            ]
            docs = list(
                self.users_col.find(query, {"_id": 0,
                    "user_id": 1, "correct_answers": 1, "total_questions": 1,
                    "quizzes_attempted": 1, "last_activity": 1})
                .sort(sort)
                .limit(limit)
            )
            results = []
            for d in docs:
                correct  = d.get("correct_answers", 0)
                total_q  = d.get("total_questions", 0) or 0
                acc      = round(correct / total_q * 100, 1) if total_q > 0 else 0
                results.append({
                    "user_id":         d["user_id"],
                    "correct_answers": correct,
                    "total_attempts":  d.get("quizzes_attempted", 0),
                    "accuracy":        acc,
                })
            return results
        except Exception as e:
            logger.error(f"get_leaderboard_by_period error: {e}")
            return []

    def get_user_rank_in_period(self, user_id: int, days: int) -> Dict:
        """Return {rank, correct, total, accuracy, above_correct} from users_col."""
        try:
            user_doc = self.users_col.find_one({"user_id": user_id},
                {"correct_answers": 1, "total_questions": 1,
                 "quizzes_attempted": 1, "last_activity": 1}) or {}

            correct = user_doc.get("correct_answers", 0)
            total_q = user_doc.get("total_questions", 0) or 0
            quizzes = user_doc.get("quizzes_attempted", 0)
            acc     = round(correct / total_q * 100, 1) if total_q > 0 else 0

            if quizzes == 0:
                return {"rank": 0, "correct": 0, "total": 0,
                        "accuracy": 0, "above_correct": None}

            query = {}
            if days < 36500:
                cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
                query  = {"last_activity": {"$gte": cutoff}}

            query["correct_answers"] = {"$gt": correct}
            higher = self.users_col.count_documents(query)

            above_doc = self.users_col.find_one(
                {**query},
                {"correct_answers": 1},
                sort=[("correct_answers", ASCENDING)])
            above_correct = above_doc.get("correct_answers") if above_doc else None

            return {
                "rank":          higher + 1,
                "correct":       correct,
                "total":         quizzes,
                "accuracy":      acc,
                "above_correct": above_correct,
            }
        except Exception as e:
            logger.error(f"get_user_rank_in_period error: {e}")
            return {"rank": 0, "correct": 0, "total": 0,
                    "accuracy": 0, "above_correct": None}
