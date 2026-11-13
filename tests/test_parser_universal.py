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


def test_embedded_question_in_option_line():
    doc = """
308. Which one of the following is a human right as well as a fundamental right under the
Constitution of India?
(a) Right to Information
(b) Right to Education
(c) Right to Work
(d) Right to Housing 309. Which Article of the Constitution envisages free and compulsory education for all children until they complete the age of fourteen years?
(a) Article 45
(b) Article 19
(c) Article 29
(d) Article 32

Answer Key:
308. (b)
309. (a)
"""
    qs, probs = _up(doc)
    assert len(qs) == 2, f"Expected 2 questions, got {len(qs)}: {probs}"
    assert qs[0]["question"] == "Which one of the following is a human right as well as a fundamental right under the Constitution of India?"
    assert qs[0]["options"] == ["Right to Information", "Right to Education", "Right to Work", "Right to Housing"]
    assert qs[0]["correct_answer"] == 1

    assert qs[1]["question"] == "Which Article of the Constitution envisages free and compulsory education for all children until they complete the age of fourteen years?"
    assert qs[1]["options"] == ["Article 45", "Article 19", "Article 29", "Article 32"]
    assert qs[1]["correct_answer"] == 0


def test_unspaced_numbered_option_cleaning():
    doc = """
1. Red Fort was built by
1. Shah Jahan
2. Akbar
3. Babur
4. Humayun
Answer: 1
"""
    qs, probs = _up(doc)
    assert len(qs) == 1
    assert qs[0]["options"] == ["Shah Jahan", "Akbar", "Babur", "Humayun"]
    assert qs[0]["correct_answer"] == 0




# ── real coaching PDFs (GK Fiesta / 12MTC question bank) ────────────────────

def test_fiesta_style_key_after_explanation_statements_and_split_rows():
    text = """1. Which corridor was stalled?
(a) INSTC (b) IMEC
(c) CPEC   (d) CAREC
2
2. Consider the following statements:
1. The U.S. operation is codenamed Epic Fury.
2. Israel's operation is codenamed Lion's Roar.
3. Strikes hit Natanz.
Which of the statements is/are correct?
(a) 1 and 2 only (b) 2 and 3 only (c) 1 and 3 only (d) 1, 2 and 3
Passage (Q.3-Q.3): MPLADS is a scheme that lets MPs recommend work.
It was examined by the apex court.
3. MPLADS funds are released by which ministry?
(a) Finance (b) MoSPI (c) Home (d) Rural Development
ANSWER KEY AND EXPLANATIONS
1. (b) IMEC was stalled.
Explanation: The corridor needs West Asian stability. India recorded
369.01 million detections — this number line is not an answer.
2. (d) All statements are correct.
3. (b) MoSPI releases MPLADS funds.
"""
    qs, probs = _up(text)
    assert [q["correct_answer"] for q in qs] == [1, 3, 1], probs
    assert qs[0]["options"] == ["INSTC", "IMEC", "CPEC", "CAREC"]           # '(c) … (d) …' row split, page no. dropped
    assert "1. The U.S. operation" in qs[1]["question"] and len(qs[1]["options"]) == 4   # statements stay in question
    assert all("MPLADS is a scheme" not in o for q in qs for o in q["options"])           # passage not glued to options


def test_12mtc_watermark_lines_and_glued_headings_removed():
    text = """20. The ABDMWhen was DRDO formed?
A. 1958
B. 1959
C. 1960
D. 1961
12minutestoclat.com
12minutestoclat.com
35
21. On to India for Pax Silica How many countries joined the first summit?
A. 14
B. 9
C. 27
D. 5
ANSWER KEY
Q20. A
Q21. B
"""
    qs, _ = _up(text)
    assert qs[0]["question"] == "When was DRDO formed?" and qs[0]["options"][3] == "1961"
    assert qs[1]["question"].startswith("How many countries") and qs[1]["correct_answer"] == 1
    # a real domain inside a question is kept
    qs, _ = _up("1. The website of NITI Aayog is niti.gov.in. Which body did it replace?\nA. Planning Commission\nB. RBI\nAnswer: A")
    assert "niti.gov.in" in qs[0]["question"]


