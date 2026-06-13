"""Tests for UI design system helpers."""

import pytest
from src.bot.ui import UI, get_thread_id, get_tracking_id
from unittest.mock import MagicMock


class TestUIBar:

    def test_full_bar(self):
        assert UI.bar(100) == "█" * 10

    def test_empty_bar(self):
        assert UI.bar(0) == "░" * 10

    def test_half_bar(self):
        b = UI.bar(50)
        assert "█" in b and "░" in b
        assert len(b) == 10

    def test_clamps_over_100(self):
        assert UI.bar(200) == "█" * 10

    def test_clamps_below_0(self):
        assert UI.bar(-10) == "░" * 10


class TestUIRank:

    def test_rookie_at_zero(self):
        label, grade = UI.rank(0)
        assert "ROOKIE" in label
        assert grade == "E"

    def test_legend_at_500(self):
        label, grade = UI.rank(500)
        assert "LEGEND" in label
        assert grade == "S"

    def test_master_at_200(self):
        label, _ = UI.rank(200)
        assert "MASTER" in label

    def test_expert_at_100(self):
        _, grade = UI.rank(100)
        assert grade == "A"


class TestUILevel:

    def test_bronze_at_zero(self):
        assert "Bronze" in UI.level(0)

    def test_gold_at_100(self):
        assert "Gold" in UI.level(100)

    def test_legendary_at_1000(self):
        assert "Legendary" in UI.level(1000)


class TestUICatEmoji:

    def test_legal_emoji(self):
        assert UI.cat_emoji("legal") == "⚖️"

    def test_english_emoji(self):
        assert UI.cat_emoji("english") == "📖"

    def test_unknown_returns_default(self):
        assert UI.cat_emoji("something_random") == "📚"

    def test_case_insensitive(self):
        assert UI.cat_emoji("LEGAL Reasoning") == "⚖️"


class TestUIDisplayName:

    def _user(self, first=None, last=None, username=None):
        u = MagicMock()
        u.first_name = first
        u.last_name  = last
        u.username   = username
        return u

    def test_first_name_preferred(self):
        assert UI.display_name(self._user(first="Alice")) == "Alice"

    def test_falls_back_to_username(self):
        name = UI.display_name(self._user(username="bob"))
        assert "bob" in name

    def test_none_user_returns_user(self):
        assert UI.display_name(None) == "User"

    def test_html_special_chars_escaped(self):
        name = UI.display_name(self._user(first="<script>"))
        assert "<script>" not in name
        assert "&lt;" in name


class TestUIFmtNum:

    def test_small_number(self):
        assert UI.fmt_num(42) == "42"

    def test_thousands(self):
        assert "K" in UI.fmt_num(1500)

    def test_millions(self):
        assert "M" in UI.fmt_num(2_000_000)


class TestThreadHelpers:

    def test_get_thread_id_none_when_no_topic(self):
        update = MagicMock()
        update.effective_message.is_topic_message = False
        assert get_thread_id(update) is None

    def test_get_thread_id_returns_id_for_topic(self):
        update = MagicMock()
        update.effective_message.is_topic_message = True
        update.effective_message.message_thread_id = 42
        assert get_thread_id(update) == 42

    def test_get_tracking_id_without_thread(self):
        assert get_tracking_id(-100123, None) == -100123

    def test_get_tracking_id_with_thread(self):
        tid = get_tracking_id(-100123, 5)
        assert tid != -100123
