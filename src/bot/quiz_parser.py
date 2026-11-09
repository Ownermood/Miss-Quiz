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
        """Universal parser first; the older strategy parser is only a fallback
        when the universal one finds nothing."""
        found, self.problems = universal_parse(text)
        if found:
            return found
        return self._legacy_parse(text)

    def _legacy_parse(self, text: str) -> List[Dict]:
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
        # Primary split: by blank lines — strip each line once
        segments = []
        seg = []
        for line in lines:
            s = line.strip()
            if s:
                seg.append(s)
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
        "category":       "General Knowledge",
    }


# ══════════════════════════════════════════════════════════════════════════
#  CATEGORY AUTO-TAGGER
# ══════════════════════════════════════════════════════════════════════════

_CAT_KW = {
    "Constitution & Law": [
        "ipc", "crpc", "act", "court", "supreme", "amendment",
        "fundamental right", "directive", "habeas", "mandamus",
        "writ", "bail", "cognizable", "tort", "contract", "negligence",
        "liability", "section", "judgement", "decree", "law", "legal",
    ],
    "Constitution": [
        "article", "constitution", "schedule", "preamble", "citizenship",
        "fundamental duty", "dpsp", "lok sabha", "rajya sabha", "parliament",
    ],
    "Current Affairs": [
        "recently", "2024", "2025", "2026", "latest", "appointed", "elected",
        "inaugurated", "launched", "scheme", "mission", "prime minister",
        "president", "government",
    ],
    "Political Science": [
        "parliament", "governor", "election", "vote", "panchayat", "federal",
        "judicial", "executive", "legislative", "polity", "democracy",
        "republic", "cabinet", "ministry",
    ],
    "History": [
        "ancient", "medieval", "mughal", "british", "independence", "1947",
        "freedom fighter", "battle", "empire", "dynasty", "revolt", "treaty",
        "colonial", "partition",
    ],
    "Geography": [
        "capital", "country", "state", "river", "mountain", "ocean",
        "latitude", "longitude", "peninsula", "plateau", "desert", "island",
        "climate", "soil", "forest",
    ],
    "Science & Technology": [
        "planet", "invention", "discovered", "atom", "cell", "element",
        "chemical", "formula", "biology", "physics", "chemistry", "orbit",
        "satellite", "dna", "enzyme",
    ],
    "Economics": [
        "gdp", "inflation", "budget", "tax", "revenue", "fiscal", "monetary",
        "rbi", "bank", "interest rate", "market", "demand", "supply",
        "poverty", "unemployment",
    ],
    "General Knowledge": [
        "largest", "smallest", "national", "award", "capital of", "founded",
        "headquarter", "who is", "which country", "first woman", "first man",
    ],
    "Static GK": [
        "emblem", "anthem", "symbol", "flag", "dance", "festival", "language",
        "bird", "animal", "flower", "sport", "currency", "slogan",
    ],
    "Arts & Literature": [
        "author", "book", "novel", "poem", "poet", "literature", "music",
        "dance", "painting", "sculpture", "art", "theatre", "cinema",
    ],
    "Sports & Games": [
        "cricket", "football", "tennis", "hockey", "badminton", "chess",
        "olympics", "world cup", "trophy", "player", "stadium", "medal",
        "tournament", "championship",
    ],
}

# Pre-built per-category regex (compiled once at import time).
# Each pattern matches any keyword for that category in one pass.
_CAT_RE = {
    cat: re.compile("|".join(re.escape(kw) for kw in kws), re.IGNORECASE)
    for cat, kws in _CAT_KW.items()
}

def guess_category(q: str) -> str:
    best_cat, best_score = "General", 0
    for cat, pattern in _CAT_RE.items():
        n = len(pattern.findall(q))
        if n > best_score:
            best_score, best_cat = n, cat
    return best_cat if best_score > 0 else "General"


# ══════════════════════════════════════════════════════════════════════════
#  BULK IMPORT COORDINATOR
# ══════════════════════════════════════════════════════════════════════════

