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
    assert "Make your own quiz" in app.replies[0][0]
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
    assert "INVITE READY" in app._last_edit
    _, kb = app._bt_challenge_view(app._bt_state()[bid], "MissQuizBot")
    btns = [x for r in kb.inline_keyboard for x in r]
    assert any(f"start%3Dbt_{bid}" in (x.url or "") for x in btns)          # Send to a friend
    assert any(x.copy_text and f"start=bt_{bid}" in x.copy_text.text for x in btns)   # Copy link

    await app.battle_join_from_link(dm(a), ctx, bid)                       # own link
    assert "your own invite" in app.replies[-1][0]
    await app.battle_join_from_link(dm(b), ctx, bid)                       # friend: accept card
    assert "BATTLE INVITE" in app.replies[-1][0] and app._bt_state()[bid].state == "pending"
    await press(app, ctx, b, f"bt_acc:{bid}")
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
    first = next(iter(app._bt_state().values()))
    await app.cmd_battle(group_update(a), ctx)                 # new /battle replaces the open one
    assert first.state == "done" and "replaced" in ctx.bot.edit_message_text.await_args.args[0]
    assert sum(b.state == "setup" for b in app._bt_state().values()) == 1
    second = [b for b in app._bt_state().values() if b.state == "setup"][0]
    second.start(2, "Bina")                                     # now actually playing
    await app.cmd_battle(group_update(a), ctx)
    assert "already in a battle" in app.replies[-1][0]


async def test_open_challenge_has_no_visible_time_limit_and_lasts():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    bid = await setup_and_create(app, ctx, a)
    b = app._bt_state()[bid]
    text, _ = app._bt_challenge_view(b, "MissQuizBot")
    dm_text, _ = app._bt_challenge_view(bt.dataclass_replace_mode(b, "dm"), "MissQuizBot")
    assert "min" not in text.replace("Max", "") and "valid" not in dm_text
    b.created -= 60 * 60                                        # an hour later still joinable
    assert b.can_accept(2)[0] is True


async def test_accepting_drops_accepters_own_open_challenge():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a, c = user(1, "Amit"), user(3, "Chetan")
    await app.cmd_battle(group_update(c), ctx)                 # Chetan's own open challenge
    mine = next(iter(app._bt_state().values()))
    await app.cmd_battle(group_update(a), ctx)
    bid = [k for k, v in app._bt_state().items() if v.p1 == 1][0]
    app._bt_state()[bid].secs = 0.2
    await press(app, ctx, a, f"bt_m:{bid}:quick")
    app._bt_state()[bid].secs = 0.2
    await press(app, ctx, c, f"bt_acc:{bid}")                  # not blocked by his open one
    assert app._bt_state()[bid].state == "running" and mine.state == "done"


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

async def test_too_few_questions_is_refused():
    app = FakeBotApp()
    for i, q in enumerate(app.quiz_manager.questions):
        q["category"] = "Polity" if i < 5 else "GK"
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    bid = next(iter(app._bt_state()))
    bt_ = app._bt_state()[bid]
    bt_.category = "Polity"               # backend filter (no topic UI) — only 5 questions
    q = await press(app, ctx, a, f"bt_m:{bid}:classic")
    assert "Only 5 questions" in q.answer.await_args.args[0] and bt_.state == "setup"
    await press(app, ctx, a, f"bt_m:{bid}:quick")
    assert bt_.state == "pending" and (bt_.n_q, bt_.secs) == (5, 15)
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


def _mongo_db(app):
    import mongomock
    from src.core.database import DatabaseManager
    real = DatabaseManager.__new__(DatabaseManager)
    real.battles_col = mongomock.MongoClient().db.battles
    for name in ("save_battle", "get_battle", "delete_battle", "open_battles_of"):
        setattr(app.db, name, getattr(real, name))
    return real


async def test_open_challenge_survives_restart():
    app = FakeBotApp()
    real = _mongo_db(app)
    ctx, _ = fake_context()
    a, b = user(1, "Amit"), user(2, "Bina")
    await app.cmd_battle(group_update(a), ctx)
    bid = next(iter(app._bt_state()))
    await press(app, ctx, a, f"bt_m:{bid}:quick")
    assert real.get_battle(bid)["state"] == "pending"

    app2 = FakeBotApp()                         # "restart": empty memory, same DB
    app2.db = app.db
    await press(app2, ctx, b, f"bt_acc:{bid}")
    bt2 = app2._bt_state()[bid]
    assert bt2.state == "running" and (bt2.n_q, bt2.secs) == (5, 15)
    assert real.get_battle(bid) is None          # running/done battles leave the DB
    bt2.state = "done"


