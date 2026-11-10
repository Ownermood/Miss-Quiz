"""Universal .txt importer: many layouts, clear reasons, safe answers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.bot.quiz_parser import universal_parse, bulk_import

CASES = {
    "standard":   ("1. Capital of India?\nA) Delhi\nB) Mumbai\nC) Pune\nD) Agra\nAnswer: A", 0),
    "paren_low":  ("Q1) Capital of India?\n(a) Delhi\n(b) Mumbai\n(c) Pune\n(d) Agra\nAns: (a)", 0),
    "inline":     ("1. Capital of India? A) Delhi B) Mumbai C) Pune D) Agra Answer: A", 0),
    "star":       ("Capital of India?\nA. Delhi *\nB. Mumbai\nC. Pune\nD. Agra", 0),
    "tick":       ("Capital of India?\nA. Delhi\nB. Mumbai ✅\nC. Pune\nD. Agra", 1),
    "ans_text":   ("1. Capital of India?\na. Delhi\nb. Mumbai\nc. Pune\nd. Agra\nCorrect Answer: Delhi", 0),
    "hindi":      ("प्रश्न 1. भारत की राजधानी?\n(A) दिल्ली\n(B) मुंबई\n(C) पुणे\n(D) आगरा\nउत्तर: (B)", 1),
    "hindi_kha":  ("प्रश्न 2: भारत?\n(क) एक\n(ख) दो\nउत्तर: (ख)", 1),
    "markdown":   ("**Q1. Capital of India?**\nA) Delhi\nB) Mumbai\n**Answer: B**", 1),
    "num_opts":   ("Q. Capital of India?\n1. Delhi\n2. Mumbai\n3. Pune\nAnswer: 3", 2),
    "option_word": ("Question 1: Capital?\nOption A: Delhi\nOption B: Mumbai\nAnswer: Option B", 1),
    "true_false": ("1. Sun rises in east.\nA) True\nB) False\nAnswer: A", 0),
    "five_opts":  ("1. Pick 5?\nA) 1\nB) 2\nC) 3\nD) 4\nE) 5\nAnswer: E", 4),
    "crlf_bom":   ("﻿1. Capital?\r\nA) Delhi\r\nB) Mumbai\r\nAnswer: B\r\n", 1),
    "roman":      ("1. Choose?\n(i) one\n(ii) two\n(iii) three\n(iv) four\nAnswer: (iv)", 3),
    "ai_bullets": ("**Question 1:** What is 2+2?\n\n* A) 3\n* B) 4\n* C) 5\n\n**Answer:** B) 4", 1),
    "letter_and_text_disagree": ("1. Capital?\nA) Delhi\nB) Mumbai\nAnswer: A. Mumbai", 1),
    "vitamin_not_split": ("1. Vitamin A. deficiency causes?\nA) Night blindness\nB) Scurvy\nAnswer: A", 0),
}


@pytest.mark.parametrize("name", list(CASES))
def test_layouts(name):
    text, want = CASES[name]
    qs, problems = universal_parse(text)
    assert len(qs) == 1 and not problems, (qs, problems)
    assert qs[0]["correct_answer"] == want
    assert not qs[0]["question"].startswith((")", "*", "Q"))


def test_answer_key_at_end_and_compact_key():
    qs, _ = universal_parse("1. A?\nA) x\nB) y\n\n2. B?\nA) p\nB) q\n\nAnswer Key:\n1. B\n2. A")
    assert [q["correct_answer"] for q in qs] == [1, 0]
    qs, _ = universal_parse("1. A?\nA) x\nB) y\n2. B?\nA) p\nB) q\nAnswers:\n1A 2B")
    assert [q["correct_answer"] for q in qs] == [0, 1]


def test_explanation_category_and_multiline():
    qs, _ = universal_parse("1. Read:\nThe sun is hot.\nTrue?\nA) yes\nB) no\nAnswer: A\n"
                            "Explanation: line one\nline two\nCategory: Science | Difficulty: Easy")
    q = qs[0]
    assert q["question"] == "Read: The sun is hot. True?"
    assert q["explanation"] == "line one line two" and q["category"] == "Science"


def test_problems_explain_what_to_fix():
    _, p = universal_parse("1. A?\nA) x\nB) y\nAnswer: E\n\n2. B?\nA) x\nB) y\n\n3. C?\nA) only")
    reasons = " ".join(x["reason"] for x in p)
    assert "doesn't match" in reasons and "no answer" in reasons and "need at least 2" in reasons


def test_bulk_import_accepts_2_to_10_options_and_reports():
    qm = MagicMock(); qm.questions = []
    qm.add_questions.side_effect = lambda batch, _existing=None: {"added": len(batch), "rejected": {}, "errors": []}
    res = bulk_import("1. Sun rises in east?\nA) True\nB) False\nAnswer: A\nExplanation: basic fact\n\n"
                      "2. Broken?\nA) x\nB) y", qm)
    saved = qm.add_questions.call_args.args[0]
    assert res["imported"] == 1 and saved[0]["options"] == ["True", "False"]
    assert saved[0]["explanation"] == "basic fact"
    assert any("no answer" in p["reason"] for p in res["problems"])


# ── finding a question among thousands ──────────────────────────────────────

@pytest.fixture
def bot():
    from src.bot.handlers_main import TelegramQuizBot
    qm = MagicMock()
    qm.questions = [{"id": 7, "question": "Which Article abolishes untouchability?",
                     "options": ["Article 14", "Article 17"], "correct_answer": 1}]
    b = TelegramQuizBot(qm, db_manager=MagicMock())
    b._is_authorized = AsyncMock(return_value=True)
    b._reply = AsyncMock()
    return b


def test_find_by_option_text_typo_and_qhash(bot):
    assert "#7" in bot._find_results("article 17", 1)[0]          # matches option text
    assert "#7" in bot._find_results("Which Artcle abolshes untouchablity?", 1)[0]   # typos
    assert "#7" in bot._find_results("Q#7", 1)[0]


async def test_forwarded_poll_finds_exact_question(bot):
    from telegram.ext import ApplicationHandlerStop
    msg = SimpleNamespace(poll=SimpleNamespace(id="P1", question="📚 something else"))
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=msg)
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_forwarded_poll(upd, SimpleNamespace(bot_data={"poll_P1": {"question_id": 7}}))
    assert "#7" in bot._reply.await_args.args[1]


async def test_forwarded_poll_text_fallback(bot):
    from telegram.ext import ApplicationHandlerStop
    bot.db.get_quiz_id_from_poll.return_value = None
    msg = SimpleNamespace(poll=SimpleNamespace(id="X", question="📚 Which Article abolishes untouchability?"))
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_message=msg)
    with pytest.raises(ApplicationHandlerStop):
        await bot.handle_forwarded_poll(upd, SimpleNamespace(bot_data={}))
    assert "#7" in bot._reply.await_args.args[1]


def test_bulk_import_end_to_end_saves_into_empty_db():
    """Real QuizManager (only MongoDB mocked): every question was being
    rejected as its own duplicate and nothing was saved."""
    from src.core.quiz import QuizManager
    db = MagicMock()
    db.get_all_questions.return_value = []
    db.add_questions_batch.side_effect = lambda b: (len(b), [(i, 100 + i) for i in range(len(b))], [])
    qm = QuizManager(db_manager=db)
    text = "\n\n".join(f"{i}. Question number {i}?\nA) x{i}\nB) y{i}\nAnswer: A" for i in range(1, 6))
    text += "\n\n6. Question number 1?\nA) x\nB) y\nAnswer: A"            # repeat inside the file
    res = bulk_import(text, qm)
    assert res["imported"] == 5 and res["duplicates"] == 1
    assert len(qm.questions) == 5
    res2 = bulk_import(text, qm)                                           # same file again
    assert res2["imported"] == 0 and res2["duplicates"] == 6


# ── more answer layouts seen in real files (AI / coaching PDFs) ──────────────

import pytest as _pytest
from src.bot.quiz_parser import universal_parse as _up


@_pytest.mark.parametrize("ans_line", [
    "✅ Answer: B", "👉 Correct Answer — (b)", "Answer — B", "🔑 Ans: (b)", "➤ Correct Option: B",
    "The correct answer is (b)", "Correct answer is B", "Right option: b", "Answer => B",
    "✔️ Answer: Delhi", "Ans → (b) Delhi",
])
def test_answer_line_variants(ans_line):
    qs, probs = _up(f"1. Capital of India?\n(a) Mumbai\n(b) Delhi\n(c) Pune\n{ans_line}\n")
    assert len(qs) == 1 and qs[0]["correct_answer"] == 1, (ans_line, probs)


@_pytest.mark.parametrize("key", [
    "ANSWER KEY\n1. B\n2. C",
    "Answers: 1-b, 2-c",
    "Answer Key\n| Q | Ans |\n| 1 | B |\n| 2 | C |",
    "Solutions\n1. (b) Delhi is the capital, see Article 1 a\n2. (c) because 2+2=4 a",
    "Answers & Explanations\nQ1 (b) — Delhi.\nQ2 (c) — four.",
    "उत्तर कुंजी\n1 (b)\n2 (c)",
])
def test_answer_key_variants(key):
    text = ("Q1. Capital of India?\nA) Mumbai\nB) Delhi\nC) Pune\n\n"
            "Q2. 2+2?\nA) 3\nB) 5\nC) 4\n\n" + key)
    qs, probs = _up(text)
    assert [q["correct_answer"] for q in qs] == [1, 2], (key, probs)


def test_math_symbols_at_start_are_kept():
    qs, _ = _up("1. √16 = ?\n(a) 2\n(b) 4\nAnswer: b")
    assert qs[0]["question"].startswith("√16")


def test_multisection_document_with_separate_answer_keys_and_explanations():
    doc = """
