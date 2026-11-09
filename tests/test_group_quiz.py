"""Group Quiz: start (admins only), timed polls, scoring, leaderboard, controls, restart resume."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import mongomock
import pytest

import src.bot.group_quiz as gq
from src.bot.battle import BattleMixin
from src.bot.group_quiz import GroupQuizMixin, Session
from src.core.database import DatabaseManager

GROUP = -100500


def questions(n):
    return [{"question": f"Q{i}?", "options": ["right", "wrong"], "correct_answer": 0} for i in range(n)]


class App(GroupQuizMixin, BattleMixin):
    def __init__(self, db_real=None):
        self.quiz_manager = MagicMock()
        self.quiz_manager.questions = questions(40)
        self.db = MagicMock()
        real = db_real or DatabaseManager.__new__(DatabaseManager)
        if db_real is None:
            client = mongomock.MongoClient()
            real.quiz_sets_col = client.db.quiz_sets
            real.quiz_sessions_col = client.db.quiz_sessions
        for fn in ("create_quiz_set", "get_quiz_set", "list_quiz_sets", "save_quiz_session",
                   "delete_quiz_session", "list_quiz_sessions"):
            setattr(self.db, fn, getattr(real, fn))
        self.real = real
        self.replies = []

    async def _reply(self, update, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))
        return SimpleNamespace(message_id=1)


def ctx(admins=(1,)):
    polls, sent = [], []
    counter = iter(range(10_000))

    async def send_poll(**kw):
        pid = f"p{next(counter)}"
        polls.append((pid, kw))
        return SimpleNamespace(poll=SimpleNamespace(id=pid), message_id=2)

    async def send_message(chat_id, text, **kw):
        sent.append(text)
        return SimpleNamespace(message_id=len(sent) + 100)

    async def member(chat_id, uid):
        return SimpleNamespace(status="administrator" if uid in admins else "member")

    bot = MagicMock()
    bot.username = "MissQuiz_Bot"
    bot.send_poll = AsyncMock(side_effect=send_poll)
    bot.send_message = AsyncMock(side_effect=send_message)
    bot.edit_message_text = AsyncMock()
    bot.get_chat_member = AsyncMock(side_effect=member)
    return SimpleNamespace(bot=bot, args=[], user_data={}), polls, sent


def user(uid, name="U"):
    return SimpleNamespace(id=uid, first_name=name, last_name=None, username=None, is_bot=False)


def group_upd(u, chat_id=GROUP):
    msg = SimpleNamespace(sender_chat=None, message_thread_id=None, is_topic_message=False)
    return SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
                           effective_message=msg, message=msg)


async def press(app, c, u, data, chat_id=GROUP):
    msg = SimpleNamespace(chat=SimpleNamespace(id=chat_id), delete=AsyncMock(),
                          message_thread_id=None, is_topic_message=False)
    q = SimpleNamespace(data=data, answer=AsyncMock(), message=msg)
    await app.handle_group_quiz_callback(SimpleNamespace(callback_query=q, effective_user=u), c)
    return q


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gq, "START_DELAY", 0)
    monkeypatch.setattr(gq, "GAP", 0)
    monkeypatch.setattr(gq, "SLACK", 0)
    gq.ACTIVE.clear()
    yield
    for s in list(gq.ACTIVE.values()):
        s.status = "stopped"
        s.wake.set()
    gq.ACTIVE.clear()


async def answer_all(app, polls, answers, upto):
    """Answer every poll as it appears: answers = {uid: option}."""
    seen = 0
    for _ in range(2000):
        while seen < len(polls):
            pid = polls[seen][0]
            for uid, opt in answers.items():
                app.gq_on_poll_answer(pid, user(uid, f"P{uid}"), [opt])
                app.gq_on_poll_answer(pid, user(uid, f"P{uid}"), [1 - opt])   # 2nd answer ignored
            seen += 1
        if seen >= upto:
            return
        await asyncio.sleep(0.005)


async def wait_done(chat_id=GROUP):
    for _ in range(2000):
        if chat_id not in gq.ACTIVE:                                    # finished + result sent
            await asyncio.sleep(0.01)
            return
        await asyncio.sleep(0.005)


async def test_non_admin_cannot_start_and_dm_points_to_group():
    app = App()
    c, _, _ = ctx(admins=())
    await app.cmd_startquiz(group_upd(user(2)), c)
    assert "Only group admins" in app.replies[-1][0]
    dm = SimpleNamespace(effective_user=user(2), effective_chat=SimpleNamespace(id=2, type="private"),
                         effective_message=None)
    await app.cmd_startquiz(dm, c)
    assert "Start in Group" in app.replies[-1][0]


async def test_full_bot_quiz_with_leaderboard_scoring_and_auto_quiz_pause():
    app = App()
    c, polls, sent = ctx()
    admin = user(1, "Admin")
    await app.cmd_startquiz(group_upd(admin), c)
    text, kb = app.replies[-1]
    assert "GROUP QUIZ" in text
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "gq|bot|1|20" in datas
    q = await press(app, c, user(2), "gq|bot|1|20")                     # someone else's menu
    assert q.answer.await_args.kwargs.get("show_alert") is True
    await press(app, c, admin, "gq|bot|1|20")
    s = gq.ACTIVE[GROUP]
    s.secs = 0.05
    assert gq.is_active(GROUP)                                          # scheduler skips this group
    await app.cmd_startquiz(group_upd(admin), c)                        # one session per group
    assert "already running" in app.replies[-1][0]
    await answer_all(app, polls, {7: 0, 8: 1, 9: 0}, 20)
    await wait_done()
    assert len(polls) == 20 and not gq.is_active(GROUP)
    assert all(p[1]["open_period"] == 0.05 and p[1]["type"] == "quiz" for p in polls)
    assert any("After 10 questions" in t for t in sent)
    final = sent[-1]
    assert "FINAL RESULT" in final and "Players › 3" in final
    assert final.index("P7") < final.index("P8")                        # 20/20 ranked above 0/20
    assert "<b>20</b>/20" in final
    assert app.real.list_quiz_sessions() == []                          # cleaned from DB


async def test_controls_admin_only_pause_resume_stop():
    app = App()
    c, polls, sent = ctx(admins=(1,))
    sid = app.real.create_quiz_set(5, "Polity Set", questions(30), 20)
    c.args = [sid]
    await app.cmd_startquiz(group_upd(user(1)), c)
    s = gq.ACTIVE[GROUP]
    s.secs = 0.2
    await asyncio.sleep(0.05)
    assert len(s.questions) == 30 and s.set_id == sid
    q = await press(app, c, user(2), f"gq|stop|{GROUP}")                # member
    assert q.answer.await_args.kwargs.get("show_alert") is True and s.status == "running"
    await press(app, c, user(1), f"gq|pause|{GROUP}")
    assert s.status == "paused"
    assert app.real.list_quiz_sessions()[0]["status"] == "paused"
    n_paused = len(polls)
    await asyncio.sleep(0.5)
    assert len(polls) == n_paused                                       # nothing sent while paused
    await press(app, c, user(1), f"gq|resume|{GROUP}")
    await asyncio.sleep(0.3)
    assert len(polls) > n_paused
    await press(app, c, user(1), f"gq|skip|{GROUP}")
    await press(app, c, user(1), f"gq|stop|{GROUP}")
    await wait_done()
    assert "Stopped after" in sent[-1] and not gq.is_active(GROUP)
    q = await press(app, c, user(1), f"gq|again|{GROUP}")               # Play again
    assert gq.is_active(GROUP) and gq.ACTIVE[GROUP].title == "Polity Set"


async def test_resume_after_restart_from_saved_session():
    app = App()
    c, polls, sent = ctx()
    s = Session(chat_id=GROUP, title="Mock 1", questions=questions(5), secs=1, started_by=1,
                starter_name="Admin", idx=3, scores={"7": {"name": "P7", "correct": 3, "wrong": 0, "time": 3.0}})
    app.real.save_quiz_session(s.to_doc())
    app2 = App(db_real=app.real)                                        # restart: same DB, empty memory
    n = await app2.gq_resume_all(c.bot)
    assert n == 1 and "continuing “Mock 1” from Q4" in sent[0]
    gq.ACTIVE[GROUP].secs = 0.05
    await answer_all(app2, polls, {7: 0}, 2)
    await wait_done()
    assert [p[1]["question"] for p in polls] == ["[4/5] Q3?", "[5/5] Q4?"]
    assert "<b>5</b>/5" in sent[-1]                                    # earlier score kept


async def test_long_question_text_first_and_start_link_needs_admin():
    app = App()
    c, polls, sent = ctx(admins=(1,))
    long_q = [{"question": "Read this passage. " * 30, "options": ["a", "b"], "correct_answer": 1}] * 3
    sid = app.real.create_quiz_set(1, "Passages", long_q, 20)
    await app.gq_start_from_link(group_upd(user(2)), c, sid)             # not admin
    assert "Only group admins" in app.replies[-1][0] and not gq.is_active(GROUP)
    await app.gq_start_from_link(group_upd(user(1)), c, sid)
    gq.ACTIVE[GROUP].secs = 0.05
    await wait_done()
    assert any(t.startswith("❓ <b>Q1/3</b>") for t in sent)
    assert polls[0][1]["question"] == "[1/3] Read the question above ⬆️"


def test_session_scoring_first_answer_and_tiebreak():
    s = Session(chat_id=1, title="t", questions=questions(2), secs=10, started_by=1)
    s.polls["a"] = (0, 0, 0.0)
    assert s.record("a", 5, "A", [0]) and s.record("a", 5, "A", [1])     # 2nd ignored
    assert s.scores["5"]["correct"] == 1 and s.scores["5"]["wrong"] == 0
    assert s.record("zzz", 5, "A", [0]) is False
    s.scores = {"1": {"name": "x", "correct": 2, "wrong": 0, "time": 9.0},
                "2": {"name": "y", "correct": 2, "wrong": 0, "time": 4.0}}
    assert [u for u, _ in s.ranking()] == [2, 1]


def test_quiz_card_has_start_in_group_link():
    from src.bot.my_quiz import MyQuizMixin
    _, kb = MyQuizMixin._mq_card_view({"id": "ab12", "title": "T", "count": 3, "secs": 20},
                                      bot_username="MissQuiz_Bot")
    urls = [b.url for r in kb.inline_keyboard for b in r if b.url]
    assert urls == ["https://t.me/MissQuiz_Bot?startgroup=gq_ab12"]
