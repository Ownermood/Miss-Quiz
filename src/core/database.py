"""
MongoDB DatabaseManager for Telegram Quiz Bot
"""

import logging
import os
import math
from typing import Optional, List, Dict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

try:
    from pymongo import MongoClient, ASCENDING, DESCENDING
    from pymongo.errors import BulkWriteError
    PYMONGO_AVAILABLE = True
except ImportError:
    PYMONGO_AVAILABLE = False
    ASCENDING  = 1
    DESCENDING = -1
    logger.error("pymongo not installed. Run: pip install pymongo")


def redact_url(url: str) -> str:
    """Hide the password in a connection URL before logging it."""
    import re
    return re.sub(r"(//[^:/@]+):[^@]*@", r"\1:***@", url or "")


class DatabaseManager:
    def __init__(self, mongo_url: Optional[str] = None):
        if not PYMONGO_AVAILABLE:
            raise RuntimeError("pymongo is required. Run: pip install pymongo")

        url = mongo_url or os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("MONGODB_DB", "quiz_bot")

        self.client = MongoClient(
            url,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=90000,
        )
        # Ping to verify connection
        self.client.admin.command('ping')
        self.db = self.client[db_name]

        self.questions_col       = self.db["questions"]
        self.users_col           = self.db["users"]
        self.groups_col          = self.db["groups"]
        self.broadcasts_col      = self.db["broadcasts"]
        self.activities_col      = self.db["activities"]
        self.developers_col      = self.db["developers"]
        self.poll_map_col        = self.db["poll_map"]
        self.auto_quiz_state_col = self.db["auto_quiz_state"]
        self.settings_col        = self.db["settings"]
        self.battles_col         = self.db["battles"]      # open battle challenges (survive restart)
        self.quiz_sets_col       = self.db["quiz_sets"]    # users' own quizzes (private, not in the bot pool)

        self._ensure_indexes()
        logger.info(f"✅ MongoDB connected: {redact_url(url)} / db={db_name}")

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
            # Compound index for ranking query
            self.users_col.create_index([
                ("total_marks", DESCENDING),
                ("correct_answers", DESCENDING),
                ("quizzes_attempted", DESCENDING),
            ])
            self.groups_col.create_index("chat_id", unique=True)
            self.groups_col.create_index([("last_active", DESCENDING)])
            # Compound index for per-group quiz schedule queries
            self.groups_col.create_index([
                ("active_status",    ASCENDING),
                ("next_quiz_due_at", ASCENDING),
            ])
            self.auto_quiz_state_col.create_index("chat_id", unique=True)
            self.poll_map_col.create_index("poll_id", unique=True)
            self.poll_map_col.create_index("created_at")      # daily purge
            self.activities_col.create_index("timestamp")     # daily purge
            # Compound indexes for time-based activity queries
            self.activities_col.create_index([("type", ASCENDING), ("timestamp", DESCENDING)])
            self.activities_col.create_index([("type", ASCENDING), ("is_correct", ASCENDING), ("timestamp", DESCENDING)])
            self.activities_col.create_index([("type", ASCENDING), ("user_id", ASCENDING), ("timestamp", DESCENDING)])
            self.activities_col.create_index([("timestamp", DESCENDING)])
            self.broadcasts_col.create_index([("created_at", DESCENDING)])
            self.battles_col.create_index("id", unique=True)
            self.battles_col.create_index("p1")
            self.battles_col.create_index("expires_at", expireAfterSeconds=0)   # Mongo auto-deletes
            self.quiz_sets_col.create_index("id", unique=True)
            self.quiz_sets_col.create_index([("owner", ASCENDING), ("created_at", DESCENDING)])
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

    def _next_id_batch(self, name: str, count: int) -> int:
        """Atomically reserve `count` sequential IDs in one round trip.
        Returns the first ID of the allocated block."""
        result = self.db["_counters"].find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": count}},
            upsert=True,
            return_document=True,
        )
        return result["seq"] - count + 1

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

    def add_questions_batch(self, questions: List[Dict]) -> tuple:
        """Bulk-insert questions in a single MongoDB round trip.
        Returns (inserted_count, [(batch_index, new_id), ...], errors_list).
        Uses ordered=False so a bad document doesn't abort the whole batch;
        on partial failure only the pairs of documents that were actually
        inserted are returned (failed positions come from writeErrors[].index)."""
        if not questions:
            return 0, [], []
        n = len(questions)
        try:
            first_id = self._next_id_batch("questions", n)
        except Exception as e:
            logger.error(f"add_questions_batch: counter allocation failed: {e}")
            return 0, [], [str(e)]

        now  = datetime.utcnow().isoformat()
        docs    = []
        new_ids = []
        for i, q in enumerate(questions):
            qid = first_id + i
            docs.append({
                "id":             qid,
                "question":       q.get("question", ""),
                "options":        q.get("options", []),
                "correct_answer": q.get("correct_answer", 0),
                "category":       q.get("category", "General"),
                "created_at":     now,
            })
            if q.get("explanation"):
                docs[-1]["explanation"] = q["explanation"]
            new_ids.append((i, qid))

        try:
            self.questions_col.insert_many(docs, ordered=False)
            return n, new_ids, []
        except BulkWriteError as exc:
            write_errors = exc.details.get("writeErrors", [])
            failed = {e.get("index") for e in write_errors}
            ok     = [pair for pair in new_ids if pair[0] not in failed]
            errs   = [str(e) for e in write_errors[:5]]
            logger.error(f"add_questions_batch: partial write inserted={len(ok)}/{n}: {exc}")
            return len(ok), ok, errs
        except Exception as exc:
            logger.error(f"add_questions_batch: insert_many failed: {exc}")
            return 0, [], [str(exc)]

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

    def record_battle_result(self, p1: int, p2: int, winner: Optional[int]) -> None:
        """Increment battle_wins / battle_losses / battle_draws on existing users."""
        if winner is None:
            updates = {p1: "battle_draws", p2: "battle_draws"}
        else:
            loser = p2 if winner == p1 else p1
            updates = {winner: "battle_wins", loser: "battle_losses"}
        for uid, fld in updates.items():
            self.users_col.update_one({"user_id": uid}, {"$inc": {fld: 1, "battles_played": 1}})

    # ── Users' own quizzes ──
    def create_quiz_set(self, owner: int, title: str, questions: List[Dict], secs: int = 20) -> str:
        import secrets
        sid = secrets.token_hex(4)
        self.quiz_sets_col.insert_one({
            "id": sid, "owner": int(owner), "title": str(title)[:40], "questions": questions,
            "count": len(questions), "secs": int(secs), "plays": 0,
            "created_at": datetime.utcnow().isoformat(),
        })
        return sid

    def get_quiz_set(self, sid: str) -> Optional[Dict]:
        return self.quiz_sets_col.find_one({"id": str(sid)}, {"_id": 0})

    def list_quiz_sets(self, owner: int, limit: int = 20) -> List[Dict]:
        return list(self.quiz_sets_col.find(
            {"owner": int(owner)}, {"_id": 0, "id": 1, "title": 1, "count": 1, "secs": 1, "created_at": 1}
        ).sort("created_at", DESCENDING).limit(limit))

    def count_quiz_sets(self, owner: int) -> int:
        return self.quiz_sets_col.count_documents({"owner": int(owner)})

    def delete_quiz_set(self, sid: str, owner: int) -> bool:
        return self.quiz_sets_col.delete_one({"id": str(sid), "owner": int(owner)}).deleted_count == 1

    # ── Open battle challenges (setup/pending) ──
    def save_battle(self, doc: Dict) -> None:
        self.battles_col.replace_one({"id": doc["id"]}, doc, upsert=True)

    def get_battle(self, bid: str) -> Optional[Dict]:
        return self.battles_col.find_one({"id": bid}, {"_id": 0})

    def delete_battle(self, bid: str) -> None:
        self.battles_col.delete_one({"id": bid})

    def open_battles_of(self, user_id: int) -> List[Dict]:
        return list(self.battles_col.find({"p1": user_id}, {"_id": 0, "id": 1}).limit(20))

    def get_battle_pref(self, user_id: int) -> Optional[tuple]:
        """Last custom battle settings (n_q, secs) or None."""
        doc = self.users_col.find_one({"user_id": user_id}, {"battle_pref": 1}) or {}
        pref = doc.get("battle_pref") or {}
        n, s = pref.get("n_q"), pref.get("secs")
        return (int(n), int(s)) if isinstance(n, int) and isinstance(s, int) else None

    def set_battle_pref(self, user_id: int, n_q: int, secs: int) -> None:
        self.users_col.update_one({"user_id": user_id},
                                  {"$set": {"battle_pref": {"n_q": int(n_q), "secs": int(secs)}}})

    def upsert_user(self, user_id: int, data: Dict):
        try:
            defaults = {
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
            # A field may not be in both $set and $setOnInsert (MongoDB error 40,
            # which silently dropped every NEW user). $set wins.
            on_insert = {k: v for k, v in defaults.items() if k not in data}
            self.users_col.update_one(
                {"user_id": user_id},
                {"$set": data, "$setOnInsert": on_insert},
                upsert=True,
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

    def get_pm_accessible_users(self) -> List[Dict]:
        return list(self.users_col.find({"pm_accessible": True}, {"_id": 0}))

    def purge_old_data(self, activity_days: int = 30, poll_days: int = 7) -> Dict[str, int]:
        """Delete answer logs and poll mappings that are no longer needed, so
        storage doesn't grow forever (~1 log per answer). Timestamps are ISO
        strings, which compare correctly as text (a TTL index needs Date)."""
        now = datetime.utcnow()
        a_cut = (now - timedelta(days=activity_days)).isoformat()
        p_cut = (now - timedelta(days=poll_days)).isoformat()
        return {
            "activities": self.activities_col.delete_many({"timestamp": {"$lt": a_cut}}).deleted_count,
            "poll_map":   self.poll_map_col.delete_many({"created_at": {"$lt": p_cut}}).deleted_count,
        }

    def remove_inactive_user(self, user_id: int) -> bool:
        """User blocked the bot / deleted account: stop DMing them but KEEP
        their scores and history (used to delete the whole user record).
        pm_accessible turns True again when they /start the bot in DM."""
        result = self.users_col.update_one(
            {"user_id": user_id},
            {"$set": {"pm_accessible": False, "dm_blocked_at": datetime.utcnow().isoformat()}})
        return result.matched_count > 0

    # ── Groups ────────────────────────────────────────────────────────────────

    def get_all_groups(self) -> List[Dict]:
        """Return every group record regardless of active_status.
        Used for admin auditing. For delivery use get_active_groups()."""
        return list(self.groups_col.find({}, {"_id": 0}))

    def get_active_groups(self) -> List[Dict]:
        """Return only groups where the bot is currently active (not kicked/blocked)."""
        return list(self.groups_col.find(
            {"active_status": {"$ne": "inactive"}}, {"_id": 0}
        ))

    def get_groups_due_for_quiz(self) -> List[Dict]:
        """Return active groups whose next_quiz_due_at has arrived.
        Groups where the bot is known NOT to be admin are excluded; groups with
        unknown status (bot_is_admin missing) are returned so the scheduler can
        check once via getChatMember and store the answer."""
        now = datetime.utcnow().isoformat()
        return list(self.groups_col.find(
            {
                "active_status":    {"$ne":  "inactive"},
                "next_quiz_due_at": {"$lte": now},
                "bot_is_admin":     {"$ne":  False},
            },
            {"_id": 0}
        ))

    def update_group_quiz_schedule(self, chat_id: int, interval_minutes: int) -> None:
        """Advance a group's schedule after a quiz is delivered (or skipped).
        last_quiz_sent_at = now, next_quiz_due_at = now + interval."""
        now      = datetime.utcnow()
        next_due = (now + timedelta(minutes=interval_minutes)).isoformat()
        try:
            self.groups_col.update_one(
                {"chat_id": chat_id},
                {"$set": {
                    "last_quiz_sent_at": now.isoformat(),
                    "next_quiz_due_at":  next_due,
                }}
            )
        except Exception as e:
            logger.error(f"update_group_quiz_schedule {chat_id}: {e}")

    def backfill_group_schedules(self, interval_minutes: int = 30) -> int:
        """One-time migration: give every existing group an initial next_quiz_due_at.
        Sets it to NOW so they are all served in the first scheduler poll after upgrade.
        Groups added after this commit are seeded by register_group_interaction."""
        now = datetime.utcnow().isoformat()
        try:
            result = self.groups_col.update_many(
                {"next_quiz_due_at": {"$exists": False}},
                {"$set": {
                    "quiz_start_time":   now,
                    "next_quiz_due_at":  now,   # due immediately
                    "last_quiz_sent_at": None,
                }}
            )
            return result.modified_count
        except Exception as e:
            logger.error(f"backfill_group_schedules: {e}")
            return 0

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

    def set_admin_profile(self, user_id: int, title: str = "", emoji: str = "",
                          emoji_id: str = "") -> None:
        """Custom title + emoji (premium custom_emoji_id optional) shown for an admin."""
        self.developers_col.update_one(
            {"user_id": user_id},
            {"$set": {"title": title, "emoji": emoji, "emoji_id": emoji_id}})

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

    def get_user_engagement_stats(self) -> Dict:
        return {
            "total_users": self.users_col.count_documents({}),
            "active_7d": self.get_active_users_count(7),
            "active_30d": self.get_active_users_count(30)
        }

    def get_user_analytics(self) -> Dict:
        """User statistics for the analytics dashboard."""
        try:
            now   = datetime.utcnow()
            d_cut = (now - timedelta(hours=24)).isoformat()
            w_cut = (now - timedelta(days=7)).isoformat()
            m_cut = (now - timedelta(days=30)).isoformat()
            ucol  = self.users_col
            u_total    = ucol.count_documents({})
            u_pm       = ucol.count_documents({"pm_accessible": True})
            u_active_d = ucol.count_documents({"last_seen": {"$gte": d_cut}})
            u_active_w = ucol.count_documents({"last_seen": {"$gte": w_cut}})
            u_new_d    = ucol.count_documents({"joined_at": {"$gte": d_cut}})
            u_new_w    = ucol.count_documents({"joined_at": {"$gte": w_cut}})
            u_new_m    = ucol.count_documents({"joined_at": {"$gte": m_cut}})
            return {
                "u_total":    u_total,    "u_pm":       u_pm,
                "u_active_d": u_active_d, "u_active_w": u_active_w,
                "u_new_d":    u_new_d,    "u_new_w":    u_new_w,
                "u_new_m":    u_new_m,
            }
        except Exception as e:
            logger.error(f"get_user_analytics error: {e}")
            return {}

    def get_group_analytics(self) -> Dict:
        """Group statistics for the analytics dashboard."""
        try:
            now   = datetime.utcnow()
            d_cut = (now - timedelta(hours=24)).isoformat()
            w_cut = (now - timedelta(days=7)).isoformat()
            m_cut = (now - timedelta(days=30)).isoformat()
            gcol  = self.groups_col
            _af   = {"active_status": {"$ne": "inactive"}}
            return {
                "g_total": gcol.count_documents(_af),
                "g_admin":  gcol.count_documents({**_af, "bot_is_admin": True}),
                "g_paused": gcol.count_documents({**_af, "bot_is_admin": False}),
                "g_new_d": gcol.count_documents({**_af, "joined_at": {"$gte": d_cut}}),
                "g_new_w": gcol.count_documents({**_af, "joined_at": {"$gte": w_cut}}),
                "g_new_m": gcol.count_documents({**_af, "joined_at": {"$gte": m_cut}}),
            }
        except Exception as e:
            logger.error(f"get_group_analytics error: {e}")
            return {}

    def get_activity_analytics(self) -> Dict:
        """Live activity from the activities log: answers + accuracy, last 24h / 1h."""
        try:
            now = datetime.utcnow()
            d_cut = (now - timedelta(hours=24)).isoformat()
            h_cut = (now - timedelta(hours=1)).isoformat()
            acol = self.activities_col
            q = {"type": "quiz_answer"}
            return {
                "a_day":     acol.count_documents({**q, "timestamp": {"$gte": d_cut}}),
                "a_day_ok":  acol.count_documents({**q, "timestamp": {"$gte": d_cut}, "is_correct": True}),
                "a_hour":    acol.count_documents({**q, "timestamp": {"$gte": h_cut}}),
                "a_players": len(acol.distinct("user_id", {**q, "timestamp": {"$gte": d_cut}})),
            }
        except Exception as e:
            logger.error(f"get_activity_analytics error: {e}")
            return {}

    def get_content_analytics(self) -> Dict:
        """Question/category counts for the analytics dashboard."""
        try:
            return {
                "q_cats": len(self.questions_col.distinct("category")),
            }
        except Exception as e:
            logger.error(f"get_content_analytics error: {e}")
            return {}

    # ── Auto Quiz State ───────────────────────────────────────────────────────

    def get_active_quiz_state(self, chat_id: int) -> Optional[Dict]:
        """Return the active quiz state document for a group, or None."""
        try:
            return self.auto_quiz_state_col.find_one({"chat_id": chat_id}, {"_id": 0})
        except Exception as e:
            logger.error(f"get_active_quiz_state error: {e}")
            return None

    def get_all_active_quiz_states(self) -> List[Dict]:
        """Return active quiz state for all groups (used on startup)."""
        try:
            return list(self.auto_quiz_state_col.find({}, {"_id": 0}))
        except Exception as e:
            logger.error(f"get_all_active_quiz_states error: {e}")
            return []

    def save_active_quiz(self, chat_id: int, message_id: int,
                         quiz_id: int = None, poll_id: str = None) -> None:
        """Persist the active quiz state for a group."""
        try:
            doc: Dict = {
                "chat_id":    chat_id,
                "message_id": message_id,
                "sent_time":  datetime.utcnow().isoformat(),
            }
            if quiz_id is not None:
                doc["quiz_id"] = quiz_id
            if poll_id is not None:
                doc["poll_id"] = poll_id
            self.auto_quiz_state_col.update_one(
                {"chat_id": chat_id},
                {"$set": doc},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"save_active_quiz error: {e}")

    def clear_active_quiz(self, chat_id: int) -> None:
        """Remove the active quiz state for a group (bot blocked / group inactive)."""
        try:
            self.auto_quiz_state_col.delete_one({"chat_id": chat_id})
        except Exception as e:
            logger.error(f"clear_active_quiz error: {e}")

    # ── Utility ───────────────────────────────────────────────────────────────


    def register_group_interaction(self, chat_id: int, thread_id=None,
                                    title: str = '', username: str = '',
                                    quiz_interval_minutes: int = 30) -> None:
        """Register/update a group. Called from every handler that observes a group.
        Sets active_status=active on every upsert.
        On first insert only: seeds quiz_start_time and next_quiz_due_at so the
        group enters the per-group scheduler immediately."""
        now      = datetime.utcnow()
        now_iso  = now.isoformat()
        next_due = (now + timedelta(minutes=quiz_interval_minutes)).isoformat()
        data = {
            "chat_id":       chat_id,
            "title":         title,
            "username":      username,
            "last_active":   now_iso,
            "last_seen":     now_iso,
            "active_status": "active",
        }
        if thread_id:
            data["message_thread_id"] = thread_id
        self.groups_col.update_one(
            {"chat_id": chat_id},
            {
                "$set": data,
                "$setOnInsert": {
                    "joined_at":        now_iso,
                    "quiz_start_time":  now_iso,
                    "next_quiz_due_at": next_due,
                    "last_quiz_sent_at": None,
                },
            },
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
        update: dict = {"$set": data}
        if is_admin:
            # promoted: reset reminders so a future demotion starts fresh
            update["$unset"] = {"admin_reminder_count": "", "admin_reminder_at": ""}
        self.groups_col.update_one({"chat_id": chat_id}, update)

    def get_setting(self, key: str, default=None):
        doc = self.settings_col.find_one({"key": key}, {"_id": 0, "value": 1})
        return doc["value"] if doc else default

    def set_setting(self, key: str, value) -> None:
        """value=None deletes the setting."""
        if value is None:
            self.settings_col.delete_one({"key": key})
        else:
            self.settings_col.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

    def count_groups_by_admin(self) -> Dict[str, int]:
        """Active groups total and how many have the bot as admin."""
        base = {"active_status": {"$ne": "inactive"}}
        return {
            "total": self.groups_col.count_documents(base),
            "admin": self.groups_col.count_documents({**base, "bot_is_admin": True}),
        }

    def clear_group_thread(self, chat_id: int, thread_id: int) -> None:
        """Forget a closed/deleted topic (only if it is still the saved one)."""
        self.groups_col.update_one({"chat_id": chat_id, "message_thread_id": thread_id},
                                   {"$unset": {"message_thread_id": ""}})

    def mark_admin_reminder(self, chat_id: int) -> None:
        """Record that a 'make me admin' alert was sent to this group."""
        self.groups_col.update_one(
            {"chat_id": chat_id},
            {"$set": {"admin_reminder_at": datetime.utcnow().isoformat()},
             "$inc": {"admin_reminder_count": 1}},
        )

    def get_groups_needing_admin_reminder(self, gap_hours: int,
                                          max_count: Optional[int] = None) -> List[Dict]:
        """Active groups where the bot is known not to be admin and that got no
        alert in the last gap_hours (and fewer than max_count alerts, if set)."""
        cutoff = (datetime.utcnow() - timedelta(hours=gap_hours)).isoformat()
        conds = [{"$or": [{"admin_reminder_at": {"$exists": False}},
                          {"admin_reminder_at": {"$lte": cutoff}}]}]
        if max_count:
            conds.append({"$or": [{"admin_reminder_count": {"$exists": False}},
                                  {"admin_reminder_count": {"$lt": max_count}}]})
        return list(self.groups_col.find(
            {
                "active_status": {"$ne": "inactive"},
                "bot_is_admin":  False,
                "$and": conds,
            },
            {"_id": 0, "chat_id": 1, "message_thread_id": 1},
        ))

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
