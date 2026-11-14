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
                   "delete_quiz_session", "list_quiz_sessions", "set_quiz_progress"):
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
    msg = SimpleNamespace(chat=SimpleNamespace(id=chat_id), delete=AsyncMock(), edit_text=AsyncMock(),
                          message_thread_id=None, is_topic_message=False)
    q = SimpleNamespace(data=data, answer=AsyncMock(), message=msg)
    await app.handle_group_quiz_callback(SimpleNamespace(callback_query=q, effective_user=u), c)
    return q


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gq, "START_DELAY", 0)
    monkeypatch.setattr(gq, "USE_LOBBY", False)
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
    assert "<b>20</b>/20" in final and "CHAMPION" in final and "Congratulations" in final
    assert "<blockquote expandable>" in final and "✅ 0 ❌ 20" in final  # every player listed
    kb = c.bot.send_message.await_args.kwargs["reply_markup"]
    btns = {b.text: b for r in kb.inline_keyboard for b in r}
    assert "🔁 Play again" in btns and "startgroup%3Dgq_bot20" in btns["📤 Share Quiz"].url
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
    s = Session(chat_id=GROUP, title="Mock 1", questions=questions(5), secs=1, started_by=1, status="running",
                starter_name="Admin", idx=3, scores={"7": {"name": "P7", "correct": 3, "wrong": 0, "time": 3.0}})
    app.real.save_quiz_session(s.to_doc())
    app2 = App(db_real=app.real)                                        # restart: same DB, empty memory
    n = await app2.gq_resume_all(c.bot)
    assert n == 1 and "continuing “Mock 1” from Q4" in sent[0]
    gq.ACTIVE[GROUP].secs = 0.05
    await answer_all(app2, polls, {7: 0}, 2)
    await wait_done()
    assert [p[1]["question"] for p in polls] == ["[4/5] 💡 Q3?", "[5/5] 💡 Q4?"]
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
    assert any(t.startswith("❓ <b>Q1/3") for t in sent)
    assert polls[0][1]["question"].endswith("Read the question above ⬆️")


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
    assert "https://t.me/MissQuiz_Bot?startgroup=gq_ab12" in urls



def test_big_group_result_lists_everyone_over_several_messages():
    app = App()
    s = Session(chat_id=1, title="Big", questions=questions(10), secs=10, started_by=1, idx=10)
    for uid in range(1, 151):
        s.scores[str(uid)] = {"name": f"N{uid}", "correct": uid % 11, "wrong": 10 - uid % 11, "time": uid}
    texts = app._gq_result_texts(s, stopped=False)
    assert len(texts) == 3 and "(61–120)" in texts[1] and "Players › 150" in texts[-1]
    assert sum(t.count("tg://user?id=") for t in texts) == 150 + 3      # everyone + the podium
    assert all(len(t) < 9000 for t in texts)
    assert "Nobody answered" in app._gq_result_texts(Session(chat_id=1, title="x", questions=questions(2),
                                                             secs=5, started_by=1), False)[0]


async def test_shared_bot_quiz_link_starts_in_another_group():
    app = App()
    c, polls, sent = ctx(admins=(4,))
    await app.gq_start_from_link(group_upd(user(4), chat_id=-777), c, "bot15")
    s = gq.ACTIVE[-777]
    assert len(s.questions) == 15 and s.title == "Bot Quiz · 15 questions"


async def test_group_lobby_ready_counter_and_toggles():
    app = App()
    c, polls, sent = ctx(admins=(1,))
    host = user(1, "Host")
    p2 = user(2, "Riva")
    s = await app._gq_begin(c, GROUP, None, host, "Mock Test", questions(3), 10, "s1", skip_lobby=False)
    assert s.status == "lobby" and len(s.ready_players) == 1
    await press(app, c, p2, f"gq|ready|{GROUP}")
    assert len(s.ready_players) == 2 and s.ready_players["2"] == "Riva"
    assert s.negative_rate == 0.0                                       # off by default
    await press(app, c, host, f"gq|tog_neg|{GROUP}")
    assert s.negative_rate == 0.25
    await press(app, c, host, f"gq|launch|{GROUP}")
    assert s.status == "running"
    s.secs = 0.05
    await wait_done()


