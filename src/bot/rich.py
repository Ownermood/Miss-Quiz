"""
Telegram Rich Messages (Bot API 10.1+: sendRichMessage / editMessageText rich_message).

python-telegram-bot 22.8 stops at Bot API 10.0, so these go out as raw API calls.
Every helper returns False on failure so the caller can fall back to its plain HTML
message — a rich message must never be the reason a message is lost.

Rules that matter (Rich HTML is stricter than normal HTML mode):
- only the supported tags; e.g. <br> is NOT one (a stray tag rejects the whole message)
- every table cell needs align="left|center|right" and valign="top|middle|bottom"
- table cells hold inline formatting only (no lists/paragraphs inside a cell)
- at most 32,768 characters and 500 blocks (every table row / list item is a block)
- editing a rich message can drop custom emoji (open Telegram bug): edits use plain emoji
"""

import html
import logging
import os
from typing import Iterable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_CHARS = 32768
SAFE_CHARS = 30000          # keep room for premium-emoji tags added later


def rich_on() -> bool:
    return os.getenv("RICH_MESSAGES", "1").strip().lower() not in ("0", "false", "no", "off")


def cell(text: str, tag: str = "td", align: str = "left", valign: str = "middle") -> str:
    """One table cell. `text` must already be safe HTML (escape user text first)."""
    return f'<{tag} align="{align}" valign="{valign}">{text}</{tag}>'


def kv_table(rows: Iterable[Tuple[str, str]]) -> str:
    """Two-column label/value table (values right-aligned, like a receipt)."""
    body = "".join("<tr>" + cell(k) + cell(v, align="right") + "</tr>" for k, v in rows)
    return f"<table bordered striped compact>{body}</table>"


def folded_list(summary: str, items: Sequence[str], budget: int, is_open: bool = False) -> Tuple[str, int]:
    """<details> with a bullet list, cut to fit `budget` characters.
    Items must already be safe HTML. Returns (html, characters used)."""
    if not items:
        return "", 0
    lis, used = [], 0
    for it in items:
        li = f"<li>{it}</li>"
        if used + len(li) > budget:
            break
        lis.append(li); used += len(li)
    more = len(items) - len(lis)
    tail = f"<p><i>+{more} more not shown.</i></p>" if more else ""
    block = (f"<details{' open' if is_open else ''}><summary>{summary}</summary>"
             f"<ul>{''.join(lis)}</ul>{tail}</details>")
    return block, len(block)


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=False)


async def send_rich(bot, chat_id, rich_html: str, *, reply_markup=None, thread_id: Optional[int] = None,
                    edit_message_id: Optional[int] = None, premium: bool = True, tag: str = "RICH") -> bool:
    """Send (or edit into) a rich message. With premium emoji enabled it tries the
    premium version first, then the same message with plain emoji. False = not sent."""
    if not rich_on() or not rich_html or len(rich_html) > MAX_CHARS:
        return False
    versions = [rich_html]
    if premium and edit_message_id is None:          # edits drop custom emoji (Telegram bug): stay plain
        try:
            from src.bot.premium import premium_on, premiumize_html
            if premium_on():
                prem = premiumize_html(rich_html)
                if len(prem) <= MAX_CHARS:
                    versions.insert(0, prem)
        except Exception:
            pass
    method = "editMessageText" if edit_message_id is not None else "sendRichMessage"
    for k, doc in enumerate(versions):
        kw = {"chat_id": chat_id, "rich_message": {"html": doc, "skip_entity_detection": True}}
        if edit_message_id is not None:
            kw["message_id"] = edit_message_id
        elif thread_id:
            kw["message_thread_id"] = thread_id
        if reply_markup is not None:
            kw["reply_markup"] = reply_markup
        try:
            await bot.do_api_request(method, api_kwargs=kw)
            return True
        except Exception as e:
            last = k == len(versions) - 1
            logger.warning(f"[{tag}] rich {method} refused in {chat_id} "
                           f"({'falling back to plain' if last else 'retrying without premium emoji'}): {e}")
    return False