async def test_ended_battle_shows_new_battle_button():
    app = FakeBotApp()
    ctx, _ = fake_context()
    q = await press(app, ctx, user(1, "A"), "bt_acc:gone")
    assert "has ended" in app._last_edit
    kb = q.edit_message_text.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "start_battle"


async def test_dm_decline_notifies_challenger_and_stays_open():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a, b = user(1, "Amit"), user(2, "Bina")
    dm = lambda u: SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                                   effective_message=SimpleNamespace(reply_to_message=None))
    await app.cmd_battle(dm(a), ctx)
    bid = await setup_and_create(app, ctx, a)
    await press(app, ctx, b, f"bt_dec:{bid}")
    assert "declined" in app._last_edit
    assert ctx.bot.send_message.await_args.args[0] == 1                     # challenger told
    assert app._bt_state()[bid].state == "pending"


async def test_dm_accept_updates_challenger_card_and_only_one_can_accept():
    app = FakeBotApp()
    ctx, _ = fake_context()
    a, b, c = user(1, "Amit"), user(2, "Bina"), user(3, "Chetan")
    dm = lambda u: SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                                   effective_message=SimpleNamespace(reply_to_message=None))
    await app.cmd_battle(dm(a), ctx)
    bid = await setup_and_create(app, ctx, a)
    await press(app, ctx, b, f"bt_acc:{bid}")
    kw = ctx.bot.edit_message_text.await_args.kwargs
    assert kw["chat_id"] == 1 and kw["message_id"] == 500                   # invite card edited
    assert "accepted your battle" in ctx.bot.edit_message_text.await_args.args[0]
    q = await press(app, ctx, c, f"bt_acc:{bid}")                           # too late
    assert q.answer.await_args.kwargs.get("show_alert") is True
    app._bt_state()[bid].state = "done"


async def test_inline_invite_card_is_bold_with_accept_and_only_for_challenger(monkeypatch):
    monkeypatch.setenv("PREMIUM_EMOJI", "0")
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    dm = lambda u: SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                                   effective_message=SimpleNamespace(reply_to_message=None))
    await app.cmd_battle(dm(a), ctx)
    bid = await setup_and_create(app, ctx, a)
    b = app._bt_state()[bid]
    _, kb = app._bt_challenge_view(b, "MissQuizBot", inline=True)
    sw = kb.inline_keyboard[0][0].switch_inline_query_chosen_chat
    assert sw.query == f"bt_{bid}" and sw.allow_user_chats

    def iq_update(uid, query):
        iq = SimpleNamespace(query=query, from_user=SimpleNamespace(id=uid), answer=AsyncMock())
        return SimpleNamespace(inline_query=iq), iq

    upd, iq = iq_update(1, f"bt_{bid}")
    await app.handle_battle_inline(upd, ctx)
    res = iq.answer.await_args.args[0]
    assert len(res) == 1
    text = res[0].input_message_content.message_text
    assert text.startswith("<b>") and "challenged you" in text                 # bold
    assert res[0].reply_markup.inline_keyboard[0][0].url.endswith(f"start=bt_{bid}_go")
    upd, iq = iq_update(2, f"bt_{bid}")                                       # not the challenger
    await app.handle_battle_inline(upd, ctx)
    assert iq.answer.await_args.args[0] == []


async def test_inline_premium_rejected_falls_back_to_plain(monkeypatch):
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    import src.bot.premium as pm
    monkeypatch.setattr(pm, "premium_on", lambda: True)
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    dm = lambda u: SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                                   effective_message=SimpleNamespace(reply_to_message=None))
    await app.cmd_battle(dm(a), ctx)
    bid = await setup_and_create(app, ctx, a)
    iq = SimpleNamespace(query=f"bt_{bid}", from_user=SimpleNamespace(id=1),
                         answer=AsyncMock(side_effect=[Exception("custom emoji not allowed"), None]))
    await app.handle_battle_inline(SimpleNamespace(inline_query=iq), ctx)
    assert iq.answer.await_count == 2
    first = iq.answer.await_args_list[0].args[0][0].input_message_content.message_text
    second = iq.answer.await_args_list[1].args[0][0].input_message_content.message_text
    assert "tg-emoji" in first and "tg-emoji" not in second


