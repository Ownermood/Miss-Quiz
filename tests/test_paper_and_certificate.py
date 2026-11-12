"""Printable question paper (HTML) and Top-3 certificate image."""
import io

from PIL import Image

from src.bot.my_quiz import _build_printable_html
from src.utils.certificate import _clean_render_text, _fmt_time, generate_certificate_image

QS = {"id": "ab12", "title": "GK <b>Bank</b>", "secs": 30, "negative_rate": 0.25, "questions": [
    {"question": "<script>alert(1)</script> Capital?", "options": ["Delhi", "\"><img src=x>", "Pune"], "correct_answer": 0,
     "explanation": "Delhi & NCR"},
    {"question": "प्रश्न?", "options": ["क", "ख"], "correct_answer": 1},
]}


def test_paper_is_interactive_and_escaped():
    h = _build_printable_html(QS, False, "MissQuiz_Bot").decode()
    assert "<script>alert(1)" not in h and "<img src=x>" not in h and "GK &lt;b&gt;Bank" in h
    assert h.count('type="radio"') == 5 and 'name="q1"' in h and 'name="q2"' in h
    assert 'data-a="0"' in h and 'data-a="1"' in h and 'data-neg="0.25"' in h
    assert "Check my score" in h and "Answer key" in h and "Delhi &amp; NCR" in h
    assert h.count("<script>") == 1          # only our own script


def test_study_sheet_has_no_inputs_or_script():
    h = _build_printable_html(QS, True, "MissQuiz_Bot").decode()
    assert 'type="radio"' not in h and "<script>" not in h and 'class="show"' in h
    assert "Check my score" not in h and "Answer key" not in h


def test_paper_handles_bad_data():
    h = _build_printable_html({"questions": [{"question": None, "options": ["a", "b"], "correct_answer": "x"}],
                               "negative_rate": 0}, False).decode()
    assert "No negative marking" in h and 'data-neg="0"' in h


def test_certificate_name_cleaning():
    assert _clean_render_text("𝙺𝚞𝚕𝚊.𝚡𝚣") == "Kula.xz"
    assert _clean_render_text("𝑻𝒉𝒆 𝑻𝒓𝒂𝒄𝒚 🖤") == "The Tracy"
    assert _clean_render_text("ᴋᴜʟᴀ ꜱɪɴɢʜ") == "KULA SINGH"
    assert _clean_render_text("आदित्य") == "आदित्य"
    assert _clean_render_text("🔥🔥", "Quiz Champion") == "Quiz Champion"
    assert _clean_render_text("♡") == "♡"
    assert (_fmt_time(45), _fmt_time(1255), _fmt_time(7300)) == ("45s", "20:55", "2:01:40")


def test_certificate_renders_for_odd_input():
    for name, rank, cor, tot, pts in [("𝙺𝚞𝚕𝚊.𝚡𝚣", 1, 152, 200, 152), ("♡", 2, 0, 0, -1.25), ("आदित्य कुमार", 3, 9, 5, 9),
                                      ("", 7, 1, 1, 1), ("x" * 300, 1, 3, 4, 2.5)]:
        png = generate_certificate_image(name, "Q" * 200, "", "", rank, cor, tot, pts, 99999, "")
        im = Image.open(io.BytesIO(png))
        assert im.size == (1600, 1000) and im.format == "PNG"
