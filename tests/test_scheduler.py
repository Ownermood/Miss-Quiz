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