async def test_accept_button_on_shared_card_starts_battle_directly():
    app = FakeBotApp()
    ctx, polls = fake_context()
    a, b = user(1, "Amit"), user(2, "Bina")
    dm = lambda u: SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                                   effective_message=SimpleNamespace(reply_to_message=None))
    await app.cmd_battle(dm(a), ctx)
    bid = await setup_and_create(app, ctx, a)
    await app.battle_join_from_link(dm(b), ctx, f"{bid}_go")
    bt_ = app._bt_state()[bid]
    assert bt_.state == "running" and bt_.p2 == 2
    assert "BATTLE STARTED" in app.replies[-1][0]
    assert "accepted your battle" in ctx.bot.edit_message_text.await_args.args[0]
    await app.battle_join_from_link(dm(user(3, "C")), ctx, f"{bid}_go")      # too late
    assert "no longer open" in app.replies[-1][0]
    bt_.state = "done"



async def test_main_card_layout_and_bot_quiz_one_tap():
    app = FakeBotApp()
    app.db.get_battle_pref.return_value = (12, 45)
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    b = next(iter(app._bt_state().values()))
    _, kb = app._bt_setup_view(b, "mode", "MissQuizBot")
    rows = [[(x.text, x.style) for x in r] for r in kb.inline_keyboard]
    assert rows == [[("➕ Create Own Quiz", "success")],
                    [("📚 My Quizzes", "primary"), ("🤖 Bot Quiz", "primary")],
                    [("⚙️ Customize", "primary")],
                    [("❌ Cancel", "danger")]]
    assert kb.inline_keyboard[0][0].url.endswith("start=mq_new")         # group → made in DM
    await press(app, ctx, a, f"bt_p:{b.id}:bot")
    assert "BOT QUIZ" in app._last_edit and "Last · 12Q · 45s" in str(app._bt_setup_view(b, "bot")[1])
    await press(app, ctx, a, f"bt_m:{b.id}:last")                       # one tap
    assert b.state == "pending" and (b.n_q, b.secs) == (12, 45)
    q = await press(app, ctx, user(2, "B"), f"bt_ch:{b.id}")            # only challenger
    assert q.answer.await_args.kwargs.get("show_alert") is True
    await press(app, ctx, a, f"bt_ch:{b.id}")                           # ✏️ Change → main card
    assert b.state == "setup" and "Make your own quiz" in app._last_edit


async def test_customize_plain_options_typing_and_create():
    from telegram.ext import ApplicationHandlerStop
    from src.bot.battle import _tlabel, T_CHOICES
    assert [_tlabel(t) for t in T_CHOICES] == ["10s", "15s", "20s", "25s", "30s", "1m"]
    assert _tlabel(90) == "1m 30s"
    app = FakeBotApp()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    await app.cmd_battle(group_update(a), ctx)
    b = next(iter(app._bt_state().values()))
    await press(app, ctx, a, f"bt_p:{b.id}:cust")
    _, kb = app._bt_setup_view(b, "cust")
    rows = [[(x.text, x.style) for x in r] for r in kb.inline_keyboard]
    assert rows[0] == [("10s", None), ("15s", None)]                    # 2 per row, no colour
    assert rows[1] == [("✅ 20s", "success"), ("25s", None)]             # current = green
    assert rows[2] == [("30s", None), ("1m", None)]
    assert rows[3] == [("🎯 Questions: 10", None)]
    assert rows[4] == [("⚔️ Create Challenge", "success")]
    assert rows[5] == [("⬅️ Back", "danger"), ("❌ Cancel", "danger")]
    await press(app, ctx, a, f"bt_t:{b.id}:25")                         # stays on Customize
    assert b.secs == 25 and b.state == "setup" and "CUSTOMIZE" in app._last_edit
    upd, m = text_update(a, "hi everyone")                              # normal chat ignored
    await app.handle_battle_input(upd, ctx)
    assert not m.delete.await_count
    upd, _ = text_update(a, "1:30")                                     # typed timer
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_battle_input(upd, ctx)
    assert b.secs == 90
    await press(app, ctx, a, f"bt_p:{b.id}:q")                          # Questions page
    assert "HOW MANY QUESTIONS" in app._last_edit
    upd, _ = text_update(a, "999")                                      # typed, clamped → back to Customize
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_battle_input(upd, ctx)
    assert b.n_q == app._bt_q_max() and "CUSTOMIZE" in ctx.bot.edit_message_text.await_args.args[0]
    await press(app, ctx, a, f"bt_q:{b.id}:15")
    await press(app, ctx, a, f"bt_t:{b.id}:2")                          # forged too-small value
    assert b.secs == 5
    q = await press(app, ctx, user(2, "B"), f"bt_go:{b.id}")            # not challenger
    assert q.answer.await_args.kwargs.get("show_alert") is True
    await press(app, ctx, a, f"bt_go:{b.id}")
    assert b.state == "pending" and (b.n_q, b.secs) == (15, 5)
    app.db.set_battle_pref.assert_called_once_with(1, 15, 5)