MAX_IMPORT_PER_FILE = 100_000
_TELEGRAM_OPT_MAX   = 100  # Telegram hard limit: poll option text must not exceed 100 chars


def _clamp_option(opt: str) -> str:
    opt = str(opt).strip()
    return opt[:97] + "…" if len(opt) > _TELEGRAM_OPT_MAX else opt


def bulk_import(text: str, quiz_manager) -> Dict:
    parser   = SmartQuizParser()
    raw      = parser.parse(text)
    problems = list(getattr(parser, "problems", []) or [])
    if len(raw) > MAX_IMPORT_PER_FILE:
        logger.warning(f"Import truncated: {len(raw)} detected, max={MAX_IMPORT_PER_FILE}")
        raw = raw[:MAX_IMPORT_PER_FILE]
    existing = {q["question"].strip().lower() for q in quiz_manager.questions}

    imported, skipped, failed, duplicates = 0, 0, 0, 0
    errors, batch = [], []

    for item in raw:
        q_text  = item.get("question", "").strip()
        options = item.get("options", [])
        correct = item.get("correct_answer")
        where   = f"“{q_text[:30]}”"

        if not q_text or len(q_text) < 3:
            skipped += 1; problems.append({"where": where, "reason": "question too short"}); continue
        if not isinstance(options, list) or not (2 <= len(options) <= 10):   # Telegram: 2–10 options
            skipped += 1; problems.append({"where": where, "reason": "needs 2–10 options"}); continue
        if not all(o and str(o).strip() for o in options):
            skipped += 1; problems.append({"where": where, "reason": "empty option"}); continue
        if correct is None or not (0 <= correct < len(options)):
            skipped += 1; problems.append({"where": where, "reason": "answer out of range"}); continue
        if q_text.lower() in existing:
            skipped += 1; duplicates += 1; continue
        if item.get("explanation"):
            item["explanation"] = item["explanation"][:200]

        # Enforce Telegram's 100-char poll option limit before saving
        clamped = [_clamp_option(o) for o in options]
        if clamped != [str(o).strip() for o in options]:
            over = [len(str(o).strip()) for o in options if len(str(o).strip()) > _TELEGRAM_OPT_MAX]
            logger.warning(
                f"[IMPORT] Option(s) truncated to fit Telegram limit "
                f"(original lengths {over}): {q_text[:60]!r}"
            )
        item["options"]  = clamped
        if not item.get("category") or item["category"] == "General Knowledge":
            item["category"] = guess_category(q_text)   # keep a category given in the file
        existing.add(q_text.lower())
        batch.append(item)

    if batch:
        try:
            # Pass existing set so add_questions() skips rebuilding it
            res      = quiz_manager.add_questions(batch, _existing=existing)
            imported = res.get("added", 0)
            dup2     = res.get("rejected", {}).get("duplicates", 0)
            skipped += dup2
            duplicates += dup2
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
        "duplicates":     duplicates,
        "problems":       problems,          # [{where, reason}] — why things were skipped
    }


# ══════════════════════════════════════════════════════════════════════════
#  UNIVERSAL PARSER  (any common .txt layout, from any AI / book / PYQ)
# ══════════════════════════════════════════════════════════════════════════
#  universal_parse(text) -> (questions, problems)
#  questions: [{question, options, correct_answer, category, explanation?}]
#  problems:  [{"where": "Q12" / first words, "reason": "..."}]

_HI_LETTERS = "कखगघङचछजझ"
_ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]

_U_Q = re.compile(
    r"^(?:(?:q(?:uestion|ues|n)?|प्रश्न|प्र)\s*[\.\-:#]?\s*(\d+)\s*[\.\):\-–]?|(\d+)\s*[\.\):–]|(\d+)\s+-|q\s*[\.\):\-–])\s*(.*)$",
    re.IGNORECASE)
_U_OPT = re.compile(
    r"^(?:option\s*)?[\(\[]?\s*([A-Ja-j]|[1-9]|10|i{1,3}|iv|v|vi{0,3}|ix|x|[" + _HI_LETTERS + r"])\s*[\)\]\.:\-–]\s*(.*)$",
    re.IGNORECASE)