SECTION 1
1. What is the capital of France?
A. Berlin
B. Paris
C. Rome

2. What is 5 x 5?
A. 20
B. 25
C. 30

ANSWER KEY — SECTION 1
1. B
2. B

Explanations
1. Paris is the capital and largest city of France.
2. 5 multiplied by 5 equals 25.

SECTION 2
1. Which planet is closest to the Sun?
A. Venus
B. Mercury
C. Mars

2. What is H2O?
A. Hydrogen peroxide
B. Water
C. Oxygen

ANSWER KEY — SECTION 2
1. B
2. B

Explanations
1. Mercury is the smallest and closest planet to the Sun.
2. H2O is the chemical formula for water.
"""
    qs, probs = _up(doc)
    assert len(qs) == 4, f"Expected 4 questions, got {len(qs)}: {probs}"
    assert qs[0]["question"] == "What is the capital of France?"
    assert qs[0]["correct_answer"] == 1
    assert "Paris is the capital" in (qs[0].get("explanation") or "")
    assert qs[0].get("category") == "SECTION 1"

    assert qs[1]["question"] == "What is 5 x 5?"
    assert qs[1]["correct_answer"] == 1
    assert "equals 25" in (qs[1].get("explanation") or "")

    assert qs[2]["question"] == "Which planet is closest to the Sun?"
    assert qs[2]["correct_answer"] == 1
    assert "Mercury is the smallest" in (qs[2].get("explanation") or "")
    assert qs[2].get("category") == "SECTION 2"

    assert qs[3]["question"] == "What is H2O?"
    assert qs[3]["correct_answer"] == 1
    assert "formula for water" in (qs[3].get("explanation") or "")

