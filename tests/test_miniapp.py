"""Telegram Mini App: initData auth, no answer leaks, scoring, card button."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

import src.web.app as webapp
import src.web.miniapp as mini

TOKEN = "123:ABC"


def make_init(uid=7, age=0, token=TOKEN, tamper=False):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "q1",
              "user": json.dumps({"id": uid, "first_name": "A"})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": 999, "first_name": "A"})
    return urlencode(fields)


QSET = {"id": "s1", "title": "T", "secs": 15, "negative_rate": 0.25, "questions": [
    {"question": "Q0", "options": ["a", "b"], "correct_answer": 1, "explanation": "E0"},
    {"question": "Q1", "options": ["a", "b"], "correct_answer": 0},
    {"question": "Q2", "options": ["a", "b"], "correct_answer": 0},
]}


@pytest.fixture
def client(monkeypatch):
    db = MagicMock()
    db.get_quiz_set.side_effect = lambda sid: dict(QSET) if sid == "s1" else None
    monkeypatch.setattr(webapp, "db_manager", db)
    monkeypatch.setenv("TELEGRAM_TOKEN", TOKEN)
    mini._last_submit.clear()
    c = webapp.app.test_client()
    c.db = db
    return c


def post(c, path, body, init=None):
    return c.post("/app/api/" + path, json=body, headers={"X-Init-Data": make_init() if init is None else init})


def test_verify_init_data():
    assert mini.verify_init_data(make_init(), TOKEN)["id"] == 7
    assert mini.verify_init_data(make_init(tamper=True), TOKEN) is None
    assert mini.verify_init_data(make_init(token="other"), TOKEN) is None
    assert mini.verify_init_data(make_init(age=90000), TOKEN) is None
    assert mini.verify_init_data("", TOKEN) is None
    assert mini.verify_init_data(make_init(), "") is None
    assert mini.verify_init_data("garbage&&==", TOKEN) is None


def test_api_requires_valid_init(client, monkeypatch):
    for path in ("quiz", "check", "submit"):
        assert post(client, path, {"sid": "s1"}, init="").status_code == 401
        assert post(client, path, {"sid": "s1"}, init=make_init(tamper=True)).status_code == 401
    monkeypatch.delenv("TELEGRAM_TOKEN")
    assert post(client, "quiz", {"sid": "s1"}).status_code == 401


def test_quiz_has_no_answers(client):
    r = post(client, "quiz", {"sid": "s1"})
    d = r.get_json()
    assert r.status_code == 200 and d["count"] == 3 and d["secs"] == 15
    raw = json.dumps(d)
    assert "correct_answer" not in raw and "E0" not in raw and '"a":' not in raw
    assert post(client, "quiz", {"sid": "nope"}).status_code == 404
    assert post(client, "quiz", {"sid": ["s1"]}).status_code == 404


def test_check(client):
    d = post(client, "check", {"sid": "s1", "i": 0, "c": 1}).get_json()
    assert d == {"ok": True, "a": 1, "e": "E0"}
    assert post(client, "check", {"sid": "s1", "i": 0, "c": True}).get_json()["ok"] is False
    assert post(client, "check", {"sid": "s1", "i": 9, "c": 0}).status_code == 404
    assert post(client, "check", {"sid": "s1", "i": True, "c": 0}).status_code == 404


def test_submit_scores_with_negative_and_counts_once(client):
    body = {"sid": "s1", "answers": {"0": 1, "1": 1, "2": -1, "x": 0, "1000": 0}, "asked": [0, 1, 2, 2, 50]}
    d = post(client, "submit", body).get_json()
    assert (d["correct"], d["wrong"], d["skipped"], d["total"]) == (1, 1, 1, 3)
    assert d["score"] == 0.75 and len(d["review"]) == 3
    client.db.inc_quiz_set_plays.assert_called_once_with("s1", correct=1, total=3)
    post(client, "submit", body)                     # immediate repeat: not counted again
    assert client.db.inc_quiz_set_plays.call_count == 1
    assert post(client, "submit", {"sid": "s1", "answers": []}).status_code == 400


def test_page_served(client):
    r = client.get("/app")
    assert r.status_code == 200 and b"telegram-web-app.js" in r.data


def _app_buttons(kb):
    return [b for row in kb.inline_keyboard for b in row if getattr(b, "web_app", None)]


def test_card_button_only_private_and_configured(monkeypatch):
    from src.bot.my_quiz import MyQuizMixin
    qs = {"id": "ab12", "title": "T", "count": 3, "secs": 20}
    monkeypatch.delenv("MINI_APP_URL", raising=False)
    assert not _app_buttons(MyQuizMixin._mq_card_view(qs, bot_username="b", private=True)[1])
    monkeypatch.setenv("MINI_APP_URL", "http://insecure.example")
    assert not _app_buttons(MyQuizMixin._mq_card_view(qs, bot_username="b", private=True)[1])
    monkeypatch.setenv("MINI_APP_URL", "https://quiz.example.com/")
    assert not _app_buttons(MyQuizMixin._mq_card_view(qs, bot_username="b", private=False)[1])
    btn = _app_buttons(MyQuizMixin._mq_card_view(qs, bot_username="b", private=True)[1])
    assert btn and btn[0].web_app.url == "https://quiz.example.com/app?sid=ab12"


def test_is_private():
    from src.bot.my_quiz import _is_private
    assert _is_private(SimpleNamespace(effective_chat=SimpleNamespace(type="private")))
    assert not _is_private(SimpleNamespace(effective_chat=SimpleNamespace(type="group")))
    assert not _is_private(SimpleNamespace(effective_chat=None))
