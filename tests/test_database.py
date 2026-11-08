"""Tests for DatabaseManager helpers that don't need a live MongoDB."""

from unittest.mock import MagicMock
from pymongo.errors import BulkWriteError
from src.core.database import DatabaseManager


def _db():
    db = DatabaseManager.__new__(DatabaseManager)
    db.questions_col = MagicMock()
    db._next_id_batch = MagicMock(return_value=100)
    return db


QS = [{"question": f"Q{i}?", "options": ["A", "B"], "correct_answer": 0} for i in range(3)]


def test_batch_all_inserted():
    db = _db()
    count, pairs, errs = db.add_questions_batch(QS)
    assert (count, pairs, errs) == (3, [(0, 100), (1, 101), (2, 102)], [])


def test_batch_partial_failure_skips_failed_index():
    db = _db()
    db.questions_col.insert_many.side_effect = BulkWriteError({
        "nInserted": 2,
        "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
    })
    count, pairs, errs = db.add_questions_batch(QS)
    assert count == 2
    assert pairs == [(1, 101), (2, 102)]   # index 0 failed, not index 2
    assert len(errs) == 1


def test_batch_empty():
    assert _db().add_questions_batch([]) == (0, [], [])
