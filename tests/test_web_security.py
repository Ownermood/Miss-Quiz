"""Security tests for the Flask admin API and Telegram webhook."""

from unittest.mock import MagicMock
import pytest

import src.web.app as webapp
from src.core.config import webhook_secret


@pytest.fixture
def client(monkeypatch):
    qm = MagicMock()
    qm.questions = [{"id": 1, "question": "Q?"}]
    monkeypatch.setattr(webapp, "quiz_manager", qm)
    return webapp.app.test_client()


# ── Admin API ───────────────────────────────────────────────────────────────

def test_api_blocked_when_key_not_configured(client, monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("DYNO", raising=False)
    assert client.get("/api/questions").status_code == 401


def test_api_rejects_wrong_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    r = client.get("/api/questions", headers={"X-Admin-Key": "nope"})
    assert r.status_code == 401


def test_api_rejects_key_in_query_string(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    assert client.get("/api/questions?api_key=s3cret").status_code == 401


def test_api_accepts_correct_header(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    r = client.get("/api/questions", headers={"X-Admin-Key": "s3cret"})
    assert r.status_code == 200 and r.get_json()["total"] == 1


@pytest.mark.parametrize("method,path", [
    ("post", "/api/questions"), ("put", "/api/questions/1"),
    ("delete", "/api/questions/1"), ("get", "/api/users"),
    ("post", "/api/broadcast"), ("post", "/api/reload"),
])
def test_all_api_routes_require_key(client, monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    assert getattr(client, method)(path, json={}).status_code == 401


# ── Webhook ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ready_bot(monkeypatch):
    bot = MagicMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    monkeypatch.setattr(webapp, "telegram_bot", bot)
    monkeypatch.setattr(webapp, "event_loop", loop)
    monkeypatch.setattr(webapp, "run_coroutine_threadsafe", MagicMock())
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.delenv("WEBHOOK_SECRET_TOKEN", raising=False)
    return bot


def test_webhook_rejects_missing_secret(client, ready_bot):
    assert client.post("/webhook", json={"update_id": 1}).status_code == 403


def test_webhook_rejects_wrong_secret(client, ready_bot):
    r = client.post("/webhook", json={"update_id": 1},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "x"})
    assert r.status_code == 403


def test_webhook_accepts_derived_secret(client, ready_bot, monkeypatch):
    monkeypatch.setattr(webapp.Update, "de_json", MagicMock())
    r = client.post("/webhook", json={"update_id": 1},
                    headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret("123:abc")})
    assert r.status_code == 200


def test_webhook_error_does_not_leak_details(client, ready_bot, monkeypatch):
    monkeypatch.setattr(webapp.Update, "de_json", MagicMock(side_effect=RuntimeError("db pass=xyz")))
    r = client.post("/webhook", json={"update_id": 1},
                    headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret("123:abc")})
    assert r.status_code == 500 and "xyz" not in r.get_data(as_text=True)


# ── webhook_secret ──────────────────────────────────────────────────────────

def test_webhook_secret_stable_and_valid(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET_TOKEN", raising=False)
    s = webhook_secret("123:abc")
    assert s == webhook_secret("123:abc") != webhook_secret("999:zzz")
    assert len(s) == 64 and s.isalnum()


def test_webhook_secret_env_override(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", "explicit_secret")
    assert webhook_secret("123:abc") == "explicit_secret"


# ── secrets never logged ────────────────────────────────────────────────────

def test_mongo_url_password_redacted():
    from src.core.database import redact_url
    assert redact_url("mongodb+srv://u:secretpw@c.mongodb.net/?x=1") == "mongodb+srv://u:***@c.mongodb.net/?x=1"
    assert "secretpw" not in redact_url("mongodb://u:secretpw@h:27017/db")
    assert redact_url("mongodb://localhost:27017") == "mongodb://localhost:27017"


async def test_initialize_retries_then_succeeds(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from telegram.error import TimedOut
    import asyncio as _a
    from src.bot.handlers_main import TelegramQuizBot
    monkeypatch.setattr(_a, "sleep", AsyncMock())
    b = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    b.application = MagicMock()
    b.application.initialize = AsyncMock(side_effect=[TimedOut(), TimedOut(), None])
    await b._initialize_with_retry()
    assert b.application.initialize.await_count == 3


async def test_initialize_gives_up_after_attempts(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from telegram.error import TimedOut
    import asyncio as _a
    from src.bot.handlers_main import TelegramQuizBot
    monkeypatch.setattr(_a, "sleep", AsyncMock())
    b = TelegramQuizBot(MagicMock(), db_manager=MagicMock())
    b.application = MagicMock()
    b.application.initialize = AsyncMock(side_effect=TimedOut())
    with pytest.raises(TimedOut):
        await b._initialize_with_retry()
    assert b.application.initialize.await_count == 3


def test_application_uses_longer_timeouts():
    from src.bot.handlers_main import TelegramQuizBot
    app = TelegramQuizBot._build_application("123:abc")
    assert app.bot.request._client.timeout.connect == 20


def test_landing_page_renders_with_safe_bot_links(monkeypatch):
    from src.web.app import app
    monkeypatch.setenv("BOT_USERNAME", "@Miss_Quiz\"><script>x")
    r = app.test_client().get("/")
    assert r.status_code == 200 and b"Fill the" in r.data
    assert b"https://t.me/Miss_Quizscriptx?startgroup=true" in r.data
    assert b"<script>x" not in r.data


def test_landing_page_without_bot_username_uses_community(monkeypatch):
    from src.web.app import app
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    r = app.test_client().get("/")
    assert r.status_code == 200 and b"t.me/VillanCommunity" in r.data and b"t.me/?" not in r.data