async def test_big_quiz_plays_in_parts_and_continues_next_time():
    app = App()
    c, polls, sent = ctx(admins=(1,))
    sid = app.real.create_quiz_set(1, "Mega Bank", questions(300), 20)
    admin = user(1, "Admin")
    q = await press(app, c, admin, f"gq|set|1|{sid}")                     # picker → part menu
    msg = q.message
    text = msg.edit_text.await_args.args[0]
    kb = msg.edit_text.await_args.kwargs["reply_markup"]
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert "Total › 300" in text and "▶️ Q1–Q200" in labels and not gq.is_active(GROUP)
    await press(app, c, admin, f"gq|part|1|{sid}|200|0")
    s = gq.ACTIVE[GROUP]
    assert len(s.questions) == 200 and s.part_start == 0 and "Q1–Q200" in s.title
    s.secs = 0.01
    await answer_all(app, polls, {7: 0}, 200)
    await wait_done()
    assert app.real.get_quiz_set(sid)["progress"][str(GROUP)] == 200          # saved for next time
    text, kb = await app._gq_part_view(1, sid, app.real.get_quiz_set(sid), GROUP)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert "from Q201" in text and "▶️ Q201–Q300" in labels and "🔁 Start again from Q1" in labels
    q = await press(app, c, user(2), f"gq|part|1|{sid}|100|200")           # someone else's menu
    assert q.answer.await_args.kwargs.get("show_alert") is True


async def test_lobby_pinned_notice_deleted_settings_page_and_unpin_on_cancel():
    app = App()
    c, polls, sent = ctx(admins=(1,))
    c.bot.pin_chat_message = AsyncMock()
    c.bot.unpin_chat_message = AsyncMock()
    c.bot.id = 999
    host = user(1, "Host")
    s = await app._gq_begin(c, GROUP, None, host, "Mock", questions(3), 10, "", skip_lobby=False)
    c.bot.pin_chat_message.assert_awaited_once()
    assert c.bot.pin_chat_message.await_args.kwargs["disable_notification"] is False   # everyone notified
    assert s.pinned and app.real.list_quiz_sessions()[0]["pinned"] is True
    kb = gq._lobby_kb(s)
    assert [[b.text for b in r] for r in kb.inline_keyboard] == [
        ["🙋 I'm Ready (1)"], ["▶️ Start Now", "⏰ Start in 30s"], ["❌ Cancel", "⚙️ Settings"]]
    await press(app, c, host, f"gq|lset|{GROUP}")
    assert s.lobby_page == "settings"
    await press(app, c, host, f"gq|tog_neg|{GROUP}")
    assert s.negative_rate == 0.25 and "✅ Negative −0.25" in str(gq._lobby_kb(s).inline_keyboard)
    await press(app, c, host, f"gq|lback|{GROUP}")
    assert s.lobby_page == "main"
    # the "pinned a message" notice from the bot is deleted, a user's is not
    notice = SimpleNamespace(from_user=SimpleNamespace(id=999), delete=AsyncMock())
    await app.gq_on_pin_notice(SimpleNamespace(effective_message=notice), c)
    notice.delete.assert_awaited_once()
    other = SimpleNamespace(from_user=SimpleNamespace(id=5), delete=AsyncMock())
    await app.gq_on_pin_notice(SimpleNamespace(effective_message=other), c)
    other.delete.assert_not_awaited()
    await press(app, c, host, f"gq|cancel_lobby|{GROUP}")
    c.bot.unpin_chat_message.assert_awaited_once()
    assert not gq.is_active(GROUP)


async def test_host_in_group_link_opens_quiz_picker_for_admins():
    from src.bot.handlers_main import TelegramQuizBot
    b = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    b.application = MagicMock()
    b.application.bot.username = "MissQuiz_Bot"
    urls = [x.url for r in b._start_kb().inline_keyboard for x in r if x.url and "startgroup" in x.url]
    assert urls and all("startgroup=host&admin=" in u for u in urls)         # has a start parameter
    b.cmd_startquiz = AsyncMock()
    c, _, _ = ctx(admins=(1,))
    c.args = ["host"]
    upd = group_upd(user(1))
    upd.effective_user.language_code = "en"
    await b.cmd_start(upd, c)
    b.cmd_startquiz.assert_awaited_once()                                    # → quiz picker (admin check inside)