_U_ANS = re.compile(
    r"^(?:correct\s*(?:answer|option|ans)?|right\s*answer|ans(?:wer)?s?|उत्तर|सही\s*उत्तर|key)\s*(?:is)?\s*[:\-–=\.\)]*\s*(.+)$",
    re.IGNORECASE)
_U_EXP = re.compile(
    r"^(?:explanation|explain|exp|solution|sol|reason|hint|व्याख्या|हल|स्पष्टीकरण)\s*[:\-–\.]\s*(.*)$",
    re.IGNORECASE)
_U_META = re.compile(r"^(category|subject|topic|difficulty|level|exam|source)\s*[:\-]\s*(.*)$", re.IGNORECASE)
_U_KEY_HDR = re.compile(
    r"^(?:answer\s*key|answers|ans(?:wer)?\s*sheet|उत्तर\s*(?:कुंजी|माला))\s*[:\-]?\s*(.*)$|^(?:answer|ans)\s*[:\-]?\s*$",
    re.IGNORECASE)
_U_KEY_ITEM = re.compile(r"(?:q\s*)?(\d+)\s*[\.\):\-–=]?\s*\(?([A-Ja-j]|\d{1,2}|[" + _HI_LETTERS + r"])\)?(?=[\s,;|]|$)",
                         re.IGNORECASE)
_U_MARK = re.compile(r"(\s*(?:\*{1,2}|✅|✔️?|☑️?|✓|\(correct\)|\[correct\]|\(ans\)|\[ans\]|←\s*correct|<-+\s*correct))\s*$",
                     re.IGNORECASE)
_U_INLINE_OPT = re.compile(r"(?:^|\s)[\(\[]?([A-Ja-j])[\)\]\.]\s+")
_U_INLINE_ANS = re.compile(
    r"(?<![Cc]orrect)(?<!CORRECT)(?<![Rr]ight)(?<!RIGHT)\s(?=(?:correct\s*answer|right\s*answer|answer|ans)\s*[:\-–])",
    re.IGNORECASE)


def _u_clean_line(line: str) -> str:
    line = line.replace("​", "").replace(" ", " ").strip()
    line = re.sub(r"^[>\-•●▪◦·*]+\s+(?=\S)", "", line)            # quote / bullet marks (incl. '* ')
    line = re.sub(r"(\*\*|__|`)", "", line)                         # markdown bold/code
    return line.strip()


def _u_split_inline(line: str) -> List[str]:
    """'Q? A) x B) y C) z Answer: B' → separate lines (only when the labels
    really are a sequence a, b, c… so 'Vitamin A. Is…' isn't split)."""
    line = _U_INLINE_ANS.sub("\n", line)
    out = []
    for part in line.split("\n"):
        ms = list(_U_INLINE_OPT.finditer(part))
        letters = [m.group(1).lower() for m in ms]
        seq_start = next((i for i, l in enumerate(letters) if l == "a"), None)
        if seq_start is None:
            out.append(part); continue
        seq = [ms[seq_start]]
        for m in ms[seq_start + 1:]:
            if ord(m.group(1).lower()) == ord(seq[-1].group(1).lower()) + 1:
                seq.append(m)
        if len(seq) < 2:
            out.append(part); continue
        cuts = [m.start() for m in seq] + [len(part)]
        head = part[:cuts[0]].strip()
        if head:
            out.append(head)
        out += [part[cuts[i]:cuts[i + 1]].strip() for i in range(len(seq))]
    return [p for p in out if p.strip()]


def _u_label_index(label: str, kind: str) -> Optional[int]:
    l = label.strip().lower()
    if kind == "num" and l.isdigit():
        return int(l) - 1
    if kind == "roman" and l in _ROMAN:
        return _ROMAN.index(l)
    if kind == "hindi" and l in _HI_LETTERS:
        return _HI_LETTERS.index(l)
    if len(l) == 1 and "a" <= l <= "j":
        return ord(l) - ord("a")
    return None


