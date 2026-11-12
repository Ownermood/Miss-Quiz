"""Telegram Mini App: in-Telegram quiz player (Practice + Exam) for quiz_sets.

Security:
  - Every API call must carry Telegram WebApp initData (header X-Init-Data),
    verified with HMAC-SHA256 against the bot token; max age 24h. Fail-closed.
  - Correct answers are never sent before the user has answered
    (/check returns one answer after a pick; /submit returns all at the end).
"""

import hashlib
import hmac
import json
import os
import random
import threading
import time
from urllib.parse import parse_qsl

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint("miniapp", __name__)

INIT_MAX_AGE = 86400
SUBMIT_GAP = 20          # seconds between counted submits per user+quiz
_last_submit = {}
_lock = threading.Lock()


def verify_init_data(init_data: str, bot_token: str, max_age: int = INIT_MAX_AGE):
    """Return the Telegram user dict if initData is genuine and fresh, else None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", "")
    if not got:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None


def _db():
    from src.web import app as web
    return web.db_manager


def _auth():
    return verify_init_data(request.headers.get("X-Init-Data", ""),
                            os.environ.get("TELEGRAM_TOKEN", ""))


def _load(sid):
    db = _db()
    if db is None or not isinstance(sid, str) or not sid or len(sid) > 64:
        return None
    qs = db.get_quiz_set(sid)
    return qs if isinstance(qs, dict) and qs.get("questions") else None


def _err(msg, code):
    return jsonify({"error": msg}), code


@bp.route("/app")
def page():
    return render_template("miniapp.html")


@bp.route("/app/api/quiz", methods=["POST"])
def api_quiz():
    if not _auth():
        return _err("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    qs = _load(body.get("sid"))
    if not qs:
        return _err("not_found", 404)
    items = [{"i": i, "q": str(q.get("question", "")), "o": [str(o) for o in (q.get("options") or [])]}
             for i, q in enumerate(qs["questions"])]
    if qs.get("shuffle"):
        random.shuffle(items)
    return jsonify({
        "title": str(qs.get("title") or "Quiz"),
        "secs": int(qs.get("secs") or 20),
        "negative": float(qs.get("negative_rate") or 0),
        "count": len(items),
        "questions": items,
    })


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _answer(q):
    try:
        return int(q.get("correct_answer", -1))
    except (TypeError, ValueError):
        return -1


@bp.route("/app/api/check", methods=["POST"])
def api_check():
    if not _auth():
        return _err("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    qs = _load(body.get("sid"))
    i = body.get("i")
    if not qs or not _is_int(i) or not 0 <= i < len(qs["questions"]):
        return _err("not_found", 404)
    q = qs["questions"][i]
    a = _answer(q)
    return jsonify({"ok": _is_int(body.get("c")) and body.get("c") == a, "a": a, "e": str(q.get("explanation") or "")})


@bp.route("/app/api/submit", methods=["POST"])
def api_submit():
    user = _auth()
    if not user:
        return _err("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    sid = body.get("sid")
    qs = _load(sid)
    answers = body.get("answers")
    if not qs or not isinstance(answers, dict):
        return _err("bad_request", 400)
    questions = qs["questions"]
    picked = {}
    for k, v in answers.items():
        if str(k).isdigit() and int(k) < len(questions) and _is_int(v):
            picked[int(k)] = v
    asked = body.get("asked")
    asked = [i for i in asked if _is_int(i) and 0 <= i < len(questions)] \
        if isinstance(asked, list) else list(range(len(questions)))
    asked = list(dict.fromkeys(asked))[:len(questions)]
    correct = wrong = 0
    review = []
    for i in asked:
        a = _answer(questions[i])
        c = picked.get(i, -1)
        if c == a:
            correct += 1
        elif c >= 0:
            wrong += 1
        review.append({"i": i, "a": a, "e": str(questions[i].get("explanation") or "")})
    neg = float(qs.get("negative_rate") or 0)
    key = (user["id"], sid)
    now = time.time()
    with _lock:
        count_it = now - _last_submit.get(key, 0) >= SUBMIT_GAP
        if count_it:
            _last_submit[key] = now
            if len(_last_submit) > 5000:
                _last_submit.clear()
    if count_it and asked:
        try:
            _db().inc_quiz_set_plays(sid, correct=correct, total=len(asked))
        except Exception:
            pass
    return jsonify({"correct": correct, "wrong": wrong, "skipped": len(asked) - correct - wrong,
                    "score": round(correct - wrong * neg, 2), "total": len(asked), "review": review})