async def test_start_link_for_big_quiz_replies_with_part_menu_keyboard_not_parse_mode():
    """Bug from the field: /start gq_<id> for a 2217-Q quiz sent the keyboard as parse_mode."""
    app = App()
    calls = []

    async def rep(update, text, *args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(message_id=1)
    app._reply = rep
    c, _, _ = ctx(admins=(1,))
    sid = app.real.create_quiz_set(1, "Master of gk", questions(300), 20)
    await app.gq_start_from_link(group_upd(user(1)), c, sid)
    args, kw = calls[-1]
    assert args == () and "reply_markup" in kw                       # keyboard by name, never positional


async def test_reply_accepts_keyboard_in_parse_mode_slot():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from src.bot.handlers_main import TelegramQuizBot
    b = TelegramQuizBot(MagicMock(), db_manager=None)
    msg = SimpleNamespace(reply_text=AsyncMock(), is_topic_message=False, message_thread_id=None)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=1, type="private"), effective_message=msg)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="x")]])
    await b._reply(upd, "hi", kb)
    kw = msg.reply_text.await_args.kwargs
    assert kw["parse_mode"] == ParseMode.HTML and kw["reply_markup"] is kb


async def test_next_part_button_continues_big_quiz_in_one_tap():
    app = App()
    c, polls, sent = ctx(admins=(1,))
    sid = app.real.create_quiz_set(1, "Master of gk", questions(60), 20)
    admin = user(1, "Admin")
    await press(app, c, admin, f"gq|part|1|{sid}|25|0")
    gq.ACTIVE[GROUP].secs = 0.01
    await answer_all(app, polls, {7: 0}, 25)
    await wait_done()
    kb = c.bot.send_message.await_args.kwargs["reply_markup"]
    first = kb.inline_keyboard[0][0]
    assert first.text == "▶️ Next part · from Q26" and first.callback_data == f"gq|next|{GROUP}|25|25"
    q = await press(app, c, user(2), first.callback_data)                 # member can't
    assert q.answer.await_args.kwargs.get("show_alert") is True and not gq.is_active(GROUP)
    await press(app, c, admin, first.callback_data)
    s = gq.ACTIVE[GROUP]
    assert s.part_start == 25 and len(s.questions) == 25 and "Q26–Q50" in s.title
    s.status = "stopped"; s.wake.set()
    await wait_done()
    q = await press(app, c, admin, f"gq|next|{GROUP}|60|25")              # nothing left
    assert "All questions done" in q.answer.await_args.args[0]


# ── rich final result (Bot API 10.1 sendRichMessage) ─────────────────────────

def test_rich_result_table_top10_rest_collapsed_and_escaped():
    app = App()
    s = Session(chat_id=1, title="GK <Mock> & more", questions=questions(10), secs=10, started_by=1, idx=10,
                negative_rate=0.25)
    for uid in range(1, 26):
        s.scores[str(uid)] = {"name": f"N<{uid}>", "correct": uid % 11, "wrong": 10 - uid % 11,
                              "score": uid % 11 - 0.25 * (10 - uid % 11), "time": 60 + uid}
    doc = app._gq_result_rich_html(s, stopped=False)
    assert "GK &lt;Mock&gt; &amp; more" in doc and "N&lt;3&gt;" in doc and "<Mock>" not in doc
    top, rest = doc.split("<details>")
    assert top.count("<tr>") == 1 + 10 and rest.count("<tr>") == 1 + 15        # header + rows
    assert "Ranks 11–25 (15 more)" in rest and "Negative −0.25" in doc
    assert 'align="left" valign="middle"' in doc                                  # every cell needs both
    assert app._gq_result_rich_html(Session(chat_id=1, title="x", questions=questions(2), secs=5,
                                            started_by=1), False) is None


def test_rich_result_small_group_has_no_collapsed_list():
    app = App()
    s = Session(chat_id=1, title="t", questions=questions(5), secs=10, started_by=1, idx=5)
    s.scores = {"1": {"name": "A", "correct": 5, "wrong": 0, "time": 5.0}}
    doc = app._gq_result_rich_html(s, stopped=True)
    assert "<details>" not in doc and "Stopped after 5 of 5" in doc and "🥇" in doc


async def test_finish_sends_rich_and_skips_plain_or_falls_back(monkeypatch):
    for ok, env, expect_plain in ((True, "1", False), (False, "1", True), (True, "0", True)):
        monkeypatch.setenv("RICH_MESSAGES", env)
        app = App()
        c, polls, sent = ctx()
        c.bot.do_api_request = AsyncMock(return_value={"message_id": 9}) if ok \
            else AsyncMock(side_effect=Exception("Bad Request: method not found"))
        s = Session(chat_id=GROUP, title="Mock", questions=questions(3), secs=1, started_by=1, idx=3,
                    status="running", certificates=False,
                    scores={"7": {"name": "P7", "correct": 3, "wrong": 0, "time": 3.0}})
        gq.ACTIVE[GROUP] = s
        await app._gq_finish(c, s)
        plain = any("FINAL RESULT" in t for t in sent)
        assert plain is expect_plain, (ok, env)
        if env == "1":
            kw = c.bot.do_api_request.await_args.kwargs["api_kwargs"]
            assert c.bot.do_api_request.await_args.args[0] == "sendRichMessage"
            assert kw["chat_id"] == GROUP and "<table" in kw["rich_message"]["html"] and kw["reply_markup"]


