"""Delete / edit / find question flows: safety and ease of use."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.bot.handlers_main import TelegramQuizBot

ADMIN, HACKER = 111, 666
QS = [{"id": 5, "question": "Which Article abolishes untouchability?", "options": ["14", "17", "19", "21"],
       "correct_answer": 1, "category": "Polity"},
      {"id": 6, "question": "Capital of France?", "options": ["Paris", "Rome"], "correct_answer": 0,
       "category": "GK"}]


@pytest.fixture
def bot():
    qm = MagicMock()
    qm.questions = [dict(q) for q in QS]
    qm.delete_question_by_db_id = MagicMock(return_value=True)
    b = TelegramQuizBot(qm, db_manager=MagicMock())
    b._is_owner = lambda uid: uid == ADMIN
    b._is_authorized = AsyncMock(side_effect=lambda uid: uid == ADMIN)
    b._reply = AsyncMock()
    return b


def cb(uid, data):
    msg = MagicMock(); msg.edit_text = AsyncMock(); msg.chat.type = "private"
    q = SimpleNamespace(data=data, answer=AsyncMock(), message=msg, from_user=SimpleNamespace(id=uid, first_name="Admin", last_name=None, username=None))
    return SimpleNamespace(callback_query=q, effective_user=q.from_user), msg


async def test_forged_delete_callback_from_non_admin_is_blocked(bot):
    u, _ = cb(HACKER, f"dq_del_5_{HACKER}")        # attacker puts own id in callback data
    await bot._cb_delquiz(u, None)
    bot.quiz_manager.delete_question_by_db_id.assert_not_called()


async def test_tap_shows_confirm_with_question_and_answer_first(bot):
    u, msg = cb(ADMIN, f"dq_ask_5_{ADMIN}")
    await bot._cb_delquiz(u, None)
    bot.quiz_manager.delete_question_by_db_id.assert_not_called()
    text = msg.edit_text.await_args.args[0]
    kb = msg.edit_text.await_args.kwargs["reply_markup"]
    assert "untouchability" in text and "✅ 17" in text
    assert kb.inline_keyboard[0][0].callback_data == f"dq_del_5_{ADMIN}"
    assert kb.inline_keyboard[0][0].style == "danger"


async def test_confirmed_delete_offers_next_steps(bot):
    u, msg = cb(ADMIN, f"dq_del_5_{ADMIN}")
    await bot._cb_delquiz(u, None)
    bot.quiz_manager.delete_question_by_db_id.assert_called_once_with(5)
    kb = msg.edit_text.await_args.kwargs["reply_markup"]
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert datas[0].startswith("dq_page_") and datas[1] == f"dq_cancel_{ADMIN}"


async def test_delquiz_with_id_goes_straight_to_confirm(bot):
    u = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN),
                        effective_message=SimpleNamespace(reply_to_message=None))
    await bot.cmd_delquiz(u, SimpleNamespace(args=["#5"], bot_data={}))
    assert "DELETE THIS QUESTION" in bot._reply.await_args.args[1]


def test_find_by_id_and_by_words(bot):
    text, kb = bot._find_results("5", ADMIN)
    assert "#5" in text and kb.inline_keyboard[0][0].callback_data == "edit_quiz_select_5"
    assert kb.inline_keyboard[0][1].callback_data == f"dq_ask_5_{ADMIN}"
    text, _ = bot._find_results("france capital", ADMIN)
    assert "#6" in text and "#5" not in text
    text, kb = bot._find_results("zzz", ADMIN)
    assert "No question matches" in text


async def test_editor_save_updates_bot_cache_immediately():
    from src.core.quiz import QuizManager
    db = MagicMock(); db.get_all_questions.return_value = [dict(QS[0])]; db.update_question.return_value = True
    qm = QuizManager(db_manager=db)
    from src.bot.dev_commands import DeveloperCommands
    dev = DeveloperCommands(db, qm)
    ctx = SimpleNamespace(user_data={"editing_quiz_5": {
        "question": "Fixed question?", "options": ["a", "b", "c", "d"], "correct_answer": 3,
        "category": "Law", "original": dict(QS[0])}})
    q = MagicMock(); q.edit_message_text = AsyncMock(); q.message.chat_id = 1
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=ADMIN))
    await dev._save_quiz_changes(upd, ctx, 5)
    served = qm.questions[0]
    assert served["question"] == "Fixed question?" and served["correct_answer"] == 3
    assert served["category"] == "Law"
