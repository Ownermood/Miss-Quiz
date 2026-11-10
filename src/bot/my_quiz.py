"""
Users' own quizzes ("Create Own Quiz" / /myquiz).

A user sends questions in the bot's DM — pasted text, any file the importer
reads, or forwarded quiz polls — taps Done, names it, and gets a private quiz
set (`quiz_sets` collection) to play, battle, share, study with flashcards,
export, or download the original Study Material file.
"""

import asyncio
import datetime
import html
import io
import logging
import random
import secrets
import urllib.parse
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.utils.cleaner import clean_text

logger = logging.getLogger(__name__)

MIN_Q         = 1                   # questions needed to save
MAX_Q_PER_SET = 5000                # safety only (MongoDB document size)
MAX_FILE      = 20 * 1024 * 1024    # Telegram: bots can't download files over 20 MB
PER_PAGE      = 8                   # quizzes per list page

HELP = (
    "<blockquote><b>How to add questions:</b>\n"
    "• 📄 <b>Send a File</b> — PDF, DOCX, TXT, CSV, XLSX or JSON\n"
    "• 📋 <b>Paste Text</b> — Questions with options (A–D) &amp; Answer\n"
    "• 🗳 <b>Forward Polls</b> — Forward any quiz polls here\n"
    "• 📚 <b>Study Material</b> — Uploaded PDF/DOCX is saved automatically</blockquote>\n"
    "<i>Send your file, text, or quiz polls below:</i>"
)
EXAMPLE = ("<blockquote>1. Capital of India?\nA) Mumbai\nB) Delhi\nC) Pune\nAnswer: B</blockquote>")


