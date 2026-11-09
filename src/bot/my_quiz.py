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

MIN_Q         = 3                   # questions needed to save
MAX_Q_PER_SET = 5000                # safety only (MongoDB document size)
MAX_FILE      = 20 * 1024 * 1024    # Telegram: bots can't download files over 20 MB
PER_PAGE      = 8                   # quizzes per list page

HELP = ("<blockquote>📄 Paste questions as text\n"
        "📎 Send any file: PDF, DOCX, PPTX, TXT, CSV, XLSX, JSON, HTML, ZIP…\n"
        "🗳 Forward quiz polls (auto-cleaned!)\n"
        "🧹 Auto-cleans [1/100], @channel_names, PDF headers &amp; phone numbers\n"
        "📚 Uploaded PDF/DOCX is automatically saved in <b>Study Materials</b>!</blockquote>\n"
        "<i>Each question needs options and the answer. Send as many as you like, then tap Done.</i>")
EXAMPLE = ("<blockquote>1. Capital of India?\nA) Mumbai\nB) Delhi\nC) Pune\nAnswer: B</blockquote>")


def _btn(text: str, data: str, style: Optional[str] = "primary") -> InlineKeyboardButton:
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
    btns = [_btn(f"·{n}·" if n == current else str(n), data(n), "success" if n == current else "primary")
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


def _build_printable_html(qs: Dict, include_answers_inline: bool = False) -> bytes:
    """Generate a clean printable HTML/PDF-ready question paper & study sheet."""
    title = html.escape(str(qs.get("title") or "Quiz"))
    questions = qs.get("questions") or []
    secs = int(qs.get("secs") or 20)
    q_cards = []
    key_items = []
    for idx, q in enumerate(questions, 1):
        qtext = html.escape(str(q.get("question") or ""))
        opts = q.get("options") or []
        cor = int(q.get("correct_answer", 0))
        exp = html.escape(str(q.get("explanation") or ""))
        opt_html = []
        for oi, o in enumerate(opts):
            letter = chr(65 + oi)
            is_cor = (oi == cor) and include_answers_inline
            cls = "opt correct" if is_cor else "opt"
            mark = " ✅" if is_cor else ""
            opt_html.append(f"<div class='{cls}'><b>({letter})</b> {html.escape(str(o))}{mark}</div>")
        exp_block = f"<div class='exp'>💡 <b>Explanation:</b> {exp}</div>" if (exp and include_answers_inline) else ""
        q_cards.append(
            f"<div class='qcard'><div class='qtitle'><b>Q{idx}.</b> {qtext}</div>"
            f"<div class='opts'>{''.join(opt_html)}</div>{exp_block}</div>"
        )
        cor_letter = chr(65 + cor) if 0 <= cor < 26 else str(cor + 1)
        cor_val = html.escape(str(opts[cor])) if 0 <= cor < len(opts) else ""
        key_items.append(
            f"<div class='kitem'><b>{idx}. ({cor_letter})</b> {cor_val}"
            + (f"<br><small>💡 {exp}</small>" if exp else "") + "</div>"
        )

    doc_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} — Miss Quiz</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; max-width: 820px; margin: 24px auto; padding: 0 16px; color: #1e293b; background: #f8fafc; }}
  .hdr {{ background: linear-gradient(135deg, #1e1b4b, #312e81); color: #fff; padding: 24px; border-radius: 14px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }}
  .hdr h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
  .meta {{ font-size: 13px; opacity: 0.9; }}
  .pbtn {{ background: #22c55e; color: #fff; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 14px; }}
  .qcard {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; margin-bottom: 12px; page-break-inside: avoid; }}
  .qtitle {{ font-size: 15px; margin-bottom: 10px; line-height: 1.45; }}
  .opts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .opt {{ background: #f1f5f9; padding: 8px 12px; border-radius: 6px; font-size: 14px; }}
  .opt.correct {{ background: #dcfce7; border: 1px solid #22c55e; color: #14532d; font-weight: 600; }}
  .exp {{ margin-top: 10px; font-size: 13px; color: #334155; background: #eff6ff; padding: 8px 12px; border-radius: 6px; }}
  .keybox {{ background: #fff; border: 2px solid #cbd5e1; border-radius: 12px; padding: 18px; margin-top: 28px; page-break-before: always; }}
  .keygrid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; margin-top: 12px; }}
  .kitem {{ background: #f8fafc; padding: 8px 10px; border-radius: 6px; font-size: 13px; border: 1px solid #e2e8f0; }}
  @media print {{ body {{ background: #fff; margin: 0; }} .pbtn {{ display: none; }} }}
</style>
</head>
<body>
  <div class="hdr">
    <div>
      <h1>📝 {title}</h1>
      <div class="meta">❓ Questions: {len(questions)} &nbsp;•&nbsp; ⏱ Time/Q: {secs}s &nbsp;•&nbsp; 🆔 ID: {html.escape(str(qs.get('id', '')))}</div>
    </div>
    <button class="pbtn" onclick="window.print()">🖨 Save / Print as PDF</button>
  </div>
  {''.join(q_cards)}
  <div class="keybox">
    <h2 style="margin:0 0 6px 0;font-size:18px;">🔑 Complete Answer Key &amp; Solutions</h2>
    <div class="keygrid">{''.join(key_items)}</div>
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
        body = (
            f"✏️ <b>CREATE YOUR QUIZ</b>\n"
            f"<blockquote>✅ Questions added › <b>{q_count}</b>" +
            (f"\n⚠️ Skipped {skipped} (missing answer or options)" if skipped else "") +
            (f"\n📎 Study file attached › <b>{html.escape(d['fname'])}</b> ({f_count} file)" if d.get("fname") else "") +
            "</blockquote>\n"
        )
        if d.get("note"):
            body += f"💡 <i>{html.escape(d['note'])}</i>\n\n"
        else:
            body += HELP + "\n\n"
        ready = q_count >= MIN_Q
        top = [
            _btn(f"✅ Done ({q_count}Q)" if ready else f"Add {MIN_Q - q_count} more…",
                 "mq_done", "success" if ready else "primary"),
            _btn("📚 My Quizzes", "mq_list", "primary"),
            _btn("❌ Cancel", "mq_cancel", "danger"),
        ]
        return body, InlineKeyboardMarkup([top])

    @staticmethod
    def _mq_list_view(sets: List[Dict], page: int, pages: int, total: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        from src.bot.battle import _tlabel
        head = (f"📚 <b>YOUR QUIZZES</b>" + (f" · page {page}/{pages}" if pages > 1 else "") + f"  ·  Total Created: <b>{total or len(sets)}</b>\n")
        if not sets:
            body = head + "\n<i>You haven't created any quizzes yet. Tap below to build your first quiz!</i>"
            return body, InlineKeyboardMarkup([[_btn("➕ Create New Quiz", "mq_new", "success")]])
        lines = []
        for i, s in enumerate(sets, 1 + (page - 1) * PER_PAGE):
            q_cnt = s.get("count") or (len(s["questions"]) if "questions" in s else 0)
            t_lbl = _tlabel(int(s.get("secs") or 20))
            plays = int(s.get("plays") or 0)
            has_file = "📎" if s.get("source_files") else ""
            lines.append(
                f"{i}. <b>{html.escape(s.get('title', 'Quiz')[:28])}</b> · {q_cnt}Q · {t_lbl} · 👥 {plays} {has_file}"
            )
        body = (
            head +
            "<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            "<i>Tap any quiz below to open its full dashboard, start in group/private, battle, flashcards, or download Study Materials.</i>"
        )
        item_rows = []
        for s in sets:
            q_cnt = s.get("count") or 0
            item_rows.append([_btn(f"📝 {s.get('title', 'Quiz')[:26]} ({q_cnt}Q)", f"mq_open:{s['id']}", "primary")])
        nav_rows = page_rows(pages, page, lambda p: f"mq_list:{p}")
        foot = [_btn("➕ Create New Quiz", "mq_new", "success")]
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
        shuffle_str = "ON 🔀" if qs.get("shuffle") else "OFF"
        neg_val = float(qs.get("negative_rate") if qs.get("negative_rate") is not None else 0.25)
        neg_str = f"-{neg_val} marks" if neg_val > 0 else "No Negative"
        created_str = str(qs.get("created_at") or datetime.datetime.utcnow().isoformat())[:10]
        attempts = int(qs.get("plays") or 0)
        files_cnt = len(qs.get("source_files") or [])
        share_link = f"https://t.me/{bot_username}?start=shared_{sid}" if bot_username else f"Code: {sid}"

        text = (
            ("🎉 <b>Saved! Quiz Published &amp; Ready!</b>\n" if saved else "") +
            f"🎓 <b>{title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>"
            f"❓ <b>Questions:</b> <code>{q_count} Qs</code>\n"
            f"⏱ <b>Timer:</b> <code>{secs}s / Q</code>  ·  🕒 <b>Duration:</b> <code>{total_time}</code>\n"
            f"⚖️ <b>Marking:</b> <code>+1.0 / {neg_str}</code>  ·  🔀 <b>Shuffle:</b> <code>{shuffle_str}</code>\n"
            f"👥 <b>Total Plays:</b> <code>{attempts}</code>  ·  📅 <b>Created:</b> <code>{created_str}</code>\n"
            f"📚 <b>Study Files:</b> <code>{files_cnt} Attached</code>  ·  🆔 <b>Code:</b> <code>{html.escape(sid)}</code>"
            f"</blockquote>\n"
            f"🔗 <b>Direct Quiz Link:</b>\n{share_link}\n\n"
            f"⚡ <i>Choose a mode below to launch, study, or share:</i>"
        )

        rows: List[List[InlineKeyboardButton]] = []

        # Row 1: Primary Play Modes (Green)
        if bot_username:
            rows.append([
                _btn("▶️ Play Solo (DM)", f"mq_prac:{sid}", "success"),
                InlineKeyboardButton("👥 Host in Group", style="success",
                                     url=f"https://t.me/{bot_username}?startgroup=gq_{sid}"),
            ])
        else:
            rows.append([_btn("▶️ Play Solo (DM)", f"mq_prac:{sid}", "success")])

        # Row 2: 1v1 Battle (Green) & Interactive Flashcards (Blue)
        rows.append([
            _btn("⚔️ 1v1 Live Battle", f"mq_bt:{sid}", "success"),
            _btn("🎴 Study Flashcards", f"mq_flash:{sid}:0:0", "primary"),
        ])

        # Row 3: Real Study Materials File & Printable Question Paper (Blue)
        sm_label = f"📚 Study Material ({files_cnt} File)" if files_cnt else "📚 Study Material"
        rows.append([
            _btn(sm_label, f"mq_mat:{sid}", "primary"),
            _btn("📄 Question Paper", f"mq_pdf:{sid}", "primary"),
        ])

        # Row 4: Stats & Customize/Edit (Blue)
        if is_owner:
            rows.append([
                _btn("📊 Quiz Stats", f"mq_stats:{sid}", "primary"),
                _btn("⚙️ Customize Quiz", f"mq_edit:{sid}", "primary"),
            ])
        else:
            rows.append([_btn("📊 Quiz Stats", f"mq_stats:{sid}", "primary")])

        # Row 5: Channel/Group & Share Link (Blue)
        if bot_username:
            share_text = urllib.parse.quote(f"🎯 Challenge yourself on \"{qs.get('title', 'Quiz')}\" ({q_count} Questions) on Miss Quiz Bot!")
            share_url = f"https://t.me/share/url?url={urllib.parse.quote(share_link)}&text={share_text}"
            rows.append([
                _btn("📢 Channel / Group", f"mq_ch:{sid}", "primary"),
                InlineKeyboardButton("📩 Share Quiz", url=share_url, style="primary"),
            ])
        else:
            rows.append([_btn("📢 Channel / Group", f"mq_ch:{sid}", "primary")])

        # Row 6: Delete (Red, owner only) & Back to My Quizzes
        if is_owner:
            rows.append([
                _btn("🗑 Delete Quiz", f"mq_del:{sid}", "danger"),
                _btn("📚 My Quizzes", "mq_list", "primary"),
            ])
        else:
            rows.append([_btn("📚 My Quizzes", "mq_list", "primary")])

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
                await self._reply(update, f"✅ <b>Study Material attached:</b> <code>{html.escape(f_entry['file_name'])}</code>")
                c_text, c_kb = self._mq_card_view(qs, bot_username=context.bot.username, is_owner=True)
                await self._reply(update, c_text, reply_markup=c_kb)
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
                await self._reply(update, f"✅ <b>Quiz renamed to:</b> <code>{html.escape(new_title)}</code>")
                if qs:
                    c_text, c_kb = self._mq_card_view(qs, bot_username=context.bot.username, is_owner=True)
                    await self._reply(update, c_text, reply_markup=c_kb)
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

        items, text, note = [], "", ""
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
            # Save the real Telegram file_id so "📚 Study Materials" can send the exact original file!
            f_entry = {
                "file_id": doc.file_id,
                "file_name": fname,
                "kind": "document" if msg.document else "photo",
            }
            existing_fids = {x.get("file_id") for x in d.get("files", [])}
            if doc.file_id not in existing_fids:
                d.setdefault("files", []).append(f_entry)

            if doc.file_size and doc.file_size > MAX_FILE:
                note = "File is too big (over 20 MB) — saved in 📚 Study Materials, send a smaller text/PDF for questions."
            else:
                try:
                    from src.bot.file_extract import extract
                    f = await context.bot.get_file(doc.file_id, read_timeout=60)
                    data = bytes(await f.download_as_bytearray(read_timeout=60))
                    items, text, note = await asyncio.to_thread(extract, fname, data)
                    if not d.get("fname") and msg.document and doc.file_name:
                        d["fname"] = doc.file_name.rsplit(".", 1)[0].replace("_", " ")[:40]
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
            user_words = await asyncio.to_thread(self.db.get_user_custom_words, user.id)

        qs, probs = (await asyncio.to_thread(parse_questions, text, items, user_words)) if (text or items) else ([], [])
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

        if action in ("mq_done", "mq_name", "mq_cancel"):
            if not d:
                await query.answer("This quiz draft has ended.", show_alert=True)
                return
            if action == "mq_cancel":
                context.user_data.pop("mq_draft", None)
                await query.answer("Cancelled.")
                await self._bt_edit(query, "❌ <b>Quiz draft cancelled.</b>",
                                    InlineKeyboardMarkup([[_btn("📚 My Quizzes", "mq_list")]]))
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
                    f"✏️ <b>NAME YOUR QUIZ</b>\n<blockquote>✅ Questions › {len(d['qs'])}</blockquote>\n"
                    f"<i>Type a name in chat, or tap the button below:</i>",
                    InlineKeyboardMarkup([[_btn(f"📝 Use “{d['default'][:28]}”", "mq_name", "success")],
                                          [_btn("❌ Cancel", "mq_cancel", "danger")]]))
                return
            await query.answer()
            if d["stage"] == "name":
                d["msg"] = query.message.message_id
                await self._mq_save(context, user, d, d.get("default") or "My Quiz")
            return

        if action == "mq_list":
            context.user_data.pop("mq_renaming", None)
            context.user_data.pop("mq_attaching_file", None)
            context.user_data.pop("mq_prac", None)
            if query.message and query.message.chat and query.message.chat.type != "private":
                bot_user = getattr(context.bot, "username", "") or ""
                await query.answer("Open my DM to view your quizzes! 📚")
                if bot_user:
                    await query.message.reply_text(
                        "📚 <b>Your custom quizzes &amp; study materials are in my DM:</b>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📚 Open My Quizzes", url=f"https://t.me/{bot_user}?start=mq", style="primary"),
                            InlineKeyboardButton("➕ Create New Quiz", url=f"https://t.me/{bot_user}?start=mq_new", style="success"),
                        ]])
                    )
                return
            await query.answer()
            await self.mq_open_list(update, context, query, int(arg) if arg.isdigit() else 1)
            return

        if action == "mq_new":
            context.user_data.pop("mq_renaming", None)
            context.user_data.pop("mq_attaching_file", None)
            context.user_data.pop("mq_prac", None)
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
        owner_only_actions = {"mq_open", "mq_edit", "mq_rename", "mq_timer", "mq_settimer", "mq_shuffle", "mq_attach", "mq_del", "mq_delok"}
        if action in owner_only_actions and not is_owner:
            await query.answer("⛔ Only the creator of this quiz can edit or delete it.", show_alert=True)
            return

        if action == "mq_open":
            context.user_data.pop("mq_renaming", None)
            context.user_data.pop("mq_attaching_file", None)
            context.user_data.pop("mq_prac", None)
            await query.answer()
            await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username, is_owner=is_owner))

        elif action == "mq_stats":
            await query.answer()
            plays = int(qs.get("plays") or 0)
            q_cnt = int(qs.get("count") or len(qs.get("questions") or []))
            tot_ans = int(qs.get("total_q_answered") or 0)
            tot_cor = int(qs.get("total_q_correct") or 0)
            avg_acc = round(100 * tot_cor / tot_ans, 1) if tot_ans > 0 else 0.0
            files_cnt = len(qs.get("source_files") or [])
            st_text = (
                f"📊 <b>QUIZ ANALYTICS &amp; STATS</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Title:</b> {html.escape(qs.get('title', 'Quiz'))}\n"
                f"🆔 <b>ID:</b> <code>{html.escape(sid)}</code>\n"
                f"❓ <b>Questions:</b> <b>{q_cnt}</b>\n"
                f"⏱ <b>Timer:</b> <b>{qs.get('secs', 20)}s / question</b>\n"
                f"🔀 <b>Shuffle:</b> <b>{'On' if qs.get('shuffle') else 'Off'}</b>\n"
                f"👥 <b>Total Attempts:</b> <b>{plays}</b>\n"
                f"🎯 <b>Avg Accuracy:</b> <b>{avg_acc}%</b> ({tot_cor}/{tot_ans})\n"
                f"📚 <b>Study Files Attached:</b> <b>{files_cnt}</b>\n"
                f"📅 <b>Created:</b> <b>{str(qs.get('created_at', ''))[:10]}</b>\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            kb = InlineKeyboardMarkup([[_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "primary")]])
            await self._bt_edit(query, st_text, kb)

        elif action == "mq_edit":
            await query.answer()
            shuf_lbl = "🔀 Shuffle: ON ✅" if qs.get("shuffle") else "🔀 Shuffle: OFF ❌"
            files_cnt = len(qs.get("source_files") or [])
            ed_text = (
                f"✏️ <b>EDIT QUIZ SETTINGS</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Quiz:</b> {html.escape(qs.get('title', 'Quiz'))}\n"
                f"⏱ <b>Timer:</b> {qs.get('secs', 20)} sec\n"
                f"🔀 <b>Shuffle Questions:</b> {'On' if qs.get('shuffle') else 'Off'}\n"
                f"📚 <b>Attached Study Files:</b> {files_cnt}\n\n"
                f"<i>Choose a setting to customize:</i>"
            )
            ed_kb = InlineKeyboardMarkup([
                [_btn("✏️ Rename Title", f"mq_rename:{sid}", "primary"),
                 _btn("⏱️ Change Timer", f"mq_timer:{sid}", "primary")],
                [_btn(shuf_lbl, f"mq_shuffle:{sid}", "success" if qs.get("shuffle") else "primary")],
                [_btn("📎 Attach Study Material (PDF/File)", f"mq_attach:{sid}", "primary")],
                [_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger")],
            ])
            await self._bt_edit(query, ed_text, ed_kb)

        elif action == "mq_shuffle":
            new_shuf = not bool(qs.get("shuffle"))
            if self.db and hasattr(self.db, "update_quiz_set"):
                await asyncio.to_thread(self.db.update_quiz_set, sid, user.id, {"shuffle": new_shuf})
            qs["shuffle"] = new_shuf
            await query.answer(f"Shuffle turned {'ON' if new_shuf else 'OFF'}!")
            await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username, is_owner=True))

        elif action == "mq_attach":
            context.user_data["mq_attaching_file"] = sid
            await query.answer("Send your PDF / DOCX / Study file now!")
            await self._bt_edit(
                query,
                f"📎 <b>ATTACH STUDY MATERIAL</b>\n"
                f"Quiz: <b>{html.escape(qs.get('title', 'Quiz'))}</b>\n\n"
                f"Send any <b>PDF, DOCX, PPTX, TXT, or Image</b> in chat right now.\n"
                f"Whenever anyone taps <b>📚 Study Materials</b> on this quiz, they will receive the real file!",
                InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_open:{sid}", "danger")]]),
            )

        elif action == "mq_prac":
            await self._mq_practice_start(query, context, user, qs)

        elif action == "mq_rename":
            context.user_data["mq_renaming"] = sid
            await query.answer("Send new title in chat!")
            await self._bt_edit(
                query,
                f"✏️ <b>RENAME QUIZ</b>\n"
                f"Current: <code>{html.escape(qs.get('title', 'Quiz'))}</code>\n\n"
                f"<i>Type and send the new title as a message now.</i>",
                InlineKeyboardMarkup([[_btn("⬅️ Cancel", f"mq_open:{sid}", "danger")]]),
            )

        elif action == "mq_timer":
            await query.answer()
            rows = [
                [_btn("10s", f"mq_settimer:{sid}:10"), _btn("15s", f"mq_settimer:{sid}:15"), _btn("20s", f"mq_settimer:{sid}:20")],
                [_btn("30s", f"mq_settimer:{sid}:30"), _btn("45s", f"mq_settimer:{sid}:45"), _btn("60s", f"mq_settimer:{sid}:60")],
                [_btn("⬅️ Back", f"mq_open:{sid}", "danger")],
            ]
            await self._bt_edit(
                query,
                f"⏱️ <b>SELECT TIMER</b>\nQuiz: <code>{html.escape(qs.get('title', 'Quiz'))}</code>\n"
                f"Current: <b>{qs.get('secs', 20)}s per question</b>\n<i>Choose new duration:</i>",
                InlineKeyboardMarkup(rows),
            )

        elif action == "mq_settimer":
            parts = query.data.split(":")
            if len(parts) >= 3 and parts[2].isdigit():
                new_secs = int(parts[2])
                if self.db and hasattr(self.db, "update_quiz_set"):
                    await asyncio.to_thread(self.db.update_quiz_set, sid, user.id, {"secs": new_secs})
                qs["secs"] = new_secs
                await query.answer(f"Timer set to {new_secs}s!")
                await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username, is_owner=True))

        elif action == "mq_pdf":
            await query.answer("Generating Printable Question Paper… 📄")
            html_bytes = _build_printable_html(qs, include_answers_inline=False)
            safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in str(qs.get("title") or "Quiz")).strip()[:40] or "Quiz"
            doc_file = InputFile(io.BytesIO(html_bytes), filename=f"{safe_name}_Question_Paper.html")
            await context.bot.send_document(
                chat_id=query.message.chat.id,
                document=doc_file,
                caption=(
                    f"📄 <b>{html.escape(qs.get('title', 'Quiz'))} — Question Paper &amp; Answer Key</b>\n"
                    f"<i>Open in browser and tap 'Save / Print as PDF' for a clean PDF sheet!</i>"
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
            await query.answer("Sending solved Study Notes file… 📚")
            study_bytes = _build_printable_html(qs, include_answers_inline=True)
            safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in str(qs.get("title") or "Quiz")).strip()[:40] or "Quiz"
            doc_file = InputFile(io.BytesIO(study_bytes), filename=f"{safe_name}_Study_Material.html")
            await context.bot.send_document(
                chat_id=query.message.chat.id,
                document=doc_file,
                caption=(
                    f"📚 <b>{html.escape(qs.get('title', 'Quiz'))} — Complete Study Material &amp; Solved Notes</b>\n"
                    + ("<i>💡 Tip: Tap ✏️ Edit Quiz → 📎 Attach Study Material to attach your own PDF/DOCX file!</i>" if is_owner else "")
                ),
                parse_mode=ParseMode.HTML,
            )

        elif action == "mq_ch":
            await query.answer()
            bot_user = getattr(context.bot, "username", "")
            share_link = f"https://t.me/{bot_user}?start=shared_{sid}" if bot_user else sid
            ch_text = (
                f"📢 <b>SEND TO CHANNEL OR GROUP</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Quiz:</b> {html.escape(qs.get('title', 'Quiz'))} ({qs.get('count', 0)}Q)\n\n"
                f"<b>Option 1 — Live Interactive Group Quiz:</b>\n"
                f"Tap <b>👥 Start in Group</b> below or send <code>/quizset {html.escape(sid)}</code> inside any group where I am present.\n\n"
                f"<b>Option 2 — Post Quiz Card in Channel / Group:</b>\n"
                f"Tap <b>📩 Share to Channel / Group</b> to post the direct quiz link so all subscribers can play!"
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
                ch_rows.append([InlineKeyboardButton("📩 Share to Channel / Group", style="primary",
                                                     url=f"https://t.me/share/url?url={urllib.parse.quote(share_link)}&text={post_text}")])
            ch_rows.append([_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger")])
            await self._bt_edit(query, ch_text, InlineKeyboardMarkup(ch_rows))

        elif action == "mq_del":
            await query.answer()
            await self._bt_edit(
                query, f"🗑 <b>Delete “{html.escape(qs.get('title', 'Quiz'))}”?</b>\n<i>This can't be undone.</i>",
                InlineKeyboardMarkup([[_btn("🗑 Yes, delete", f"mq_delok:{sid}", "danger"),
                                       _btn("⬅️ Back", f"mq_open:{sid}", "danger")]]))

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
            f"🎴 <b>FLASHCARDS (STUDY MODE)</b>  ·  Card <b>{idx + 1}/{total}</b>\n"
            f"📝 <b>{html.escape(qs.get('title', 'Quiz'))}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
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
            _btn("🙈 Hide Answer", f"mq_flash:{sid}:{idx}:0", "primary")
            if revealed else
            _btn("👁 Show Answer", f"mq_flash:{sid}:{idx}:1", "success")
        )
        nav_row = [
            _btn("⬅️ Prev", f"mq_flash:{sid}:{(idx - 1) % total}:0", "primary"),
            toggle_btn,
            _btn("Next ➡️", f"mq_flash:{sid}:{(idx + 1) % total}:0", "primary"),
        ]
        kb = InlineKeyboardMarkup([
            nav_row,
            [_btn("⬅️ Back to Quiz Card", f"mq_open:{sid}", "danger")],
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
                f"🎉 <b>QUIZ COMPLETED!</b>\n"
                f"<b>Quiz:</b> {html.escape(prac['title'])}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✅ Correct: <b>{cor}</b>\n"
                f"❌ Wrong: <b>{wrg}</b>\n"
                f"📊 Accuracy: <b>{acc}%</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Great session! Keep going."
            )
            kb = InlineKeyboardMarkup([
                [_btn("🔁 Play Again", f"mq_prac:{prac['set_id']}", "success")],
                [_btn("⬅️ Back to Quiz Card", f"mq_open:{prac['set_id']}", "primary")],
                [_btn("📚 My Quizzes", "mq_list", "primary")],
            ])
            if hasattr(query_or_update, "edit_message_text"):
                await query_or_update.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await self._reply(query_or_update, text, reply_markup=kb)
            return

        q = prac["questions"][idx]
        q_text = html.escape(str(q.get("question") or ""))
        opts = [str(o) for o in (q.get("options") or [])]

        head = f"▶️ <b>PRIVATE QUIZ</b> [Q{idx+1}/{total}]\n<b>{html.escape(prac['title'])}</b>\n━━━━━━━━━━━━━━━━━━\n"
        body = f"❓ <b>{q_text}</b>\n\n"
        if explanation_note:
            body += explanation_note + "\n\n"

        rows = []
        for i, opt in enumerate(opts):
            label = f"{chr(65+i)}) {opt[:40]}"
            rows.append([_btn(label, f"mq_p_ans:{prac['set_id']}:{idx}:{i}", "primary")])
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
        err = await self._bt_create(b)
        if err:
            await query.answer(err, show_alert=True)
            return
        b.msg_id = query.message.message_id
        self._bt_state()[bid] = b
        await self._bt_save(b)
        await query.answer("Challenge created! ⚔️")
        await self._bt_edit(query, *self._bt_challenge_view(b, context.bot.username, self._bt_inline_on(context)))

    handle_myquiz_callback = handle_my_quiz_callback
