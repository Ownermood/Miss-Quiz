"""Tests for QuizManager business logic (in-memory, no DB)."""

import pytest
from unittest.mock import MagicMock
from src.core.quiz import QuizManager, _fmt_question
from src.core.exceptions import ValidationError


# ── Helpers ────────────────────────────────────────────────────────────────

def make_manager(questions=None):
    """Create a QuizManager with a mocked DB."""
    db = MagicMock()
    db.get_all_questions.return_value = questions or []
    mgr = QuizManager.__new__(QuizManager)
    mgr.db                       = db
    mgr.questions                = [_fmt_question(q) for q in (questions or [])]
    mgr.scores                   = {}
    mgr.active_chats             = set()
    mgr.stats                    = {}
    from collections import defaultdict, deque
    mgr.recent_questions         = defaultdict(lambda: deque(maxlen=50))
    mgr.last_question_time       = defaultdict(dict)
    import threading
    mgr._questions_lock          = threading.Lock()
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
        mgr.db.add_questions_batch.return_value = (1, [(0, 10)], [])
        result = mgr.add_questions([{
            "question": "New Q?", "options": ["A","B","C","D"],
            "correct_answer": 0, "category": "General"
        }])
        assert result["added"] == 1
        assert mgr.questions[-1]["id"] == 10
        assert len(mgr.questions) == 1

    def test_partial_batch_maps_ids_by_index(self):
        mgr = make_manager([])
        # 3 questions sent, the middle one failed in DB
        mgr.db.add_questions_batch.return_value = (2, [(0, 10), (2, 12)], ["dup"])
        result = mgr.add_questions([
            {"question": f"Q{n}?", "options": ["A","B"], "correct_answer": 0}
            for n in range(3)
        ])
        assert result["added"] == 2
        ids = {q["question"]: q["id"] for q in mgr.questions}
        assert ids == {"Q0?": 10, "Q2?": 12}

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


# ── get_score ─────────────────────────────────────────────

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


# ── startup ────────────────────────────────────────────────────────────────

class TestStartup:

    def test_init_fetches_questions_once_and_still_migrates(self):
        long_opt = "x" * 150
        db = MagicMock()
        db.get_all_questions.return_value = [
            {"id": 1, "question": "Q?", "options": ["A", long_opt], "correct_answer": 0, "category": "GK"},
            {"id": 2, "question": "Q2?", "options": ["A", "B"], "correct_answer": 1, "category": "GK"},
        ]
        mgr = QuizManager(db_manager=db)
        assert db.get_all_questions.call_count == 1
        assert len(mgr.questions) == 2
        # only the over-long question is rewritten, with an option within the 100-char limit
        assert db.update_question.call_count == 1
        args = db.update_question.call_args
        assert args.args[0] == 1 and len(args.args[2][1]) <= 100
