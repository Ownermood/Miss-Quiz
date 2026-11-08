"""Group gets told when auto quiz turns on/off with the bot's admin status."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.bot.tracking import TrackingMixin


class Bot(TrackingMixin):
    def __init__(self):
        self.db = MagicMock()
        self._seen_groups = set()
        self.ensure_group_registered = MagicMock()


def upd(old, new):
    chat = SimpleNamespace(id=-100, type="supergroup", title="G")
    nm = SimpleNamespace(status=new)
    return SimpleNamespace(my_chat_member=SimpleNamespace(
        chat=chat, old_chat_member=SimpleNamespace(status=old), new_chat_member=nm))


@pytest.mark.parametrize("old,new,expect", [
    ("left", "member", "paused"),             # added without admin
    ("member", "administrator", "ON"),        # promoted
    ("left", "administrator", "ON"),          # added directly as admin
    ("administrator", "member", "paused"),    # demoted
    ("member", "restricted", None),           # no note
])
async def test_admin_status_note(old, new, expect):
    bot = Bot()
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await bot.handle_my_chat_member(upd(old, new), ctx)
    bot.db.update_group_admin_status.assert_called_once()
    assert bot.db.update_group_admin_status.call_args.args[1] is (new == "administrator")
    if expect:
        assert expect in ctx.bot.send_message.call_args.args[1]
    else:
        ctx.bot.send_message.assert_not_awaited()