def _u_kind(label: str) -> str:
    l = label.lower()
    if l.isdigit():
        return "num"
    if l in _HI_LETTERS:
        return "hindi"
    if l in _ROMAN and l not in ("i", "v", "x"):   # i/v/x alone are ambiguous letters
        return "roman"
    return "letter"


def _u_resolve_answer(raw: str, opts: List[str], kind: str) -> Optional[int]:
    """'(b)', 'B', 'Option B', '2', 'b. Mumbai', 'Mumbai' → option index."""
    if raw is None:
        return None
    r = _u_clean_line(raw)
    r = re.sub(r"^(?:option|opt)\s*", "", r, flags=re.IGNORECASE).strip()
    m = re.match(r"^[\(\[]?\s*([A-Ja-j]|\d{1,2}|i{1,3}|iv|vi{0,3}|ix|[" + _HI_LETTERS + r"])\s*[\)\]]?(?:[\s\.\):\-–,]|$)", r)
    if m:
        lab = m.group(1)
        k = "num" if lab.isdigit() else ("hindi" if lab in _HI_LETTERS else
                                         ("roman" if kind == "roman" else "letter"))
        idx = _u_label_index(lab, k)
        if idx is not None and 0 <= idx < len(opts):
            rest = r[m.end():].strip(" .:-–)")
            # 'B. Mumbai' where B and the text disagree → trust the text
            if rest:
                t = _u_text_match(rest, opts)
                if t is not None:
                    return t
            return idx
    return _u_text_match(r, opts)


def _u_norm(s: str) -> str:
    return re.sub(r"[\s\.\,;:'\"\(\)]+", " ", s.lower()).strip()


def _u_text_match(text: str, opts: List[str]) -> Optional[int]:
    t = _u_norm(text)
    if not t:
        return None
    norm = [_u_norm(o) for o in opts]
    if t in norm:
        return norm.index(t)
    hits = [i for i, o in enumerate(norm) if o and (t in o or o in t)]
    return hits[0] if len(hits) == 1 else None


def _u_answer_key(lines: List[str]) -> Tuple[List[str], Dict[int, str]]:
    """Find an 'Answer Key' section (usually at the end) → (other lines, {num: answer})."""
    key: Dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _U_KEY_HDR.match(line)
        if m:
            body = " ".join(([m.group(1)] if m.group(1) else []) + lines[i + 1:])
            items = _U_KEY_ITEM.findall(body)
            if len(items) >= 1:
                key = {int(n): a for n, a in items}
                return lines[:i], key
    return lines, key


