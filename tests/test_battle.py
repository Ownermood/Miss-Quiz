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
    bot.edit_message_text = AsyncMock()
    return SimpleNamespace(bot=bot, args=None, user_data={}), sent_polls


def user(uid, name):
    return SimpleNamespace(id=uid, first_name=name, last_name=None, username=None, is_bot=False)


def group_update(u, reply_to=None):
    msg = SimpleNamespace(reply_to_message=SimpleNamespace(from_user=reply_to) if reply_to else None)
    return SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=-100, type="supergroup"),
                           effective_message=msg, message=msg)


async def press(app, ctx, uid_user, data):
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=-100)))
    await app.handle_battle_callback(SimpleNamespace(callback_query=q, effective_user=uid_user), ctx)
    if q.edit_message_text.await_args:
        app._last_edit = q.edit_message_text.await_args.args[0]
    return q


async def setup_and_create(app, ctx, challenger, secs=0.2, n_q=None):
    bid = next(iter(app._bt_state()))
    b = app._bt_state()[bid]
    b.secs = secs                       # keep tests fast
    if n_q:
        await press(app, ctx, challenger, f"bt_q:{bid}:{n_q}")
    await press(app, ctx, challenger, f"bt_go:{bid}")
    return bid


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
    assert "BATTLE SETUP" in app.replies[0][0]
    bid = await setup_and_create(app, ctx, a, n_q=5)
    await press(app, ctx, b, f"bt_acc:{bid}")

    # Amit answers everything correctly (option 1), Bina always wrong
    await _answer_all(app, polls, 0, {1: 1, 2: 0, 99: 1})
    for _ in range(200):
        if app._bt_state()[bid].state == "done":
            break
        await asyncio.sleep(0.01)

    battle = app._bt_state()[bid]
    assert len(polls) == 5 and all(p[0] == -100 for p in polls)
    assert battle.scores == {1: 5, 2: 0}
    app.db.record_battle_result.assert_called_once_with(1, 2, 1)
    assert ctx.bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == f"bt_rm:{bid}"
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
    bid = await setup_and_create(app, ctx, a, n_q=5)
    text = app._last_edit
    assert f"start=bt_{bid}" in text

    await app.battle_join_from_link(dm(b), ctx, bid)
    await _answer_all(app, polls, 0, {1: 0, 2: 1})   # Bina correct, Amit wrong
    for _ in range(200):
        if app._bt_state()[bid].state == "done":
            break
        await asyncio.sleep(0.01)
    battle = app._bt_state()[bid]
    assert len(polls) == 2 * 5          # one poll per player per question
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
    await setup_and_create(app, ctx, a)
    q = await press(app, ctx, c, f"bt_acc:{bid}")
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



# ── setup card ─────────────────────────────────────────────────────────────

async def test_setup_only_challenger_can_change_and_settings_apply():
    app = FakeBotApp()
    ctx, polls = fake_context()
    a, b = user(1, "Amit"), user(2, "Bina")
    await app.cmd_battle(group_update(a), ctx)
    bid = next(iter(app._bt_state()))
    q = await press(app, ctx, b, f"bt_q:{bid}:15")                     # not the challenger
    assert q.answer.await_args.kwargs.get("show_alert") is True
    await press(app, ctx, a, f"bt_q:{bid}:15")
    await press(app, ctx, a, f"bt_t:{bid}:30")
    bt_ = app._bt_state()[bid]
    assert (bt_.n_q, bt_.secs, bt_.state) == (15, 30, "setup")
    q = await press(app, ctx, b, f"bt_acc:{bid}")                       # can't accept during setup
    assert bt_.state == "setup"
    await press(app, ctx, a, f"bt_go:{bid}")
    assert bt_.state == "pending" and "15" in app._last_edit and "30s" in app._last_edit


async def test_timer_choices_and_labels():
    from src.bot.battle import _tlabel, T_CHOICES
    assert [_tlabel(t) for t in T_CHOICES] == ["10s", "15s", "20s", "30s", "1m", "4m"]
    assert _tlabel(90) == "1m 30s"
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    b = next(iter(app._bt_state().values()))
    _, kb = app._bt_setup_view(b, "t")
    rows = [[x.text for x in r] for r in kb.inline_keyboard]
    assert ["10s", "15s", "✅ 20s"] in rows and ["30s", "1m", "4m"] in rows
    styles = {x.text: x.style for r in kb.inline_keyboard for x in r}
    assert styles["➡️ Next"] == "success" and styles["✅ 20s"] == "primary"   # only Next is green
    await press(app, ctx, a, f"bt_t:{b.id}:240")
    assert b.secs == 240 and "4m each" in app._last_edit