# Tags Telegram's Rich HTML accepts (Bot API 10.3 "Rich HTML style"); anything else rejects the message.
RICH_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "mark", "sub", "sup",
             "tg-spoiler", "a", "tg-reference", "tg-emoji", "img", "tg-time", "tg-math", "h1", "h2", "h3",
             "h4", "h5", "h6", "p", "pre", "footer", "hr", "ul", "ol", "li", "input", "blockquote", "cite",
             "aside", "video", "audio", "tg-document", "figure", "figcaption", "tg-map", "tg-collage",
             "tg-slideshow", "table", "caption", "tr", "th", "td", "details", "summary", "tg-math-block",
             "tg-button", "tg-button-row"}


def test_rich_result_uses_only_supported_tags_and_fits_limit(monkeypatch):
    import re
    from src.bot.premium import premiumize_html
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    app = App()
    s = Session(chat_id=1, title="Big", questions=questions(10), secs=10, started_by=1, idx=10)
    for uid in range(1, 451):
        s.scores[str(uid)] = {"name": f"Player number {uid} 🔥", "correct": uid % 11,
                              "wrong": 10 - uid % 11, "time": uid}
    doc = app._gq_result_rich_html(s, stopped=True)
    tags = {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z0-9-]+)", premiumize_html(doc))}
    assert tags <= RICH_TAGS, tags - RICH_TAGS
    assert len(premiumize_html(doc)) < 32768 and doc.count("<tr>") <= 500
    assert "more players. Full list" in doc                     # the rest pointed to the HTML report


async def test_rich_retries_without_premium_before_plain(monkeypatch):
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    monkeypatch.setenv("RICH_MESSAGES", "1")
    app = App()
    c, _, _ = ctx()
    c.bot.do_api_request = AsyncMock(side_effect=[Exception("Bad Request: DOCUMENT_INVALID"), {"message_id": 3}])
    s = Session(chat_id=GROUP, title="t", questions=questions(2), secs=1, started_by=1, idx=2)
    assert await app._gq_send_rich(c, s, "<p>🏆 hi</p>") is True
    first, second = (call.kwargs["api_kwargs"]["rich_message"]["html"] for call in c.bot.do_api_request.await_args_list)
    assert "tg-emoji" in first and "tg-emoji" not in second


async def test_big_quiz_start_from_any_point():
    """2217-question bank: '📍 Start from…' → Q1/Q201/…/Q2201 → size picker → plays from there."""
    app = App()
    c, polls, sent = ctx(admins=(1,))
    sid = app.real.create_quiz_set(1, "GK Bank", questions(2217), 20)
    admin = user(1, "Admin")
    q = await press(app, c, admin, f"gq|set|1|{sid}")
    kb = q.message.edit_text.await_args.kwargs["reply_markup"]
    btn = [b for r in kb.inline_keyboard for b in r if b.text == "📍 Start from…"]
    assert btn and btn[0].callback_data == f"gq|from|1|{sid}|0"

    q = await press(app, c, admin, f"gq|from|1|{sid}|0")                  # grid of starting points
    kb = q.message.edit_text.await_args.kwargs["reply_markup"]
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert labels[:3] == ["Q1", "Q201", "Q401"] and "Q2201" in labels and "More ▶️" not in labels
    assert all(len(b.callback_data.encode()) <= 64 for r in kb.inline_keyboard for b in r)

    q = await press(app, c, admin, f"gq|at|1|{sid}|1000")                  # chose Q1001
    text = q.message.edit_text.await_args.args[0]
    labels = [b.text for r in q.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard for b in r]
    assert "📍 Start › Q1001" in text and "Played here › none yet" in text   # real progress, not 1000
    assert "▶️ Q1001–Q1025" in labels

    q = await press(app, c, user(2), f"gq|at|1|{sid}|1000")                # someone else's menu
    assert q.answer.await_args.kwargs.get("show_alert") is True
    q = await press(app, c, admin, f"gq|at|1|{sid}|-1")                    # Back → normal menu
    assert "Next › from Q1" in q.message.edit_text.await_args.args[0]

    await press(app, c, admin, f"gq|part|1|{sid}|25|1000")
    s = gq.ACTIVE[GROUP]
    assert s.part_start == 1000 and len(s.questions) == 25 and "Q1001–Q1025" in s.title
    assert {q["question"] for q in s.questions} == {f"Q{i}?" for i in range(1000, 1025)}   # exactly that slice


