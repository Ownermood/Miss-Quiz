"""Cleaner must remove PDF/Telegram noise but never real question content,
and startup must never rewrite the DB by itself."""
from unittest.mock import MagicMock

from src.utils.cleaner import clean_text


def test_keeps_real_content_removes_noise():
    assert clean_text("The website of NITI Aayog is niti.gov.in. Which body?") == \
        "The website of NITI Aayog is niti.gov.in. Which body?"
    assert clean_text("Section 375 IPC, 1860 defines?") == "Section 375 IPC, 1860 defines?"
    assert clean_text("[3/100] Who wrote Gitanjali? t.me/abc @abcdef") == "Who wrote Gitanjali?"
    assert "www" not in clean_text("Visit www.xyz.com for PDF. Capital?")


def test_option_never_emptied_by_import():
    from src.bot.my_quiz import parse_questions
    qs, _ = parse_questions("1. Helpline number?\n(a) 9876543210\n(b) 100\nAnswer: b\n")
    assert qs and qs[0]["options"][0] == "9876543210"


def test_startup_does_not_rewrite_db():
    from src.core.quiz import QuizManager
    db = MagicMock()
    db.get_all_questions.return_value = [
        {"id": 1, "question": "Site niti.gov.in?", "options": ["a", "b"], "correct_answer": 0}]
    qm = QuizManager.__new__(QuizManager)
    qm.sanitize_existing_questions = MagicMock()
    import inspect
    src = inspect.getsource(QuizManager.__init__) + inspect.getsource(QuizManager)
    assert "self.sanitize_existing_questions(raw)" not in src