# ── own quizzes ────────────────────────────────────────────────────────────

def _quiz_app():
    import mongomock
    from src.core.database import DatabaseManager
    from src.bot.my_quiz import MyQuizMixin
    app = type("A", (MyQuizMixin, FakeBotApp), {})()
    real = DatabaseManager.__new__(DatabaseManager)
    real.quiz_sets_col = mongomock.MongoClient().db.quiz_sets
    real.battles_col = mongomock.MongoClient().db.battles
    for name in ("create_quiz_set", "get_quiz_set", "list_quiz_sets", "count_quiz_sets",
                 "delete_quiz_set", "update_quiz_set", "save_battle", "get_battle", "delete_battle", "open_battles_of"):
        setattr(app.db, name, getattr(real, name))
    return app, real


QTEXT = """1. Capital of India?
A) Mumbai
B) Delhi
C) Pune
Answer: B

2. 2+2?
A) 3
B) 4
Answer: B

3. Largest planet?
A) Earth
B) Jupiter
Answer: Jupiter

4. Broken question without answer
A) x
B) y
"""


def dm_update(u, **msg):
    m = SimpleNamespace(text=None, poll=None, document=None, photo=None, caption=None,
                        reply_to_message=None, delete=AsyncMock())
    for k, v in msg.items():
        setattr(m, k, v)
    return SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=u.id, type="private"),
                           effective_message=m, message=m)


async def test_create_own_quiz_text_poll_dedupe_name_and_save():
    from telegram.ext import ApplicationHandlerStop
    app, real = _quiz_app()
    ctx, _ = fake_context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    ctx.bot.delete_message = AsyncMock()
    a = user(1, "Amit")
    await app.mq_start_draft(None, ctx, a, dm_update(a))
    assert "CREATE NEW QUIZ" in app.replies[-1][0]

    with pytest.raises(ApplicationHandlerStop):
        await app.handle_myquiz_input(dm_update(a, text=QTEXT), ctx)
    d = ctx.user_data["mq_draft"]
    assert len(d["qs"]) == 3
    panel = ctx.bot.send_message.await_args.args[1]
    assert "Questions ready › <b>3 Qs</b>" in panel and "Skipped › <b>1</b>" in panel

    with pytest.raises(ApplicationHandlerStop):                         # same text again: no duplicates
        await app.handle_myquiz_input(dm_update(a, text=QTEXT), ctx)
    assert len(d["qs"]) == 3
    poll = SimpleNamespace(type="quiz", question="Fastest land animal?", correct_option_id=0,
                           options=[SimpleNamespace(text="Cheetah"), SimpleNamespace(text="Horse")],
                           explanation="")
    with pytest.raises(ApplicationHandlerStop):                         # forwarded quiz poll
        await app.handle_myquiz_input(dm_update(a, poll=poll), ctx)
    assert len(d["qs"]) == 4

    q = await press_mq(app, ctx, a, "mq_done")
    assert d["stage"] == "name" and "STEP 1/4" in q.edit_message_text.await_args.args[0]
    with pytest.raises(ApplicationHandlerStop):                         # typed name → step 2
        await app.handle_myquiz_input(dm_update(a, text="  Polity   Set 1 "), ctx)
    assert d["title"] == "Polity Set 1" and "STEP 2/4" in ctx.bot.send_message.await_args.args[1]
    q = await press_mq(app, ctx, a, "mq_wt:30")                         # timer
    assert "STEP 3/4" in q.edit_message_text.await_args.args[0]
    q = await press_mq(app, ctx, a, "mq_wb:neg")                        # back to timer
    assert "STEP 2/4" in q.edit_message_text.await_args.args[0]
    await press_mq(app, ctx, a, "mq_wt:30")
    await press_mq(app, ctx, a, "mq_wn:0.25")                           # negative
    await press_mq(app, ctx, a, "mq_ws:1")                              # shuffle → saved
    assert "mq_draft" not in ctx.user_data
    sets = real.list_quiz_sets(1)
    assert len(sets) == 1 and sets[0]["title"] == "Polity Set 1" and sets[0]["count"] == 4
    full = real.get_quiz_set(sets[0]["id"])
    assert (full["secs"], full["negative_rate"], full["shuffle"]) == (30, 0.25, True)
    assert "Saved!" in ctx.bot.send_message.await_args.args[1]
    assert app.quiz_manager.questions[0]["question"] == "Q0?"           # bot pool untouched