def _btn(text: str, data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


def _fmt_total_time(total_secs: int) -> str:
    if total_secs < 60:
        return f"{total_secs} sec"
    mins, rem = divmod(total_secs, 60)
    if rem == 0:
        return f"{mins} min"
    return f"{mins}m {rem}s"


def page_rows(pages: int, current: int, data) -> List[List[InlineKeyboardButton]]:
    if pages <= 1:
        return []
    start = max(1, min(current - 4, pages - 9))
    nums = list(range(start, min(pages, start + 9) + 1))
    btns = [_btn(f"·{n}·" if n == current else str(n), data(n), "success" if n == current else None)
            for n in nums]
    return [btns[k:k + 5] for k in range(0, len(btns), 5)]


async def load_page(db_call, count_call, owner: int, page: int):
    total = await count_call(owner)
    total = total if isinstance(total, int) else 0
    pages = max(1, -(-total // PER_PAGE))
    page = max(1, min(page, pages))
    sets = await db_call(owner, PER_PAGE, (page - 1) * PER_PAGE)
    return (sets if isinstance(sets, list) else []), page, pages


def parse_questions(text: str, items: Optional[List[Dict]] = None,
                    custom_words: Optional[List[str]] = None) -> Tuple[List[Dict], List[Dict]]:
    """Text and/or structured rows → valid poll questions + problems (why skipped)."""
    from src.bot.quiz_parser import SmartQuizParser, items_to_questions, _clamp_option
    parser = SmartQuizParser()
    raw = parser.parse(text) if (text or "").strip() else []
    problems = list(getattr(parser, "problems", []) or [])
    if items:
        more, more_problems = items_to_questions(items)
        raw += more
        problems += more_problems
    out = []
    for it in raw:
        raw_q = " ".join(str(it.get("question") or "").split())
        q = clean_text(raw_q, custom_words, is_option=False)
        opts = [clean_text(str(o).strip(), custom_words, is_option=True) for o in it.get("options") or []]
        c = it.get("correct_answer")
        where = f"“{q[:30]}”"
        if len(q) < 3:
            problems.append({"where": where, "reason": "question too short"}); continue
        if not 2 <= len(opts) <= 10 or not all(opts):
            problems.append({"where": where, "reason": "needs 2–10 options"}); continue
        if not isinstance(c, int) or not 0 <= c < len(opts):
            problems.append({"where": where, "reason": "answer missing or doesn't match an option"}); continue
        d = {"question": q[:280], "options": [_clamp_option(o) for o in opts], "correct_answer": c}
        if it.get("explanation"):
            d["explanation"] = clean_text(str(it["explanation"]).strip(), custom_words, is_option=True)[:190]
        out.append(d)
    return out, problems


def _build_printable_html(qs: Dict, include_answers_inline: bool = False, bot_username: str = "") -> bytes:
    """Self-contained, print-ready HTML exam paper (or solved study sheet).

    Paper mode: clean exam layout, answers hidden behind a "Show answers" toggle,
    answer key on its own printed page. Study mode: answers + explanations inline.
    Prints to A4 in two columns (Save as PDF from any browser).
    """
    esc = lambda v: html.escape(str(v if v is not None else ""))
    title = esc(qs.get("title") or "Quiz")
    questions = qs.get("questions") or []
    n = len(questions)
    secs = int(qs.get("secs") or 20)
    neg_raw = qs.get("negative_rate")
    neg = float(neg_raw) if neg_raw is not None else 0.25
    neg_txt = f"−{neg:g}" if neg > 0 else "None"
    total_min = max(1, round(n * secs / 60))
    date_str = datetime.datetime.utcnow().strftime("%d %b %Y")
    mode_label = "Solved Study Sheet" if include_answers_inline else "Question Paper"
    brand = f"@{esc(bot_username)}" if bot_username else "Miss Quiz"

    items, keys = [], []
    for i, q in enumerate(questions, 1):
        opts = [str(o) for o in (q.get("options") or [])]
        try:
            cor = int(q.get("correct_answer", 0))
        except (TypeError, ValueError):
            cor = 0
        letter = chr(97 + cor) if 0 <= cor < 26 else "?"
        one_col = any(len(o) > 34 for o in opts)
        lis = "".join(
            f"<li class=\"opt{' ok' if k == cor else ''}\"><span class=\"bul\">{chr(97 + k)}</span>"
            f"<span class=\"ot\">{esc(o)}</span></li>"
            for k, o in enumerate(opts)
        )
        exp = str(q.get("explanation") or "").strip()
        ans_txt = esc(opts[cor]) if 0 <= cor < len(opts) else ""
        sol = (f"<div class=\"sol\"><b>Answer ({letter})</b> {ans_txt}"
               + (f"<div class=\"why\">{esc(exp)}</div>" if exp else "") + "</div>")
        items.append(
            f"<section class=\"q\"><div class=\"qh\"><span class=\"qn\">{i}.</span>"
            f"<p>{esc(q.get('question'))}</p></div>"
            f"<ol class=\"opts{' one' if one_col else ''}\">{lis}</ol>{sol}</section>"
        )
        keys.append(f"<div class=\"k\"><span>{i}</span><b>{letter}</b></div>")

    neg_line = (f"<li>Each correct answer: <b>+1</b> · each wrong answer: <b>{neg_txt}</b>.</li>"
                if neg > 0 else "<li>Each correct answer: <b>+1</b> · no negative marking.</li>")
    candidate = "" if include_answers_inline else (
        "<div class=\"cand\"><div>Name <i></i></div><div>Roll No. <i></i></div>"
        f"<div>Score <i class=\"s\"></i> / {n}</div></div>"
    )
    key_page = "" if include_answers_inline else (
        f"<section class=\"key\"><h2>Answer Key</h2><div class=\"kg\">{''.join(keys)}</div></section>"
    )
    toggle = "" if include_answers_inline else (
        "<button class=\"ghost\" onclick=\"document.body.classList.toggle('show');"
        "this.textContent=document.body.classList.contains('show')?'Hide answers':'Show answers'\">"
        "Show answers</button>"
    )

    doc_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · {mode_label}</title>
<style>
  :root {{ --ink:#0f172a; --mute:#64748b; --line:#e2e8f0; --acc:#4338ca; --ok:#15803d; --okbg:#f0fdf4; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#f1f5f9; color:var(--ink);
         font:15px/1.55 "Segoe UI", Inter, Roboto, "Noto Sans", "Noto Sans Devanagari", system-ui, sans-serif; }}
  .bar {{ position:sticky; top:0; z-index:5; display:flex; gap:8px; justify-content:flex-end;
         padding:10px 16px; background:rgba(241,245,249,.92); backdrop-filter:blur(6px); }}
  .bar button {{ font:inherit; font-weight:600; border-radius:999px; padding:8px 18px; cursor:pointer; border:0; }}
  .solid {{ background:var(--acc); color:#fff; }}
  .ghost {{ background:#fff; color:var(--acc); border:1px solid #c7d2fe !important; }}
  .paper {{ max-width:840px; margin:0 auto 40px; background:#fff; padding:40px 44px;
           border-radius:14px; box-shadow:0 1px 3px rgba(15,23,42,.06), 0 12px 32px rgba(15,23,42,.06); }}
  header {{ text-align:center; padding-bottom:18px; border-bottom:2px solid var(--ink); }}
  .brand {{ font-size:11px; letter-spacing:.28em; text-transform:uppercase; color:var(--acc); font-weight:700; }}
  h1 {{ margin:6px 0 2px; font-size:26px; line-height:1.25; }}
  .sub {{ color:var(--mute); font-size:13px; }}
  .meta {{ display:grid; grid-template-columns:repeat(4,1fr); margin-top:16px;
          border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  .meta div {{ padding:8px 4px; border-right:1px solid var(--line); }}
  .meta div:last-child {{ border-right:0; }}
  .meta small {{ display:block; font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--mute); }}
  .meta b {{ font-size:16px; }}
  .cand {{ display:grid; grid-template-columns:2fr 1.2fr 1fr; gap:18px; margin:18px 0 4px; font-size:13px; color:var(--mute); }}
  .cand div {{ display:flex; align-items:flex-end; gap:8px; white-space:nowrap; }}
  .cand i {{ flex:1; border-bottom:1px solid #94a3b8; height:1.1em; }}
  .cand i.s {{ flex:0 0 42px; }}
  .inst {{ margin:16px 0 22px; padding:10px 16px; background:#f8fafc; border-left:3px solid var(--acc);
          border-radius:0 8px 8px 0; font-size:13px; color:#334155; }}
  .inst ul {{ margin:4px 0 0; padding-left:18px; }}
  .q {{ break-inside:avoid; page-break-inside:avoid; padding:12px 0 14px; border-bottom:1px dashed var(--line); }}
  .q:last-of-type {{ border-bottom:0; }}
  .qh {{ display:flex; gap:8px; }}
  .qn {{ font-weight:700; min-width:28px; color:var(--acc); }}
  .qh p {{ margin:0; font-weight:600; }}
  .opts {{ list-style:none; margin:8px 0 0 36px; padding:0; display:grid; grid-template-columns:1fr 1fr; gap:4px 18px; }}
  .opts.one {{ grid-template-columns:1fr; }}
  .opt {{ display:flex; gap:8px; align-items:flex-start; padding:3px 8px; border-radius:6px; }}
  .bul {{ flex:0 0 22px; height:22px; border:1px solid #cbd5e1; border-radius:50%; display:inline-flex;
         align-items:center; justify-content:center; font-size:12px; font-weight:600; color:#475569; }}
  .sol {{ display:none; margin:8px 0 0 36px; padding:8px 12px; background:var(--okbg); border-radius:8px;
         color:var(--ok); font-size:13.5px; }}
  .why {{ color:#334155; margin-top:3px; }}
  .show .sol {{ display:block; }}
  .show .opt.ok {{ background:var(--okbg); color:var(--ok); font-weight:600; }}
  .show .opt.ok .bul {{ background:var(--ok); border-color:var(--ok); color:#fff; }}
  .key {{ margin-top:28px; padding-top:18px; border-top:2px solid var(--ink); break-before:page; page-break-before:always; }}
  .key h2 {{ margin:0 0 12px; font-size:18px; text-align:center; letter-spacing:.06em; text-transform:uppercase; }}
  .kg {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(64px,1fr)); gap:6px; }}
  .k {{ display:flex; justify-content:space-between; padding:5px 10px; border:1px solid var(--line); border-radius:6px; font-size:13px; }}
  .k span {{ color:var(--mute); }}
  .k b {{ color:var(--ok); text-transform:uppercase; }}
  footer {{ margin-top:26px; text-align:center; font-size:11px; color:var(--mute); }}
  @media (max-width:640px) {{
    .paper {{ padding:24px 18px; border-radius:0; }}
    h1 {{ font-size:21px; }}
    .meta {{ grid-template-columns:repeat(2,1fr); }}
    .meta div:nth-child(2) {{ border-right:0; }}
    .meta div:nth-child(-n+2) {{ border-bottom:1px solid var(--line); }}
    .cand {{ grid-template-columns:1fr; gap:10px; }}
    .opts {{ grid-template-columns:1fr; margin-left:28px; }}
    .sol {{ margin-left:28px; }}
  }}
  @page {{ size:A4; margin:12mm 11mm; }}
  @media print {{
    body {{ background:#fff; font-size:11.5pt; }}
    .bar {{ display:none; }}
    .paper {{ max-width:none; margin:0; padding:0; box-shadow:none; border-radius:0; }}
    main {{ column-count:2; column-gap:9mm; column-rule:1px solid var(--line); }}
    .opts {{ grid-template-columns:1fr; margin-left:26px; }}
    .sol {{ margin-left:26px; }}
    * {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  }}
</style>
</head>
<body class="{'show' if include_answers_inline else ''}">
<div class="bar">{toggle}<button class="solid" onclick="window.print()">Save as PDF</button></div>
<div class="paper">
  <header>
    <div class="brand">{brand}</div>
    <h1>{title}</h1>
    <div class="sub">{mode_label} · {date_str}</div>
    <div class="meta">
      <div><small>Questions</small><b>{n}</b></div>
      <div><small>Time</small><b>{total_min} min</b></div>
      <div><small>Max marks</small><b>{n}</b></div>
      <div><small>Negative</small><b>{neg_txt}</b></div>
    </div>
  </header>
  {candidate}
  <div class="inst"><b>Instructions</b><ul>
    <li>Choose the single best option for each question.</li>
    {neg_line}
    {"" if include_answers_inline else "<li>The answer key is on the last page.</li>"}
  </ul></div>
  <main>{''.join(items)}</main>
  {key_page}
  <footer>Made with {brand} · {n} questions</footer>
</div>
</body>
</html>"""
    return doc_html.encode("utf-8")


class MyQuizMixin:

    # ── state ────────────────────────────────────────────────────────────

    @staticmethod
    def _mq_draft(context) -> Optional[Dict]:
        ud = getattr(context, "user_data", None)
        return ud.get("mq_draft") if ud else None

    async def _mq_db(self, fn: str, *args, default=None):
        if not self.db or not hasattr(self.db, fn):
            return default
        try:
            return await asyncio.to_thread(getattr(self.db, fn), *args)
        except Exception as e:
            logger.warning(f"[MYQUIZ] db {fn} failed: {e}")
            return default

    # ── views ────────────────────────────────────────────────────────────

    @staticmethod
    def _mq_collect_view(d: Dict) -> Tuple[str, InlineKeyboardMarkup]:
        q_count = len(d["qs"])
        f_count = len(d.get("files") or [])
        skipped = len(d.get("problems") or [])
        if q_count > 0 or skipped > 0 or d.get("fname"):
            status_lines = [f"✅ Questions ready › <b>{q_count} Qs</b>"]
            if skipped:
                status_lines.append(f"⚠️ Skipped › <b>{skipped}</b> (missing answer/options)")
            if d.get("fname"):
                status_lines.append(f"📎 Study file › <b>{html.escape(d['fname'])}</b> ({f_count} file)")
            body = (
                "✏️ <b>CREATE NEW QUIZ</b>\n"
                "<blockquote>" + "\n".join(status_lines) + "</blockquote>\n"
            )
        else:
            body = "✏️ <b>CREATE NEW QUIZ</b>\n"

        if d.get("note"):
            body += f"💡 <i>{html.escape(d['note'])}</i>\n\n"
        else:
            body += HELP + "\n\n"

        rows: List[List[InlineKeyboardButton]] = []
        if q_count >= MIN_Q:
            if d.get("fname"):
                rows.append([_btn(f"⚡ Quick Save “{d['fname'][:22]}” ({q_count} Qs)", "mq_qsave", "success")])
                rows.append([_btn("✏️ Custom Name & Save", "mq_done", "primary")])
            else:
                rows.append([_btn(f"✅ Save Quiz ({q_count} Qs)", "mq_done", "success")])
            rows.append([_btn("📚 My Quizzes", "mq_list", None), _btn("❌ Cancel", "mq_cancel", "danger")])
        else:
            rows.append([_btn("📚 My Quizzes", "mq_list", None), _btn("❌ Cancel", "mq_cancel", "danger")])
            rows.append([_btn("🏠 Home", "nav_home", "primary")])
        return body, InlineKeyboardMarkup(rows)

    @staticmethod
    def _mq_list_view(sets: List[Dict], page: int, pages: int, total: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        from src.bot.battle import _tlabel
        head = (f"📚 <b>YOUR QUIZZES</b>" + (f" · Page {page}/{pages}" if pages > 1 else "") + f"  ·  Total: <b>{total or len(sets)}</b>\n")
        if not sets:
            body = head + "\n<i>You haven't created any quizzes yet. Tap below to build your first quiz!</i>"
            return body, InlineKeyboardMarkup([[_btn("➕ Create New Quiz", "mq_new", "success")],
                                               [_btn("🏠 Home", "nav_home", "primary")]])
        lines = []
        for i, s in enumerate(sets, 1 + (page - 1) * PER_PAGE):
            q_cnt = s.get("count") or (len(s["questions"]) if "questions" in s else 0)
            t_lbl = _tlabel(int(s.get("secs") or 20))
            plays = int(s.get("plays") or 0)
            has_file = " · 📎" if s.get("source_files") else ""
            lines.append(
                f"{i}. <b>{html.escape(s.get('title', 'Quiz')[:28])}</b>  ›  {q_cnt}Q · {t_lbl} · {plays} plays{has_file}"
            )
        body = (
            head +
            "<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            "<i>Select a quiz below to play, edit, battle, study, or share:</i>"
        )
        item_rows = []
        for s in sets:
            q_cnt = s.get("count") or 0
            item_rows.append([_btn(f"📝 {s.get('title', 'Quiz')[:26]} · {q_cnt}Q", f"mq_open:{s['id']}", None)])
        nav_rows = page_rows(pages, page, lambda p: f"mq_list:{p}")
        foot = [_btn("➕ Create New", "mq_new", "success"), _btn("🏠 Home", "nav_home", "primary")]
        return body, InlineKeyboardMarkup(item_rows + nav_rows + [foot])

    async def _mq_page(self, owner: int, page: int = 1):
        call = lambda fn: (lambda *a: self._mq_db(fn, *a))
        return await load_page(call("list_quiz_sets"), call("count_quiz_sets"), owner, page)

    @staticmethod
    def _mq_card_view(qs: Dict, saved: bool = False, bot_username: str = "", is_owner: bool = True):
        sid = str(qs.get("id", ""))
        title = html.escape(str(qs.get("title", "Quiz")))
        q_count = int(qs.get("count") or len(qs.get("questions") or []))
        secs = int(qs.get("secs") or 20)
        total_time = _fmt_total_time(q_count * secs)
        shuffle_str = "ON" if qs.get("shuffle") else "OFF"
        neg_val = float(qs.get("negative_rate") if qs.get("negative_rate") is not None else 0.25)
        neg_str = f"-{neg_val} marks" if neg_val > 0 else "No Negative"
        attempts = int(qs.get("plays") or 0)
        files_cnt = len(qs.get("source_files") or [])
        share_link = f"https://t.me/{bot_username}?start=shared_{sid}" if bot_username else f"Code: {sid}"

        text = (
            ("🎉 <b>Saved! Quiz Published &amp; Ready!</b>\n\n" if saved else "") +
            f"🎓 <b>{title}</b>\n"
            f"<blockquote>"
            f"• <b>Questions</b>  ›  {q_count} Qs  ({total_time})\n"
            f"• <b>Timer</b>  ›  {secs} sec per question\n"
            f"• <b>Marking</b>  ›  +1.0  /  {neg_str}\n"
            f"• <b>Shuffle</b>  ›  {shuffle_str}  ·  <b>Plays</b>  ›  {attempts}\n"
            f"• <b>Study Files</b>  ›  {files_cnt} attached\n"
            f"• <b>Quiz Code</b>  ›  <code>{html.escape(sid)}</code>"
            f"</blockquote>\n"
            f"🔗 <b>Share Link:</b>\n{share_link}\n\n"
            f"<i>Choose a mode below to launch, study, edit, or share:</i>"
        )

        rows: List[List[InlineKeyboardButton]] = []

        # Row 1: Primary Launch Modes (Green CTA)
        if bot_username:
            rows.append([
                _btn("▶️ Play Solo (DM)", f"mq_prac:{sid}", "success"),
                InlineKeyboardButton("👥 Host in Group", style="success",
                                     url=f"https://t.me/{bot_username}?startgroup=gq_{sid}"),
            ])
        else:
            rows.append([_btn("▶️ Play Solo (DM)", f"mq_prac:{sid}", "success")])

        # Row 2: 1v1 Battle & Interactive Flashcards (Clean Dark Glass)
        rows.append([
            _btn("⚔️ 1v1 Live Battle", f"mq_bt:{sid}", None),
            _btn("🎴 Study Flashcards", f"mq_flash:{sid}:0:0", None),
        ])

        # Row 3: Real Study Materials File & Printable Question Paper (Clean Dark Glass)
        sm_label = f"📚 Study Material ({files_cnt})" if files_cnt else "📚 Study Material"
        rows.append([
            _btn(sm_label, f"mq_mat:{sid}", None),
            _btn("📄 Question Paper", f"mq_pdf:{sid}", None),
        ])

        # Row 4: Edit (owner) & Stats (Clean Dark Glass)
        if is_owner:
            rows.append([
                _btn("✏️ Edit Quiz", f"mq_edit:{sid}", None),
                _btn("📊 Quiz Stats", f"mq_stats:{sid}", None),
            ])
        else:
            rows.append([_btn("📊 Quiz Stats", f"mq_stats:{sid}", None)])

        # Row 5: Channel/Group & Share Link (Clean Dark Glass)
        if bot_username:
            share_text = urllib.parse.quote(f"🎯 Challenge yourself on \"{qs.get('title', 'Quiz')}\" ({q_count} Questions) on Miss Quiz Bot!")
            share_url = f"https://t.me/share/url?url={urllib.parse.quote(share_link)}&text={share_text}"
            rows.append([
                _btn("📢 Channel / Group", f"mq_ch:{sid}", None),
                InlineKeyboardButton("📩 Share Quiz", url=share_url, style=None),
            ])
        else:
            rows.append([_btn("📢 Channel / Group", f"mq_ch:{sid}", None)])

        # Row 6: Navigation (Red Back + Blue Home)
        rows.append([_btn("⬅️ My Quizzes", "mq_list", "danger"), _btn("🏠 Home", "nav_home", "primary")])

        return text, InlineKeyboardMarkup(rows)

    # ── entry points ─────────────────────────────────────────────────────

    async def cmd_myquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat and chat.type != "private":
            link = f"https://t.me/{context.bot.username}?start=mq"
            await self._reply(update, "📚 <b>Your quizzes live in my DM.</b>",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                  "📚 Open My Quizzes", url=link, style="primary")]]))
            return
        await self.mq_open_list(update, context)

    async def mq_open_shared(self, update: Update, context: ContextTypes.DEFAULT_TYPE, sid: str):
        """Open a shared quiz via /start shared_<sid> for any user."""
        user = update.effective_user
        qs = await self._mq_db("get_quiz_set", sid.strip())
        if not isinstance(qs, dict):
            await self._reply(update, "❌ <b>This quiz was not found or has been deleted by its creator.</b>")
            return
        is_owner = bool(user and qs.get("owner") == user.id)
        text, kb = self._mq_card_view(qs, bot_username=context.bot.username, is_owner=is_owner)
        await self._reply(update, text, reply_markup=kb)

    async def mq_open_list(self, update: Update, context, query=None, page: int = 1):
        user = update.effective_user
        sets, cur_page, pages = await self._mq_page(user.id, page)
        total = await self._mq_db("count_quiz_sets", user.id, default=len(sets))
        text, kb = self._mq_list_view(sets, cur_page, pages, total=total if isinstance(total, int) else len(sets))
        if query:
            await self._bt_edit(query, text, kb)
        else:
            await self._reply(update, text, reply_markup=kb)

    async def mq_start_draft(self, query, context, user, update: Optional[Update] = None):
        d = {"qs": [], "seen": set(), "problems": [], "note": "", "stage": "collect", "fname": "", "files": []}
        text, kb = self._mq_collect_view(d)
        if query:
            sent = await self._bt_edit(query, text, kb)
            msg_id = getattr(sent, "message_id", None) if isinstance(getattr(sent, "message_id", None), int) else query.message.message_id
            d["chat"], d["msg"] = query.message.chat.id, msg_id
        else:
            sent = await self._reply(update, text, reply_markup=kb)
            d["chat"], d["msg"] = update.effective_chat.id, getattr(sent, "message_id", None)
        context.user_data["mq_draft"] = d
        context.user_data.pop("bt_await", None)

    async def _mq_show(self, context, d: Dict, text: str, kb) -> None:
        old = d.get("msg")
        try:
            sent = await context.bot.send_message(d["chat"], text, parse_mode=ParseMode.HTML, reply_markup=kb)
            d["msg"] = sent.message_id
        except Exception as e:
            logger.debug(f"[MYQUIZ] panel send failed: {e}")
            return
        if old:
            try:
                await context.bot.delete_message(d["chat"], old)
            except Exception:
                pass

    # ── input: text / file / forwarded poll ──────────────────────────────

    async def handle_myquiz_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg, user = update.effective_message, update.effective_user
        if not msg or not user:
            return

        # 1. Check if attaching a Study Material file to an existing quiz
        attaching_id = context.user_data.get("mq_attaching_file")
        if attaching_id and (msg.document or msg.photo):
            qs = await self._mq_db("get_quiz_set", attaching_id)
            if isinstance(qs, dict) and qs.get("owner") == user.id:
                context.user_data.pop("mq_attaching_file", None)
                if msg.document:
                    f_entry = {
                        "file_id": msg.document.file_id,
                        "file_name": msg.document.file_name or "Study_Material.pdf",
                        "kind": "document",
                    }
                else:
                    f_entry = {
                        "file_id": msg.photo[-1].file_id,
                        "file_name": "Study_Material_Image.jpg",
                        "kind": "photo",
                    }
                s_files = list(qs.get("source_files") or [])
                s_files.append(f_entry)
                if self.db and hasattr(self.db, "update_quiz_set"):
                    await asyncio.to_thread(self.db.update_quiz_set, attaching_id, user.id, {"source_files": s_files})
                qs["source_files"] = s_files
                e_text, e_kb = self._mq_editor_view(qs, f"✅ Study file attached: {f_entry['file_name']}")
                await self._reply(update, e_text, reply_markup=e_kb)
                raise ApplicationHandlerStop

        # 2. Check if renaming a quiz
        renaming_id = context.user_data.get("mq_renaming")
        if renaming_id and msg.text:
            new_title = " ".join(msg.text.strip().split())[:60]
            if new_title:
                context.user_data.pop("mq_renaming", None)
                if self.db and hasattr(self.db, "update_quiz_set"):
                    await asyncio.to_thread(self.db.update_quiz_set, renaming_id, user.id, {"title": new_title})
                qs = await self._mq_db("get_quiz_set", renaming_id)
                if qs:
                    e_text, e_kb = self._mq_editor_view(qs, f"✅ Renamed to “{new_title}”")
                    await self._reply(update, e_text, reply_markup=e_kb)
                raise ApplicationHandlerStop

        # 3. Replacing one question (Edit Questions → ✏️ Rewrite)
        editing = context.user_data.get("mq_edit_q")
        if editing:
            await self._mq_take_edit_q(update, context, user, msg, editing)
            raise ApplicationHandlerStop

        # 4. Adding more questions to a saved quiz
        adding_id = context.user_data.get("mq_adding")
        if adding_id:
            await self._mq_take_add(update, context, user, msg, adding_id)
            raise ApplicationHandlerStop

        d = self._mq_draft(context)
        if not d:
            return
        lock = d.setdefault("lock", asyncio.Lock())
        async with lock:
            if self._mq_draft(context) is d:
                await self._mq_take(context, user, d, msg)
        raise ApplicationHandlerStop

    async def _mq_take(self, context, user, d: Dict, msg) -> None:
        if d["stage"] == "name":
            plain = (msg.text or "").strip()
            if plain and "\n" not in plain and not msg.document and not msg.poll and not msg.photo:
                await self._mq_save(context, user, d, " ".join(plain.split())[:60])
                return
            d["stage"] = "collect"

        wait_msg = None
        if msg.document or msg.photo:
            try:
                wait_msg = await context.bot.send_message(
                    d["chat"], "⏳ <i>Reading file &amp; extracting questions…</i>", parse_mode=ParseMode.HTML
                )
            except Exception:
                wait_msg = None
        try:
            qs, probs, note, f_entry = await self._mq_read_msg(context, user, msg)
        finally:
            if wait_msg:
                try:
                    await context.bot.delete_message(d["chat"], wait_msg.message_id)
                except Exception:
                    pass
        if f_entry:
            if f_entry["file_id"] not in {x.get("file_id") for x in d.get("files", [])}:
                d.setdefault("files", []).append(f_entry)
            if not d.get("fname") and msg.document and f_entry["kind"] == "document":
                d["fname"] = f_entry["file_name"].rsplit(".", 1)[0].replace("_", " ")[:40]
        added = 0
        for q in qs:
            key = q["question"].lower()
            if key in d["seen"]:
                continue
            if len(d["qs"]) >= MAX_Q_PER_SET:
                note = f"A quiz can have up to {MAX_Q_PER_SET} questions — the rest were skipped."
                break
            d["seen"].add(key)
            d["qs"].append(q)
            added += 1
        d["problems"] = (d["problems"] + probs)[-50:]
        if not added and not note:
            note = ("No new questions in that (already added)." if qs else
                    "No questions found in that. Use a layout like the example below.")
        d["note"] = (f"Cleaned & Added: +{added}. " if added and (msg.document or msg.photo or msg.poll) else "") + note
        text_out, kb = self._mq_collect_view(d)
        if note.startswith("No questions found"):
            text_out += "\n" + EXAMPLE
        await self._mq_show(context, d, text_out, kb)

    # ── shared message reader (draft / add-more / rewrite) ───────────────

    async def _mq_read_msg(self, context, user, msg):
        """Text / file / forwarded quiz poll → (questions, problems, note, file_entry|None)."""
        items, text, note, f_entry = [], "", "", None
        if msg.poll:
            p = msg.poll
            if p.type == "quiz" and p.correct_option_id is not None:
                items = [{"question": p.question, "options": [o.text for o in p.options],
                          "correct_answer": p.correct_option_id, "explanation": p.explanation or ""}]
            else:
                note = "Only quiz polls with a visible answer can be added."
        elif msg.document or msg.photo:
            doc = msg.document or msg.photo[-1]
            fname = getattr(doc, "file_name", None) or "photo.jpg"
            # Real Telegram file_id so "📚 Study Material" can send the exact original file
            f_entry = {"file_id": doc.file_id, "file_name": fname,
                       "kind": "document" if msg.document else "photo"}
            if doc.file_size and doc.file_size > MAX_FILE:
                note = "File is too big (over 20 MB) — saved in 📚 Study Material, send a smaller text/PDF for questions."
            else:
                try:
                    from src.bot.file_extract import extract
                    f = await context.bot.get_file(doc.file_id, read_timeout=60)
                    data = bytes(await f.download_as_bytearray(read_timeout=60))
                    items, text, note = await asyncio.to_thread(extract, fname, data)
                except Exception as e:
                    logger.warning(f"[MYQUIZ] file read failed: {e}")
                    note = "Couldn't download that file. Please send it again."
            if msg.caption and not (text or items):
                text = msg.caption
        elif msg.text:
            text = msg.text
        else:
            note = "Send text, a file, or forward a quiz poll."

        user_words = []
        if self.db and hasattr(self.db, "get_user_custom_words"):
            try:
                user_words = await asyncio.to_thread(self.db.get_user_custom_words, user.id) or []
            except Exception:
                user_words = []
        qs, probs = (await asyncio.to_thread(parse_questions, text, items, user_words)) if (text or items) else ([], [])
        return qs, probs, note or "", f_entry

    @staticmethod
    def _mq_clear_modes(context) -> None:
        ud = getattr(context, "user_data", None)
        if ud is None:
            return
        for k in ("mq_renaming", "mq_attaching_file", "mq_prac", "mq_edit_q", "mq_adding"):
            ud.pop(k, None)

    async def _mq_set_questions(self, sid: str, owner: int, questions: List[Dict]) -> None:
        await self._mq_db("update_quiz_set", sid, owner, {"questions": questions, "count": len(questions)})

    # ── editor views ─────────────────────────────────────────────────────

    @staticmethod
    def _mq_editor_view(qs: Dict, note: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        sid = str(qs.get("id", ""))
        q_count = int(qs.get("count") or len(qs.get("questions") or []))
        files_cnt = len(qs.get("source_files") or [])
        shuffle_on = bool(qs.get("shuffle"))
        text = (
            (f"{html.escape(note)}\n\n" if note else "") +
            f"✏️  <b>EDIT QUIZ STUDIO</b>\n"
            f"<blockquote><b>📝 {html.escape(str(qs.get('title', 'Quiz')))}\n"
            f"• Questions  ›  {q_count} Qs\n"
            f"• Timer      ›  {qs.get('secs', 20)}s / Q\n"
            f"• Shuffle    ›  {'ON' if shuffle_on else 'OFF'}\n"
            f"• Files      ›  {files_cnt} Attached</b></blockquote>\n"
            f"<i>Select an option below to modify questions or settings:</i>"
        )
        kb = InlineKeyboardMarkup([
            [_btn("📝 Edit Questions", f"mq_ql:{sid}:1", "success"),
             _btn("➕ Add Questions", f"mq_add:{sid}", "success")],
            [_btn("🏷 Rename", f"mq_rename:{sid}", None),
             _btn("⏱ Timer", f"mq_timer:{sid}", None)],
            [_btn("🔀 Shuffle: ON" if shuffle_on else "🔀 Shuffle: OFF", f"mq_shuffle:{sid}",
                  "success" if shuffle_on else None),
             _btn("📎 Attach File", f"mq_attach:{sid}", None)],
            [_btn("🗑 Delete Quiz", f"mq_del:{sid}", "danger")],
            [_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger"),
             _btn("🏠 Home", "nav_home", "primary")],
        ])
        return text, kb

    @staticmethod
    def _mq_qlist_view(qs: Dict, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
        sid = str(qs.get("id", ""))
        questions = qs.get("questions") or []
        per = 8
        pages = max(1, -(-len(questions) // per))
        page = max(1, min(page, pages))
        start = (page - 1) * per
        rows = []
        for i, q in enumerate(questions[start:start + per], start):
            label = " ".join(str(q.get("question") or "").split())
            label = (label[:38] + "…") if len(label) > 39 else label
            rows.append([_btn(f"{i + 1}. {label}", f"mq_qv:{sid}:{i}", None)])
        rows += page_rows(pages, page, lambda p: f"mq_ql:{sid}:{p}")
        rows.append([_btn("➕ Add Questions", f"mq_add:{sid}", "success"),
                     _btn("⬅️ Back", f"mq_edit:{sid}", "danger")])
        text = (
            f"📝  <b>EDIT QUESTIONS</b>" + (f"  ·  Page {page}/{pages}" if pages > 1 else "") + "\n"
            f"<blockquote><b>{html.escape(str(qs.get('title', 'Quiz')))}  ·  {len(questions)} Questions</b></blockquote>\n"
            f"<i>Tap a question below to edit its text, options, answer, or delete it:</i>"
        )
        return text, InlineKeyboardMarkup(rows)

    @staticmethod
    def _mq_qview(qs: Dict, idx: int, note: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        sid = str(qs.get("id", ""))
        questions = qs.get("questions") or []
        total = len(questions)
        idx = max(0, min(idx, total - 1))
        q = questions[idx]
        opts = [str(o) for o in (q.get("options") or [])]
        cor = int(q.get("correct_answer", 0))
        lines = [(f"✅ <b>{chr(65 + k)}) {html.escape(o)}</b>" if k == cor else f"▫️ {chr(65 + k)}) {html.escape(o)}")
                 for k, o in enumerate(opts)]
        exp = str(q.get("explanation") or "").strip()
        text = (
            (f"{html.escape(note)}\n\n" if note else "") +
            f"📝  <b>QUESTION {idx + 1} of {total}</b>\n"
            f"<blockquote><b>{html.escape(str(q.get('question') or ''))}</b></blockquote>\n"
            + "\n".join(lines)
            + (f"\n\n💡 <i>{html.escape(exp)}</i>" if exp else "")
        )
        nav = []
        if idx > 0:
            nav.append(_btn("⬅️ Prev", f"mq_qv:{sid}:{idx - 1}", None))
        if idx < total - 1:
            nav.append(_btn("Next ➡️", f"mq_qv:{sid}:{idx + 1}", None))
        rows = [nav] if nav else []
        rows += [
            [_btn("✏️ Rewrite", f"mq_qe:{sid}:{idx}", "success"),
             _btn("🔑 Change Answer", f"mq_qa:{sid}:{idx}", None)],
            [_btn("🗑 Delete Question", f"mq_qd:{sid}:{idx}", "danger")],
            [_btn("📋 All Questions", f"mq_ql:{sid}:{idx // 8 + 1}", None),
             _btn("⬅️ Back", f"mq_edit:{sid}", "danger")],
        ]
        return text, InlineKeyboardMarkup(rows)

    # ── editor text input ────────────────────────────────────────────────

    async def _mq_take_edit_q(self, update, context, user, msg, editing: Dict) -> None:
        sid, idx = editing.get("sid"), int(editing.get("idx", 0))
        qs = await self._mq_db("get_quiz_set", sid)
        if not isinstance(qs, dict) or qs.get("owner") != user.id:
            context.user_data.pop("mq_edit_q", None)
            return
        questions = list(qs.get("questions") or [])
        if not 0 <= idx < len(questions):
            context.user_data.pop("mq_edit_q", None)
            return
        parsed, _, note, _ = await self._mq_read_msg(context, user, msg)
        if not parsed:
            back = InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_qv:{sid}:{idx}", "danger")]])
            await self._reply(update,
                              "⚠️ <b>Couldn't read that question.</b>\n"
                              "<i>Send it with options and the answer, like:</i>\n" + EXAMPLE,
                              reply_markup=back)
            return
        context.user_data.pop("mq_edit_q", None)
        questions[idx] = parsed[0]
        await self._mq_set_questions(sid, user.id, questions)
        qs["questions"], qs["count"] = questions, len(questions)
        text, kb = self._mq_qview(qs, idx, f"✅ Question {idx + 1} updated")
        await self._reply(update, text, reply_markup=kb)

    async def _mq_take_add(self, update, context, user, msg, sid: str) -> None:
        qs = await self._mq_db("get_quiz_set", sid)
        if not isinstance(qs, dict) or qs.get("owner") != user.id:
            context.user_data.pop("mq_adding", None)
            return
        parsed, probs, note, f_entry = await self._mq_read_msg(context, user, msg)
        questions = list(qs.get("questions") or [])
        seen = {str(q.get("question") or "").lower() for q in questions}
        added = 0
        for q in parsed:
            key = q["question"].lower()
            if key in seen or len(questions) >= MAX_Q_PER_SET:
                continue
            seen.add(key)
            questions.append(q)
            added += 1
        updates = {}
        if added:
            updates.update({"questions": questions, "count": len(questions)})
        if f_entry:
            files = list(qs.get("source_files") or [])
            if f_entry["file_id"] not in {x.get("file_id") for x in files}:
                files.append(f_entry)
                updates["source_files"] = files
                qs["source_files"] = files
        if updates:
            await self._mq_db("update_quiz_set", sid, user.id, updates)
        qs["questions"], qs["count"] = questions, len(questions)
        if added:
            head = f"✅ <b>+{added} added</b> · now {len(questions)} questions"
        elif parsed:
            head = "ℹ️ <b>Those questions are already in this quiz.</b>"
        else:
            head = "⚠️ <b>No questions found in that.</b>" + (f"\n<i>{html.escape(note)}</i>" if note else "")
        skipped = f"\n<i>Skipped {len(probs)} (missing options or answer)</i>" if probs else ""
        text = (f"{head}{skipped}\n\n➕ <b>ADD QUESTIONS</b> · {html.escape(str(qs.get('title', 'Quiz')))}\n"
                f"<i>Keep sending text, files or quiz polls. Tap Done when finished.</i>")
        kb = InlineKeyboardMarkup([[_btn("✅ Done", f"mq_adddone:{sid}", "success")]])
        await self._reply(update, text, reply_markup=kb)

    async def _mq_save(self, context, user, d: Dict, title: str) -> None:
        qs_list = list(d["qs"])
        files_list = list(d.get("files") or [])
        context.user_data.pop("mq_draft", None)
        sid = await self._mq_db("create_quiz_set", user.id, title[:60], qs_list, 20)
        if not isinstance(sid, str) or not sid:
            sid = secrets.token_hex(4)
        if files_list and self.db and hasattr(self.db, "update_quiz_set"):
            await self._mq_db("update_quiz_set", sid, user.id, {"source_files": files_list})
        now_iso = datetime.datetime.utcnow().isoformat()
        doc = {
            "id": sid,
            "owner": user.id,
            "title": title[:60],
            "count": len(qs_list),
            "questions": qs_list,
            "secs": 20,
            "plays": 0,
            "shuffle": False,
            "source_files": files_list,
            "created_at": now_iso,
        }
        logger.info(f"[MYQUIZ] saved {sid} '{title}' ({len(qs_list)} Q, {len(files_list)} files) for {user.id}")
        card_text, kb = self._mq_card_view(doc, saved=True, bot_username=context.bot.username, is_owner=True)
        await self._mq_show(context, d, card_text, kb)

    # ── callbacks ────────────────────────────────────────────────────────

    async def handle_my_quiz_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query, user = update.callback_query, update.effective_user
        if not query or not user or not query.data:
            return
        action, _, arg = query.data.partition(":")
        d = self._mq_draft(context)

        # Practice mode responses
        if action == "mq_p_ans":
            parts = query.data.split(":")
            if len(parts) >= 4:
                sid, q_idx, chosen = parts[1], int(parts[2]), int(parts[3])
                await self._mq_practice_answer(query, context, user, sid, q_idx, chosen)
                return
        if action == "mq_p_next":
            prac = context.user_data.get("mq_prac")
            if prac:
                await query.answer()
                await self._mq_practice_show_q(query, context, prac)
                return

        # Flashcard mode responses: mq_flash:<sid>:<idx>:<reveal 0|1>
        if action == "mq_flash":
            parts = query.data.split(":")
            if len(parts) >= 4:
                sid = parts[1]
                idx = int(parts[2]) if parts[2].lstrip("-").isdigit() else 0
                rev = parts[3] == "1"
                qs = await self._mq_db("get_quiz_set", sid)
                if not isinstance(qs, dict):
                    await query.answer("Quiz not found.", show_alert=True)
                    return
                await self._mq_flashcard_view(query, qs, idx, rev)
                return

        if action in ("mq_done", "mq_qsave", "mq_name", "mq_cancel", "mq_back"):
            if not d:
                await query.answer("This quiz draft has ended.", show_alert=True)
                return
            if action == "mq_cancel":
                context.user_data.pop("mq_draft", None)
                await query.answer("Cancelled.")
                await self._bt_edit(query, "❌ <b>Quiz draft cancelled.</b>\n<i>Nothing was saved.</i>",
                                    InlineKeyboardMarkup([
                                        [_btn("➕ Start Again", "mq_new", "success"),
                                         _btn("📚 My Quizzes", "mq_list", None)],
                                        [_btn("🏠 Home", "nav_home", "primary")]]))
                return
            if action == "mq_back":
                d["stage"] = "collect"
                d["note"] = ""
                d["msg"] = query.message.message_id
                await query.answer()
                await self._bt_edit(query, *self._mq_collect_view(d))
                return
            if action == "mq_qsave":
                if len(d["qs"]) < MIN_Q:
                    await query.answer(f"Add at least {MIN_Q} question first.", show_alert=True)
                    return
                await query.answer("Saving quiz… ✅")
                d["msg"] = query.message.message_id
                title = (d.get("fname") or "My Quiz").strip()[:60]
                await self._mq_save(context, user, d, title)
                return
            if action == "mq_done":
                if len(d["qs"]) < MIN_Q:
                    await query.answer(f"Add at least {MIN_Q} questions first.", show_alert=True)
                    return
                n = await self._mq_db("count_quiz_sets", user.id, default=0)
                d["stage"] = "name"
                d["default"] = d.get("fname") or f"My Quiz {(n if isinstance(n, int) else 0) + 1}"
                d["msg"] = query.message.message_id
                await query.answer()
                await self._bt_edit(
                    query,
                    f"🏷  <b>NAME YOUR QUIZ</b>\n<blockquote><b>✅ {len(d['qs'])} questions ready</b></blockquote>\n"
                    f"<i>Type a name in chat, or tap the suggested title below:</i>",
                    InlineKeyboardMarkup([[_btn(f"✅ Use “{d['default'][:28]}”", "mq_name", "success")],
                                          [_btn("⬅️ Add More", "mq_back", "danger"),
                                           _btn("❌ Cancel", "mq_cancel", "danger")]]))
                return
            await query.answer()
            if d["stage"] == "name":
                d["msg"] = query.message.message_id
                await self._mq_save(context, user, d, d.get("default") or "My Quiz")
            return

        if action == "mq_list":
            self._mq_clear_modes(context)
            if query.message and query.message.chat and query.message.chat.type != "private":
                bot_user = getattr(context.bot, "username", "") or ""
                await query.answer("Open my DM to view your quizzes! 📚")
                if bot_user:
                    await query.message.reply_text(
                        "📚 <b>Your custom quizzes &amp; study materials are in my DM:</b>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📚 Open My Quizzes", url=f"https://t.me/{bot_user}?start=mq"),
                            InlineKeyboardButton("➕ Create New Quiz", url=f"https://t.me/{bot_user}?start=mq_new", style="success"),
                        ]])
                    )
                return
            await query.answer()
            await self.mq_open_list(update, context, query, int(arg) if arg.isdigit() else 1)
            return

        if action == "mq_new":
            self._mq_clear_modes(context)
            if query.message and query.message.chat and query.message.chat.type != "private":
                bot_user = getattr(context.bot, "username", "") or ""
                await query.answer("Quizzes are created in my DM! ✏️")
                if bot_user:
                    await query.message.reply_text(
                        "✨ <b>CREATE YOUR OWN QUIZ</b>\n"
                        "<i>Tap below to create a quiz from PDF, DOCX, Text, or Polls in my DM:</i>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("➕ Create New Quiz in DM", url=f"https://t.me/{bot_user}?start=mq_new", style="success")
                        ]])
                    )
                return
            await query.answer()
            await self.mq_start_draft(query, context, user)
            return

        # Extract sid (may be "sid" or "sid:extra")
        sid = arg.split(":")[0] if arg else ""
        qs = await self._mq_db("get_quiz_set", sid) if sid else None
        if not isinstance(qs, dict):
            await query.answer("That quiz is no longer available.", show_alert=True)
            return

        is_owner = (qs.get("owner") == user.id)
        owner_only_actions = {"mq_edit", "mq_rename", "mq_timer", "mq_settimer", "mq_shuffle", "mq_attach",
                              "mq_del", "mq_delok", "mq_ql", "mq_qv", "mq_qe", "mq_qa", "mq_qas", "mq_qd",
                              "mq_qdok", "mq_add", "mq_adddone"}
        if action in owner_only_actions and not is_owner:
            await query.answer("⛔ Only the creator of this quiz can edit or delete it.", show_alert=True)
            return
        x = query.data.split(":")       # x[1] = sid, x[2:] = extra args
        num = lambda i, dflt=0: int(x[i]) if len(x) > i and x[i].lstrip("-").isdigit() else dflt
        questions = list(qs.get("questions") or [])

        if action == "mq_open":
            self._mq_clear_modes(context)
            await query.answer()
            await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username, is_owner=is_owner))

        elif action == "mq_ql":
            self._mq_clear_modes(context)
            await query.answer()
            await self._bt_edit(query, *self._mq_qlist_view(qs, num(2, 1)))

        elif action == "mq_qv":
            self._mq_clear_modes(context)
            if not questions:
                await query.answer("This quiz has no questions.", show_alert=True)
                return
            await query.answer()
            await self._bt_edit(query, *self._mq_qview(qs, num(2)))

        elif action == "mq_qe":
            idx = num(2)
            if not 0 <= idx < len(questions):
                await query.answer("Question not found.", show_alert=True)
                return
            self._mq_clear_modes(context)
            context.user_data["mq_edit_q"] = {"sid": sid, "idx": idx}
            q = questions[idx]
            opts = [str(o) for o in (q.get("options") or [])]
            cor = int(q.get("correct_answer", 0))
            current = (f"{idx + 1}. {q.get('question', '')}\n"
                       + "\n".join(f"{chr(65 + k)}) {o}" for k, o in enumerate(opts))
                       + (f"\nAnswer: {chr(65 + cor)}" if 0 <= cor < len(opts) else "")
                       + (f"\nExplanation: {q.get('explanation')}" if q.get("explanation") else ""))
            await query.answer("Send the corrected question now ✏️")
            await self._bt_edit(
                query,
                f"✏️ <b>REWRITE QUESTION {idx + 1}</b>\n"
                f"<i>Tap the text below to copy it, fix it, and send it back:</i>\n\n"
                f"<pre>{html.escape(current)}</pre>",
                InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_qv:{sid}:{idx}", "danger")]]),
            )

        elif action == "mq_qa":
            idx = num(2)
            if not 0 <= idx < len(questions):
                await query.answer("Question not found.", show_alert=True)
                return
            await query.answer()
            q = questions[idx]
            opts = [str(o) for o in (q.get("options") or [])]
            cor = int(q.get("correct_answer", 0))
            rows = [[_btn(("✅ " if k == cor else "") + f"{chr(65 + k)}) {o[:40]}", f"mq_qas:{sid}:{idx}:{k}",
                          "success" if k == cor else None)] for k, o in enumerate(opts)]
            rows.append([_btn("⬅️ Back", f"mq_qv:{sid}:{idx}", "danger")])
            await self._bt_edit(
                query,
                f"🔑 <b>CORRECT ANSWER · Q{idx + 1}</b>\n"
                f"<blockquote>{html.escape(str(q.get('question') or ''))}</blockquote>\n"
                f"<i>Tap the right option:</i>",
                InlineKeyboardMarkup(rows),
            )

        elif action == "mq_qas":
            idx, opt = num(2), num(3)
            if not 0 <= idx < len(questions) or not 0 <= opt < len(questions[idx].get("options") or []):
                await query.answer("Question not found.", show_alert=True)
                return
            questions[idx] = dict(questions[idx], correct_answer=opt)
            await self._mq_set_questions(sid, user.id, questions)
            qs["questions"] = questions
            await query.answer(f"Answer set to {chr(65 + opt)} ✅")
            await self._bt_edit(query, *self._mq_qview(qs, idx, f"✅ Answer set to {chr(65 + opt)}"))

        elif action == "mq_qd":
            idx = num(2)
            if not 0 <= idx < len(questions):
                await query.answer("Question not found.", show_alert=True)
                return
            if len(questions) <= MIN_Q:
                await query.answer(f"A quiz needs at least {MIN_Q} questions.", show_alert=True)
                return
            await query.answer()
            await self._bt_edit(
                query,
                f"🗑 <b>Delete question {idx + 1}?</b>\n"
                f"<blockquote>{html.escape(str(questions[idx].get('question') or ''))}</blockquote>",
                InlineKeyboardMarkup([[_btn("🗑 Yes, delete", f"mq_qdok:{sid}:{idx}", "danger"),
                                       _btn("⬅️ Keep it", f"mq_qv:{sid}:{idx}", None)]]),
            )

        elif action == "mq_qdok":
            idx = num(2)
            if not 0 <= idx < len(questions) or len(questions) <= MIN_Q:
                await query.answer("Can't delete this question.", show_alert=True)
                return
            questions.pop(idx)
            await self._mq_set_questions(sid, user.id, questions)
            qs["questions"], qs["count"] = questions, len(questions)
            await query.answer("Question deleted 🗑")
            await self._bt_edit(query, *self._mq_qview(qs, min(idx, len(questions) - 1),
                                                       f"🗑 Question {idx + 1} deleted · {len(questions)} left"))

        elif action == "mq_add":
            self._mq_clear_modes(context)
            context.user_data["mq_adding"] = sid
            await query.answer("Send questions now ➕")
            await self._bt_edit(
                query,
                f"➕ <b>ADD QUESTIONS</b> · {html.escape(str(qs.get('title', 'Quiz')))}\n"
                f"<blockquote>Now {len(questions)} questions</blockquote>\n" + HELP,
                InlineKeyboardMarkup([[_btn("✅ Done", f"mq_adddone:{sid}", "success"),
                                       _btn("⬅️ Cancel", f"mq_edit:{sid}", "danger")]]),
            )

        elif action == "mq_adddone":
            self._mq_clear_modes(context)
            await query.answer("Saved ✅")
            await self._bt_edit(query, *self._mq_editor_view(qs, f"✅ Quiz now has {len(questions)} questions"))

        elif action == "mq_stats":
            await query.answer()
            plays = int(qs.get("plays") or 0)
            q_cnt = int(qs.get("count") or len(qs.get("questions") or []))
            tot_ans = int(qs.get("total_q_answered") or 0)
            tot_cor = int(qs.get("total_q_correct") or 0)
            avg_acc = round(100 * tot_cor / tot_ans, 1) if tot_ans > 0 else 0.0
            files_cnt = len(qs.get("source_files") or [])
            st_text = (
                f"📊  <b>QUIZ ANALYTICS &amp; STATS</b>\n"
                f"<blockquote><b>📝 {html.escape(qs.get('title', 'Quiz'))}\n"
                f"• Quiz ID      ›  <code>{html.escape(sid)}</code>\n"
                f"• Questions    ›  {q_cnt} Qs\n"
                f"• Timer        ›  {qs.get('secs', 20)}s / question\n"
                f"• Shuffle      ›  {'ON' if qs.get('shuffle') else 'OFF'}\n"
                f"• Total Plays  ›  {plays}\n"
                f"• Avg Accuracy ›  {avg_acc}% ({tot_cor}/{tot_ans})\n"
                f"• Study Files  ›  {files_cnt} Attached\n"
                f"• Created      ›  {str(qs.get('created_at', ''))[:10]}</b></blockquote>"
            )
            kb = InlineKeyboardMarkup([[_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger"),
                                        _btn("🏠 Home", "nav_home", "primary")]])
            await self._bt_edit(query, st_text, kb)

        elif action == "mq_edit":
            self._mq_clear_modes(context)
            await query.answer()
            await self._bt_edit(query, *self._mq_editor_view(qs))

        elif action == "mq_shuffle":
            new_shuf = not bool(qs.get("shuffle"))
            await self._mq_db("update_quiz_set", sid, user.id, {"shuffle": new_shuf})
            qs["shuffle"] = new_shuf
            await query.answer(f"Shuffle {'ON' if new_shuf else 'OFF'}")
            await self._bt_edit(query, *self._mq_editor_view(qs))

        elif action == "mq_attach":
            self._mq_clear_modes(context)
            context.user_data["mq_attaching_file"] = sid
            await query.answer("Send your file now 📎")
            await self._bt_edit(
                query,
                f"📎 <b>ATTACH STUDY FILE</b>\n"
                f"<blockquote>{html.escape(str(qs.get('title', 'Quiz')))}</blockquote>\n"
                f"Send a <b>PDF, DOCX, PPTX, TXT or image</b> now.\n"
                f"<i>Anyone who taps 📚 Study Material on this quiz gets the original file.</i>",
                InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_edit:{sid}", "danger")]]),
            )

        elif action == "mq_prac":
            self._mq_clear_modes(context)
            await self._mq_practice_start(query, context, user, qs)

        elif action == "mq_rename":
            self._mq_clear_modes(context)
            context.user_data["mq_renaming"] = sid
            await query.answer("Send the new name ✏️")
            await self._bt_edit(
                query,
                f"🏷 <b>RENAME QUIZ</b>\n"
                f"<blockquote>Current: {html.escape(str(qs.get('title', 'Quiz')))}</blockquote>\n"
                f"<i>Type the new name and send it.</i>",
                InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_edit:{sid}", "danger")]]),
            )

        elif action == "mq_timer":
            await query.answer()
            cur = int(qs.get("secs") or 20)
            t = lambda s: _btn(("✅ " if s == cur else "") + f"{s} sec", f"mq_settimer:{sid}:{s}",
                               "success" if s == cur else None)
            rows = [[t(10), t(15)], [t(20), t(30)], [t(45), t(60)],
                    [_btn("⬅️ Back", f"mq_edit:{sid}", "danger")]]
            await self._bt_edit(
                query,
                f"⏱ <b>TIME PER QUESTION</b>\n"
                f"<blockquote>{html.escape(str(qs.get('title', 'Quiz')))} · now {cur}s</blockquote>\n"
                f"<i>Pick a new time:</i>",
                InlineKeyboardMarkup(rows),
            )

        elif action == "mq_settimer":
            new_secs = num(2)
            if new_secs in (10, 15, 20, 30, 45, 60):
                await self._mq_db("update_quiz_set", sid, user.id, {"secs": new_secs})
                qs["secs"] = new_secs
                await query.answer(f"Timer set to {new_secs}s ✅")
                await self._bt_edit(query, *self._mq_editor_view(qs, f"✅ Timer set to {new_secs}s"))
            else:
                await query.answer()

        elif action == "mq_pdf":
            await query.answer("Preparing your question paper… 📄")
            html_bytes = _build_printable_html(qs, include_answers_inline=False,
                                               bot_username=getattr(context.bot, "username", "") or "")
            safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in str(qs.get("title") or "Quiz")).strip()[:40] or "Quiz"
            doc_file = InputFile(io.BytesIO(html_bytes), filename=f"{safe_name}_Question_Paper.html")
            await context.bot.send_document(
                chat_id=query.message.chat.id,
                document=doc_file,
                caption=(
                    f"📄 <b>{html.escape(qs.get('title', 'Quiz'))}</b> · Question Paper\n"
                    f"<i>Open it → tap <b>Save as PDF</b> to print. Answer key is on the last page; "
                    f"<b>Show answers</b> reveals them inline.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )

        elif action == "mq_mat":
            # Send REAL uploaded file(s) if present; otherwise send complete solved Study Material file
            source_files = qs.get("source_files") or []
            if source_files:
                await query.answer(f"Sending {len(source_files)} Study Material file(s)… 📚")
                sent_any = False
                for sf in source_files:
                    fid = sf.get("file_id")
                    fname = sf.get("file_name") or "Study_Material"
                    kind = sf.get("kind", "document")
                    if not fid:
                        continue
                    try:
                        if kind == "photo":
                            await context.bot.send_photo(
                                chat_id=query.message.chat.id,
                                photo=fid,
                                caption=f"📚 <b>Study Material:</b> {html.escape(qs.get('title', 'Quiz'))}",
                                parse_mode=ParseMode.HTML,
                            )
                        else:
                            await context.bot.send_document(
                                chat_id=query.message.chat.id,
                                document=fid,
                                caption=f"📚 <b>Study Material:</b> <code>{html.escape(fname)}</code>\n📝 Quiz: <b>{html.escape(qs.get('title', 'Quiz'))}</b>",
                                parse_mode=ParseMode.HTML,
                            )
                        sent_any = True
                    except Exception as e:
                        logger.warning(f"[MYQUIZ] failed to send source_file {fname}: {e}")
                if sent_any:
                    return
            # Fallback if created from plain text / polls (or if file_id expired): send solved Study Notes file
            await query.answer("Sending solved study sheet… 📚")
            study_bytes = _build_printable_html(qs, include_answers_inline=True,
                                                bot_username=getattr(context.bot, "username", "") or "")
            safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in str(qs.get("title") or "Quiz")).strip()[:40] or "Quiz"
            doc_file = InputFile(io.BytesIO(study_bytes), filename=f"{safe_name}_Study_Sheet.html")
            await context.bot.send_document(
                chat_id=query.message.chat.id,
                document=doc_file,
                caption=(
                    f"📚 <b>{html.escape(qs.get('title', 'Quiz'))}</b> · Solved Study Sheet\n"
                    + ("<i>Tip: ✏️ Edit Quiz → 📎 Attach File to share your own PDF here instead.</i>" if is_owner else "")
                ),
                parse_mode=ParseMode.HTML,
            )

        elif action == "mq_ch":
            await query.answer()
            bot_user = getattr(context.bot, "username", "")
            share_link = f"https://t.me/{bot_user}?start=shared_{sid}" if bot_user else sid
            ch_text = (
                f"📢  <b>SHARE TO A GROUP OR CHANNEL</b>\n"
                f"<blockquote><b>📝 {html.escape(qs.get('title', 'Quiz'))}  ·  {qs.get('count', 0)} Questions</b></blockquote>\n"
                f"• <b>Start Live in Group</b> — Host this quiz directly inside any group with a live lobby.\n"
                f"• <b>Post Quiz Link</b> — Forward an invite card to a channel or chat."
            )
            ch_rows = []
            if bot_user:
                ch_rows.append([InlineKeyboardButton("👥 Start Live in Group", style="success",
                                                     url=f"https://t.me/{bot_user}?startgroup=gq_{sid}")])
                post_text = urllib.parse.quote(
                    f"📝 {qs.get('title', 'Quiz')}\n"
                    f"❓ Questions: {qs.get('count', 0)}  •  ⏱ {qs.get('secs', 20)}s/Q\n"
                    f"👇 Tap below to start the quiz!"
                )
                ch_rows.append([InlineKeyboardButton("📩 Post Quiz Link",
                                                     url=f"https://t.me/share/url?url={urllib.parse.quote(share_link)}&text={post_text}")])
            ch_rows.append([_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger"),
                            _btn("🏠 Home", "nav_home", "primary")])
            await self._bt_edit(query, ch_text, InlineKeyboardMarkup(ch_rows))

        elif action == "mq_del":
            await query.answer()
            await self._bt_edit(
                query, f"🗑 <b>Delete “{html.escape(qs.get('title', 'Quiz'))}”?</b>\n"
                       f"<i>All {len(questions)} questions will be removed. This can't be undone.</i>",
                InlineKeyboardMarkup([[_btn("🗑 Yes, delete", f"mq_delok:{sid}", "danger"),
                                       _btn("⬅️ Keep it", f"mq_edit:{sid}", None)]]))

        elif action == "mq_delok":
            await self._mq_db("delete_quiz_set", sid, user.id)
            await query.answer("Deleted.")
            await self.mq_open_list(update, context, query)

        elif action == "mq_bt":
            await self._mq_battle(query, context, user, qs)

        else:
            await query.answer()

    # ── flashcards mode (Study) ───────────────────────────────────────────

    async def _mq_flashcard_view(self, query, qs: Dict, idx: int, revealed: bool):
        questions = qs.get("questions") or []
        total = len(questions)
        if not total:
            await query.answer("No questions in this quiz.", show_alert=True)
            return
        idx = idx % total
        q = questions[idx]
        sid = qs["id"]
        qtext = html.escape(str(q.get("question") or ""))
        opts = [str(o) for o in (q.get("options") or [])]
        cor = int(q.get("correct_answer", 0))
        exp = str(q.get("explanation") or "").strip()

        opt_lines = []
        for i, o in enumerate(opts):
            letter = chr(65 + i)
            if revealed and i == cor:
                opt_lines.append(f"✅ <b>{letter}) {html.escape(o)}</b>")
            else:
                opt_lines.append(f"▫️ {letter}) {html.escape(o)}")

        text = (
            f"🎴  <b>FLASHCARDS  ·  Card {idx + 1} of {total}</b>\n"
            f"<blockquote><b>📝 {html.escape(qs.get('title', 'Quiz'))}</b></blockquote>\n"
            f"❓ <b>{qtext}</b>\n\n"
            + "\n".join(opt_lines)
        )
        if revealed:
            cor_letter = chr(65 + cor) if 0 <= cor < 26 else str(cor + 1)
            cor_text = opts[cor] if 0 <= cor < len(opts) else ""
            text += f"\n\n<blockquote>🔑 <b>Answer: ({cor_letter}) {html.escape(cor_text)}</b>"
            if exp:
                text += f"\n💡 <i>{html.escape(exp)}</i>"
            text += "</blockquote>"

        toggle_btn = (
            _btn("🙈 Hide Answer", f"mq_flash:{sid}:{idx}:0", None)
            if revealed else
            _btn("👁 Show Answer", f"mq_flash:{sid}:{idx}:1", "success")
        )
        kb = InlineKeyboardMarkup([
            [toggle_btn],
            [_btn("⬅️ Prev", f"mq_flash:{sid}:{(idx - 1) % total}:0", None),
             _btn("Next ➡️", f"mq_flash:{sid}:{(idx + 1) % total}:0", None)],
            [_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger")],
        ])
        await query.answer()
        await self._bt_edit(query, text, kb)

    # ── practice mode (DM) ────────────────────────────────────────────────

    async def _mq_practice_start(self, query, context, user, qs: Dict):
        questions = list(qs.get("questions") or [])
        if not questions:
            await query.answer("This quiz has no questions.", show_alert=True)
            return
        if qs.get("shuffle"):
            random.shuffle(questions)
        prac = {
            "set_id": qs["id"],
            "title": qs.get("title", "Practice"),
            "questions": questions,
            "idx": 0,
            "correct": 0,
            "wrong": 0,
        }
        context.user_data["mq_prac"] = prac
        await query.answer("Starting Quiz in Private! ▶️")
        await self._mq_practice_show_q(query, context, prac)

    async def _mq_practice_show_q(self, query_or_update, context, prac: Dict, explanation_note: str = ""):
        idx = prac["idx"]
        total = len(prac["questions"])
        if idx >= total:
            cor = prac["correct"]
            wrg = prac["wrong"]
            acc = round(100 * cor / total) if total else 0
            # Increment attempts count in DB
            await self._mq_db("inc_quiz_set_plays", prac["set_id"], cor, total)
            text = (
                f"🎉  <b>QUIZ COMPLETED!</b>\n"
                f"<blockquote><b>📝 {html.escape(prac['title'])}\n"
                f"• Correct  ›  {cor} ✅\n"
                f"• Wrong    ›  {wrg} ❌\n"
                f"• Accuracy ›  {acc}% 📊</b></blockquote>\n"
                f"<i>Great session! Keep practicing to boost your score.</i>"
            )
            kb = InlineKeyboardMarkup([
                [_btn("🔁 Play Again", f"mq_prac:{prac['set_id']}", "success")],
                [_btn("⬅️ Back to Quiz", f"mq_open:{prac['set_id']}", "danger"),
                 _btn("📚 My Quizzes", "mq_list", None)],
            ])
            context.user_data.pop("mq_prac", None)
            if hasattr(query_or_update, "edit_message_text"):
                try:
                    await query_or_update.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                except Exception:
                    await query_or_update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await self._reply(query_or_update, text, reply_markup=kb)
            return

        q = prac["questions"][idx]
        q_text = html.escape(str(q.get("question") or ""))
        opts = [str(o) for o in (q.get("options") or [])]

        head = f"▶️  <b>SOLO PRACTICE  ·  Q{idx+1} of {total}</b>\n<blockquote><b>📝 {html.escape(prac['title'])}</b></blockquote>\n"
        body = f"❓ <b>{q_text}</b>\n\n"
        if explanation_note:
            body += explanation_note + "\n\n"

        rows = []
        for i, opt in enumerate(opts):
            label = f"{chr(65+i)}) {opt[:40]}"
            rows.append([_btn(label, f"mq_p_ans:{prac['set_id']}:{idx}:{i}", None)])
        rows.append([_btn("❌ Exit Quiz", f"mq_open:{prac['set_id']}", "danger")])

        kb = InlineKeyboardMarkup(rows)
        full_text = head + body
        if hasattr(query_or_update, "edit_message_text"):
            try:
                await query_or_update.edit_message_text(full_text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                await query_or_update.message.reply_text(full_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await self._reply(query_or_update, full_text, reply_markup=kb)

    async def _mq_practice_answer(self, query, context, user, set_id: str, idx: int, chosen: int):
        prac = context.user_data.get("mq_prac")
        if not prac or prac.get("set_id") != set_id or prac.get("idx") != idx:
            await query.answer("Question expired or already answered.", show_alert=True)
            return

        q = prac["questions"][idx]
        correct = int(q.get("correct_answer", 0))
        opts = [str(o) for o in (q.get("options") or [])]
        exp = str(q.get("explanation") or "").strip()

        is_right = (chosen == correct)
        if is_right:
            prac["correct"] += 1
            verdict = "✅ <b>Correct!</b>"
        else:
            prac["wrong"] += 1
            cor_letter = chr(65 + correct) if 0 <= correct < 26 else str(correct + 1)
            cor_text = opts[correct] if 0 <= correct < len(opts) else ""
            verdict = f"❌ <b>Incorrect!</b> Correct was <b>{cor_letter}) {html.escape(cor_text)}</b>"

        exp_note = f"<blockquote>{verdict}" + (f"\n💡 <i>{html.escape(exp)}</i>" if exp else "") + "</blockquote>"
        prac["idx"] += 1

        total = len(prac["questions"])
        next_btn = _btn("Next Question ➡️", f"mq_p_next:{set_id}", "success") if prac["idx"] < total else _btn("View Results 🏁", f"mq_p_next:{set_id}", "success")
        kb = InlineKeyboardMarkup([
            [next_btn],
            [_btn("❌ Exit Quiz", f"mq_open:{set_id}", "danger")],
        ])

        q_text = html.escape(str(q.get("question") or ""))
        text = (
            f"▶️ <b>PRIVATE QUIZ</b> [Q{idx+1}/{total}]\n"
            f"<b>{html.escape(prac['title'])}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"❓ <b>{q_text}</b>\n\n"
            f"{exp_note}"
        )
        await query.answer("Correct! 🎉" if is_right else "Wrong! ❌")
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def _mq_battle(self, query, context, user, qs: Dict) -> None:
        from src.bot.battle import Battle
        if query.message.chat.type != "private":
            await query.answer("Open this in my DM.", show_alert=True)
            return
        if self._bt_busy(user.id):
            await query.answer("You're already in a battle — finish it first.", show_alert=True)
            return
        await self._bt_drop_open(user.id, context)
        bid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        b = Battle(id=bid, mode="dm", p1=user.id, p1_name=self._bt_name(user), state="setup")
        self._bt_apply_set(b, qs)
        sent = await self._bt_edit(query, *self._bt_setup_view(b, "set", context.bot.username))
        b.msg_id = getattr(sent, "message_id", None) or query.message.message_id
        self._bt_state()[bid] = b
        await self._bt_save(b)
        await query.answer("1v1 Battle Setup ⚔️")

    handle_myquiz_callback = handle_my_quiz_callback