def test_start_from_grid_pages_for_huge_quiz_and_small_quiz_has_no_button():
    app = App()
    qs = {"title": "Huge", "questions": questions(10_000)}
    text, kb = app._gq_from_view(1, "ab12", qs, 0)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert labels.count("More ▶️") == 1 and "◀️ Prev" not in labels and labels[0] == "Q1"
    text, kb = app._gq_from_view(1, "ab12", qs, 2)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    assert labels[0] == f"Q{2 * gq.JUMP_PER_PAGE * gq.JUMP_STEP + 1}" and "◀️ Prev" in labels and "More ▶️" not in labels


async def test_small_quiz_has_no_start_from_button():
    app = App()
    _, kb = await app._gq_part_view(1, "ab12", {"title": "Small", "questions": questions(150)}, GROUP)
    assert "📍 Start from…" not in [b.text for r in kb.inline_keyboard for b in r]


async def test_bot_owner_runs_everything_in_any_group(monkeypatch):
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "OWNER_ID", 4242)
    monkeypatch.setattr(cfg, "WIFU_ID", None)
    app = App()
    c, polls, sent = ctx(admins=(1,))                      # 4242 is NOT an admin of this group
    sid = app.real.create_quiz_set(1, "Bank", questions(300), 20)
    owner, stranger = user(4242, "Owner"), user(77, "Member")
    assert await app._gq_is_admin(c, GROUP, owner) is True
    assert await app._gq_is_admin(c, GROUP, stranger) is False
    q = await press(app, c, owner, f"gq|at|1|{sid}|200")   # menu opened by admin 1, pressed by owner
    assert "📍 Start › Q201" in q.message.edit_text.await_args.args[0]
    q = await press(app, c, stranger, f"gq|at|1|{sid}|200")
    assert q.answer.await_args.kwargs.get("show_alert") is True
    await press(app, c, owner, f"gq|part|1|{sid}|25|200")
    assert gq.is_active(GROUP) and gq.ACTIVE[GROUP].part_start == 200


async def test_no_owner_bypass_when_owner_id_unset(monkeypatch):
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "OWNER_ID", 0)
    monkeypatch.setattr(cfg, "WIFU_ID", None)
    app = App()
    c, _, _ = ctx(admins=(1,))
    assert app._is_bot_owner(0) is False
    assert await app._gq_is_admin(c, GROUP, user(0, "x")) is False


async def test_anonymous_admin_menu_usable_by_real_admin_not_by_members():
    """Live: '/start@MissQuiz_Bot' sent as the group (anonymous admin) → every button said
    'This menu isn't yours', because the command came from GroupAnonymousBot."""
    app = App()
    c, polls, sent = ctx(admins=(1,))
    sid = app.real.create_quiz_set(5, "GK Bank", questions(300), 20)
    anon = group_upd(user(gq.GroupQuizMixin.ANON_ADMIN_BOT, "Group"))
    anon.effective_message.sender_chat = SimpleNamespace(id=GROUP)
    await app.gq_start_from_link(anon, c, sid)
    text, kb = app.replies[-1]
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "Total › 300" in text and f"gq|from|0|{sid}|0" in datas          # owner 0 = any admin

    q = await press(app, c, user(77, "Member"), f"gq|from|0|{sid}|0")       # not an admin
    assert q.answer.await_args.kwargs.get("show_alert") is True and not q.message.edit_text.await_args
    q = await press(app, c, user(77, "Member"), "gq|close|0")
    assert q.answer.await_args.kwargs.get("show_alert") is True and not q.message.delete.await_args

    q = await press(app, c, user(1, "RealAdmin"), f"gq|from|0|{sid}|0")     # the real admin behind it
    assert "Where should this quiz start?" in q.message.edit_text.await_args.args[0]
    await press(app, c, user(1, "RealAdmin"), f"gq|part|0|{sid}|25|200")
    assert gq.is_active(GROUP) and gq.ACTIVE[GROUP].part_start == 200


def test_menu_owner_is_the_user_for_normal_messages():
    upd = group_upd(user(9, "A"))
    assert gq.GroupQuizMixin._gq_menu_owner(upd) == 9