def universal_parse(text: str) -> Tuple[List[Dict], List[Dict]]:
    text = (text or "").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    raw_lines: List[str] = []
    for ln in text.split("\n"):
        c = _u_clean_line(ln)
        raw_lines.extend(_u_split_inline(c) if c else [""])
    lines, key = _u_answer_key(raw_lines)

    blocks: List[Dict] = []
    cur: Optional[Dict] = None
    blank_before = True

    def new_q(num, qtext):
        nonlocal cur
        cur = {"num": num, "q": [qtext] if qtext else [], "opts": [], "kind": None,
               "marked": None, "ans": None, "exp": [], "meta": {}, "in_exp": False}
        blocks.append(cur)

    for line in lines:
        if not line:
            blank_before = True
            if cur:
                cur["in_exp"] = False
            continue

        m_ans = _U_ANS.match(line)
        if cur and m_ans and cur["opts"]:
            cur["ans"] = m_ans.group(1); cur["in_exp"] = False
            blank_before = False; continue
        m_exp = _U_EXP.match(line)
        if cur and m_exp:
            cur["exp"] = [m_exp.group(1)] if m_exp.group(1) else []
            cur["in_exp"] = True; blank_before = False; continue
        m_meta = _U_META.match(line)
        if cur and m_meta:
            cur["meta"][m_meta.group(1).lower()] = m_meta.group(2).strip()
            for part in re.split(r"\s*\|\s*", m_meta.group(2)):      # 'Polity | Difficulty: Easy'
                mm = _U_META.match(part)
                if mm:
                    cur["meta"][mm.group(1).lower()] = mm.group(2).strip()
                elif part and m_meta.group(1).lower() == "category":
                    cur["meta"]["category"] = part.strip()
            blank_before = False; continue

        m_opt = _U_OPT.match(line)
        m_q = _U_Q.match(line)
        is_opt, kind = False, None
        if m_opt and cur is not None and m_opt.group(2).strip():
            lab, kind = m_opt.group(1), _u_kind(m_opt.group(1))
            expected = len(cur["opts"])
            # roman lists: '(i)' starts one; afterwards i/v/x keep the roman meaning
            if lab.lower() in _ROMAN and (cur["kind"] == "roman" or (expected == 0 and lab.lower() == "i")):
                kind = "roman"
            idx = _u_label_index(lab, kind if cur["kind"] in (None, kind) else cur["kind"])
            if kind == "num":
                # a numbered line is an option only while this question is
                # collecting numbered options in order and has no answer yet
                is_opt = (cur["kind"] in (None, "num") and idx == expected and cur["ans"] is None
                          and (cur["q"] or cur["kind"] == "num") and not (cur["kind"] is None and m_q and m_q.group(2) and expected == 0 and not cur["q"]))
            else:
                is_opt = (idx == expected) or (expected == 0 and idx == 0)
        if is_opt:
            body = m_opt.group(2).strip()
            mk = _U_MARK.search(body)
            if mk:
                body = body[:mk.start()].strip()
                cur["marked"] = len(cur["opts"])
            cur["kind"] = cur["kind"] or kind
            cur["opts"].append(body)
            cur["in_exp"] = False; blank_before = False; continue

        if m_q and (m_q.group(4) or "").strip():
            num = m_q.group(1) or m_q.group(2) or m_q.group(3)
            new_q(int(num) if num else None, m_q.group(4).strip())
            blank_before = False; continue

        # plain text line
        if cur and cur["in_exp"]:
            cur["exp"].append(line)
        elif cur is None or (cur["opts"] and (blank_before or line.rstrip().endswith("?"))):
            new_q(None, line)                       # unnumbered question
        elif not cur["opts"]:
            cur["q"].append(line)                   # multi-line question text
        else:
            cur["opts"][-1] += " " + line           # wrapped option text
        blank_before = False

    questions, problems = [], []
    for n, b in enumerate(blocks, 1):
        where = f"Q{b['num']}" if b["num"] else f"#{n} “{' '.join(b['q'])[:30]}”"
        qtext = re.sub(r"\s+", " ", " ".join(b["q"])).strip()
        opts = [re.sub(r"\s+", " ", o).strip() for o in b["opts"] if o.strip()]
        if not qtext:
            problems.append({"where": where, "reason": "question text missing"}); continue
        if len(opts) < 2:
            problems.append({"where": where, "reason": f"found {len(opts)} option(s), need at least 2"}); continue
        if len(opts) > 10:
            problems.append({"where": where, "reason": "more than 10 options"}); continue
        correct = None
        if b["ans"] is not None:
            correct = _u_resolve_answer(b["ans"], opts, b["kind"] or "letter")
            if correct is None:
                problems.append({"where": where, "reason": f"answer “{b['ans'][:20]}” doesn't match any option"}); continue
        elif b["marked"] is not None:
            correct = b["marked"]
        elif key:
            k = key.get(b["num"]) if b["num"] else None
            k = k if k is not None else key.get(n)
            if k is not None:
                correct = _u_resolve_answer(k, opts, b["kind"] or "letter")
        if correct is None:
            problems.append({"where": where, "reason": "no answer found (add 'Answer: B', mark ✅, or an answer key)"})
            continue
        item = _make(qtext, opts, correct)
        exp = re.sub(r"\s+", " ", " ".join(b["exp"])).strip()
        if exp:
            item["explanation"] = exp
        if b["meta"].get("category"):
            item["category"] = b["meta"]["category"][:40]
        questions.append(item)
    return questions, problems