async def test_topic_with_too_few_questions_is_refused():
    app = FakeBotApp()
    for i, q in enumerate(app.quiz_manager.questions):
        q["category"] = "Polity" if i < 5 else "GK"
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    bid = next(iter(app._bt_state()))
    bt_ = app._bt_state()[bid]
    bt_.cat_options = ["Polity"]          # topic UI removed; backend filter still works
    idx = 0
    await press(app, ctx, a, f"bt_c:{bid}:{idx}")
    await press(app, ctx, a, f"bt_q:{bid}:10")
    q = await press(app, ctx, a, f"bt_go:{bid}")
    assert "Only 5 questions" in q.answer.await_args.args[0] and bt_.state == "setup"
    await press(app, ctx, a, f"bt_q:{bid}:5")
    await press(app, ctx, a, f"bt_go:{bid}")
    assert bt_.state == "pending"
    picked = app._bt_pick_questions(bt_)
    assert len(picked) == 5 and all(q["category"] == "Polity" for q in picked)


async def test_rematch_challenges_the_other_player_with_same_settings():
    app = FakeBotApp()
    ctx, _ = fake_context()
    old = Battle(id="old", mode="group", p1=1, p1_name="Amit", chat_id=-100, n_q=15, secs=10)
    old.start(2, "Bina")
    app._bt_finished_put(old)
    q = await press(app, ctx, user(2, "Bina"), "bt_rm:old")
    new = next(v for v in app._bt_state().values())
    assert (new.p1, new.p2, new.n_q, new.secs, new.state) == (2, 1, 15, 10, "pending")
    assert ctx.bot.send_message.await_args.args[0] == -100
    q = await press(app, ctx, user(9, "X"), "bt_rm:old")                  # outsider
    assert q.answer.await_args.kwargs.get("show_alert") is True



def text_update(u, text, chat_id=-100):
    msg = SimpleNamespace(text=text, delete=AsyncMock())
    return SimpleNamespace(effective_user=u, effective_message=msg,
                           effective_chat=SimpleNamespace(id=chat_id)), msg


def test_parse_secs():
    from src.bot.battle import _parse_secs
    assert [_parse_secs(x) for x in ("45", "45s", "2m", "1:30", "1m 30s", "hello", "")] == \
        [45, 45, 120, 90, 90, None, None]


async def test_setup_steps_typed_values_summary_and_edit():
    from telegram.ext import ApplicationHandlerStop
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    b = next(iter(app._bt_state().values()))
    text, kb = app._bt_setup_view(b)
    assert "1️⃣ Questions › 10" in text and "2️⃣ Timer › 20s" in text and "3️⃣ Summary" in text
    assert [r[0].text for r in kb.inline_keyboard] == \
        ["🎯 Questions: 10", "⏱ Timer: 20s each", "➡️ Next", "❌ Cancel"]
    styles = [r[0].style for r in kb.inline_keyboard]
    assert styles == ["primary", "primary", "success", "danger"]

    await press(app, ctx, a, f"bt_p:{b.id}:q")                          # Questions step
    assert "👉 Questions" in app._last_edit and "type any number" in app._last_edit
    await press(app, ctx, a, f"bt_q:{b.id}:15")                         # tap stays on the step
    assert b.n_q == 15 and "👉 Questions › 15" in app._last_edit

    upd, m = text_update(a, "hi everyone")                              # normal chat ignored
    await app.handle_battle_input(upd, ctx)
    assert b.n_q == 15 and not m.delete.await_count
    upd, m = text_update(a, "12")                                       # typed count
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_battle_input(upd, ctx)
    assert b.n_q == 12 and m.delete.await_count == 1
    assert "Questions › 12" in ctx.bot.edit_message_text.await_args.args[0]
    upd, _ = text_update(a, "999")
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_battle_input(upd, ctx)
    assert b.n_q == app._bt_q_max()                                     # clamped
    upd, _ = text_update(user(2, "B"), "7")                             # other user: no await state
    await app.handle_battle_input(upd, SimpleNamespace(bot=ctx.bot, user_data={}))
    assert b.n_q == app._bt_q_max()

    await press(app, ctx, a, f"bt_p:{b.id}:t")                          # Timer step
    assert "👉 Timer" in app._last_edit
    upd, _ = text_update(a, "1:30")
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_battle_input(upd, ctx)
    assert b.secs == 90
    upd, _ = text_update(a, "2")
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_battle_input(upd, ctx)
    assert b.secs == 5                                                  # Telegram minimum

    await press(app, ctx, a, f"bt_p:{b.id}:sum")                        # Summary
    assert "BATTLE SUMMARY" in app._last_edit and ctx.user_data.get("bt_await") is None
    upd, m = text_update(a, "20")                                       # typing no longer applies
    await app.handle_battle_input(upd, ctx)
    assert b.secs == 5 and not m.delete.await_count
    await press(app, ctx, a, f"bt_p:{b.id}:t")                          # ✏️ Timer edit
    await press(app, ctx, a, f"bt_t:{b.id}:30")
    assert b.secs == 30 and b.state == "setup"
    q = await press(app, ctx, user(2, "B"), f"bt_q:{b.id}:7")           # not challenger
    assert q.answer.await_args.kwargs.get("show_alert") is True
    await press(app, ctx, a, f"bt_go:{b.id}")
    assert b.state == "pending"
