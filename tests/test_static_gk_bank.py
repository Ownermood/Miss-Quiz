import os
import json

from src.bot.ui import UI
from src.utils.cleaner import clean_text

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "static_gk_1000_clean.json")


def test_static_gk_bank_loaded():
    assert os.path.exists(DATA_PATH), "Clean static GK dataset must exist"
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert len(data) == 980, f"Expected exactly 980 questions, found {len(data)}"

    for i, item in enumerate(data):
        assert item.get("question"), f"Item {i+1} has empty question"
        opts = item.get("options", [])
        assert 2 <= len(opts) <= 10, f"Item {i+1} has invalid options length {len(opts)}"
        ans = item.get("correct_answer")
        assert isinstance(ans, int) and 0 <= ans < len(opts), f"Item {i+1} has invalid correct_answer {ans}"
        cat = item.get("category")
        assert cat, f"Item {i+1} missing category"
        emoji = UI.cat_emoji(cat)
        assert emoji and emoji != "📚" or cat == "General", f"Item {i+1} category '{cat}' must map to valid emoji"


def test_question_308_and_309_exact_format():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    q308 = next((q for q in data if q.get("_original_num") == 308), None)
    assert q308 is not None, "Q308 must be in clean dataset"
    assert "Constitution of India" in q308["question"]
    assert "human right as well as a fundamental right" in q308["question"]
    assert q308["options"] == ["Right to Information", "Right to Education", "Right to Work", "Right to Housing"]
    assert q308["correct_answer"] == 1
    assert "309" not in q308["options"][3]

    q309 = next((q for q in data if q.get("_original_num") == 309), None)
    assert q309 is not None, "Q309 must be in clean dataset"
    assert "free and compulsory education" in q309["question"]
    assert q309["options"] == ["Article 32", "Article 19 s", "Article 29", "Article 45"]
    assert q309["correct_answer"] == 3


def test_cleaner_strips_leaked_question_sentence_from_option():
    corrupted_opt = "Right to Housing 309. Which Article of the Constitution envisages free and compulsory education f..."
    cleaned = clean_text(corrupted_opt, is_option=True)
    assert cleaned == "Right to Housing"