# ── "400 Questions on Constitution" PDF (live import: 380 detected, 21 not understood) ──
from src.bot.quiz_parser import universal_parse as _up


def _q(n, q, opts):
    return f"{n}. {q}\n" + "\n".join(opts)


def test_lone_inline_A_after_question_dash():
    t = _q(65, "In the Parliament, Lower House is called as— (A) Legislative Assembly",
           ["(B) Legislative Council", "(C) Rajya Sabha", "(D) Lok Sabha"]) + "\nAnswers\n65.(D)"
    items, probs = _up(t)
    assert probs == [] and items[0]["options"] == ["Legislative Assembly", "Legislative Council", "Rajya Sabha", "Lok Sabha"]
    assert items[0]["correct_answer"] == 3


def test_lone_A_inside_question_text_is_not_split():
    items, _ = _up("1. Under clause (a) of Article 19 which right is given?\n(A) Speech\n(B) Vote\nAnswer: A")
    assert items[0]["question"].startswith("Under clause (a) of Article 19") and items[0]["options"] == ["Speech", "Vote"]


def test_key_row_with_blank_answer_and_missing_space():
    body = "\n".join(_q(n, f"Question {n}?", ["(A) a", "(B) b", "(C) c", "(D) d"]) for n in range(171, 181))
    body += "\n" + "\n".join(_q(n, f"Question {n}?", ["(A) a", "(B) b", "(C) c", "(D) d"]) for n in (399, 400))
    t = body + "\nAnswers\n171.(C) 172.(B) 173.(D) 174.( ) 175.(B) 176.(D) 177.(C) 178.(A) 179.(C) 180.(A)\n399.(D)400.(B)"
    items, probs = _up(t)
    assert [p["where"] for p in probs] == ["Q174"]
    assert [i["correct_answer"] for i in items] == [2, 1, 3, 1, 3, 2, 0, 2, 0, 3, 1]


def test_wrapped_bracket_number_joins_previous_option():
    t = _q(319, "Under which Article is the Election Commission a Tribunal—",
           ["(A) Article 136", "(B) Article 136(3)", "(C) Article 136", "(2)(D) Article 136(1)"]) + "\nAnswers\n319.(D)"
    items, probs = _up(t)
    assert probs == [] and len(items) == 1
    assert items[0]["options"] == ["Article 136", "Article 136(3)", "Article 136(2)", "Article 136(1)"]


def test_option_label_typos_keep_four_options():
    t = (_q(96, "Deputy PM?", ["(A) Nehru", "(B) Patel", "(D) Azad", "(D) Nanda"]) + "\n"
         + _q(197, "Financial year starts—", ["(A) 1 April", "(B) 2 April", "(C) 1 March", "(f) 15 March"]) + "\n"
         + _q(350, "Claims of SC and ST—", ["(A) Article 336", "(B) Article 335", "(B) Article 338", "(D) Article 339"])
         + "\nAnswers\n96.(D) 197.(A) 350.(B)")
    items, probs = _up(t)
    assert probs == [] and [len(i["options"]) for i in items] == [4, 4, 4]
    assert items[0]["options"][3] == "Nanda" and items[2]["options"][2] == "Article 338"
    assert [i["correct_answer"] for i in items] == [3, 0, 1]


def test_initials_in_option_are_not_split():
    t = _q(50, "Chairman of the Drafting Committee—", ["(A) Dr. B. R. Ambedkar", "(B) K. M. Munshi",
           "(C) N. D. Deva Gowda", "(D) None"]) + "\nAnswers\n50.(A)"
    items, probs = _up(t)
    assert probs == [] and items[0]["options"] == ["Dr. B. R. Ambedkar", "K. M. Munshi", "N. D. Deva Gowda", "None"]


def test_same_style_inline_options_still_split():
    items, _ = _up("1. Capital of India? (A) Delhi (B) Mumbai (C) Pune (D) Agra\nAnswer: A")
    assert items[0]["options"] == ["Delhi", "Mumbai", "Pune", "Agra"]
    items, _ = _up("1. Capital of India? A. Delhi B. Mumbai C. Pune\nAnswer: A")
    assert items[0]["options"] == ["Delhi", "Mumbai", "Pune"]
