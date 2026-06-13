"""
╔══════════════════════════════════════════════════════════╗
║         SMART QUIZ PARSER — Bulk Import Engine          ║
║   Auto-detects ANY quiz format from plain text files    ║
╚══════════════════════════════════════════════════════════╝
"""

import re
import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────

# Question number prefix: "1." "1)" "Q1." "Q:" "Question 1:"
_Q_NUM_RE = re.compile(
    r"^(?:Q(?:uestion)?[\.\s]*\d*[\s:\.]?\s*|\d{1,3}[\.)\s]+)",
    re.IGNORECASE
)

# Option label prefix: "(A)" "A)" "A." "1)" "1." "[A]"
_OPT_RE = re.compile(
    r"^[\(\[\{]?([A-Da-d]|[1-4])[\)\]\}\.:>\s]",
    re.IGNORECASE
)

# Answer marker: "Answer: B" "Ans: 2" "Correct: C" "Key: 1" etc
_ANS_RE = re.compile(
    r"^(?:answer|ans(?:wer)?|correct(?:\s+answer)?|key|solution|right)\s*[:\-\.>]?\s*[\(\[]?([A-Da-d1-4])[\)\]]?",
    re.IGNORECASE
)

# Inline 4-option on same line
_INLINE4 = re.compile(
    r"[\(\[]?[Aa][\)\]]?[\s\.:]+(.+?)\s+"
    r"[\(\[]?[Bb][\)\]]?[\s\.:]+(.+?)\s+"
    r"[\(\[]?[Cc][\)\]]?[\s\.:]+(.+?)\s+"
    r"[\(\[]?[Dd][\)\]]?[\s\.:]+(.+?)\s*$"
)

# Correct marker inline: trailing * or [correct]
_STAR_RE = re.compile(r"[\*]{1,2}\s*$|\[correct\]\s*$|\[ans\]\s*$", re.IGNORECASE)


def _label_to_idx(ch: str) -> Optional[int]:
    return {"A":0,"B":1,"C":2,"D":3,"a":0,"b":1,"c":2,"d":3,
            "1":0,"2":1,"3":2,"4":3}.get(ch)


def _strip_opt_label(line: str) -> Tuple[Optional[int], str]:
    m = _OPT_RE.match(line.strip())
    if not m:
        return None, line.strip()
    ch   = m.group(1)
    rest = line.strip()[m.end():].strip()
    return _label_to_idx(ch), rest


def _is_question_line(line: str) -> bool:
    """True if line starts with a question numbering prefix."""
    return bool(_Q_NUM_RE.match(line.strip()))


def _is_option_line(line: str) -> bool:
    """True if line starts with an option label (A/B/C/D or 1/2/3/4)."""
    return bool(_OPT_RE.match(line.strip()))


def _clean_q(raw: str) -> str:
    return _Q_NUM_RE.sub("", raw.strip()).strip()


# ══════════════════════════════════════════════════════════════════════════

