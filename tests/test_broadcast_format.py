"""/broadcast keeps Telegram formatting, line breaks and buttons."""

import datetime
from unittest.mock import AsyncMock, MagicMock
from telegram import Chat, Message, MessageEntity, User

from src.bot.dev_commands import DeveloperCommands


def make_dev():
    db = MagicMock()
    db.get_pm_accessible_users.return_value = [{"user_id": 1}]
    db.get_active_groups.return_value = [{"chat_id": -1}]
    dev = DeveloperCommands(db, MagicMock())
    dev.check_access = AsyncMock(return_value=True)
    return dev


def make_update(text, entities):
    msg = Message(1, datetime.datetime.now(), Chat(5, "private"), text=text,
                  entities=entities, from_user=User(5, "Owner", False))
    upd = MagicMock()
    upd.message = msg
    upd.effective_user = msg.from_user
    upd.effective_chat = msg.chat
    return upd


async def test_text_broadcast_keeps_formatting_newlines_and_buttons(monkeypatch):
    text = '/broadcast Hello team\nBig news & more\n[["Join","https://t.me/x?a=1&b=2"]]'
    ents = [MessageEntity("bot_command", 0, 10), MessageEntity("bold", 11, 5),
            MessageEntity("blockquote", 22, 15)]
    upd = make_update(text, ents)
    ctx = MagicMock()
    ctx.args = text.split()[1:]
    ctx.user_data = {}
    monkeypatch.setattr(Message, "reply_text", AsyncMock())
    await make_dev().broadcast(upd, ctx)

    saved = ctx.user_data["broadcast_message"]
    assert saved.startswith("<b>Hello</b> team\n")            # bold + newline kept
    assert "<blockquote>Big news &amp; more</blockquote>" in saved
    assert "[[" not in saved and "/broadcast" not in saved
    btn = ctx.user_data["broadcast_buttons"].inline_keyboard[0][0]
    assert btn.text == "Join" and btn.url == "https://t.me/x?a=1&b=2"   # URL not escaped


async def test_placeholders_are_html_escaped():
    dev = make_dev()
    out = await dev.replace_placeholders("Hi {first_name}", 1, MagicMock(),
                                         user_data={"first_name": "<b>A&B</b>"}, bot_name_cache="Bot")
    assert out == "Hi &lt;b&gt;A&amp;B&lt;/b&gt;"


async def test_confirm_preview_is_plain_text_not_raw_html(monkeypatch):
    """Live: preview showed '<tg-emoji emoji-id=…> <b>Roses…</b> I&#x27;ll' as literal text."""
    text = "😎 Roses are red, CLAT clear karna hai? Baby, I'll be right there for you 🌹"
    ents = [MessageEntity("custom_emoji", 0, 2, custom_emoji_id="5424663180838182778"),
            MessageEntity("bold", 3, len(text) - 3)]
    upd = make_update(text, ents)
    ctx = MagicMock()
    ctx.args = text.split()
    ctx.user_data = {}
    reply = AsyncMock()
    monkeypatch.setattr(Message, "reply_text", reply)
    await make_dev().broadcast(upd, ctx)

    preview = reply.call_args.args[0]
    assert "Message: 😎 Roses are red, CLAT clear karna hai? Baby, I'll be right there for you 🌹" in preview
    assert "<" not in preview and "&#x27;" not in preview
    saved = ctx.user_data["broadcast_message"]                  # what gets sent keeps the formatting
    assert "<tg-emoji" in saved and "<b>" in saved
