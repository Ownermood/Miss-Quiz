"""Tests for AutoQuizScheduler delivery: send-then-delete and error handling."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram.error import RetryAfter, TimedOut, BadRequest, Forbidden

from src.utils.scheduler import AutoQuizScheduler

CHAT = -100123
OLD_MSG = 11


def make_scheduler(send_side_effect=None, admin=True, member_status="administrator"):
    db = MagicMock()
    db.get_all_active_quiz_states.return_value = [
        {"chat_id": CHAT, "message_id": OLD_MSG, "quiz_id": 1, "poll_id": "p-old"}
    ]
    group = {"chat_id": CHAT}
    if admin is not None:
        group["bot_is_admin"] = admin
    db.get_groups_due_for_quiz.return_value = [group]
    qm = MagicMock()
    qm.questions = [1]
    qm.get_random_question.return_value = {
        "id": 7, "question": "Q?", "options": ["A", "B"], "correct_answer": 1, "category": "GK"}
    bot = MagicMock()
    tg = bot.application.bot
    calls = []
    tg.delete_message = AsyncMock(side_effect=lambda **kw: calls.append(("delete", kw["message_id"])))

    async def _send(**kw):
        calls.append(("send", None))
        if send_side_effect:
            raise send_side_effect
        m = MagicMock(); m.message_id = 99; m.poll.id = "p-new"
        return m
    tg.send_poll = AsyncMock(side_effect=_send)
    tg.get_chat_member = AsyncMock(return_value=MagicMock(status=member_status))
    bot.application.bot_data = {}
    sch = AutoQuizScheduler(bot, qm, db_manager=db, interval_minutes=60)
    return sch, db, calls


async def test_new_quiz_sent_before_old_deleted():
    sch, db, calls = make_scheduler()
    await sch._do_send_auto_quiz()
    assert calls == [("send", None), ("delete", OLD_MSG)]
    db.save_active_quiz.assert_called_once()
    assert db.save_active_quiz.call_args.kwargs["message_id"] == 99
    db.update_group_quiz_schedule.assert_called_with(CHAT, 60)


async def test_failed_send_keeps_old_quiz():
    sch, db, calls = make_scheduler(send_side_effect=TimedOut())
    await sch._do_send_auto_quiz()
    assert ("delete", OLD_MSG) not in calls
    db.update_group_quiz_schedule.assert_called_with(CHAT, AutoQuizScheduler.TRANSIENT_RETRY_MIN)


async def test_flood_limit_retries_after_telegram_delay():
    sch, db, _ = make_scheduler(send_side_effect=RetryAfter(timedelta(seconds=30)))
    await sch._do_send_auto_quiz()
    minutes = db.update_group_quiz_schedule.call_args.args[1]
    assert 0.9 <= minutes < 5


async def test_chat_not_found_marks_inactive_not_retry():
    sch, db, _ = make_scheduler(send_side_effect=BadRequest("Chat not found"))
    await sch._do_send_auto_quiz()
    db.groups_col.update_one.assert_called_once()   # _mark_group_inactive
    db.update_group_quiz_schedule.assert_not_called()


async def test_kicked_marks_inactive():
    sch, db, _ = make_scheduler(send_side_effect=Forbidden("bot was kicked from the group chat"))
    await sch._do_send_auto_quiz()
    db.groups_col.update_one.assert_called_once()
    db.update_group_quiz_schedule.assert_not_called()


async def test_other_bad_request_waits_full_interval():
    sch, db, _ = make_scheduler(send_side_effect=BadRequest("Poll question length must be ..."))
    await sch._do_send_auto_quiz()
    db.update_group_quiz_schedule.assert_called_with(CHAT, 60)


# ── bot must be admin ──────────────────────────────────────────────────────

async def test_known_non_admin_group_gets_no_quiz():
    sch, db, calls = make_scheduler(admin=False)
    await sch._do_send_auto_quiz()
    assert calls == []
    sch.bot.application.bot.get_chat_member.assert_not_awaited()


async def test_unknown_status_checked_once_and_admin_gets_quiz():
    sch, db, calls = make_scheduler(admin=None, member_status="administrator")
    await sch._do_send_auto_quiz()
    assert ("send", None) in calls
    db.update_group_admin_status.assert_called_once_with(CHAT, True)


async def test_unknown_status_member_paused_and_stored():
    sch, db, calls = make_scheduler(admin=None, member_status="member")
    await sch._do_send_auto_quiz()
    assert calls == []
    db.update_group_admin_status.assert_called_once_with(CHAT, False)


async def test_creator_counts_as_admin():
    sch, db, calls = make_scheduler(admin=None, member_status="creator")
    await sch._do_send_auto_quiz()
    assert ("send", None) in calls


def test_due_query_excludes_known_non_admin_groups():
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    db.groups_col = MagicMock()
    db.groups_col.find.return_value = []
    db.get_groups_due_for_quiz()
    assert db.groups_col.find.call_args.args[0]["bot_is_admin"] == {"$ne": False}


# ── topic (forum) groups ───────────────────────────────────────────────────

def _topic_sched(first_error=None):
    from telegram.error import BadRequest
    sch, db, calls = make_scheduler()
    db.get_groups_due_for_quiz.return_value = [{"chat_id": CHAT, "bot_is_admin": True, "message_thread_id": 42}]
    seen = []

    async def _send(**kw):
        seen.append(kw.get("message_thread_id"))
        if kw.get("message_thread_id") == 42 and first_error:
            raise BadRequest(first_error)
        m = MagicMock(); m.message_id = 99; m.poll.id = "p-new"
        return m
    sch.bot.application.bot.send_poll = AsyncMock(side_effect=_send)
    return sch, db, seen


async def test_quiz_goes_to_the_open_topic():
    sch, db, seen = _topic_sched()
    await sch._do_send_auto_quiz()
    assert seen == [42]
    db.clear_group_thread.assert_not_called()


@pytest.mark.parametrize("err", ["Topic_closed", "Message thread not found", "Topic_deleted"])
async def test_closed_topic_falls_back_to_main_topic(err):
    sch, db, seen = _topic_sched(first_error=err)
    await sch._do_send_auto_quiz()
    assert seen == [42, None]                                 # retried in General
    db.clear_group_thread.assert_called_once_with(CHAT, 42)
    db.groups_col.update_one.assert_not_called()              # group NOT marked inactive
    db.update_group_quiz_schedule.assert_called_with(CHAT, 60)


async def test_other_errors_do_not_trigger_topic_fallback():
    sch, db, seen = _topic_sched(first_error="Poll options must be non-empty")
    await sch._do_send_auto_quiz()
    assert seen == [42]
    db.clear_group_thread.assert_not_called()


def test_clear_group_thread_only_if_still_saved():
    mongomock = pytest.importorskip("mongomock")
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    db.groups_col = mongomock.MongoClient().db.g
    db.groups_col.insert_one({"chat_id": 1, "message_thread_id": 7})
    db.clear_group_thread(1, 99)                # someone already moved to topic 7
    assert db.groups_col.find_one({"chat_id": 1})["message_thread_id"] == 7
    db.clear_group_thread(1, 7)
    assert "message_thread_id" not in db.groups_col.find_one({"chat_id": 1})


async def test_housekeeping_purges_db_and_old_polls_in_memory():
    import time as _t
    sch, db, _ = make_scheduler()
    db.purge_old_data.return_value = {"activities": 5, "poll_map": 2}
    bd = sch.bot.application.bot_data = {
        "poll_old": {"ts": _t.time() - 10 * 86400}, "poll_new": {"ts": _t.time()},
        "poll_nots": {}, "other_key": 1}
    await sch._housekeeping()
    db.purge_old_data.assert_called_once_with(30, 7)
    assert "poll_old" not in bd and {"poll_new", "poll_nots", "other_key"} <= set(bd)


def test_purge_old_data_semantics():
    mongomock = pytest.importorskip("mongomock")
    from datetime import datetime, timedelta
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    c = mongomock.MongoClient().db
    db.activities_col, db.poll_map_col = c.a, c.p
    iso = lambda d: (datetime.utcnow() - timedelta(days=d)).isoformat()
    db.activities_col.insert_many([{"timestamp": iso(40)}, {"timestamp": iso(1)}])
    db.poll_map_col.insert_many([{"created_at": iso(8)}, {"created_at": iso(1)}])
    assert db.purge_old_data(30, 7) == {"activities": 1, "poll_map": 1}
    assert db.activities_col.count_documents({}) == 1 and db.poll_map_col.count_documents({}) == 1


def test_blocked_user_keeps_data():
    mongomock = pytest.importorskip("mongomock")
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    db.users_col = mongomock.MongoClient().db.u
    db.users_col.insert_one({"user_id": 1, "pm_accessible": True, "xp": 500})
    assert db.remove_inactive_user(1) is True
    doc = db.users_col.find_one({"user_id": 1})
    assert doc["xp"] == 500 and doc["pm_accessible"] is False


# ── broadcasts delete themselves before Telegram's 48h limit ─────────────────

def _bc_db():
    import mongomock
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    db.db = mongomock.MongoClient().quizdb
    db.broadcasts_col = db.db.broadcasts
    return db


async def test_broadcast_expiry_deletes_due_skips_new_and_too_old(monkeypatch):
    from datetime import datetime
    import src.utils.scheduler as sm
    monkeypatch.setattr(sm.asyncio, "sleep", AsyncMock())
    db = _bc_db()
    now = datetime.utcnow()
    db.broadcasts_col.insert_many([
        {"id": 1, "broadcast_id": "due", "created_at": (now - timedelta(hours=47, minutes=5)).isoformat(),
         "messages": {"111": 5, "-1002": 9}},
        {"id": 2, "broadcast_id": "new", "created_at": (now - timedelta(hours=3)).isoformat(),
         "messages": {"111": 6}},
        {"id": 3, "broadcast_id": "old", "created_at": (now - timedelta(hours=60)).isoformat(),
         "messages": {"111": 1}},
    ])
    bot = MagicMock()
    tg = bot.application.bot
    tg.delete_message = AsyncMock(side_effect=[True, BadRequest("message to delete not found")])
    sch = AutoQuizScheduler(bot, MagicMock(), db_manager=db, interval_minutes=60)

    await sch._expire_broadcasts()
    calls = [(c.kwargs["chat_id"], c.kwargs["message_id"]) for c in tg.delete_message.await_args_list]
    assert calls == [(111, 5), (-1002, 9)]                      # only the due one; old one not tried
    b1, b2, b3 = (db.broadcasts_col.find_one({"id": i}) for i in (1, 2, 3))
    assert b1["auto_deleted"] and b1["auto_delete"]["deleted"] == 1 and b1["auto_delete"]["failed"] == 1
    assert b1["messages"] == {}
    assert "auto_deleted" not in b2 and b2["messages"] == {"111": 6}
    assert b3["auto_deleted"] and b3["auto_delete"]["note"] == "too old"

    await sch._expire_broadcasts()                              # second run: nothing is deleted again
    assert tg.delete_message.await_count == 2


def test_broadcast_expiry_job_registered_and_under_telegram_limit():
    assert 0 < AutoQuizScheduler.BROADCAST_DELETE_AFTER_H < AutoQuizScheduler.TELEGRAM_DELETE_LIMIT_H