async def press_mq(app, ctx, u, data):
    q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                        message=SimpleNamespace(chat=SimpleNamespace(id=u.id, type="private"), message_id=77))
    await app.handle_myquiz_callback(SimpleNamespace(callback_query=q, effective_user=u), ctx)
    return q


async def test_create_needs_min_questions_cancel_and_limit():
    from telegram.ext import ApplicationHandlerStop
    from src.bot import my_quiz
    app, real = _quiz_app()
    ctx, _ = fake_context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    ctx.bot.delete_message = AsyncMock()
    a = user(1, "Amit")
    await app.handle_myquiz_input(dm_update(a, text="hello"), ctx)      # no draft → ignored
    await app.mq_start_draft(None, ctx, a, dm_update(a))
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_myquiz_input(dm_update(a, text="just chatting"), ctx)
    assert "No questions found" in ctx.bot.send_message.await_args.args[1]
    q = await press_mq(app, ctx, a, "mq_done")
    assert q.answer.await_args.kwargs.get("show_alert") is True        # 0 questions → can't save
    await press_mq(app, ctx, a, "mq_cancel")
    assert "mq_draft" not in ctx.user_data
    for i in range(25):                                                 # no limit any more
        real.create_quiz_set(1, f"S{i}", [], 20)
    await app.mq_start_draft(None, ctx, a, dm_update(a))
    assert "CREATE NEW QUIZ" in app.replies[-1][0] and "mq_draft" in ctx.user_data


async def test_my_quizzes_page_numbers():
    from src.bot.my_quiz import page_rows, PER_PAGE
    rows = page_rows(10, 1, lambda n: f"p{n}")
    assert [[b.text for b in r] for r in rows] == [["·1·", "2", "3", "4", "5"], ["6", "7", "8", "9", "10"]]
    assert rows[0][0].style == "success" and rows[0][1].style != "success"
    assert page_rows(1, 1, str) == []
    big = page_rows(30, 20, str)                                        # window of 10 around page 20
    assert [b.text for r in big for b in r] == ["16", "17", "18", "19", "·20·", "21", "22", "23", "24", "25"]

    app, real = _quiz_app()
    ctx, _ = fake_context()
    a = user(1, "Amit")
    for i in range(PER_PAGE * 2 + 3):                                   # 19 quizzes → 3 pages
        real.create_quiz_set(1, f"S{i:02d}", [], 20)
    q = await press_mq(app, ctx, a, "mq_list:3")
    text = q.edit_message_text.await_args.args[0]
    kb = q.edit_message_text.await_args.kwargs.get("reply_markup") or q.edit_message_text.await_args.args[1]
    names = [b.text for r in kb.inline_keyboard for b in r]
    assert "Page 3/3" in text and text.count("<b>S0") == 3                # 3 quizzes on the last page
    assert ["1", "2", "·3·"] == [n for n in names if n.strip("·").isdigit()]
    await app.cmd_battle(group_update(a), ctx)                          # same pages inside /battle
    bt_ = next(iter(app._bt_state().values()))
    await press(app, ctx, a, f"bt_my:{bt_.id}:2")
    assert "page 2/3" in app._last_edit.lower()


