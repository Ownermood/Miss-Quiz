"""'Make me admin' alerts: DB query semantics (mongomock), scheduler job, tracking."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

mongomock = pytest.importorskip("mongomock")
from telegram.error import Forbidden, RetryAfter, BadRequest

from src.core.database import DatabaseManager
from src.utils.scheduler import AutoQuizScheduler
from src.bot.ui import make_admin_keyboard, ADMIN_ALERT_TEXT


def real_db():
    db = DatabaseManager.__new__(DatabaseManager)
    db.groups_col = mongomock.MongoClient().db.groups
    return db


def ago(hours):
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


# ── DB semantics ────────────────────────────────────────────────────────────

def test_reminder_query_selects_only_eligible_groups():
    db = real_db()
    db.groups_col.insert_many([
        {"chat_id": 1, "bot_is_admin": False},                                      # never alerted → yes
        {"chat_id": 2, "bot_is_admin": False, "admin_reminder_count": 1,
         "admin_reminder_at": ago(100)},                                            # old alert → yes
        {"chat_id": 3, "bot_is_admin": False, "admin_reminder_count": 1,
         "admin_reminder_at": ago(1)},                                              # too recent → no
        {"chat_id": 4, "bot_is_admin": False, "admin_reminder_count": 3,
         "admin_reminder_at": ago(500)},                                            # max reached → no
        {"chat_id": 5, "bot_is_admin": True},                                       # admin → no
        {"chat_id": 6},                                                             # unknown → no
        {"chat_id": 7, "bot_is_admin": False, "active_status": "inactive"},         # inactive → no
    ])
    got = {g["chat_id"] for g in db.get_groups_needing_admin_reminder(72, 3)}
    assert got == {1, 2}


def test_mark_and_reset_on_promotion():
    db = real_db()
    db.groups_col.insert_one({"chat_id": 1, "bot_is_admin": False})
    db.mark_admin_reminder(1)
    db.mark_admin_reminder(1)
    doc = db.groups_col.find_one({"chat_id": 1})
    assert doc["admin_reminder_count"] == 2 and doc["admin_reminder_at"]

    db.update_group_admin_status(1, False)          # still member: counter kept
    assert db.groups_col.find_one({"chat_id": 1})["admin_reminder_count"] == 2

    db.update_group_admin_status(1, True)           # promoted: reset
    doc = db.groups_col.find_one({"chat_id": 1})
    assert doc["bot_is_admin"] is True and "admin_reminder_count" not in doc


# ── scheduler job ───────────────────────────────────────────────────────────

def make_sched(groups, send_effect=None):
    db = MagicMock()
    db.get_all_active_quiz_states.return_value = []
    db.get_groups_needing_admin_reminder.return_value = groups
    bot = MagicMock()
    tg = bot.application.bot
    tg.username = "MissQuizBot"
    tg.send_message = AsyncMock(side_effect=send_effect)
    return AutoQuizScheduler(bot, MagicMock(), db_manager=db), db, tg


async def test_job_alerts_each_group_with_button_and_marks():
    sch, db, tg = make_sched([{"chat_id": -1}, {"chat_id": -2, "message_thread_id": 9}])
    await sch._send_admin_reminders()
    assert tg.send_message.await_count == 2
    first = tg.send_message.await_args_list[0]
    assert first.args == (-1, ADMIN_ALERT_TEXT)
    assert "startgroup&admin=" in first.kwargs["reply_markup"].inline_keyboard[0][0].url
    assert tg.send_message.await_args_list[1].kwargs["message_thread_id"] == 9
    assert db.mark_admin_reminder.call_count == 2
    db.get_groups_needing_admin_reminder.assert_called_once_with(72, 3)


async def test_job_marks_kicked_group_inactive():
    sch, db, tg = make_sched([{"chat_id": -1}], send_effect=Forbidden("bot was kicked from the group chat"))
    await sch._send_admin_reminders()
    db.groups_col.update_one.assert_called_once()       # _mark_group_inactive
    db.mark_admin_reminder.assert_not_called()


async def test_job_counts_failed_post_to_avoid_endless_retries():
    sch, db, tg = make_sched([{"chat_id": -1}], send_effect=BadRequest("not enough rights to send text messages"))
    await sch._send_admin_reminders()
    db.mark_admin_reminder.assert_called_once_with(-1)


async def test_job_stops_on_flood_limit():
    sch, db, tg = make_sched([{"chat_id": -1}, {"chat_id": -2}], send_effect=RetryAfter(30))
    await sch._send_admin_reminders()
    assert tg.send_message.await_count == 1
    db.mark_admin_reminder.assert_not_called()


# ── tracking (bot added / demoted) ─────────────────────────────────────────

async def test_added_as_member_sends_alert_with_button_and_counts_it():
    from src.bot.tracking import TrackingMixin

    class Bot(TrackingMixin):
        def __init__(self):
            self.db = MagicMock(); self._seen_groups = set()
            self.ensure_group_registered = MagicMock()

    bot = Bot()
    send = AsyncMock()
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send, username="MissQuizBot"))
    chat = SimpleNamespace(id=-100, type="supergroup", title="G")
    upd = SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=chat, old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(status="member")))
    await bot.handle_my_chat_member(upd, ctx)
    assert send.await_args.kwargs["reply_markup"] is not None
    bot.db.mark_admin_reminder.assert_called_once_with(-100)


def test_keyboard_url_and_missing_username():
    kb = make_admin_keyboard("MissQuizBot")
    assert kb.inline_keyboard[0][0].url == \
        "https://t.me/MissQuizBot?startgroup&admin=delete_messages+pin_messages"
    assert make_admin_keyboard("") is None


def test_due_query_keeps_unknown_and_admin_groups_only():
    db = real_db()
    now_past = ago(1)
    db.groups_col.insert_many([
        {"chat_id": 1, "next_quiz_due_at": now_past, "bot_is_admin": True},
        {"chat_id": 2, "next_quiz_due_at": now_past},                         # unknown → checked
        {"chat_id": 3, "next_quiz_due_at": now_past, "bot_is_admin": False},  # skipped
    ])
    assert {g["chat_id"] for g in db.get_groups_due_for_quiz()} == {1, 2}
