"""
UI constants, design tokens, and forum-topic helpers.
All other modules import OWNER_ID, OWNER_NAME, OWNER_LINK, COMMUNITY,
_NO_PREVIEW, UI, get_thread_id, and get_tracking_id from here.
"""

import html
import os
from typing import Optional

from telegram import Update, LinkPreviewOptions

# ── Module-level constants ─────────────────────────────────────────────────────
OWNER_ID   = int(os.environ.get("OWNER_ID", "8403136097"))
OWNER_NAME = "🌷 𝐂𝐋𝐀𝐓 𝐎𝐖𝐍𝐄𝐑 🌷"
OWNER_LINK = OWNER_NAME  # plain text only — tg profile links expose user bios
COMMUNITY  = "@CLAT_Vision"
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ══════════════════════════════════════════════════════════════
#  PREMIUM DESIGN SYSTEM
# ══════════════════════════════════════════════════════════════

class UI:
    """Central design token system — all visual constants live here."""

    LINE  = "━" * 30
    THIN  = "─" * 26
    DOT   = "·"

    # ── Progress bar ──────────────────────────────────────────
    @staticmethod
    def bar(pct: float, width: int = 10) -> str:
        filled = max(0, min(width, int(float(pct) / 100 * width)))
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def mini_bar(pct: float, width: int = 5) -> str:
        filled = max(0, min(width, int(float(pct) / 100 * width)))
        return "▰" * filled + "▱" * (width - filled)

    # ── Rank tier system (based on correct answers) ───────────
    @staticmethod
    def rank(score: int) -> tuple:
        """Returns (rank_label, grade_letter)."""
        if   score >= 500: return "👑 LEGEND",   "S"
        elif score >= 200: return "🔱 MASTER",   "A+"
        elif score >= 100: return "⚔️  EXPERT",  "A"
        elif score >= 50:  return "🎯 ADVANCED", "B"
        elif score >= 20:  return "📈 RISING",   "C"
        elif score >= 5:   return "🌱 BEGINNER", "D"
        else:              return "🎲 ROOKIE",   "E"

    # ── XP Level system (score × 10 = XP) ────────────────────
    @staticmethod
    def level(score: int) -> str:
        xp = score * 10
        if   xp >= 10000: return "💠 Legendary"
        elif xp >= 5000:  return "💎 Diamond"
        elif xp >= 2500:  return "🔷 Platinum"
        elif xp >= 1000:  return "🥇 Gold"
        elif xp >= 500:   return "🥈 Silver"
        else:             return "🥉 Bronze"

    @staticmethod
    def xp_bar(score: int) -> str:
        """Show progress within current level."""
        breakpoints = [0, 50, 100, 250, 500, 1000]
        for i, bp in enumerate(breakpoints):
            if score < bp:
                prev = breakpoints[i - 1] if i > 0 else 0
                pct  = (score - prev) / (bp - prev) * 100 if bp > prev else 100
                return UI.mini_bar(pct)
        return "▰▰▰▰▰"

    # ── Medal & ranking display ───────────────────────────────
    MEDALS = ["🥇", "🥈", "🥉"] + ["🏅"] * 20

    @staticmethod
    def rank_badge(pos: int) -> str:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        return medals.get(pos, f"  {pos}.")

    # ── Category system ───────────────────────────────────────
    CATS = {
        "legal":     ("⚖️",  "Legal Reasoning"),
        "english":   ("📖",  "English"),
        "gk":        ("🌐",  "General Knowledge"),
        "current":   ("📰",  "Current Affairs"),
        "polity":    ("🏛️",  "Polity"),
        "math":      ("🔢",  "Mathematics"),
        "reasoning": ("🧠",  "Logical Reasoning"),
        "history":   ("📜",  "History"),
        "default":   ("📚",  "General"),
    }

    @staticmethod
    def cat_emoji(cat: str) -> str:
        cat_lower = (cat or "").lower()
        for k, (emoji, _) in UI.CATS.items():
            if k in cat_lower:
                return emoji
        return UI.CATS["default"][0]

    # ── Inline mention ────────────────────────────────────────
    @staticmethod
    def mention(user_id: int, name: str) -> str:
        """Return a bold display name. No tg:// link — avoids profile card previews."""
        return f'<b>{html.escape(str(name))}</b>'

    # ── Display name (HTML-safe) ──────────────────────────────
    @staticmethod
    def display_name(user) -> str:
        """Returns HTML-safe display name: first_name > full name > @username > 'User'"""
        if not user:
            return "User"
        name = (user.first_name or "").strip()
        if not name and user.last_name:
            name = user.last_name.strip()
        if not name and user.username:
            name = f"@{user.username}"
        if not name:
            name = "User"
        return name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # ── Streak display ────────────────────────────────────────
    @staticmethod
    def streak_display(n: int) -> str:
        if n == 0:
            return "— No streak yet"
        fires = "🔥" * min(n, 5)
        suffix = " 🔥" if n > 5 else ""
        return f"{fires}{suffix} <b>{n} days</b>"

    # ── Number formatter ──────────────────────────────────────
    @staticmethod
    def fmt_num(n: int) -> str:
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000:     return f"{n/1_000:.1f}K"
        return str(n)


# ══════════════════════════════════════════════════════════════
#  FORUM / TOPIC HELPERS
# ══════════════════════════════════════════════════════════════

def get_thread_id(update: Update) -> Optional[int]:
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False):
        return msg.message_thread_id
    return None

def get_tracking_id(chat_id: int, thread_id: Optional[int]) -> int:
    return int(f"{abs(chat_id)}{thread_id}") if thread_id else chat_id