class SmartQuizParser:

    def parse(self, text: str) -> List[Dict]:
        lines  = [l.rstrip() for l in text.splitlines()]
        blocks = self._make_blocks(lines)
        result = []
        for b in blocks:
            q = self._parse_block(b)
            if q:
                result.append(q)
        logger.info(f"SmartQuizParser: {len(result)}/{len(blocks)} blocks parsed")
        return result

    # ── Block splitting ───────────────────────────────────────────────────

    def _make_blocks(self, lines: List[str]) -> List[List[str]]:
        """
        Split lines into question blocks.
        Uses blank lines as primary separator,
        numbered question lines as secondary separator.
        """
        # Primary split: by blank lines
        segments = []
        seg = []
        for line in lines:
            if line.strip():
                seg.append(line.strip())
            else:
                if seg:
                    segments.append(seg)
                    seg = []
        if seg:
            segments.append(seg)

        if not segments:
            return []

        # Secondary: if no blank lines at all, split by numbered question lines
        # A line is a TRUE question start if:
        #   - Matches Q_STARTS pattern  AND
        #   - (ends with ? OR is longer than 40 chars OR has question words)
        def _is_true_question(line: str) -> bool:
            if not _is_question_line(line):
                return False
            text = _Q_NUM_RE.sub("", line).strip()
            if line.strip().endswith("?"):
                return True
            if len(text) > 45:
                return True
            q_words = ("who", "what", "which", "when", "where", "why", "how",
                       "whose", "find", "identify", "choose", "select")
            if any(text.lower().startswith(w) for w in q_words):
                return True
            return False

        if len(segments) == 1 and len(segments[0]) > 6:
            # Everything in one segment — try to split by question lines
            blocks, current = [], []
            for line in segments[0]:
                if _is_true_question(line) and current and self._block_has_opts(current):
                    blocks.append(current)
                    current = [line]
                else:
                    current.append(line)
            if current:
                blocks.append(current)
            return [b for b in blocks if b] if blocks else segments

        return segments

    def _block_has_opts(self, lines: List[str]) -> bool:
        return sum(1 for l in lines if _is_option_line(l.strip())) >= 2

    # ── Block parser ──────────────────────────────────────────────────────

    def _parse_block(self, lines: List[str]) -> Optional[Dict]:
        lines = [l.strip() for l in lines if l.strip()]
        if len(lines) < 3:
            return None

        # Try each strategy in order
        for strategy in [
            self._s_standard_labeled,
            self._s_numbered_opts,
            self._s_inline_opts,
            self._s_asterisk,
            self._s_loose,
        ]:
            q = strategy(lines)
            if q:
                return q
        return None

    # ── Strategy: standard A/B/C/D labeled options ────────────────────────

    def _s_standard_labeled(self, lines: List[str]) -> Optional[Dict]:
        """
        Question line(s)
        A) opt / B) opt / C) opt / D) opt  (1 per line or inline)
        Answer: B
        """
        q_lines, opt_lines, ans_line = [], [], None

        for line in lines:
            if _ANS_RE.match(line):
                ans_line = line
            elif _is_option_line(line) and not (_is_question_line(line) and not q_lines):
                opt_lines.append(line)
            else:
                if not opt_lines:
                    q_lines.append(line)

        if not q_lines:
            return None

        question = _clean_q(" ".join(q_lines))
        if len(question) < 8:
            return None

        options, correct = self._extract_abcd(opt_lines)
        if options is None:
            return None

        if ans_line:
            m = _ANS_RE.match(ans_line)
            if m:
                correct = _label_to_idx(m.group(1))

        if correct is None:
            return None

        return _make(question, options, correct)

    # ── Strategy: numbered options 1.2.3.4. ──────────────────────────────

    def _s_numbered_opts(self, lines: List[str]) -> Optional[Dict]:
        """
        Question text
        1. opt1  2. opt2  3. opt3  4. opt4
        Ans: 2
        """
        # Find answer line first
        ans_idx  = None
        ans_line_idx = None
        for i, line in enumerate(lines):
            m = _ANS_RE.match(line)
            if m:
                ans_idx = _label_to_idx(m.group(1))
                ans_line_idx = i
                break

        if ans_idx is None:
            return None

        # Lines before answer
        content = lines[:ans_line_idx]
        if not content:
            return None

        # Collect numbered option lines (short lines like "1. Mumbai")
        opt_lines = []
        q_lines   = []
        for line in content:
            # A numbered option: starts with 1-4 followed by . or )
            # AND is SHORT (options are usually short, < 60 chars)
            num_m = re.match(r"^([1-4])[\.)\s]+(.+)$", line)
            if num_m and len(line) < 80:
                opt_lines.append((int(num_m.group(1)) - 1, num_m.group(2).strip()))
            else:
                if not opt_lines:
                    q_lines.append(line)

        if len(opt_lines) < 4:
            return None

        question = _clean_q(" ".join(q_lines))
        if len(question) < 8:
            return None

        options = [None, None, None, None]
        for idx, text in opt_lines[:4]:
            if 0 <= idx <= 3:
                options[idx] = text

        if None in options:
            return None

        return _make(question, options, ans_idx)

    # ── Strategy: inline 4 options on same line ───────────────────────────

    def _s_inline_opts(self, lines: List[str]) -> Optional[Dict]:
        full = " ".join(lines)
        m = _INLINE4.search(full)
        if not m:
            return None

        question = _clean_q(full[:m.start()])
        if len(question) < 8:
            return None

        options = [m.group(i).strip() for i in range(1, 5)]
        rest    = full[m.end():]
        ans_m   = _ANS_RE.search(rest)
        correct = _label_to_idx(ans_m.group(1)) if ans_m else None

        if correct is None:
            return None

        return _make(question, options, correct)

    # ── Strategy: asterisk correct marker ────────────────────────────────

    def _s_asterisk(self, lines: List[str]) -> Optional[Dict]:
        q_lines, opt_lines = [], []
        for line in lines:
            if _is_option_line(line):
                opt_lines.append(line)
            elif not opt_lines:
                q_lines.append(line)

        if not q_lines or len(opt_lines) < 2:
            return None

        question = _clean_q(" ".join(q_lines))
        if len(question) < 8:
            return None

        options, correct = [], None
        for line in opt_lines:
            is_correct = bool(_STAR_RE.search(line))
            clean      = _STAR_RE.sub("", line).strip()
            _, text    = _strip_opt_label(clean)
            options.append(text)
            if is_correct:
                correct = len(options) - 1

        if len(options) != 4 or correct is None:
            return None

        return _make(question, options, correct)

    # ── Strategy: loose / creative ────────────────────────────────────────

    def _s_loose(self, lines: List[str]) -> Optional[Dict]:
        question, options, correct = None, [], None

        for line in lines:
            m = _ANS_RE.match(line)
            if m:
                correct = _label_to_idx(m.group(1))
                continue
            if _is_option_line(line) and not (_is_question_line(line) and not question):
                _, text = _strip_opt_label(line)
                options.append(text)
            elif not question:
                q = _clean_q(line)
                if len(q) >= 8:
                    question = q

        if not question or len(options) < 4 or correct is None:
            return None

        return _make(question, options[:4], correct)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _extract_abcd(self, opt_lines: List[str]) -> Tuple[Optional[List[str]], Optional[int]]:
        """Extract exactly 4 A/B/C/D labeled options."""
        opts = [None, None, None, None]
        for line in opt_lines:
            # Might be inline: (A) x (B) y (C) z (D) w
            m4 = _INLINE4.match(line)
            if m4:
                return [m4.group(i).strip() for i in range(1, 5)], None

            idx, text = _strip_opt_label(line)
            if idx is not None and 0 <= idx <= 3:
                opts[idx] = text

        if None in opts:
            return None, None
        return opts, None