async def test_my_quiz_open_delete_owner_only_and_battle():
    app, real = _quiz_app()
    ctx, polls = fake_context()
    qs = [{"question": f"Own {i}?", "options": ["x", "y"], "correct_answer": 1} for i in range(4)]
    sid = real.create_quiz_set(1, "Mine", qs, 15)
    a, other = user(1, "Amit"), user(2, "Bina")
    q = await press_mq(app, ctx, other, f"mq_open:{sid}")               # shared link: viewing is allowed
    assert q.answer.await_args.kwargs.get("show_alert") is not True
    q = await press_mq(app, ctx, other, f"mq_del:{sid}")                # …but not deleting
    assert q.answer.await_args.kwargs.get("show_alert") is True
    q = await press_mq(app, ctx, a, f"mq_open:{sid}")
    assert "Mine" in q.edit_message_text.await_args.args[0]
    q = await press_mq(app, ctx, a, f"mq_bt:{sid}")                     # battle with it
    b = [x for x in app._bt_state().values() if x.set_id == sid][0]
    assert b.state == "setup" and (b.n_q, b.secs) == (4, 15)               # confirm page first
    await press(app, ctx, a, f"bt_go:{b.id}")
    assert b.state == "pending" and "Mine" in app._last_edit
    picked = await app._bt_questions(b)
    assert sorted(x["question"] for x in picked) == sorted(x["question"] for x in qs)
    b.state = "done"
    await press_mq(app, ctx, other, f"mq_delok:{sid}")                  # forged delete
    assert real.get_quiz_set(sid) is not None
    await press_mq(app, ctx, a, f"mq_delok:{sid}")
    assert real.get_quiz_set(sid) is None


async def test_group_own_quiz_needs_admin_and_dm_battle_uses_own_questions():
    app, real = _quiz_app()
    ctx, polls = fake_context()
    qs = [{"question": f"Own {i}?", "options": ["x", "y"], "correct_answer": 1} for i in range(3)]
    sid = real.create_quiz_set(1, "Mine", qs, 20)
    a, b = user(1, "Amit"), user(2, "Bina")
    await app.cmd_battle(group_update(a), ctx)
    bt_ = next(iter(app._bt_state().values()))
    await press(app, ctx, a, f"bt_my:{bt_.id}")
    assert "Mine · 3Q" in str(app._bt_my_view(bt_, real.list_quiz_sets(1))[1])
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    q = await press(app, ctx, a, f"bt_s:{bt_.id}:{sid}")
    assert "only admins" in q.answer.await_args.args[0] and bt_.state == "setup"
    q = await press(app, ctx, b, f"bt_s:{bt_.id}:{sid}")                # someone else's button press
    assert q.answer.await_args.kwargs.get("show_alert") is True
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))
    await press(app, ctx, a, f"bt_s:{bt_.id}:{sid}")
    assert bt_.set_id == sid and bt_.state == "setup"                   # quiz chosen → confirm page
    await press(app, ctx, a, f"bt_go:{bt_.id}")
    assert bt_.state == "pending" and bt_.set_id == sid
    bt_.secs = 0.2
    await press(app, ctx, b, f"bt_acc:{bt_.id}")
    await _answer_all(app, polls, 0, {1: 1, 2: 0})
    for _ in range(200):
        if bt_.state == "done":
            break
        await asyncio.sleep(0.01)
    assert len(polls) == 3 and all("Own" in p[2]["question"] for p in polls)
    assert bt_.scores == {1: 3, 2: 0}



async def test_create_own_quiz_from_file_and_too_big_file():
    from telegram.ext import ApplicationHandlerStop
    app, real = _quiz_app()
    ctx, _ = fake_context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    ctx.bot.delete_message = AsyncMock()
    f = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(QTEXT.encode())))
    ctx.bot.get_file = AsyncMock(return_value=f)
    a = user(1, "Amit")
    await app.mq_start_draft(None, ctx, a, dm_update(a))
    big = SimpleNamespace(file_id="x", file_name="huge.pdf", file_size=50 * 1024 * 1024)
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_myquiz_input(dm_update(a, document=big), ctx)
    assert "too big" in ctx.bot.send_message.await_args.args[1] and not ctx.bot.get_file.await_count
    doc = SimpleNamespace(file_id="x", file_name="Polity_Set.txt", file_size=300)
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_myquiz_input(dm_update(a, document=doc), ctx)
    d = ctx.user_data["mq_draft"]
    assert len(d["qs"]) == 3 and d["fname"] == "Polity Set"
    await press_mq(app, ctx, a, "mq_done")
    for step in ("mq_name", "mq_wt:20", "mq_wn:0", "mq_ws:0"):           # suggested name + defaults
        await press_mq(app, ctx, a, step)
    assert real.list_quiz_sets(1)[0]["title"] == "Polity Set"



