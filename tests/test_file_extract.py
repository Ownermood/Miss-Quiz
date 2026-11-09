"""Every supported file type → questions; unsupported ones get a clear note."""

import io, json, zipfile
from unittest.mock import MagicMock
import pytest

from src.bot.file_extract import extract
from src.bot.quiz_parser import bulk_import

TXT = "1. Capital of India?\nA) Delhi\nB) Mumbai\nC) Pune\nD) Agra\nAnswer: A\n\n2. 2+2?\nA) 3\nB) 4\nAnswer: B"


def _import(name, data):
    items, text, note = extract(name, data)
    assert not note, note
    qm = MagicMock(); qm.questions = []
    qm.add_questions.side_effect = lambda b, _existing=None: {"added": len(b), "rejected": {}, "errors": []}
    res = bulk_import(text, qm, items)
    return res, qm.add_questions.call_args.args[0] if qm.add_questions.called else []


def _pdf(text_lines):
    """Minimal valid text PDF (no extra libraries)."""
    esc = lambda l: l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(f"({esc(l)}) Tj T*" for l in text_lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer << /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


@pytest.mark.parametrize("name,maker", [
    ("q.txt", lambda: TXT.encode()),
    ("q.md", lambda: TXT.encode()),
    ("q_utf16.txt", lambda: TXT.encode("utf-16")),
    ("q.html", lambda: ("<html><body>" + "".join(f"<p>{l}</p>" for l in TXT.split("\n")) + "</body></html>").encode()),
    ("q.rtf", lambda: ("{\\rtf1\\ansi " + "\\par ".join(TXT.split("\n")) + "}").encode()),
    ("q.pdf", lambda: _pdf(TXT.split("\n"))),
])
def test_text_like_files(name, maker):
    res, saved = _import(name, maker())
    assert res["imported"] == 2 and [q["correct_answer"] for q in saved] == [0, 1]


def test_docx_paragraphs():
    import docx
    d = docx.Document()
    for line in TXT.split("\n"):
        d.add_paragraph(line)
    b = io.BytesIO(); d.save(b)
    res, _ = _import("q.docx", b.getvalue())
    assert res["imported"] == 2


def test_docx_table_with_columns():
    import docx
    d = docx.Document()
    t = d.add_table(rows=2, cols=6)
    for c, v in zip(t.rows[0].cells, ["Question", "A", "B", "C", "D", "Answer"]):
        c.text = v
    for c, v in zip(t.rows[1].cells, ["Capital of India?", "Delhi", "Mumbai", "Pune", "Agra", "Delhi"]):
        c.text = v
    b = io.BytesIO(); d.save(b)
    res, saved = _import("q.docx", b.getvalue())
    assert res["imported"] == 1 and saved[0]["correct_answer"] == 0


def test_csv_and_xlsx_columns():
    csv_data = "question,option a,option b,option c,answer,explanation,subject\nCapital?,Delhi,Mumbai,Pune,B,because,GK\n"
    res, saved = _import("q.csv", csv_data.encode())
    assert res["imported"] == 1 and saved[0]["correct_answer"] == 1
    assert saved[0]["explanation"] == "because" and saved[0]["category"] == "GK"
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Q", "A", "B", "C", "D", "Correct"]); ws.append(["2+2?", 3, 4, 5, 6, 2])
    b = io.BytesIO(); wb.save(b)
    res, saved = _import("q.xlsx", b.getvalue())
    assert res["imported"] == 1 and saved[0]["correct_answer"] == 1          # "2" = 2nd option


def test_json_shapes():
    data = {"questions": [
        {"question": "Capital?", "options": ["Delhi", "Mumbai"], "answer": "Delhi"},
        {"q": "2+2?", "a": "3", "b": "4", "correct_option_id": 1},                  # zero-based key
        {"question": "Sky?", "choices": [{"text": "Blue", "correct": True}, {"text": "Red"}]},
    ]}
    res, saved = _import("q.json", json.dumps(data).encode())
    assert res["imported"] == 3 and [q["correct_answer"] for q in saved] == [0, 1, 0]


def test_pptx_and_odt():
    def zipped(files):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for n, c in files.items():
                z.writestr(n, c)
        return b.getvalue()
    paras = "".join(f"<a:p><a:r><a:t>{l}</a:t></a:r></a:p>" for l in TXT.split("\n"))
    res, _ = _import("q.pptx", zipped({"ppt/slides/slide1.xml": f"<p:sld>{paras}</p:sld>"}))
    assert res["imported"] == 2
    odt = "".join(f"<text:p>{l}</text:p>" for l in TXT.split("\n"))
    res, _ = _import("q.odt", zipped({"content.xml": f"<office:document-content>{odt}</office:document-content>"}))
    assert res["imported"] == 2


@pytest.mark.parametrize("name,data,word", [
    ("pic.jpg", b"\xff\xd8\xff", "OCR"),
    ("old.doc", b"\xd0\xcf\x11\xe0", ".docx"),
    ("scan.pdf", _pdf([]), "scan"),
    ("x.bin", b"\x00\x01\x02" * 100, "Can't read"),
])
def test_unsupported_files_explain_why(name, data, word):
    items, text, note = extract(name, data)
    assert not items and not text and word in note


async def test_document_handler_imports_csv_end_to_end(monkeypatch):
    """Full path: Telegram document → download → extract → background import → report."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.bot.handlers_main import TelegramQuizBot
    qm = MagicMock(); qm.questions = []
    qm.add_questions.side_effect = lambda b, _existing=None: {"added": len(b), "rejected": {}, "errors": []}
    bot = TelegramQuizBot(qm, db_manager=MagicMock())
    bot._is_authorized = AsyncMock(return_value=True)
    bot._reply = AsyncMock(return_value=MagicMock()); bot._edit = AsyncMock()
    data = b"question,A,B,answer\nCapital?,Delhi,Mumbai,A\n"
    file_obj = MagicMock(); file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    doc = SimpleNamespace(file_name="q.csv", mime_type="text/csv", file_size=len(data), file_id="f",
                          get_file=AsyncMock(return_value=file_obj))
    ctx = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=file_obj)))
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1, first_name="A", last_name=None, username=None),
                          effective_message=SimpleNamespace(document=doc))
    await bot.handle_document(upd, ctx)
    for _ in range(50):
        if qm.add_questions.called:
            break
        await asyncio.sleep(0.02)
    assert qm.add_questions.call_args.args[0][0]["question"] == "Capital?"
