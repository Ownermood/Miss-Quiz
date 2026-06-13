"""Tests for QuizManager business logic (in-memory, no DB)."""

import pytest
from unittest.mock import MagicMock, patch
from src.core.quiz import QuizManager, _fmt_question
from src.core.exceptions import ValidationError


# ── Helpers ────────────────────────────────────────────────────────────────

def make_manager(questions=None):
    """Create a QuizManager with a mocked DB."""
    db = MagicMock()
    db.get_all_questions.return_value = questions or []
    db.get_questions_by_category.return_value = []
    mgr = QuizManager.__new__(QuizManager)
    mgr.db                       = db
    mgr.questions                = [_fmt_question(q) for q in (questions or [])]
    mgr.scores                   = {}
    mgr.active_chats             = []
    mgr.stats                    = {}
    mgr._cached_questions        = None
    from collections import defaultdict, deque
    mgr.recent_questions         = defaultdict(lambda: deque(maxlen=50))
    mgr.last_question_time       = defaultdict(dict)
    mgr.available_questions      = defaultdict(list)
    return mgr


SAMPLE_QUESTIONS = [
    {"id": 1, "question": "Q1?", "options": ["A","B","C","D"], "correct_answer": 0, "category": "Legal Reasoning"},
    {"id": 2, "question": "Q2?", "options": ["W","X","Y","Z"], "correct_answer": 2, "category": "English"},
    {"id": 3, "question": "Q3?", "options": ["P","Q","R","S"], "correct_answer": 1, "category": "Legal Reasoning"},
]


# ── _fmt_question ──────────────────────────────────────────────────────────

class TestFmtQuestion:

    def test_normalises_fields(self):
        q = _fmt_question({"id": 5, "question": "Hi?", "options": ["A","B","C","D"],
                           "correct_answer": 1, "category": "Math"})
        assert q["id"] == 5
        assert q["question"] == "Hi?"
        assert q["options"] == ["A","B","C","D"]
        assert q["correct_answer"] == 1
        assert q["category"] == "Math"

    def test_defaults_category_to_general(self):
        q = _fmt_question({"question": "X?", "options": [], "correct_answer": 0})
        assert q["category"] == "General"

    def test_parses_json_string_options(self):
        import json
        q = _fmt_question({"question": "X?", "options": json.dumps(["A","B"]), "correct_answer": 0})
        assert q["options"] == ["A", "B"]


# ── get_random_question ────────────────────────────────────────────────────

class TestGetRandomQuestion:

    def test_returns_question_from_pool(self):
        mgr = make_manager(SAMPLE_QUESTIONS)
        mgr.db.get_all_questions.return_value = SAMPLE_QUESTIONS
        q = mgr.get_random_question(chat_id=1)
        assert q is not None
        assert q["question"].endswith("?")

    def test_returns_none_when_empty(self):
        mgr = make_manager([])
        q = mgr.get_random_question(chat_id=1)
        assert q is None

    def test_invalid_category_type_raises(self):
        mgr = make_manager(SAMPLE_QUESTIONS)
        with pytest.raises(ValidationError):
            mgr.get_random_question(chat_id=1, category=123)


# ── add_questions ──────────────────────────────────────────────────────────

class TestAddQuestions:

    def test_add_new_question(self):
        mgr = make_manager([])
        mgr.db.add_question.return_value = 10
        result = mgr.add_questions([{
            "question": "New Q?", "options": ["A","B","C","D"],
            "correct_answer": 0, "category": "General"
        }])
        assert result["added"] == 1
        assert len(mgr.questions) == 1

    def test_duplicate_rejected(self):
        mgr = make_manager(SAMPLE_QUESTIONS)
        result = mgr.add_questions([{
            "question": "Q1?", "options": ["A","B","C","D"],
            "correct_answer": 0, "category": "Legal Reasoning"
        }])
        assert result["added"] == 0
        assert result["rejected"]["duplicates"] == 1


# ── record_attempt ─────────────────────────────────────────────────────────

class TestRecordAttempt:

    def test_correct_increments_score(self):
        mgr = make_manager()
        mgr.record_attempt(42, is_correct=True)
        assert mgr.scores.get(42, 0) == 1

    def test_wrong_does_not_increment(self):
        mgr = make_manager()
        mgr.record_attempt(42, is_correct=False)
        assert mgr.scores.get(42, 0) == 0

    def test_invalid_user_id_raises(self):
        mgr = make_manager()
        with pytest.raises(ValidationError):
            mgr.record_attempt(0, is_correct=True)

    def test_streak_increments_on_consecutive_correct(self):
        mgr = make_manager()
        mgr.record_attempt(1, is_correct=True)
        mgr.record_attempt(1, is_correct=True)
        stats = mgr.stats.get("1", {})
        assert stats.get("correct_answers", 0) == 2


# ── get_score / get_user_stats ─────────────────────────────────────────────

class TestGetScore:

    def test_zero_for_unknown_user(self):
        mgr = make_manager()
        assert mgr.get_score(999) == 0

    def test_returns_correct_score_after_attempts(self):
        mgr = make_manager()
        mgr.record_attempt(7, True)
        mgr.record_attempt(7, True)
        mgr.record_attempt(7, False)
        assert mgr.get_score(7) == 2

    def test_get_user_stats_keys(self):
        mgr = make_manager()
        stats = mgr.get_user_stats(1)
        for key in ("total_quizzes","correct_answers","success_rate","current_score"):
            assert key in stats


# ── delete_question_by_db_id ───────────────────────────────────────────────

class TestDeleteQuestion:

    def test_delete_removes_from_cache(self):
        mgr = make_manager(SAMPLE_QUESTIONS)
        mgr.db.delete_question.return_value = True
        mgr.delete_question_by_db_id(1)
        assert all(q["id"] != 1 for q in mgr.questions)

    def test_delete_returns_false_when_not_found(self):
        mgr = make_manager(SAMPLE_QUESTIONS)
        mgr.db.delete_question.return_value = False
        result = mgr.delete_question_by_db_id(999)
        assert result is False
