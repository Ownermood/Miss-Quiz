"""Tests for configuration loading and validation."""

import os
import pytest
from src.core.config import Config
from src.core.exceptions import ConfigurationError


class TestConfig:

    def test_load_defaults(self):
        cfg = Config.load(validate=False)
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 5000
        assert cfg.database_path.endswith("quiz_bot.db")

    def test_validate_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        cfg = Config.load(validate=False)
        cfg.telegram_token = ""
        with pytest.raises(ConfigurationError):
            cfg.validate()

    def test_get_mode_polling_by_default(self, monkeypatch):
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        monkeypatch.delenv("RENDER_URL", raising=False)
        cfg = Config.load(validate=False)
        assert cfg.get_mode() == "polling"

    def test_get_mode_webhook_when_url_set(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_URL", "https://example.com")
        cfg = Config.load(validate=False)
        assert cfg.get_mode() == "webhook"

    def test_get_webhook_url_appends_path(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_URL", "https://example.com")
        cfg = Config.load(validate=False)
        assert cfg.get_webhook_url() == "https://example.com/webhook"

    def test_render_url_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_URL", "https://fallback.com")
        monkeypatch.setenv("RENDER_URL", "https://primary.com")
        cfg = Config.load(validate=False)
        assert cfg.get_webhook_url() == "https://primary.com/webhook"

    def test_owner_id_from_env(self, monkeypatch):
        monkeypatch.setenv("OWNER_ID", "12345")
        cfg = Config.load(validate=False)
        assert cfg.owner_id == 12345

    def test_wifu_id_optional(self, monkeypatch):
        monkeypatch.delenv("WIFU_ID", raising=False)
        cfg = Config.load(validate=False)
        assert cfg.wifu_id is None

    def test_wifu_id_parsed(self, monkeypatch):
        monkeypatch.setenv("WIFU_ID", "99999")
        cfg = Config.load(validate=False)
        assert cfg.wifu_id == 99999

    def test_validate_auto_generates_session_secret(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN", "fake:token")
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        cfg = Config.load(validate=False)
        cfg.session_secret = ""
        cfg.validate()
        assert len(cfg.session_secret) > 0
