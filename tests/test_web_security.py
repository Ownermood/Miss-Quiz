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
