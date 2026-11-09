"""
file_extract.py — turn any question file into questions.

extract(filename, data) -> (items, text, note)
  items : questions already structured (CSV/XLSX/JSON with columns/keys)
  text  : plain text for quiz_parser.universal_parse (everything else)
  note  : why nothing could be read (unsupported / scanned PDF / ...)

Supported: .txt .md .text .log .rtf .csv .tsv .xlsx .xlsm .json .docx .odt
           .pptx .pdf (text PDFs) .html .htm .xml — plus any text/* file.
Not supported (honest): images & scanned PDFs (need OCR), old binary
.doc/.xls/.ppt (ask to re-save as .docx/.xlsx/.pptx).
"""

import csv
import html
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

MAX_BYTES = 20 * 1024 * 1024        # Telegram bot download limit


# ── text decoding ──────────────────────────────────────────────────────────

def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ol", "ul"}

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, d):
        if not self._skip:
            self.parts.append(d)


def html_to_text(s: str) -> str:
    p = _HTMLText()
    p.feed(s)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts))


def _xml_text(xml: str, para_tag: str, text_tag: str) -> str:
    """Pull paragraphs out of OOXML/ODF XML without extra libraries."""
    out = []
    for para in re.findall(rf"<{para_tag}[\s>].*?</{para_tag}>", xml, flags=re.S):
        out.append(html.unescape("".join(re.findall(rf"<{text_tag}[^>]*>(.*?)</{text_tag}>", para, flags=re.S))))
    return "\n".join(out)


def rtf_to_text(s: str) -> str:
    s = re.sub(r"\\par[d]?\s?", "\n", s)
    s = re.sub(r"\\'[0-9a-f]{2}", "", s)
    s = re.sub(r"\\[a-z]+-?\d* ?", "", s)
    return re.sub(r"[{}]", "", s)


# ── tables (CSV / XLSX) ────────────────────────────────────────────────────

_COL = {
    "question": ("question", "questions", "q", "ques", "prashn", "प्रश्न", "statement"),
    "answer": ("answer", "ans", "correct", "correct answer", "correct option", "key", "उत्तर", "right answer"),
    "explanation": ("explanation", "explain", "solution", "reason", "व्याख्या"),
    "category": ("category", "subject", "topic", "section"),
}
_OPT_HDR = re.compile(r"^(?:option|opt|choice)?\s*[\(\[]?([a-j1-9])[\)\]]?$|^(?:option|opt|choice)\s*([a-j1-9])$", re.I)


def _table_items(rows: List[List[str]]) -> Tuple[List[Dict], Optional[str]]:
    """Header row with question/option/answer columns → items.
    No usable header → None (caller turns cells into text instead)."""
    rows = [[str(c or "").strip() for c in r] for r in rows if any(str(c or "").strip() for c in r)]
    if len(rows) < 2:
        return [], None
    head = [h.lower().strip(" :*") for h in rows[0]]
    col = {k: next((i for i, h in enumerate(head) if h in names), None) for k, names in _COL.items()}
    opt_cols = [i for i, h in enumerate(head) if _OPT_HDR.match(h) and i not in col.values()]
    if col["question"] is None or len(opt_cols) < 2:
        return [], None
    items = []
    for r in rows[1:]:
        r = r + [""] * (len(head) - len(r))
        opts = [r[i] for i in opt_cols if r[i]]
        q = r[col["question"]]
        if not q:
            continue
        ans = r[col["answer"]] if col["answer"] is not None else ""
        item = {"question": q, "options": opts, "answer_raw": ans}
        if col["explanation"] is not None and r[col["explanation"]]:
            item["explanation"] = r[col["explanation"]]
        if col["category"] is not None and r[col["category"]]:
            item["category"] = r[col["category"]]
        items.append(item)
    return items, "table"


def _rows_to_text(rows: List[List[str]]) -> str:
    return "\n".join(" ".join(str(c).strip() for c in r if str(c or "").strip()) for r in rows)


# ── JSON ───────────────────────────────────────────────────────────────────

