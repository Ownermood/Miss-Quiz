"""
premium.py — premium (custom) emoji + styled buttons.

Telegram shows custom emoji from a bot only if the bot OWNER has Telegram
Premium (or the bot bought a Fragment username). So this is opt-in:
PREMIUM_EMOJI=1 → <tg-emoji> / button icons; otherwise the normal emoji.
IDs come from the packs "Peach and Goma" (@TgEmojis) and "UI Animation"
(@technoprint_official).
"""

import os
from typing import Optional

from telegram import InlineKeyboardButton

PREMIUM = os.environ.get("PREMIUM_EMOJI", "0") == "1"

EMOJI = {
    # Peach and Goma (cute reactions)
    "hi":       ("👋", "5424611538151437896"),
    "love":     ("💕", "5424895302345695654"),
    "party":    ("🥳", "5424968454228680999"),
    "cool":     ("😎", "5424663180838182778"),
    "cry":      ("😭", "5424908668283919719"),
    "think":    ("🤨", "5424613565375978984"),
    "wow":      ("🤩", "5424639292230082961"),
    "clap":     ("👏", "5424679359979986283"),
    "flower":   ("🌸", "5424768308752686263"),
    "ok":       ("👌", "5424750497523311050"),
    "fight":    ("👊", "5425027561568608124"),
    "question": ("❓", "5424700761802023186"),
    # UI Animation (icons)
    "check":    ("✅", "5444987348334965906"),
    "cross":    ("❌", "5445092669522996408"),
    "warn":     ("⚠️", "5447381715293074599"),
    "gear":     ("⚙️", "5444869180899752137"),
    "stats":    ("📊", "5445146408153806223"),
    "bell":     ("🔔", "5445333299360723379"),
    "plus":     ("➕", "5447224884562263112"),
    "trash":    ("🗑", "5445005936953424165"),
    "medal":    ("🏅", "5444931419270839381"),
    "star":     ("⭐️", "5444939047132755535"),
    "info":     ("ℹ️", "5247236071795754971"),
    "diamond":  ("💎", "5260681660189408650"),
    "key":      ("🔑", "5445373775132522312"),
    "folder":   ("📂", "5447282724886839705"),
    "play":     ("▶️", "5444883062234053429"),
    "back":     ("⬅️", "5447506720316225765"),
    "clock":    ("⏰", "5445350406215465190"),
    "signal":   ("📡", "5447448489149625830"),
    "users":    ("🫂", "5445372439397691719"),
    "edit":     ("📝", "5444889156792646660"),
    "bolt":     ("⚡️", "5445388803223091254"),
    "door":     ("🚪", "5447434637880098257"),
}


def e(name: str) -> str:
    """Emoji for HTML messages: premium <tg-emoji> if enabled, else the plain one."""
    fallback, cid = EMOJI[name]
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>' if PREMIUM else fallback


def btn(text: str, emoji: Optional[str] = None, style: Optional[str] = None,
        **kw) -> InlineKeyboardButton:
    """Inline button with optional premium icon and colour.
    style: "danger" (red), "success" (green), "primary" (blue).
    Without premium, the plain emoji is put in front of the text instead."""
    icon = None
    if emoji:
        if PREMIUM:
            icon = EMOJI[emoji][1]
        else:
            text = f"{EMOJI[emoji][0]} {text}"
    return InlineKeyboardButton(text, style=style, icon_custom_emoji_id=icon, **kw)
