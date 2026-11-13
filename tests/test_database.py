"""Tests for DatabaseManager helpers that don't need a live MongoDB."""

from unittest.mock import MagicMock
from pymongo.errors import BulkWriteError
from src.core.database import DatabaseManager


def _db():
    db = DatabaseManager.__new__(DatabaseManager)
    db.questions_col = MagicMock()
    db._next_id_batch = MagicMock(return_value=100)
    return db


QS = [{"question": f"Q{i}?", "options": ["A", "B"], "correct_answer": 0} for i in range(3)]


def test_batch_all_inserted():
    db = _db()
    count, pairs, errs = db.add_questions_batch(QS)
    assert (count, pairs, errs) == (3, [(0, 100), (1, 101), (2, 102)], [])


def test_batch_partial_failure_skips_failed_index():
    db = _db()
    db.questions_col.insert_many.side_effect = BulkWriteError({
        "nInserted": 2,
        "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
    })
    count, pairs, errs = db.add_questions_batch(QS)
    assert count == 2
    assert pairs == [(1, 101), (2, 102)]   # index 0 failed, not index 2
    assert len(errs) == 1


def test_batch_empty():
    assert _db().add_questions_batch([]) == (0, [], [])


def test_upsert_user_never_puts_a_field_in_set_and_setoninsert():
    """MongoDB rejects that with error 40 — every NEW user was silently lost."""
    db = DatabaseManager.__new__(DatabaseManager)
    db.users_col = MagicMock()
    db.upsert_user(5, {"user_id": 5, "active_status": "active", "first_name": "A"})
    update = db.users_col.update_one.call_args.args[1]
    overlap = set(update["$set"]) & set(update["$setOnInsert"])
    assert not overlap
    assert update["$set"]["active_status"] == "active"           # caller's value kept
    assert update["$setOnInsert"]["xp"] == 0                      # other defaults still seeded
    assert db.users_col.update_one.call_args.kwargs["upsert"] is True


def test_battle_pref_roundtrip():
    import mongomock
    db = _db()
    db.users_col = mongomock.MongoClient().db.users
    db.users_col.insert_one({"user_id": 7})
    assert db.get_battle_pref(7) is None and db.get_battle_pref(999) is None
    db.set_battle_pref(7, 12, 45)
    assert db.get_battle_pref(7) == (12, 45)


def test_group_title_not_wiped_by_callers_without_a_name():
    import mongomock
    db = DatabaseManager.__new__(DatabaseManager)
    db.groups_col = mongomock.MongoClient().db.groups
    db.register_group_interaction(-1, title="CLAT QUIZ BATALIAN", username="clatq")
    db.register_group_interaction(-1, title="", username="")          # e.g. from a poll answer
    g = db.groups_col.find_one({"chat_id": -1})
    assert g["title"] == "CLAT QUIZ BATALIAN" and g["username"] == "clatq"
    db.register_group_interaction(-2)                                  # first sight without a name
    assert db.groups_col.find_one({"chat_id": -2})["title"] == ""
    db.register_group_interaction(-2, title="New Name")               # name learnt later
    assert db.groups_col.find_one({"chat_id": -2})["title"] == "New Name"


async def test_startup_fills_missing_group_titles():
    import mongomock
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.bot.handlers_main import TelegramQuizBot
    db = DatabaseManager.__new__(DatabaseManager)
    db.groups_col = mongomock.MongoClient().db.groups
    db.groups_col.insert_many([{"chat_id": -1, "title": "", "active_status": "active"},
                               {"chat_id": -2, "title": "Kept", "active_status": "active"}])
    b = TelegramQuizBot(MagicMock(), db_manager=db)
    b.application = MagicMock()
    b.application.bot.get_chat = AsyncMock(return_value=SimpleNamespace(title="CLATIANS CV", username=None))
    assert await b._fill_missing_group_titles() == 1
    assert db.groups_col.find_one({"chat_id": -1})["title"] == "CLATIANS CV"
    b.application.bot.get_chat.assert_awaited_once()                    # named group untouched


def _real_db():
    import mongomock
    db = DatabaseManager.__new__(DatabaseManager)
    db.db = mongomock.MongoClient().quizdb
    db.questions_col = db.db.questions
    db.questions_col.create_index("id", unique=True)
    return db


def test_batch_counter_behind_existing_ids_still_saves_everything():
    """Live bug: counter was behind max(id) → 273 of 380 PDF questions rejected with E11000."""
    db = _real_db()
    db.questions_col.insert_many([{"id": i, "question": f"old {i}"} for i in range(1, 501)])
    db.db["_counters"].insert_one({"_id": "questions", "seq": 100})       # behind by 400
    qs = [{"question": f"new {k}", "options": ["a", "b"], "correct_answer": 0} for k in range(380)]
    count, pairs, errs = db.add_questions_batch(qs)
    assert (count, errs) == (380, [])
    assert [i for i, _ in pairs] == list(range(380))
    ids = [qid for _, qid in pairs]
    assert len(set(ids)) == 380 and min(ids) == 501
    assert db.questions_col.count_documents({}) == 880
    assert db.questions_col.find_one({"id": 50})["question"] == "old 50"   # old ones untouched


def test_batch_error_count_is_not_capped():
    db = _real_db()
    db.questions_col.insert_one({"id": 1, "question": "x"})
    bad = [{"question": f"q{k}", "options": ["a", "b"], "correct_answer": 0} for k in range(12)]
    orig = db.questions_col.insert_many

    def boom(docs, ordered=False):
        raise BulkWriteError({"nInserted": 0, "writeErrors": [
            {"index": k, "code": 121, "errmsg": "validation"} for k in range(len(docs))]})
    db.questions_col.insert_many = boom
    count, pairs, errs = db.add_questions_batch(bad)
    db.questions_col.insert_many = orig
    assert count == 0 and pairs == [] and len(errs) == 12
