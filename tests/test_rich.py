"""Telegram Rich Messages: only supported tags, size limits, and a safe fallback."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot import rich
from src.bot.commands.admin_cmds import AdminCommandsMixin
from tests.test_group_quiz import RICH_TAGS


def _tags(doc):
    return {t.lower() for t in re.findall(r"<\s*/?\s*([a-zA-Z0-9-]+)", doc)}


def _report(n_problems=589, n_dups=410):
    probs = [{"where": f"Q{i}", "reason": "no answer found (add 'Answer: B', mark ✅, or an answer key)"}
             for i in range(n_problems)]
    dups = [{"where": f"“Question <{i}> & text”", "reason": "already in the bot"} for i in range(n_dups)]
    return AdminCommandsMixin._import_report_rich('<a href="tg://user?id=1">Owner</a>', "Law <MCQ>.pdf", 1000,
                                                  0, n_dups, probs, 0, 0, 2400, 0.4, dups, ["E11000 dup"])


def test_import_report_rich_supported_tags_all_problems_and_limit():
    doc = _report(589)
    assert _tags(doc) <= RICH_TAGS, _tags(doc) - RICH_TAGS
    assert len(doc) < rich.SAFE_CHARS and doc.count("<li>") + doc.count("<tr>") <= 500
    assert "Fix these (589)" in doc and "<details open>" in doc        # problems shown open
    assert "Law &lt;MCQ&gt;.pdf" in doc and "Question &lt;0&gt; &amp; text" in doc
    assert doc.count("<li><b>Q") > 10                                  # more than the plain report's 10


def test_import_report_small_has_every_item():
    doc = _report(3, 2)
    assert all(f"<b>Q{i}</b>" in doc for i in range(3)) and "more not shown" not in doc


async def test_send_rich_edit_stays_plain_and_failure_returns_false(monkeypatch):
    monkeypatch.setenv("PREMIUM_EMOJI", "1")
    monkeypatch.setenv("RICH_MESSAGES", "1")
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value=True)
    assert await rich.send_rich(bot, 5, "<p>🏆 x</p>", edit_message_id=9) is True
    name, = bot.do_api_request.await_args.args
    kw = bot.do_api_request.await_args.kwargs["api_kwargs"]
    assert name == "editMessageText" and kw["message_id"] == 9 and "tg-emoji" not in kw["rich_message"]["html"]
    bot.do_api_request = AsyncMock(side_effect=Exception("Bad Request: can't parse rich message"))
    assert await rich.send_rich(bot, 5, "<p>x</p>") is False
    monkeypatch.setenv("RICH_MESSAGES", "0")
    assert await rich.send_rich(bot, 5, "<p>x</p>") is False
    assert await rich.send_rich(bot, 5, "x" * (rich.MAX_CHARS + 1)) is False
