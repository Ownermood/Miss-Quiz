"""
Universal Poll & Text Cleaner — Safai Filter.
Auto-removes poll counters, channel usernames, invite links, coaching PDF headers/footers
(URLs, bare domains, branch phone numbers, leaked question numbers on options),
and custom user words without harming dates (1951-1956), fractions (3/4), or numeric options.
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

# 3. Leading question number (for questions only, NOT options): "1.", "476.", "Q1:", "Question 2 -"
_Q_PREFIX_RE = re.compile(
    r"^\s*(?:(?:Q(?:uestion|ues|n)?|प्रश्न|प्र)\s*[\.\-:#]?\s*\d{1,4}\s*(?:[\.\):\-–]+\s*|\s+)|\d{1,4}\s*[\.\):\-–]+\s*)(?=\S)",
    re.IGNORECASE
)

# 4. @usernames, Telegram links, URLs, and bare domains (e.g. WWW.manoeuvreedu.com, manoeuvreedu.com)
_TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)?[a-zA-Z0-9_/\+\-]+",
    re.IGNORECASE
)
_USERNAME_RE = re.compile(r"@[a-zA-Z0-9_]{3,32}", re.IGNORECASE)
_URL_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|\b[a-zA-Z0-9\-]{3,}\.(?:com|in|org|edu|net|co\.in|ac\.in|gov\.in|info|io|me)\b(?:/\S*)?)",
    re.IGNORECASE
)

# 5. Coaching PDF branch + phone numbers & contact numbers:
#    e.g., "Borivali- 9076424208", "Dadar-7304366681", "Vasai-7045313787", "Ph: 022-28986681", "+91 9876543210"
_BRANCH_PHONE_RE = re.compile(
    r"\b[A-Za-z]{2,25}\s*[-:]+\s*(?:\+?91[\s\-]?)?(?:\d{10,12}|\d{3,5}\-\d{5,8})\b",
    re.IGNORECASE
)
_MOBILE_RE = re.compile(
    r"(?:(?<=\s)|^)(?:\+?91[\s\-]?)?[6-9]\d{9}\b"
)

# 6. Page headers / footers: "Page 12 of 40", "Page - 5"
_PAGE_MARKER_RE = re.compile(
    r"\bPage\s*(?:No\.?\s*)?[\-:]?\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\b",
    re.IGNORECASE
)

# 7. Common spam / promo phrases
_SPAM_WORDS_RE = re.compile(
    r"(?:join\s+(?:our\s+)?channel|subscribe\s+now|join\s+telegram|whatsapp\s+group|contact\s+for\s+pdf|dm\s+for\s+notes|all\s+rights\s+reserved)",
    re.IGNORECASE
)

# 8. Trailing leaked next-question number on an option:
#    e.g. "AP and Karnataka 477." -> "AP and Karnataka", "Mobile 694." -> "Mobile"
#    Safe guard: checks the preceding word is NOT Article/Section/Month/Rs/etc.
_SAFE_PRE_WORDS = {
    "article", "art", "section", "sec", "rule", "order", "clause", "amendment",
    "schedule", "chapter", "part", "act", "no", "number", "rs", "inr", "usd",
    "vol", "volume", "fig", "figure", "table", "class", "grade", "type", "stage",
    "phase", "level", "group", "tier", "step", "point", "para", "item", "code",
    "year", "ad", "bc", "bce", "ce", "in", "of", "to", "by", "at", "on", "from",
    "for", "is", "was", "are", "were", "under", "between", "and", "or",
    "question", "ques", "qn", "problem", "exam", "set",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
}
_TRAILING_OPT_QNUM_RE = re.compile(
    r"^(.*\b([A-Za-z]{2,})[\)\]'\"”’]?)\s+(?:Q(?:uestion|ues)?\.?\s*)?\d{1,4}\s*[\.\)]\s*$",
    re.IGNORECASE
)


def _strip_trailing_opt_qnum(opt: str) -> str:
    """Strip a leaked next-question number or leaked question sentence from an option
    (e.g. 'Mobile 694.' or 'Right to Housing 309. Which Article...')."""
    s = opt.strip()
    m_body = re.search(r'\s+(?:Q(?:uestion|ues)?\.?\s*)?(\d{1,4})\.\s+([A-Z].*)$', s)
    if m_body:
        pre_text = s[:m_body.start()].strip()
        last_word = re.split(r'\s+', pre_text)[-1].lower() if pre_text else ""
        if last_word not in _SAFE_PRE_WORDS:
            return pre_text

    m = _TRAILING_OPT_QNUM_RE.match(s)
    if m:
        last_word = m.group(2).lower()
        if last_word not in _SAFE_PRE_WORDS:
            return m.group(1).strip()
    return opt


def clean_text(text: str, custom_words: Optional[List[str]] = None, is_option: bool = False) -> str:
    """
    Clean raw question text or option string:
    1. Strip [1/100] bracketed counters
    2. Strip Telegram links, URLs, bare domains, @usernames
    3. Strip coaching branch+phone numbers (e.g. 'Borivali- 9076424208 Dadar-7304366681')
    4. Strip page markers and generic spam phrases
    5. Strip custom blacklist words
    6. Strip leading question numbers (when is_option=False) or trailing leaked question numbers (when is_option=True)
    """
    if not text:
        return ""

    cleaned = str(text)

    # 1. Remove bracketed [1/100] style counters
    cleaned = _BRACKET_COUNTER_RE.sub(" ", cleaned)
    if not is_option:
        cleaned = _LEADING_COUNTER_RE.sub("", cleaned)

    # 2. Remove URLs, bare domains, and @mentions
    cleaned = _TELEGRAM_LINK_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _USERNAME_RE.sub(" ", cleaned)

    # 3. Remove coaching branch + phone numbers & standalone mobile numbers
    cleaned = _BRANCH_PHONE_RE.sub(" ", cleaned)
    cleaned = _MOBILE_RE.sub(" ", cleaned)
    cleaned = _PAGE_MARKER_RE.sub(" ", cleaned)
    cleaned = _SPAM_WORDS_RE.sub(" ", cleaned)

    # 4. Remove custom banned words
    if custom_words:
        for w in custom_words:
            w_strip = w.strip()
            if w_strip:
                pattern = re.compile(re.escape(w_strip), re.IGNORECASE)
                cleaned = pattern.sub(" ", cleaned)

    # 5. Normalise spaces before prefix/suffix stripping
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # 6. Question vs Option specific cleanup
    if not is_option:
        # Repeatedly strip leading question numbers (e.g. if header was before '476. ')
        while True:
            next_cleaned = _Q_PREFIX_RE.sub("", cleaned).strip()
            if next_cleaned == cleaned or not next_cleaned:
                break
            cleaned = next_cleaned
    else:
        cleaned = _strip_trailing_opt_qnum(cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_question_dict(q: Dict[str, Any], custom_words: Optional[List[str]] = None) -> Dict[str, Any]:
    """Clean a question object with question text, options, and explanation."""
    q_copy = dict(q)

    if "question" in q_copy and q_copy["question"]:
        q_copy["question"] = clean_text(q_copy["question"], custom_words, is_option=False)

    if "options" in q_copy and isinstance(q_copy["options"], list):
        q_copy["options"] = [clean_text(opt, custom_words, is_option=True) for opt in q_copy["options"]]

    if "explanation" in q_copy and q_copy["explanation"]:
        q_copy["explanation"] = clean_text(q_copy["explanation"], custom_words, is_option=True)

    return q_copy