def _json_items(obj) -> List[Dict]:
    if isinstance(obj, dict):
        for key in ("questions", "quiz", "data", "items", "mcqs"):
            if isinstance(obj.get(key), list):
                return _json_items(obj[key])
        obj = [obj]
    items = []
    for o in obj if isinstance(obj, list) else []:
        if not isinstance(o, dict):
            continue
        low = {str(k).lower(): v for k, v in o.items()}
        q = next((low[k] for k in ("question", "q", "text", "ques", "title") if low.get(k)), None)
        opts = next((low[k] for k in ("options", "choices", "answers_list", "option") if isinstance(low.get(k), list)), None)
        if opts is None:
            opts = [low[k] for k in ("a", "b", "c", "d", "e", "option_a", "option_b", "option_c", "option_d", "option_e")
                    if low.get(k)]
        if isinstance(opts, list) and opts and isinstance(opts[0], dict):   # [{"text":..,"correct":true}]
            marked = next((i for i, x in enumerate(opts) if x.get("correct") or x.get("is_correct")), None)
            opts = [str(x.get("text") or x.get("option") or "") for x in opts]
            if marked is not None:
                low.setdefault("answer", chr(ord("A") + marked))   # letter: no 0/1-based guessing
        ans = next((low[k] for k in ("answer", "correct", "correct_answer", "correct_option", "ans", "key")
                    if k in low and low[k] is not None), "")
        # Telegram-style zero-based index keys → turn into a letter (A = first)
        for k in ("correct_option_id", "correct_index", "answer_index"):
            if isinstance(low.get(k), int) and not isinstance(low.get(k), bool):
                ans = chr(ord("A") + low[k])
                break
        if isinstance(ans, bool):
            ans = ""
        if q and opts:
            item = {"question": str(q), "options": [str(x) for x in opts], "answer_raw": ans}
            for k in ("explanation", "solution"):
                if low.get(k):
                    item["explanation"] = str(low[k]); break
            for k in ("category", "subject", "topic"):
                if low.get(k):
                    item["category"] = str(low[k]); break
            items.append(item)
    return items


# ── main entry ─────────────────────────────────────────────────────────────

def extract(filename: str, data: bytes) -> Tuple[List[Dict], str, str]:
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    if ext in ("jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "tif", "tiff"):
        return [], "", "Images need OCR (reading text from a picture), which this bot doesn't do. Send the questions as text, .txt, .docx or .pdf."
    if ext in ("doc", "xls", "ppt"):
        return [], "", f"Old .{ext} format isn't supported. Open it and 'Save as' .{ext}x, then send again."

    if ext in ("xlsx", "xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        items, text = [], []
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            got, _ = _table_items(rows)
            if got:
                items += got
            else:
                text.append(_rows_to_text(rows))
        return items, "\n\n".join(text), ""

    if ext in ("csv", "tsv"):
        s = decode_text(data)
        delim = "\t" if ext == "tsv" else (csv.Sniffer().sniff(s[:4096], ",;\t|").delimiter
                                          if s.strip() else ",")
        rows = list(csv.reader(io.StringIO(s), delimiter=delim))
        items, _ = _table_items(rows)
        return items, "" if items else _rows_to_text(rows), ""

    if ext == "json":
        try:
            obj = json.loads(decode_text(data))
        except ValueError:
            return [], decode_text(data), ""
        items = _json_items(obj)
        return items, "" if items else decode_text(data), ""

    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            rows = [[c.text for c in r.cells] for r in t.rows]
            got, _ = _table_items(rows)
            if got:
                return got, "\n".join(parts), ""
            parts.append(_rows_to_text(rows))
        return [], "\n".join(parts), ""

    if ext in ("odt", "pptx"):
        z = zipfile.ZipFile(io.BytesIO(data))
        if ext == "odt":
            xml = z.read("content.xml").decode("utf-8", "replace")
            xml = re.sub(r"</text:(?:p|h)>|<text:line-break/>", "\n", xml)
            return [], html.unescape(re.sub(r"<[^>]+>", "", xml)), ""
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return [], "\n\n".join(_xml_text(z.read(n).decode("utf-8", "replace"), "a:p", "a:t") for n in slides), ""

    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        if len(text.strip()) < 20:
            return [], "", "This PDF has no selectable text (it's a scan/photo). Scanned PDFs need OCR, which this bot doesn't do."
        return [], text, ""

    s = decode_text(data)
    if ext in ("html", "htm", "xml") or re.match(r"\s*<(?:!doctype html|html)", s, re.I):
        return [], html_to_text(s), ""
    if ext == "rtf" or s.startswith("{\\rtf"):
        return [], rtf_to_text(s), ""
    if "\x00" in s[:2000]:
        return [], "", f"Can't read this file type (.{ext or '?'}). Send .txt, .docx, .pdf, .csv, .xlsx or .json."
    return [], s, ""
