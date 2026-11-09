"""/admin panel: admins only, DM only, owner-only restart, scoped commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.bot.handlers_main import TelegramQuizBot

OWNER, DEV, RANDOM = 111, 222, 333


@pytest.fixture
def bot(monkeypatch):
    b = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    b._is_owner = lambda uid: uid == OWNER
    b._is_authorized = AsyncMock(side_effect=lambda uid: uid in (OWNER, DEV))
    b._reply = AsyncMock()
    b._edit = AsyncMock()
    b._unauthorized = AsyncMock()
    b._q_count = lambda: 10
    b.db.get_user_engagement_stats.return_value = {"total_users": 5}
    b.db.count_groups_by_admin.return_value = {"total": 4, "admin": 3}
    b.cmd_restart = AsyncMock()
    b.cmd_botstats = AsyncMock()
    return b


def upd(uid, chat_type="private", data=None):
    q = SimpleNamespace(data=data, answer=AsyncMock(), message=MagicMock()) if data else None
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(type=chat_type), callback_query=q)


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_non_admin_blocked(bot):
    await bot.cmd_admin(upd(RANDOM), None)
    bot._unauthorized.assert_awaited_once()
    bot._reply.assert_not_awaited()


async def test_group_redirects_to_dm(bot):
    await bot.cmd_admin(upd(OWNER, "supergroup"), None)
    assert "DM" in bot._reply.await_args.args[1]
    assert "reply_markup" not in bot._reply.await_args.kwargs


async def test_owner_panel_red_buttons_with_restart(bot):
    await bot.cmd_admin(upd(OWNER), None)
    text, kb = bot._reply.await_args.args[1], bot._reply.await_args.kwargs["reply_markup"]
    assert "ADMIN PANEL" in text and "Paused (not admin)  ›  1" in text
    assert "ap:restart" in buttons(kb)
    styles = {b.callback_data: b.style for row in kb.inline_keyboard for b in row}
    assert styles["ap:stats"] == "primary" and styles["ap:restart"] == "success" and styles["ap:close"] == "danger"


async def test_dev_has_no_restart(bot):
    await bot.cmd_admin(upd(DEV), None)
    assert "ap:restart" not in buttons(bot._reply.await_args.kwargs["reply_markup"])


async def test_callback_rechecks_auth(bot):
    u = upd(RANDOM, data="ap:stats")
    await bot.handle_admin_panel_callback(u, None)
    bot.cmd_botstats.assert_not_awaited()
    assert u.callback_query.answer.await_args.kwargs.get("show_alert") is True


async def test_dev_cannot_restart_via_crafted_callback(bot):
    await bot.handle_admin_panel_callback(upd(DEV, data="ap:restart_yes"), None)
    bot.cmd_restart.assert_not_awaited()


async def test_owner_restart_confirm(bot):
    await bot.handle_admin_panel_callback(upd(OWNER, data="ap:restart_yes"), None)
    bot.cmd_restart.assert_awaited_once()


async def test_guide_and_stats(bot):
    await bot.handle_admin_panel_callback(upd(DEV, data="ap:g:bc"), None)
    assert "BROADCAST" in bot._edit.await_args.args[1]
    await bot.handle_admin_panel_callback(upd(DEV, data="ap:stats"), None)
    bot.cmd_botstats.assert_awaited_once()


async def test_menu_has_only_user_commands_and_clears_admin_menus(bot, monkeypatch):
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "AUTHORIZED_USERS", [OWNER])
    bot.db.get_all_developers.return_value = [{"user_id": DEV}]
    bot.application = MagicMock()
    bot.application.bot.set_my_commands = AsyncMock()
    bot.application.bot.delete_my_commands = AsyncMock()
    await bot._set_commands()
    assert bot.application.bot.set_my_commands.await_count == 1        # no per-admin menus
    menu = [c.command for c in bot.application.bot.set_my_commands.await_args.args[0]]
    for admin_cmd in ("admin", "broadcast", "addquiz", "botstats", "reload"):
        assert admin_cmd not in menu
    cleared = {c.kwargs["scope"].chat_id for c in bot.application.bot.delete_my_commands.await_args_list}
    assert cleared == {OWNER, DEV}


async def test_help_is_bold_organized_without_admin_section(bot):
    bot._smart_edit = AsyncMock()
    u = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER))
    await bot.cmd_help(u, None)
    text = bot._smart_edit.await_args.args[1]
    assert "/broadcast" not in text and "/addquiz" not in text and "CLAT" not in text
    assert text.count("<blockquote><b>") == 3 and "/battle" in text
    assert "╔" not in text      # no box drawing that breaks on mobile


def test_registered_and_routed_before_generic():
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler
    b = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    b.application = Application.builder().token("1:a").build()
    b._register_handlers()
    hs = b.application.handlers[0]
    assert any(isinstance(h, CommandHandler) and "admin" in h.commands for h in hs)
    cbs = [h.callback for h in hs if isinstance(h, CallbackQueryHandler)]
    assert cbs.index(b.handle_admin_panel_callback) < cbs.index(b.handle_callback)


# ── start screen + start photo ─────────────────────────────────────────────

def test_start_kb_admin_button_only_when_allowed(bot):
    bot.application = MagicMock(); bot.application.bot.username = "MissQuizBot"
    plain = buttons(bot._start_kb())
    assert "ap:open" not in plain and "start_battle" in plain
    kb = bot._start_kb(show_admin=True)
    last = kb.inline_keyboard[-1][0]
    assert last.callback_data == "ap:open" and last.style == "danger"
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert "https://t.me/VillanCommunity" in urls


def test_greeting_rebranded(bot):
    t = bot._build_greeting("Amit")
    assert "MISS QUIZ" in t and "CLAT" not in t and "<blockquote><b>" in t
    assert "WELCOME TO" not in t
    assert len(t) < 1024          # fits a photo caption


def _input_update(uid, text=None, photo=None):
    sent = MagicMock(); sent.photo = [MagicMock(file_id="FILE_SAVED")]
    msg = MagicMock(text=text, photo=photo)
    msg.reply_photo = AsyncMock(return_value=sent)
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=msg), msg


async def test_photo_link_saved_as_file_id(bot):
    from telegram.ext import ApplicationHandlerStop
    u, msg = _input_update(OWNER, text="https://i.ibb.co/x/pic.jpg")
    ctx = SimpleNamespace(user_data={"ap_await": "start_photo"})
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(u, ctx)
    assert msg.reply_photo.await_args.args[0] == "https://i.ibb.co/x/pic.jpg"
    bot.db.set_setting.assert_called_once_with("start_photo", "FILE_SAVED")
    assert await bot._get_start_photo() == "FILE_SAVED" and "ap_await" not in ctx.user_data


async def test_input_ignored_without_flag_or_for_non_admin(bot):
    u, msg = _input_update(OWNER, text="hello")
    await bot.handle_admin_input(u, SimpleNamespace(user_data={}))       # no flag: passes through
    u, msg = _input_update(RANDOM, text="https://x/y.jpg")
    ctx = SimpleNamespace(user_data={"ap_await": "start_photo"})
    await bot.handle_admin_input(u, ctx)
    msg.reply_photo.assert_not_awaited()
    bot.db.set_setting.assert_not_called()


async def test_bad_image_not_saved(bot):
    from telegram.ext import ApplicationHandlerStop
    u, msg = _input_update(OWNER, text="https://example.com/notimage")
    msg.reply_photo = AsyncMock(side_effect=Exception("wrong type of web page content"))
    ctx = SimpleNamespace(user_data={"ap_await": "start_photo"})
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(u, ctx)
    bot.db.set_setting.assert_not_called()
    assert ctx.user_data["ap_await"] == "start_photo"   # can retry


async def test_start_sends_photo_with_caption(bot):
    bot._start_photo_cache = "FILE_SAVED"
    bot.application = MagicMock(); bot.application.bot.username = "MissQuizBot"
    msg = MagicMock(); msg.reply_photo = AsyncMock(return_value=MagicMock())
    u = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER, first_name="Amit", last_name=None, username=None),
                        effective_chat=SimpleNamespace(id=OWNER, type="private"),
                        effective_message=msg, message=msg)
    await bot.cmd_start(u, SimpleNamespace(args=None))
    kw = msg.reply_photo.await_args.kwargs
    assert msg.reply_photo.await_args.args[0] == "FILE_SAVED" and "MISS QUIZ" in kw["caption"]
    assert "ap:open" in buttons(kw["reply_markup"])          # owner in DM sees admin button


# ── every admin command is a working button ────────────────────────────────

def _cb(uid, data):
    u = upd(uid, data=data)
    u.callback_query.message = MagicMock()
    return u


def _ctx():
    return SimpleNamespace(user_data={}, args=None)


def test_panel_has_a_button_for_every_admin_command(bot):
    cbs = set(buttons(bot._panel_kb(True)))
    for need in ("ap:stats", "ap:reload", "ap:addquiz", "ap:import", "ap:edit", "ap:delquiz",
                 "ap:bc", "ap:dbc", "ap:photo", "ap:team", "ap:restart", "ap:close"):
        assert need in cbs


@pytest.mark.parametrize("data,handler", [
    ("ap:import", "cmd_importquiz"), ("ap:delquiz", "cmd_delquiz"), ("ap:edit", "cmd_editquiz"),
])
async def test_one_tap_commands_run_without_reply_target(bot, data, handler):
    setattr(bot, handler, AsyncMock())
    await bot.handle_admin_panel_callback(_cb(DEV, data), _ctx())
    passed = getattr(bot, handler).await_args.args[0]
    assert passed.message.reply_to_message is None       # panel msg must not look like a reply
    assert passed.effective_user.id == DEV


async def test_addquiz_button_then_text_runs_addquiz(bot):
    from telegram.ext import ApplicationHandlerStop
    ctx = _ctx()
    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:addquiz"), ctx)
    assert ctx.user_data["ap_await"] == "addquiz"
    bot.cmd_addquiz = AsyncMock()
    u = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER), effective_message=MagicMock(text="Q?\nA\nB\nC\nD\n1\nGK"))
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(u, ctx)
    bot.cmd_addquiz.assert_awaited_once()
    assert "ap_await" not in ctx.user_data


async def test_broadcast_text_then_confirm_buttons(bot):
    from telegram.ext import ApplicationHandlerStop
    bot._dev = MagicMock(); bot._dev.broadcast = AsyncMock(); bot._dev.broadcast_confirm = AsyncMock()
    ctx = _ctx(); ctx.user_data["ap_await"] = "broadcast"
    u = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER), effective_message=MagicMock(text="Hello all", photo=None))
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(u, ctx)
    assert ctx.args == ["Hello", "all"]
    assert "ap:bc_yes" in buttons(bot._reply.await_args.kwargs["reply_markup"])
    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:bc_yes"), ctx)
    bot._dev.broadcast_confirm.assert_awaited_once()


async def test_broadcast_media_is_sent_as_replied_message(bot):
    from telegram.ext import ApplicationHandlerStop
    bot._dev = MagicMock(); bot._dev.broadcast = AsyncMock()
    ctx = _ctx(); ctx.user_data["ap_await"] = "broadcast"
    media = MagicMock(text=None)
    u = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER), effective_message=media, message=media)
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(u, ctx)
    passed = bot._dev.broadcast.await_args.args[0]
    assert passed.message.reply_to_message is media


@pytest.fixture
def owner_only(monkeypatch):
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "AUTHORIZED_USERS", [OWNER])


def _msg(text=None, entities=None, origin=None):
    m = MagicMock(text=text, entities=entities or [], forward_origin=origin, photo=None)
    m.reply_text = AsyncMock()
    return m


async def test_add_admin_full_flow_with_premium_emoji(bot, owner_only):
    from telegram.ext import ApplicationHandlerStop
    from telegram import MessageEntity
    bot.db.add_developer = MagicMock(); bot.db.set_admin_profile = MagicMock()
    bot.db.get_all_developers.return_value = []
    ctx = _ctx(); ctx.bot = MagicMock()
    ctx.bot.get_chat = AsyncMock(return_value=SimpleNamespace(full_name="Rahul", first_name="Rahul"))
    ctx.bot.send_message = AsyncMock()

    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:adm_add"), ctx)
    assert ctx.user_data["ap_await"] == "adm_id"
    owner = SimpleNamespace(id=OWNER)
    for text, ents in (("4455", None), ("Quiz Master", None),
                       ("🔥", [MessageEntity("custom_emoji", 0, 1, custom_emoji_id="5116414868357907335")])):
        u = SimpleNamespace(effective_user=owner, effective_message=_msg(text, ents))
        with pytest.raises(ApplicationHandlerStop):
            await bot.handle_admin_input(u, ctx)

    bot.db.add_developer.assert_called_once()
    assert bot.db.add_developer.call_args.args[0] == 4455
    bot.db.set_admin_profile.assert_called_once_with(4455, "Quiz Master", "🔥", "5116414868357907335")
    dm_to, dm_text = ctx.bot.send_message.await_args.args
    assert dm_to == 4455 and "promoted to Admin" in dm_text and "Quiz Master" in dm_text
    assert "ap_await" not in ctx.user_data


async def test_developer_cannot_add_admins(bot, owner_only):
    ctx = _ctx()
    u = _cb(DEV, "ap:adm_add")
    await bot.handle_admin_panel_callback(u, ctx)
    assert "ap_await" not in ctx.user_data
    assert u.callback_query.answer.await_args.kwargs.get("show_alert") is True
    ctx.user_data["ap_await"] = "adm_id"             # even with a forged state
    from telegram.ext import ApplicationHandlerStop
    bot.db.add_developer = MagicMock()
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(SimpleNamespace(effective_user=SimpleNamespace(id=DEV),
                                                     effective_message=_msg("4455")), ctx)
    bot.db.add_developer.assert_not_called()


async def test_add_admin_by_forward_and_skips(bot, owner_only):
    from telegram.ext import ApplicationHandlerStop
    bot.db.add_developer = MagicMock(); bot.db.set_admin_profile = MagicMock()
    bot.db.get_all_developers.return_value = []
    ctx = _ctx(); ctx.bot = MagicMock(); ctx.bot.send_message = AsyncMock(side_effect=Exception("Forbidden"))
    ctx.user_data["ap_await"] = "adm_id"
    fwd = SimpleNamespace(sender_user=SimpleNamespace(id=777, full_name="Neha"))
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_admin_input(SimpleNamespace(effective_user=SimpleNamespace(id=OWNER),
                                                     effective_message=_msg("hi", origin=fwd)), ctx)
    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:adm_skip"), ctx)     # skip title
    assert ctx.user_data["ap_await"] == "adm_emoji"
    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:adm_skip"), ctx)     # skip emoji → save
    bot.db.set_admin_profile.assert_called_once_with(777, "", "", "")
    assert "Couldn't message them" in bot._edit.await_args.args[1]          # DM failed → owner told


async def test_remove_admin_confirms_and_notifies(bot, owner_only):
    bot.db.remove_developer = MagicMock(return_value=True)
    bot.db.get_all_developers.return_value = []
    ctx = _ctx(); ctx.bot = MagicMock(); ctx.bot.send_message = AsyncMock()
    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:adm_rmc:777"), ctx)
    bot.db.remove_developer.assert_not_called()                              # confirm first
    await bot.handle_admin_panel_callback(_cb(OWNER, "ap:adm_rmy:777"), ctx)
    bot.db.remove_developer.assert_called_once_with(777)
    assert ctx.bot.send_message.await_args.args[0] == 777


async def test_slash_dev_add_is_owner_only(owner_only):
    from src.bot.dev_commands import DeveloperCommands
    dev = DeveloperCommands(MagicMock(), MagicMock())
    dev.check_access = AsyncMock(return_value=True)
    msg = MagicMock(); msg.reply_text = AsyncMock(); msg.reply_to_message = None
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=DEV, username="dev", first_name="Dev", last_name=None),
                          effective_chat=SimpleNamespace(id=1, type="private"),
                          message=msg, effective_message=msg)
    await dev.dev(upd, SimpleNamespace(args=["add", "999"]))
    dev.db.add_developer.assert_not_called()
    assert "Only the owner" in msg.reply_text.await_args.args[0]


async def test_cancel_clears_pending_input(bot):
    ctx = _ctx(); ctx.user_data["ap_await"] = "broadcast"
    await bot.cmd_cancel(upd(OWNER), ctx)
    assert "ap_await" not in ctx.user_data


# ── outgoing messages: premium emoji everywhere + red Back ─────────────────

async def _capture_post(monkeypatch, premium, endpoint="sendMessage", **data):
    from src.bot.premium import make_premium_bot_class
    from telegram.ext import ExtBot
    monkeypatch.setenv("PREMIUM_EMOJI", "1" if premium else "0")
    seen = {}

    async def fake(self, endpoint, data, *a, **k):
        seen.update(data); return True
    monkeypatch.setattr(ExtBot, "_do_post", fake)
    bot = make_premium_bot_class()("123:abc")
    await bot._do_post(endpoint, dict(data))
    return seen


async def test_every_message_emoji_becomes_premium(monkeypatch):
    from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
    kb = M([[B("🏆 Leaderboard", callback_data="x"), B("⬅️ Back", callback_data="y")]])
    out = await _capture_post(monkeypatch, True, text="🎓 <b>Hi</b> 1 < 2", parse_mode="HTML", reply_markup=kb)
    assert out["text"].startswith('<b><tg-emoji emoji-id="') and out["text"].endswith("</b>")
    b1, b2 = out["reply_markup"].inline_keyboard[0]
    assert b1.text == "Leaderboard" and b1.icon_custom_emoji_id
    assert b2.style == "danger"


async def test_plain_text_message_made_safe_html(monkeypatch):
    out = await _capture_post(monkeypatch, True, text="Score <5 ✅")
    assert out["parse_mode"] == "HTML" and "&lt;5" in out["text"] and "tg-emoji" in out["text"]


async def test_premium_off_leaves_text_but_back_still_red(monkeypatch):
    from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
    out = await _capture_post(monkeypatch, False, text="✅ ok", parse_mode="HTML",
                              reply_markup=M([[B("🔙 Back", callback_data="y")]]))
    assert out["text"] == "<b>✅ ok</b>" and "tg-emoji" not in out["text"]
    assert out["reply_markup"].inline_keyboard[0][0].style == "danger"


async def test_callback_popup_text_untouched(monkeypatch):
    out = await _capture_post(monkeypatch, True, endpoint="answerCallbackQuery", text="✅ Saved <ok>")
    assert out["text"] == "✅ Saved <ok>" and "parse_mode" not in out


async def test_quiz_poll_question_and_explanation_premium(monkeypatch):
    out = await _capture_post(monkeypatch, True, endpoint="sendPoll",
                              question="📚 Is 2 < 3?", explanation="✅ Yes · 📚 Math", options=["A", "B"])
    assert out["question_parse_mode"] == "HTML" and "&lt;" in out["question"] and "tg-emoji" in out["question"]
    assert out["explanation_parse_mode"] == "HTML" and "tg-emoji" in out["explanation"]
    assert out["options"] == ["A", "B"]


async def test_rejected_premium_is_resent_plain(monkeypatch):
    from src.bot.premium import make_premium_bot_class
    from telegram.ext import ExtBot
    from telegram.error import BadRequest
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    calls = []

    async def fake(self, endpoint, data, *a, **k):
        calls.append(dict(data))
        if "tg-emoji" in str(data.get("text")):
            raise BadRequest("Can't parse entities: custom emoji not allowed")
        return True
    monkeypatch.setattr(ExtBot, "_do_post", fake)
    bot = make_premium_bot_class()("123:abc")
    assert await bot._do_post("sendMessage", {"text": "✅ hi", "parse_mode": "HTML"}) is True
    assert len(calls) == 2 and calls[1]["text"] == "✅ hi"


# ── edit-in-place flow, bold/gaps, live stats ──────────────────────────────

async def test_panel_command_edits_panel_message_with_back():
    from src.bot.admin_panel import _MsgProxy
    panel = MagicMock(); panel.edit_text = AsyncMock(return_value="edited"); panel.reply_text = AsyncMock()
    proxy = _MsgProxy(panel, edit=True)
    assert await proxy.reply_text("Reloaded", parse_mode="HTML") == "edited"
    panel.reply_text.assert_not_awaited()                     # no new message in chat
    kb = panel.edit_text.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[-1][0].callback_data == "ap:home"


async def test_panel_edit_falls_back_to_new_message_on_photo():
    from src.bot.admin_panel import _MsgProxy
    panel = MagicMock()
    panel.edit_text = AsyncMock(side_effect=Exception("There is no text in the message to edit"))
    panel.reply_text = AsyncMock(return_value="new")
    assert await _MsgProxy(panel, edit=True).reply_text("x", message_thread_id=5) == "new"


async def test_bold_and_gaps_on_every_message(monkeypatch):
    out = await _capture_post(monkeypatch, False, text="<b>A</b>\n\n\n\nB", parse_mode="HTML")
    assert out["text"] == "<b>A\n\nB</b>"
    out = await _capture_post(monkeypatch, False, text="Use <code>/x</code>", parse_mode="HTML")
    assert out["text"] == "Use <code>/x</code>"           # code can't be inside bold


def test_group_analytics_counts_only_real_admin_groups():
    mongomock = pytest.importorskip("mongomock")
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    db.groups_col = mongomock.MongoClient().db.g
    db.groups_col.insert_many([{"bot_is_admin": True}, {"bot_is_admin": False}, {}])
    d = db.get_group_analytics()
    assert (d["g_total"], d["g_admin"], d["g_paused"]) == (3, 1, 1)


def test_activity_analytics_live_counts():
    mongomock = pytest.importorskip("mongomock")
    from datetime import datetime, timedelta
    from src.core.database import DatabaseManager
    db = DatabaseManager.__new__(DatabaseManager)
    db.activities_col = mongomock.MongoClient().db.a
    now = datetime.utcnow()
    ts = lambda h: (now - timedelta(hours=h)).isoformat()
    db.activities_col.insert_many([
        {"type": "quiz_answer", "timestamp": ts(0.1), "is_correct": True, "user_id": 1},
        {"type": "quiz_answer", "timestamp": ts(5), "is_correct": False, "user_id": 2},
        {"type": "quiz_answer", "timestamp": ts(30), "is_correct": True, "user_id": 3},
        {"type": "command", "timestamp": ts(0.1)},
    ])
    assert db.get_activity_analytics() == {"a_day": 2, "a_day_ok": 1, "a_hour": 1, "a_players": 2}


async def test_botstats_live_text_and_back_to_panel(bot):
    bot.db.get_user_analytics.return_value = {"u_total": 9}
    bot.db.get_group_analytics.return_value = {"g_total": 4, "g_admin": 3, "g_paused": 1}
    bot.db.get_content_analytics.return_value = {"q_cats": 2}
    bot.db.get_activity_analytics.return_value = {"a_day": 10, "a_day_ok": 7, "a_hour": 3, "a_players": 5}
    bot.db.get_db_stats.return_value = {"objects": 50}
    from src.bot.commands.quiz_cmds import QuizCommandsMixin
    msg = MagicMock(); msg.edit_text = AsyncMock()
    await QuizCommandsMixin.cmd_botstats(bot, upd(OWNER), None, edit_msg=msg)
    text, kb = bot._edit.await_args.args[1], bot._edit.await_args.args[2]
    assert "accuracy 70%" in text and "Answers (last hour) › 3" in text and "Updated" in text
    assert "Paused (not admin) › 1" in text
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["bs_refresh", "ap:home"]


def test_refresh_button_is_routed():
    from telegram.ext import Application, CallbackQueryHandler
    b = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    b.application = Application.builder().token("1:a").build()
    b._register_handlers()
    cbs = [h for h in b.application.handlers[0] if isinstance(h, CallbackQueryHandler)]
    h = next(h for h in cbs if h.callback == b._cb_botstats_refresh)
    assert h.pattern.match("bs_refresh")
    assert cbs.index(h) < [x.callback for x in cbs].index(b.handle_callback)


def test_emoji_test_pages_cover_every_mapped_emoji(bot, monkeypatch):
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    from src.bot.premium import EMOJI_ID, premiumize_html
    seen, page = [], 0
    while True:
        text, kb = bot._emoji_test_page(page)
        assert premiumize_html(text).count("<tg-emoji") <= 60       # per-message custom emoji budget
        seen += [tok.split(".", 1)[1].lstrip("≈") for tok in text.split("\n", 4)[-1].split() if "." in tok]
        datas = [b.callback_data for r in kb.inline_keyboard for b in r]
        if f"ap:emoji:{page + 1}" not in datas:
            break
        page += 1
    assert set(seen) == set(EMOJI_ID)


def test_keycap_and_new_pack_emojis_premium(monkeypatch):
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    from src.bot.premium import premiumize_html, EXACT
    out = premiumize_html("1️⃣ 🔥 10")
    assert out.count("<tg-emoji") == 2 and out.endswith(" 10")      # plain digits untouched
    assert "🔥" in EXACT and "💪" in EXACT