async def test_two_files_at_once_both_added_and_file_while_naming():
    from telegram.ext import ApplicationHandlerStop
    app, real = _quiz_app()
    ctx, _ = fake_context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    ctx.bot.delete_message = AsyncMock()
    t2 = QTEXT.replace("Capital of India", "Capital of France").replace("2+2", "3+3") \
              .replace("Largest planet", "Smallest planet")
    files = {"a": QTEXT, "b": t2}

    async def get_file(fid, **kw):
        await asyncio.sleep(0.05 if fid == "a" else 0)                 # 'a' slower: real overlap
        return SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(files[fid].encode())))
    ctx.bot.get_file = get_file
    a = user(1, "Amit")
    await app.mq_start_draft(None, ctx, a, dm_update(a))

    async def send(fid):
        doc = SimpleNamespace(file_id=fid, file_name=f"{fid}.txt", file_size=100)
        with pytest.raises(ApplicationHandlerStop):
            await app.handle_myquiz_input(dm_update(a, document=doc), ctx)
    await asyncio.gather(send("a"), send("b"))
    d = ctx.user_data["mq_draft"]
    assert len(d["qs"]) == 6                                          # nothing lost

    await press_mq(app, ctx, a, "mq_done")
    assert d["stage"] == "name"
    files["c"] = "1. Fastest animal?\n(a) Cheetah\n(b) Cow\nAnswer: a\n"
    with pytest.raises(ApplicationHandlerStop):                         # file during naming → added
        await app.handle_myquiz_input(dm_update(a, document=SimpleNamespace(
            file_id="c", file_name="c.txt", file_size=50)), ctx)
    assert d["stage"] == "collect" and len(d["qs"]) == 7


async def test_photo_question_via_ocr_and_card_buttons_side_by_side(monkeypatch):
    from telegram.ext import ApplicationHandlerStop
    import src.bot.file_extract as fe
    monkeypatch.setattr(fe, "_ocr_image_bytes", lambda data: QTEXT)
    app, real = _quiz_app()
    ctx, _ = fake_context()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    ctx.bot.delete_message = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"jpg"))))
    a = user(1, "Amit")
    await app.mq_start_draft(None, ctx, a, dm_update(a))
    photo = [SimpleNamespace(file_id="s", file_size=10), SimpleNamespace(file_id="big", file_size=99)]
    with pytest.raises(ApplicationHandlerStop):
        await app.handle_myquiz_input(dm_update(a, photo=photo), ctx)
    assert len(ctx.user_data["mq_draft"]["qs"]) == 3
    assert ctx.bot.get_file.await_args.args[0] == "big"                # largest size used
    _, kb = app._mq_card_view({"id": "x1", "title": "T", "count": 3, "secs": 20})
    assert [b.text for b in kb.inline_keyboard[-1]] == ["⬅️ My Quizzes", "🏠 Home"]


def test_create_quiz_panel_one_clear_action():
    from src.bot.my_quiz import MyQuizMixin, MIN_Q
    t, kb = MyQuizMixin._mq_collect_view({"qs": [], "problems": []})
    assert [[b.text for b in r] for r in kb.inline_keyboard] == [["❌ Cancel"]]   # no My Quizzes/Home
    assert "1️⃣" in t and "3️⃣ Tap Next" in t
    qs = [{"question": f"Q{i}", "options": ["a", "b"], "correct_answer": 0} for i in range(max(MIN_Q, 3))]
    t, kb = MyQuizMixin._mq_collect_view({"qs": qs, "problems": []})
    rows = [[(b.text, b.style) for b in r] for r in kb.inline_keyboard]
    assert rows[0][0][1] == "success" and rows[0][0][0].startswith("➡️ Next: Set up quiz") and rows[-1] == [("❌ Cancel", "danger")]


async def test_group_create_and_my_quizzes_open_dm_without_posting():
    app, real = _quiz_app()
    ctx, _ = fake_context()
    for data, param in (("mq_new", "mq_new"), ("mq_list", "mq")):
        msg = SimpleNamespace(chat=SimpleNamespace(id=-100, type="supergroup"), message_id=5,
                              reply_text=AsyncMock())
        q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=msg)
        await app.handle_myquiz_callback(SimpleNamespace(callback_query=q, effective_user=user(1, "A")), ctx)
        assert q.answer.await_args.kwargs["url"].endswith(f"?start={param}")
        msg.reply_text.assert_not_awaited()                               # nothing posted in the group
