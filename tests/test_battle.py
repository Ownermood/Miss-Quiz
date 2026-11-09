"""Tests for 1v1 battle mode: pure scoring and full group/DM flows with a fake bot."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

import src.bot.battle as bt
from src.bot.battle import Battle, BattleMixin, _Round


# ── pure scoring ────────────────────────────────────────────────────────────

def _running(mode="group"):
    b = Battle(id="x", mode=mode, p1=1, p1_name="A")
    b.start(2, "B")
    return b


def test_cannot_accept_own_or_targeted_or_expired():
    b = Battle(id="x", mode="group", p1=1, p1_name="A", p2=2)
    assert b.can_accept(1)[0] is False
    assert b.can_accept(3)[0] is False
    assert b.can_accept(2)[0] is True
    b.created -= bt.INVITE_TTL + 1
    assert b.can_accept(2)[0] is False


def test_only_first_answer_of_players_counts():
    b = _running()
    b.rounds["p"] = (1, _Round(correct=0, sent_at=0.0))
    assert b.record_answer("p", 1, 0, now=2.0)
    assert not b.record_answer("p", 1, 0, now=3.0)      # second answer ignored
    assert not b.record_answer("p", 99, 0, now=1.0)     # spectator ignored
    assert b.record_answer("p", 2, 1, now=1.0)          # wrong
    assert b.scores == {1: 1, 2: 0} and b.rounds["p"][1].done.is_set()


def test_tie_broken_by_time_then_draw():
    b = _running()
    b.scores, b.times = {1: 3, 2: 3}, {1: 10.0, 2: 8.0}
    assert b.result() == (2, 1)
    b.times = {1: 8.0, 2: 8.0}
    assert b.result() == (None, None)
    b.scores = {1: 0, 2: 0}
    assert b.result() == (None, None)


def test_dm_opponent_cannot_answer_forwarded_poll():
    b = _running("dm")
    b.rounds["p1poll"] = (1, _Round(correct=0, sent_at=0.0, owner=1))
    assert not b.record_answer("p1poll", 2, 0, now=1.0)
    assert b.scores[2] == 0


def test_dm_round_done_only_when_both_polls_answered():
    b = _running("dm")
    r1, r2 = _Round(correct=0, sent_at=0.0, owner=1), _Round(correct=0, sent_at=0.0, owner=2)
    b.rounds.update({"p1poll": (1, r1), "p2poll": (1, r2)})
    b.record_answer("p1poll", 1, 0, now=1.0)
    assert not r1.done.is_set()
    b.record_answer("p2poll", 2, 0, now=1.0)
    assert r1.done.is_set() and r2.done.is_set()


# ── full flows with a fake bot ──────────────────────────────────────────────

class FakeBotApp(BattleMixin):
    def __init__(self):
        self.db = MagicMock()
        self.quiz_manager = MagicMock()
        qs = [{"id": i, "question": f"Q{i}?", "options": ["A", "B", "C"], "correct_answer": 1}
              for i in range(20)]
        self.quiz_manager.questions = qs
        it = iter(qs)
        self.quiz_manager.get_random_question.side_effect = lambda chat_id=0: next(it)
        self.replies = []

    async def _reply(self, update, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(message_id=500)


def fake_context():
    sent_polls = []
    counter = iter(range(1000))

    async def send_poll(**kw):
        pid = f"poll{next(counter)}"
        sent_polls.append((kw["chat_id"], pid, kw))
        return SimpleNamespace(poll=SimpleNamespace(id=pid), message_id=1)

    bot = MagicMock()
    bot.username = "MissQuizBot"
    bot.send_poll = AsyncMock(side_effect=send_poll)
    bot.send_message = AsyncMock()
    return SimpleNamespace(bot=bot, args=None), sent_polls


def user(uid, name):
    return SimpleNamespace(id=uid, first_name=name, last_name=None, username=None, is_bot=False)


def group_update(u, reply_to=None):
    msg = SimpleNamespace(reply_to_message=SimpleNamespace(from_user=reply_to) if reply_to else None)
    return SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=-100, type="supergroup"),
                           effective_message=msg, message=msg)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bt, "START_DELAY", 0)
    monkeypatch.setattr(bt, "ROUND_GAP", 0)
    monkeypatch.setattr(bt, "BATTLE_SECONDS", 0.2)
    monkeypatch.setattr(bt, "get_thread_id", lambda u: None)


async def _answer_all(app, polls, start, answers):
    """Wait for each new poll and answer it: answers = {uid: option}."""
    seen = start
    while True:
        for _ in range(100):
            if len(polls) > seen:
                break
            await asyncio.sleep(0.01)
        else:
            return
        chat_id, pid, _ = polls[seen]
        for uid, opt in answers.items():
            app.battle_on_poll_answer(pid, uid, [opt])
        seen += 1


async def test_group_battle_full_flow():
    app = FakeBotApp()
    ctx, polls = fake_context()
    a, b = user(1, "Amit"), user(2, "Bina")
    await app.cmd_battle(group_update(a), ctx)
    bid = next(iter(app._bt_state()))
    assert "1v1 BATTLE" in app.replies[0][0]

    q = SimpleNamespace(data=f"bt_acc:{bid}", answer=AsyncMock(), edit_message_text=AsyncMock())
    await app.handle_battle_callback(SimpleNamespace(callback_query=q, effective_user=b), ctx)

    # Amit answers everything correctly (option 1), Bina always wrong
    await _answer_all(app, polls, 0, {1: 1, 2: 0, 99: 1})
    for _ in range(200):
        if app._bt_state()[bid].state == "done":
            break
        await asyncio.sleep(0.01)

    battle = app._bt_state()[bid]
    assert len(polls) == bt.BATTLE_QUESTIONS and all(p[0] == -100 for p in polls)
    assert battle.scores == {1: 5, 2: 0}
    app.db.record_battle_result.assert_called_once_with(1, 2, 1)
    result_text = ctx.bot.send_message.call_args.args[1]
    assert "Winner" in result_text and "Amit" in result_text


async def test_poll_answer_hook_ignores_regular_polls():
    app = FakeBotApp()
    assert app.battle_on_poll_answer("random", 1, [0]) is False


async def test_dm_battle_via_link():
    app = FakeBotApp()
    ctx, polls = fake_context()
    a, b = user(1, "Amit"), user(2, "Bina")
    dm = lambda u: SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                                   effective_message=SimpleNamespace(reply_to_message=None))
    await app.cmd_battle(dm(a), ctx)
    text = app.replies[0][0]
    bid = next(iter(app._bt_state()))
    assert f"start=bt_{bid}" in text

    await app.battle_join_from_link(dm(b), ctx, bid)
    await _answer_all(app, polls, 0, {1: 0, 2: 1})   # Bina correct, Amit wrong
    for _ in range(200):
        if app._bt_state()[bid].state == "done":
            break
        await asyncio.sleep(0.01)
    battle = app._bt_state()[bid]
    assert len(polls) == 2 * bt.BATTLE_QUESTIONS          # one poll per player per question
    assert {p[0] for p in polls} == {1, 2}
    assert battle.scores == {1: 0, 2: 5}
    app.db.record_battle_result.assert_called_once_with(1, 2, 2)


async def test_busy_player_and_self_challenge_rejected():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a, reply_to=a), ctx)
    assert "yourself" in app.replies[-1][0]
    await app.cmd_battle(group_update(a), ctx)
    await app.cmd_battle(group_update(a), ctx)
    assert "already" in app.replies[-1][0]


async def test_decline_by_target_and_outsider_blocked():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a, b, c = user(1, "Amit"), user(2, "Bina"), user(3, "Chetan")
    await app.cmd_battle(group_update(a, reply_to=b), ctx)
    bid = next(iter(app._bt_state()))
    q = SimpleNamespace(data=f"bt_acc:{bid}", answer=AsyncMock(), edit_message_text=AsyncMock())
    await app.handle_battle_callback(SimpleNamespace(callback_query=q, effective_user=c), ctx)
    assert app._bt_state()[bid].state == "pending"           # outsider can't accept
    q.data = f"bt_dec:{bid}"
    await app.handle_battle_callback(SimpleNamespace(callback_query=q, effective_user=b), ctx)
    assert app._bt_state()[bid].state == "done"
    assert ctx.bot.send_poll.await_count == 0


# ── wiring into the real bot class ──────────────────────────────────────────

def _real_bot():
    from src.bot.handlers_main import TelegramQuizBot
    from telegram.ext import Application
    bot = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    bot.application = Application.builder().token("123:abc").build()
    bot._register_handlers()
    return bot


def test_battle_handlers_registered_before_generic_callback():
    from telegram.ext import CallbackQueryHandler, CommandHandler
    bot = _real_bot()
    hs = bot.application.handlers[0]
    assert any(isinstance(h, CommandHandler) and "battle" in h.commands for h in hs)
    cbs = [h for h in hs if isinstance(h, CallbackQueryHandler)]
    idx_battle = next(i for i, h in enumerate(cbs) if h.callback == bot.handle_battle_callback)
    idx_generic = next(i for i, h in enumerate(cbs) if h.callback == bot.handle_callback)
    assert idx_battle < idx_generic


async def test_battle_answer_skips_regular_stats():
    bot = _real_bot()
    b = Battle(id="z", mode="group", p1=1, p1_name="A", chat_id=-100)
    b.start(2, "B")
    b.rounds["bp"] = (1, _Round(correct=0, sent_at=0.0))
    bot._bt_state()["z"] = b
    bot._battle_polls["bp"] = "z"
    upd = SimpleNamespace(poll_answer=SimpleNamespace(
        user=SimpleNamespace(id=1, first_name="A", last_name=None, username=None, is_bot=False),
        poll_id="bp", option_ids=[0]), effective_chat=None, effective_user=None)
    bot.ensure_group_registered = MagicMock()
    bot.ensure_user_registered = MagicMock()
    await bot.handle_poll_answer(upd, SimpleNamespace(bot_data={}))
    assert b.scores[1] == 1
    bot.quiz_manager.record_attempt.assert_not_called()
    bot.db.record_quiz_result.assert_not_called()


async def test_start_deep_link_joins_battle():
    bot = _real_bot()
    bot.battle_join_from_link = AsyncMock()
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=2, first_name="B", last_name=None, username=None),
                          effective_chat=SimpleNamespace(id=2, type="private"))
    await bot.cmd_start(upd, SimpleNamespace(args=["bt_abc123"]))
    bot.battle_join_from_link.assert_awaited_once()
    assert bot.battle_join_from_link.call_args.args[2] == "abc123"


async def test_battle_button_from_bot_message_is_open_challenge():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    bot_msg = SimpleNamespace(from_user=SimpleNamespace(is_bot=True),
                              reply_to_message=SimpleNamespace(from_user=a))   # bot's reply to /start
    upd = SimpleNamespace(effective_user=a, effective_chat=SimpleNamespace(id=-100, type="supergroup"),
                          effective_message=bot_msg, message=None)
    await app.cmd_battle(upd, ctx)
    b = next(iter(app._bt_state().values()))
    assert b.p2 is None and "anyone" in app.replies[-1][0]
