"""
Universal Poll & Text Cleaner — Safai Filter.
Auto-removes poll counters, channel usernames, invite links, and custom user words
without harming dates (1951-1956), fractions (3/4), or numeric options.
"""

import re
from typing import List, Dict, Optional, Any

# 1. Bracketed counters anywhere: [1/100], (1/50), [Q. 1/20], {12/100}
_BRACKET_COUNTER_RE = re.compile(
    r"[\[\(\{]\s*(?:Q(?:uestion)?\.?\s*)?\d{1,4}\s*(?:/|of)\s*\d{1,4}\s*[\]\)\}]",
    re.IGNORECASE
)

# 2. Unbracketed counter ONLY at the very start of a question: "1/100. ", "Q 1/50 - "
_LEADING_COUNTER_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\.?\s*)?\d{1,4}\s*/\s*\d{1,4}(?:\s*[:\.\-\)\]]\s*|\s+)",
    re.IGNORECASE
)

# 3. Leading question number (for questions only, NOT options): "1.", "Q1:", "Question 2 -"
_Q_PREFIX_RE = re.compile(
    r"^(?:(?:Q(?:uestion)?[\.\s]*\d+[\s:\.\)-]*)|(?:\d{1,4}[\.\)-]\s+))",
    re.IGNORECASE
)

# 4. @usernames and Telegram links
_TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/)?[a-zA-Z0-9_/\+\-]+",
    re.IGNORECASE
)
_USERNAME_RE = re.compile(r"@[a-zA-Z0-9_]{4,32}", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# 5. Common spam / promo phrases
_SPAM_WORDS_RE = re.compile(
    r"(?:join\s+(?:our\s+)?channel|subscribe\s+now|join\s+telegram|whatsapp\s+group|contact\s+for\s+pdf|dm\s+for\s+notes)",
    re.IGNORECASE
)


def clean_text(text: str, custom_words: Optional[List[str]] = None, is_option: bool = False) -> str:
    """
    Clean raw question text or option string:
    1. Strip [1/100] bracketed counters
    2. Strip leading question numbers (only when is_option=False)
    3. Strip Telegram links & @usernames
    4. Strip URLs and generic spam phrases
    5. Strip custom blacklist words
    """
    if not text:
        return ""

    cleaned = str(text)

    # 1. Remove bracketed [1/100] style counters
    cleaned = _BRACKET_COUNTER_RE.sub(" ", cleaned)
    if not is_option:
        cleaned = _LEADING_COUNTER_RE.sub("", cleaned)

    # 2. Remove URLs and @mentions
    cleaned = _TELEGRAM_LINK_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _USERNAME_RE.sub(" ", cleaned)
    cleaned = _SPAM_WORDS_RE.sub(" ", cleaned)

    # 3. Remove custom banned words
    if custom_words:
        for w in custom_words:
            w_strip = w.strip()
            if w_strip:
                pattern = re.compile(re.escape(w_strip), re.IGNORECASE)
                cleaned = pattern.sub(" ", cleaned)

    # 4. Remove leading Q prefix if this is a question (not an option)
    cleaned = cleaned.strip()
    if not is_option:
        cleaned = _Q_PREFIX_RE.sub("", cleaned).strip()

    # 5. Normalise spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_question_dict(q: Dict[str, Any], custom_words: Optional[List[str]] = None) -> Dict[str, Any]:
    """Clean a question object with question text, options, and explanation."""
    q_copy = dict(q)

    if "question" in q_copy:
        q_copy["question"] = clean_text(q_copy["question"], custom_words, is_option=False)

    if "options" in q_copy and isinstance(q_copy["options"], list):
        q_copy["options"] = [clean_text(opt, custom_words, is_option=True) for opt in q_copy["options"]]

    if "explanation" in q_copy and q_copy["explanation"]:
        q_copy["explanation"] = clean_text(q_copy["explanation"], custom_words, is_option=True)

    return q_copy
