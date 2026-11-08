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
