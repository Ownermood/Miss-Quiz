"""Regular quiz poll answers: stored in MongoDB, nothing piles up in RAM."""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _bot():
    from src.bot.handlers_main import TelegramQuizBot
    qm = MagicMock()
    b = TelegramQuizBot(qm, db_manager=MagicMock())
    b.ensure_group_registered = MagicMock()
    b.ensure_user_registered = MagicMock()
    return b


def _answer(uid, poll, opt):
    return SimpleNamespace(poll_answer=SimpleNamespace(
        user=SimpleNamespace(id=uid, first_name="u", last_name=None, username=None, is_bot=False),
        poll_id=poll, option_ids=[opt]), effective_chat=None, effective_user=None)


async def test_correct_and_wrong_answers_are_recorded_in_db():
    b = _bot()
    ctx = SimpleNamespace(bot_data={"poll_P": {"correct_option_id": 2, "chat_id": -100, "category": "GK"}})
    await b.handle_poll_answer(_answer(7, "P", 2), ctx)
    await b.handle_poll_answer(_answer(8, "P", 0), ctx)
    results = [c.args for c in b.db.record_quiz_result.call_args_list]
    assert results[0][0] == 7 and results[0][1]["correct"] == 1
    assert results[1][0] == 8 and results[1][1]["wrong"] == 1
    b.quiz_manager.record_attempt.assert_not_called()          # no in-RAM stats growth


async def test_unknown_poll_is_counted_lost_not_crash():
    b = _bot()
    await b.handle_poll_answer(_answer(7, "nope", 0), SimpleNamespace(bot_data={}))
    b.db.record_quiz_result.assert_not_called()
    assert b._poll_stats["lost"] == 1
