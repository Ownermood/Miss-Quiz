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
    assert "ADMIN PANEL" in text and "Paused (not admin)  ›  <b>1</b>" in text
    assert "ap:restart" in buttons(kb)
    styles = {b.callback_data: b.style for row in kb.inline_keyboard for b in row}
    assert styles["ap:stats"] == "primary" and styles["ap:restart"] == "danger"


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


async def test_admin_commands_only_in_admin_scope(bot, monkeypatch):
    import src.core.config as cfg
    monkeypatch.setattr(cfg, "AUTHORIZED_USERS", [OWNER])
    bot.db.get_all_developers.return_value = [{"user_id": DEV}]
    bot.application = MagicMock()
    bot.application.bot.set_my_commands = AsyncMock()
    await bot._set_commands()
    calls = bot.application.bot.set_my_commands.await_args_list
    default = [c.command for c in calls[0].args[0]]
    assert "admin" not in default and "broadcast" not in default
    scoped = {c.kwargs["scope"].chat_id: [x.command for x in c.args[0]] for c in calls[1:]}
    assert set(scoped) == {OWNER, DEV} and "admin" in scoped[OWNER]


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
    assert "MISS QUIZ" in t and "CLAT" not in t and "<blockquote>" in t
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