# ── Helpers ────────────────────────────────────────────────────────────────

def _make(question: str, options: List[str], correct: int) -> Dict:
    return {
        "question":       question,
        "options":        [str(o).strip() for o in options],
        "correct_answer": correct,
        "category":       "General",
    }


# ══════════════════════════════════════════════════════════════════════════
#  CATEGORY AUTO-TAGGER
# ══════════════════════════════════════════════════════════════════════════

_CAT_KW = {
    "Legal Reasoning": [
        "article", "constitution", "ipc", "crpc", "act", "court", "supreme",
        "amendment", "fundamental right", "directive", "habeas", "mandamus",
        "writ", "bail", "cognizable", "tort", "contract", "negligence",
        "liability", "section", "judgement", "decree",
    ],
    "Current Affairs": [
        "recently", "2024", "2025", "2026", "latest", "appointed", "elected",
        "inaugurated", "launched", "scheme", "mission", "prime minister",
        "president", "government",
    ],
    "English": [
        "synonym", "antonym", "grammar", "passage", "comprehension", "sentence",
        "phrase", "idiom", "verb", "noun", "adjective", "tense", "vocabulary",
        "word", "meaning", "spelling",
    ],
    "Logical Reasoning": [
        "series", "pattern", "sequence", "analogy", "syllogism", "conclusion",
        "inference", "assumption", "assertion", "coding", "blood relation",
        "direction", "ranking",
    ],
    "GK": [
        "capital", "country", "state", "river", "mountain", "ocean", "planet",
        "invention", "discovered", "largest", "smallest", "national", "award",
    ],
    "History": [
        "ancient", "medieval", "mughal", "british", "independence", "1947",
        "freedom fighter", "battle", "empire", "dynasty", "revolt", "treaty",
    ],
    "Polity": [
        "parliament", "rajya sabha", "lok sabha", "governor", "election",
        "vote", "panchayat", "federal", "judicial", "executive", "legislative",
    ],
}

def guess_category(q: str) -> str:
    q_low  = q.lower()
    scores = {cat: sum(1 for kw in kws if kw in q_low) for cat, kws in _CAT_KW.items()}
    best   = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General"


# ══════════════════════════════════════════════════════════════════════════
#  BULK IMPORT COORDINATOR
# ══════════════════════════════════════════════════════════════════════════

def bulk_import(text: str, quiz_manager) -> Dict:
    parser   = SmartQuizParser()
    raw      = parser.parse(text)
    existing = {q["question"].strip().lower() for q in quiz_manager.questions}

    imported, skipped, failed = 0, 0, 0
    errors, batch = [], []

    for item in raw:
        q_text  = item.get("question", "").strip()
        options = item.get("options", [])
        correct = item.get("correct_answer")

        if not q_text or len(q_text) < 8:
            skipped += 1; continue
        if not isinstance(options, list) or len(options) != 4:
            skipped += 1; continue
        if not all(o and str(o).strip() for o in options):
            skipped += 1; continue
        if correct is None or not (0 <= correct <= 3):
            skipped += 1; continue
        if q_text.lower() in existing:
            skipped += 1; continue

        item["category"] = guess_category(q_text)
        existing.add(q_text.lower())
        batch.append(item)

    if batch:
        try:
            res      = quiz_manager.add_questions(batch)
            imported = res.get("added", 0)
            skipped += res.get("rejected", {}).get("duplicates", 0)
            errs     = res.get("errors", [])
            failed  += len(errs)
            errors   = errs[:5]
        except Exception as e:
            failed = len(batch)
            errors = [str(e)]

    return {
        "total_detected": len(raw),
        "imported":       imported,
        "skipped":        skipped,
        "failed":         failed,
        "errors":         errors,
    }
