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
import os
import random
import secrets
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Poll, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.utils.cleaner import clean_text
from src.bot.ui import UI

logger = logging.getLogger(__name__)

class SoloQuizSession:
    def __init__(self, user_id: int, qs: Dict):
        self.user_id = user_id
        self.set_id = str(qs.get("id", ""))
        self.title = str(qs.get("title", "Quiz"))
        self.questions = list(qs.get("questions") or [])
        if qs.get("shuffle"):
            random.shuffle(self.questions)
        self.secs = max(5, min(600, int(qs.get("secs") or 15)))
        self.status = "running"
        self.idx = 0
        self.correct = 0
        self.wrong = 0
        self.skipped = 0
        self.start_time = time.monotonic()
        self.wake = asyncio.Event()
        self.poll_id: Optional[str] = None
        self.current_correct: int = 0
        self.task: Optional[asyncio.Task] = None

    def record_answer(self, poll_id: str, uid: int, option_ids: List[int]) -> bool:
        if poll_id != self.poll_id or self.status != "running":
            return False
        if option_ids:
            chosen = option_ids[0]
            if chosen == self.current_correct:
                self.correct += 1
            else:
                self.wrong += 1
        else:
            self.skipped += 1
        self.wake.set()
        return True


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


def _is_private(update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "type", "") == "private"


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
        q = clean_text(raw_q, custom_words, is_option=False) or raw_q
        opts = [clean_text(str(o).strip(), custom_words, is_option=True) or str(o).strip()
                for o in it.get("options") or []]       # never let cleaning empty a real option
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


_PAPER_CSS = """
:root{--paper:#f4f5fa;--sheet:#fff;--text:#15172a;--mut:#62667c;--line:#e3e5ef;--ink:#2132b8;--on:#fff;
  --omr:#e06f8c;--omrbg:rgba(224,111,140,.09);--ok:#17864a;--okbg:#e9f7ef;--bad:#cf3526;--badbg:#fdecea;--warn:#a86a12}
@media screen and (prefers-color-scheme:dark){:root{--paper:#0e1020;--sheet:#171a2e;--text:#eef0fa;--mut:#9ea3bd;--line:#2a2e4a;
  --ink:#8493ff;--on:#0e1020;--omr:#f08fa8;--omrbg:rgba(240,143,168,.1);--ok:#3cc37a;--okbg:rgba(60,195,122,.13);--bad:#f0604f;--badbg:rgba(240,96,79,.13);--warn:#e0a84a}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--paper);color:var(--text);font:16px/1.6 Lexend,"Noto Sans","Noto Sans Devanagari","Segoe UI",Roboto,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;cursor:pointer}
:focus-visible{outline:2.5px solid var(--ink);outline-offset:2px;border-radius:6px}
.paper{max-width:820px;margin:0 auto;background:var(--sheet);padding:34px 40px 120px;min-height:100vh}
.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:34px}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:15px}
.brand i{width:16px;height:16px;border-radius:50%;background:var(--ink);box-shadow:0 0 0 2px var(--sheet),0 0 0 3.5px var(--omr)}
.acts{display:flex;gap:8px}
.pill{border:1.5px solid var(--line);background:transparent;border-radius:999px;padding:7px 13px;font-size:13.5px;font-weight:500;white-space:nowrap}
.kind{color:var(--mut);font-size:14px;margin:0}
h1{font:600 clamp(30px,6.2vw,46px)/1.08 Fraunces,Georgia,"Times New Roman",serif;letter-spacing:-.02em;margin:6px 0 24px;word-break:break-word}
.facts{display:grid;grid-template-columns:repeat(4,1fr);border-top:2px solid var(--text);border-bottom:1px solid var(--line)}
.facts div{padding:12px 4px 11px}.facts div+div{padding-left:14px;border-left:1px solid var(--line)}
.facts b{display:block;font-size:22px;font-weight:600;line-height:1.2;font-variant-numeric:tabular-nums}.facts span{font-size:12.5px;color:var(--mut)}
.cand{display:none}
.inst{margin:22px 0 8px;padding:0 0 0 14px;border-left:3px solid var(--omr);font-size:14.5px;color:var(--mut)}
.inst ul{margin:0;padding-left:18px}.inst b{color:var(--text);font-weight:600}
.po{display:none}
.res{display:none;margin:26px 0 6px;padding:22px;border-radius:22px;background:var(--ink);color:var(--on)}
.done .res{display:block}
.res .n{font:600 68px/1 Fraunces,Georgia,serif;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.res .n small{font:400 22px Lexend,sans-serif;opacity:.7;letter-spacing:0}
.res p{margin:6px 0 16px;opacity:.85;font-size:15px}
.rf{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.rf div{border-radius:14px;padding:10px 12px;background:rgba(255,255,255,.12)}
@media screen and (prefers-color-scheme:dark){.rf div{background:rgba(14,16,32,.12)}}
.rf b{display:block;font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.2}.rf span{font-size:12.5px;opacity:.8}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1.5px solid currentColor;background:transparent;color:inherit;border-radius:999px;padding:7px 14px;font-size:14px;font-weight:500;opacity:.75}
.chip.on{background:var(--on);color:var(--ink);border-color:var(--on);opacity:1}
main{margin-top:14px}
.q{padding:22px 0 20px;border-bottom:1px solid var(--line);scroll-margin-top:16px}
.q:last-child{border-bottom:0}
.qh{display:flex;gap:12px;align-items:baseline}
.qn{flex:none;min-width:30px;font:600 17px/1.5 Fraunces,Georgia,serif;color:var(--omr);font-variant-numeric:tabular-nums}
.qh p{margin:0;font-weight:500;font-size:17px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.tag{display:none;font-size:12px;font-weight:600;border-radius:999px;padding:1px 9px;margin-left:8px;white-space:nowrap;vertical-align:middle}
.done .q.r .tag,.done .q.w .tag,.done .q.s .tag{display:inline-block}
.q.r .tag{background:var(--okbg);color:var(--ok)}.q.w .tag{background:var(--badbg);color:var(--bad)}.q.s .tag{background:var(--omrbg);color:var(--warn)}
.q.r .tag::after{content:"Correct"}.q.w .tag::after{content:"Wrong"}.q.s .tag::after{content:"Skipped"}
.opts{list-style:none;margin:12px 0 0 42px;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:6px 14px}
.opts.one{grid-template-columns:1fr}
.opt label,.opt>.st{display:flex;gap:11px;align-items:flex-start;padding:7px 10px 7px 7px;border-radius:14px;border:1.5px solid transparent;transition:background .15s,border-color .15s}
.opt label{cursor:pointer}
.opt input{position:absolute;opacity:0;width:0;height:0}
.bul{flex:none;width:28px;height:28px;border-radius:50%;border:1.6px solid var(--omr);background:var(--omrbg);color:var(--omr);display:grid;place-items:center;font-size:12.5px;font-weight:600;text-transform:uppercase;transition:background .15s,color .15s,border-color .15s,transform .2s cubic-bezier(.34,1.56,.64,1)}
.ot{padding-top:2px;word-break:break-word}
.opt input:checked~.bul{background:var(--ink);border-color:var(--ink);color:var(--on);transform:scale(1.1)}
.opt input:checked~.ot{font-weight:500}
.opt input:focus-visible~.bul{outline:2.5px solid var(--ink);outline-offset:2px}
.done .opt label{cursor:default}
.sol{display:none;margin:12px 0 0 42px;padding:10px 14px;border-left:3px solid var(--ok);background:var(--okbg);border-radius:4px 14px 14px 4px;font-size:14.5px}
.sol b{color:var(--ok);font-weight:600}.why{margin-top:3px;white-space:pre-wrap;word-break:break-word}
.show .sol{display:block}
.show .opt.ok label,.show .opt.ok>.st{background:var(--okbg);border-color:var(--ok)}
.show .opt.ok .bul{background:var(--ok);border-color:var(--ok);color:#fff}
.done .opt.no label{background:var(--badbg);border-color:var(--bad)}
.done .opt.no .bul{background:var(--bad);border-color:var(--bad);color:#fff}
body[data-f="w"] .q:not(.w),body[data-f="s"] .q:not(.s){display:none}
.key{margin-top:34px;padding-top:22px;border-top:2px solid var(--text)}
.key h2{font:600 24px/1.2 Fraunces,Georgia,serif;margin:0 0 14px}
.kg{display:grid;grid-template-columns:repeat(auto-fill,minmax(66px,1fr));gap:6px}
.k{display:flex;justify-content:space-between;align-items:center;padding:5px 10px;border:1px solid var(--line);border-radius:10px;font-size:13.5px;font-variant-numeric:tabular-nums}
.k span{color:var(--mut)}.k b{color:var(--ok);text-transform:uppercase;font-weight:600}
body:not(.show) .key{display:none}
footer{margin-top:30px;font-size:12.5px;color:var(--mut)}
.dock{position:fixed;left:0;right:0;bottom:0;z-index:9;padding:10px 14px calc(10px + env(safe-area-inset-bottom));background:var(--sheet);border-top:1px solid var(--line);box-shadow:0 -12px 30px -18px rgba(21,23,42,.35)}
.dock>div{max-width:820px;margin:0 auto;display:flex;align-items:center;gap:14px}
.prog{flex:1;min-width:0;font-size:13.5px;color:var(--mut)}.prog b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.track{height:4px;border-radius:4px;background:var(--line);margin-top:6px;overflow:hidden}.track i{display:block;height:100%;width:0;background:var(--ink);border-radius:4px;transition:width .35s cubic-bezier(.2,.8,.2,1)}
.go{border:0;border-radius:999px;background:var(--ink);color:var(--on);padding:13px 22px;font-weight:600;font-size:15.5px;white-space:nowrap}
@media (max-width:640px){
  .paper{padding:22px 18px 120px}.top{margin-bottom:26px}
  .facts{grid-template-columns:repeat(2,1fr)}.facts div:nth-child(3){padding-left:4px;border-left:0}.facts div:nth-child(n+3){border-top:1px solid var(--line)}
  .opts{grid-template-columns:1fr;margin-left:0}.sol{margin-left:0}.res .n{font-size:56px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@page{size:A4;margin:12mm 11mm}
@media print{
  :root{--paper:#fff;--sheet:#fff;--text:#111;--mut:#555;--line:#d9dbe3;--ink:#1d2a9c;--omr:#888;--omrbg:transparent}
  body{font-size:10.5pt;line-height:1.45}
  .paper{max-width:none;padding:0;min-height:0}
  .top .acts,.dock,.res,.so,.tag{display:none!important}.po{display:revert}
  .top{margin-bottom:10px}h1{font-size:22pt;margin-bottom:12px}
  .cand{display:grid;grid-template-columns:2fr 1.2fr 1fr;gap:18px;margin:14px 0 2px;font-size:10pt;color:var(--mut)}
  .cand div{display:flex;align-items:flex-end;gap:8px;white-space:nowrap}.cand i{flex:1;border-bottom:1px solid #888;height:1.2em}.cand i.sc{flex:0 0 42px}
  main{column-count:2;column-gap:9mm;column-rule:1px solid var(--line)}
  .q{break-inside:avoid;page-break-inside:avoid;padding:8px 0 9px}.qh p,.qn{font-size:10.5pt}
  .opts{grid-template-columns:1fr;margin-left:26px;gap:1px}.opt label,.opt>.st{padding:1px 0;border:0;background:none!important}
  .bul{width:17px;height:17px;font-size:8.5pt;border-width:1px;transform:none!important}
  .opt input:checked~.bul{background:none;color:var(--omr);border-color:var(--omr)}
  .sol{margin-left:26px;padding:4px 8px;font-size:9.5pt}
  .show .opt.ok .bul{background:#111;border-color:#111;color:#fff}
  .key{display:block!important;break-before:page;page-break-before:always;margin-top:0}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
"""

_PAPER_JS = """
(function(){
var B=document.body,qs=[].slice.call(document.querySelectorAll('.q')),N=qs.length,neg=parseFloat(B.getAttribute('data-neg'))||0;
var $=function(i){return document.getElementById(i)},KEY='mq-paper:'+B.getAttribute('data-id')+':'+N,done=false;
function pick(q){var c=q.querySelector('input:checked');return c?+c.value:-1}
function fmt(x){x=Math.round(x*100)/100;return String(x)}
function save(){try{localStorage.setItem(KEY,JSON.stringify(qs.map(pick)))}catch(e){}}
function progress(){var n=0;qs.forEach(function(q){if(pick(q)>=0)n++});$('pn').textContent=n;$('pb').style.width=(N?n/N*100:0)+'%';return n}
function countUp(el,to){var t0=null,rm=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;if(rm||!window.requestAnimationFrame){el.textContent=fmt(to);return}
  (function f(t){if(!t0)t0=t;var k=Math.min((t-t0)/900,1),e=1-Math.pow(1-k,4);el.textContent=fmt(k<1?Math.round(to*e):to);if(k<1)requestAnimationFrame(f)})(performance.now())}
function check(){
  var r=0,w=0,s=0;done=true;
  qs.forEach(function(q){var a=+q.getAttribute('data-a'),p=pick(q);q.classList.remove('r','w','s');
    [].forEach.call(q.querySelectorAll('.opt'),function(li){li.classList.remove('no')});
    if(p<0){s++;q.classList.add('s')}else if(p===a){r++;q.classList.add('r')}else{w++;q.classList.add('w');var li=q.querySelectorAll('.opt')[p];if(li)li.classList.add('no')}
    [].forEach.call(q.querySelectorAll('input'),function(i){i.disabled=true})});
  var score=r-w*neg,pct=N?Math.round(r/N*100):0;
  B.classList.add('done','show');countUp($('rs'),score);$('rr').textContent=r;$('rw').textContent=w;$('rk').textContent=s;
  $('rm').textContent=pct>=80?'Excellent. '+pct+'% correct.':pct>=50?'Good effort. '+pct+'% correct.':pct+'% correct. Review the wrong ones below and try again.';
  $('go').textContent='Try again';$('ans').textContent='Hide answers';filter('a');
  $('res').scrollIntoView({behavior:'smooth',block:'start'});
}
function reset(){
  done=false;B.classList.remove('done','show');filter('a');
  qs.forEach(function(q){q.classList.remove('r','w','s');[].forEach.call(q.querySelectorAll('.opt'),function(li){li.classList.remove('no')});
    [].forEach.call(q.querySelectorAll('input'),function(i){i.disabled=false;i.checked=false;i._on=false})});
  $('go').textContent='Check my score';$('ans').textContent='Show answers';save();progress();window.scrollTo(0,0);
}
function filter(f){B.setAttribute('data-f',f);[].forEach.call(document.querySelectorAll('.chip'),function(c){c.classList.toggle('on',c.getAttribute('data-f')===f)})}
try{var old=JSON.parse(localStorage.getItem(KEY)||'[]');qs.forEach(function(q,i){var v=old[i];if(v>=0){var el=q.querySelectorAll('input')[v];if(el){el.checked=true;el._on=true}}})}catch(e){}
qs.forEach(function(q){[].forEach.call(q.querySelectorAll('input'),function(i){i.addEventListener('click',function(){
  if(i._on){i.checked=false;i._on=false}else{[].forEach.call(q.querySelectorAll('input'),function(o){o._on=false});i._on=true}
  save();progress()})})});
$('go').onclick=function(){if(done)return reset();var n=progress();if(n<N&&!confirm((N-n)+' question'+(N-n===1?' is':'s are')+' not answered. Check your score anyway?'))return;check()};
$('ans').onclick=function(){var on=B.classList.toggle('show');this.textContent=on?'Hide answers':'Show answers'};
$('pdf').onclick=function(){window.print()};
[].forEach.call(document.querySelectorAll('.chip'),function(c){c.onclick=function(){filter(c.getAttribute('data-f'))}});
progress();
})();
"""


def _build_printable_html(qs: Dict, include_answers_inline: bool = False, bot_username: str = "") -> bytes:
    """Self-contained HTML exam paper (or solved study sheet).

    Paper mode: a practice test in the file itself. Tap a bubble to mark an answer,
    "Check my score" marks it with the quiz's negative rate and shows what was wrong.
    Printing gives a clean two-column A4 paper with the answer key on its own page.
    Study mode: answers and explanations inline, nothing to tap.
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
    mode_label = "Solved study sheet" if include_answers_inline else "Question paper"
    brand = f"@{esc(bot_username)}" if bot_username else "Miss Quiz"
    paper = not include_answers_inline

    items, keys = [], []
    for i, q in enumerate(questions, 1):
        opts = [str(o) for o in (q.get("options") or [])]
        try:
            cor = int(q.get("correct_answer", 0))
        except (TypeError, ValueError):
            cor = 0
        letter = chr(97 + cor) if 0 <= cor < 26 else "?"
        one_col = any(len(o) > 34 for o in opts)
        lis = []
        for k, o in enumerate(opts):
            inner = (f"<span class=\"bul\">{chr(97 + k)}</span><span class=\"ot\">{esc(o)}</span>")
            body = (f"<label><input type=\"radio\" name=\"q{i}\" value=\"{k}\">{inner}</label>" if paper
                    else f"<span class=\"st\">{inner}</span>")
            lis.append(f"<li class=\"opt{' ok' if k == cor else ''}\">{body}</li>")
        exp = str(q.get("explanation") or "").strip()
        ans_txt = esc(opts[cor]) if 0 <= cor < len(opts) else ""
        sol = (f"<div class=\"sol\"><b>Answer ({letter})</b> {ans_txt}"
               + (f"<div class=\"why\">{esc(exp)}</div>" if exp else "") + "</div>")
        items.append(
            f"<section class=\"q\" id=\"q{i}\" data-a=\"{cor}\"><div class=\"qh\"><span class=\"qn\">{i}.</span>"
            f"<p>{esc(q.get('question'))}<span class=\"tag\"></span></p></div>"
            f"<ol class=\"opts{' one' if one_col else ''}\">{''.join(lis)}</ol>{sol}</section>"
        )
        keys.append(f"<div class=\"k\"><span>{i}</span><b>{letter}</b></div>")

    marks = (f"Each correct answer: <b>+1</b>. Each wrong answer: <b>{neg_txt}</b>."
             if neg > 0 else "Each correct answer: <b>+1</b>. No negative marking.")
    if paper:
        inst = (f"<ul class=\"so\"><li><b>Tap a bubble</b> to mark your answer. Tap it again to clear it.</li>"
                f"<li>{marks}</li><li>Tap <b>Check my score</b> when you are done.</li></ul>"
                f"<ul class=\"po\"><li>Choose the single best option for each question.</li>"
                f"<li>{marks}</li><li>The answer key is on the last page.</li></ul>")
    else:
        inst = f"<ul><li>The correct option is marked in green, with the explanation below it.</li><li>{marks}</li></ul>"
    candidate = ("<div class=\"cand\"><div>Name <i></i></div><div>Roll No. <i></i></div>"
                 f"<div>Score <i class=\"sc\"></i> / {n}</div></div>") if paper else ""
    key_page = (f"<section class=\"key\"><h2>Answer key</h2><div class=\"kg\">{''.join(keys)}</div></section>"
                if paper else "")
    acts = ("<button class=\"pill\" id=\"ans\" type=\"button\">Show answers</button>" if paper else "") + \
           "<button class=\"pill\" id=\"pdf\" type=\"button\" onclick=\"window.print()\">Save as PDF</button>"
    result = ("""<section class="res" id="res" aria-live="polite">
    <div class="n"><span id="rs">0</span><small> / %d</small></div><p id="rm"></p>
    <div class="rf"><div><b id="rr">0</b><span>Correct</span></div><div><b id="rw">0</b><span>Wrong</span></div><div><b id="rk">0</b><span>Skipped</span></div></div>
    <div class="chips"><button class="chip on" data-f="a" type="button">All questions</button><button class="chip" data-f="w" type="button">Wrong only</button><button class="chip" data-f="s" type="button">Skipped only</button></div>
  </section>""" % n) if paper else ""
    dock = ("""<div class="dock"><div><div class="prog"><b id="pn">0</b> of %d answered<div class="track"><i id="pb"></i></div></div>
  <button class="go" id="go" type="button">Check my score</button></div></div>""" % n) if paper else ""
    doc_id = esc(str(qs.get("id") or qs.get("title") or "quiz"))[:80]

    doc_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · {mode_label}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Lexend:wght@400;500;600&display=swap" rel="stylesheet">
<style>{_PAPER_CSS}</style>
</head>
<body class="{'' if paper else 'show'}" data-neg="{neg:g}" data-id="{doc_id}" data-f="a">
<div class="paper">
  <div class="top"><div class="brand"><i></i>{brand}</div><div class="acts">{acts}</div></div>
  <header>
    <p class="kind">{mode_label} · {date_str}</p>
    <h1>{title}</h1>
    <div class="facts">
      <div><b>{n}</b><span>Questions</span></div>
      <div><b>{total_min} min</b><span>Time</span></div>
      <div><b>{n}</b><span>Max marks</span></div>
      <div><b>{neg_txt}</b><span>Negative</span></div>
    </div>
  </header>
  {candidate}
  <div class="inst">{inst}</div>
  {result}
  <main>{''.join(items)}</main>
  {key_page}
  <footer>Made with {brand} · {n} questions</footer>
</div>
{dock}
{f'<script>{_PAPER_JS}</script>' if paper else ''}
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
            body += f"💡 <i>{html.escape(d['note'])}</i>\n"
        if not q_count:
            body += ("<blockquote>1️⃣ Send your questions here — a file (PDF, DOCX, TXT, CSV, XLSX…), "
                     "pasted text, or forwarded quiz polls\n"
                     "2️⃣ I show how many questions I found\n"
                     "3️⃣ Tap Next — name, timer, marking — done!</blockquote>\n<i>Send them now 👇</i>")
        elif q_count < MIN_Q:
            body += f"<i>Add at least {MIN_Q} questions, then tap Next.</i>"
        else:
            body += "<i>Send more, or tap Next.</i>"

        # One clear action at a time: Save when there is something to save, else only Cancel.
        rows: List[List[InlineKeyboardButton]] = []
        if q_count >= MIN_Q:
            rows.append([_btn(f"➡️ Next: Set up quiz ({q_count} Qs)", "mq_done", "success")])
        rows.append([_btn("❌ Cancel", "mq_cancel", "danger")])
        return body, InlineKeyboardMarkup(rows)

    # ── setup steps after questions are ready (one message, edited) ──────

    WZ_TIMERS = (10, 15, 20, 30, 45, 60)
    WZ_NEG = (0.25, 0.33, 0.5)

    @staticmethod
    def _mq_wizard_view(d: Dict, step: str) -> Tuple[str, InlineKeyboardMarkup]:
        n = len(d["qs"])
        title = d.get("title") or d.get("default") or "My Quiz"
        secs, neg = int(d.get("secs") or 20), float(d.get("neg") or 0)
        summary = (f"<blockquote>✅ Questions › {n}\n🏷 Name › {html.escape(title)}"
                   + (f"\n⏱ Timer › {secs}s" if step in ("neg", "shuf") else "")
                   + (f"\n⚖️ Negative › {'−' + format(neg, 'g') if neg else 'OFF'}" if step == "shuf" else "")
                   + "</blockquote>\n")
        back_cancel = [_btn("⬅️ Back", f"mq_wb:{step}", "danger"), _btn("❌ Cancel", "mq_cancel", "danger")]
        if step == "name":
            text = (f"🏷 <b>STEP 1/4 · NAME</b>\n<blockquote>✅ Questions › {n}</blockquote>\n"
                    f"Suggested › <b>{html.escape(title)}</b>\n<i>Tap to use it, or type a new name in chat.</i>")
            rows = [[_btn(f"✅ Use “{title[:28]}”", "mq_name", "success")],
                    [_btn("⬅️ Add more", "mq_back", "danger"), _btn("❌ Cancel", "mq_cancel", "danger")]]
        elif step == "timer":
            text = ("⏱ <b>STEP 2/4 · TIMER</b>\n" + summary +
                    "<i>Time for each question (20s suits most quizzes).</i>")
            opts = [_btn(("✅ " if t == secs else "") + (f"{t}s" if t < 60 else "1m"), f"mq_wt:{t}", None)
                    for t in MyQuizMixin.WZ_TIMERS]
            rows = [opts[:3], opts[3:], back_cancel]
        elif step == "neg":
            text = ("⚖️ <b>STEP 3/4 · NEGATIVE MARKING</b>\n" + summary +
                    "<i>Marks cut for each wrong answer. Most quizzes use none.</i>")
            rows = [[_btn("✅ No negative" if not neg else "No negative", "mq_wn:0", "success")],
                    [_btn(("✅ " if neg == v else "") + f"−{v:g}", f"mq_wn:{v}", None) for v in MyQuizMixin.WZ_NEG],
                    back_cancel]
        else:
            text = ("🔀 <b>STEP 4/4 · ORDER</b>\n" + summary +
                    "<i>Shuffle = every game asks the questions in a new order (stops copying).</i>")
            rows = [[_btn("🔀 Shuffle questions & Save", "mq_ws:1", "success")],
                    [_btn("📋 Keep file order & Save", "mq_ws:0", "primary")],
                    back_cancel]
        return text, InlineKeyboardMarkup(rows)

    @staticmethod
    def _mq_list_view(sets: List[Dict], page: int, pages: int, total: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        from src.bot.battle import _tlabel
        head = ("📚 <b>YOUR QUIZZES</b>" + (f" · Page {page}/{pages}" if pages > 1 else "") + f"  ·  Total: <b>{total or len(sets)}</b>\n")
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
        foot = [_btn("🏠 Home", "nav_home", "primary"), _btn("➕ Create New", "mq_new", "success")]   # main action on the right
        return body, InlineKeyboardMarkup(item_rows + nav_rows + [foot])

    async def _mq_page(self, owner: int, page: int = 1):
        call = lambda fn: (lambda *a: self._mq_db(fn, *a))
        return await load_page(call("list_quiz_sets"), call("count_quiz_sets"), owner, page)

    @staticmethod
    def _mq_card_view(qs: Dict, saved: bool = False, bot_username: str = "", is_owner: bool = True,
                      private: bool = False):
        sid = str(qs.get("id", ""))
        title = html.escape(str(qs.get("title", "Quiz")))
        q_count = int(qs.get("count") or len(qs.get("questions") or []))
        secs = int(qs.get("secs") or 20)
        total_time = _fmt_total_time(q_count * secs)
        shuffle_str = "ON" if qs.get("shuffle") else "OFF"
        neg_val = float(qs.get("negative_rate") or 0)
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
                _btn("▶️ Play Solo (Quiz)", f"mq_prac:{sid}", "success"),
                InlineKeyboardButton("👥 Host in Group", style="success",
                                     url=f"https://t.me/{bot_username}?startgroup=gq_{sid}"),
            ])
        else:
            rows.append([_btn("▶️ Play Solo (Quiz)", f"mq_prac:{sid}", "success")])

        # Mini App player (web_app buttons only work in private chats; off unless MINI_APP_URL is set)
        app_base = os.environ.get("MINI_APP_URL", "").strip().rstrip("/")
        if private and app_base.startswith("https://"):
            rows.append([InlineKeyboardButton(
                "📱 Open in App", web_app=WebAppInfo(url=f"{app_base}/app?sid={urllib.parse.quote(sid)}"))])

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
        text, kb = self._mq_card_view(qs, bot_username=context.bot.username, is_owner=is_owner,
                                      private=_is_private(update))
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
                d["title"] = " ".join(plain.split())[:60]
                d["stage"] = "wizard"
                await self._mq_show(context, d, *self._mq_wizard_view(d, "timer"))
                return
            d["stage"] = "collect"

        # Custom count typed after "✏️ Custom Number" on the file picker
        if d["stage"] in ("pick", "pick_custom") and d.get("pending"):
            plain = (msg.text or "").strip().replace(",", "")
            if plain.isdigit():
                n = int(plain)
                total = len(d["pending"])
                if n < 1:
                    await self._mq_show(context, d, *self._mq_pick_view(d, f"⚠️ Enter a number between 1 and {total}."))
                    return
                self._mq_apply_pick(d, min(n, total))
                await self._mq_show(context, d, *self._mq_collect_view(d))
                return
            if d["stage"] == "pick_custom" and not (msg.document or msg.photo or msg.poll):
                await self._mq_show(context, d, *self._mq_pick_view(
                    d, f"⚠️ Send only a number (1–{len(d['pending'])}), e.g. 25"))
                return
            d["stage"] = "collect"

        # A new file while an earlier pick is still open → add the earlier one fully
        if d.get("pending"):
            self._mq_apply_pick(d, len(d["pending"]))

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
            if (not d.get("fname") or not d.get("qs")) and qs and msg.document and f_entry["kind"] == "document":
                d["fname"] = f_entry["file_name"].rsplit(".", 1)[0].replace("_", " ")[:40]

        # New, non-duplicate questions in file order
        fresh, local_seen = [], set()
        for q in qs:
            key = q["question"].lower()
            if key in d["seen"] or key in local_seen:
                continue
            local_seen.add(key)
            fresh.append(q)
        d["problems"] = (d["problems"] + probs)[-50:]

        # File with more than 10 questions → let the user choose how many to add
        if (msg.document or msg.photo) and len(fresh) > 10:
            room = MAX_Q_PER_SET - len(d["qs"])
            d["pending"] = fresh[:max(0, room)]
            d["pending_src"] = (f_entry or {}).get("file_name") or "file"
            d["stage"] = "pick"
            d["note"] = ""
            await self._mq_show(context, d, *self._mq_pick_view(d))
            return

        added = 0
        for q in fresh:
            if len(d["qs"]) >= MAX_Q_PER_SET:
                note = f"A quiz can have up to {MAX_Q_PER_SET} questions — the rest were skipped."
                break
            d["seen"].add(q["question"].lower())
            d["qs"].append(q)
            added += 1
        if not added and not note:
            note = ("No new questions in that (already added)." if qs else
                    "No questions found in that. Use a layout like the example below.")
        d["note"] = (f"Cleaned & Added: +{added}. " if added and (msg.document or msg.photo or msg.poll) else "") + note
        text_out, kb = self._mq_collect_view(d)
        if note.startswith("No questions found"):
            text_out += "\n" + EXAMPLE
        await self._mq_show(context, d, text_out, kb)

    # ── file question-count picker (10 / 20 / 30 / 50 / 100 / custom / full) ──

    @staticmethod
    def _mq_pick_view(d: Dict, warn: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        total = len(d.get("pending") or [])
        src = html.escape(str(d.get("pending_src") or "file"))
        custom = d.get("stage") == "pick_custom"
        body = (
            "📄 <b>FILE READ SUCCESSFULLY</b>\n"
            f"<blockquote>📎 File › <b>{src}</b>\n"
            f"✅ Questions found › <b>{total} Qs</b>"
            + (f"\n📝 Already in quiz › <b>{len(d['qs'])} Qs</b>" if d.get("qs") else "")
            + "</blockquote>\n"
        )
        if warn:
            body += f"{html.escape(warn)}\n\n"
        if custom:
            body += f"✏️ <b>Type how many questions to add</b> (1–{total}):\n<i>Example: 25</i>"
            rows = [[_btn(f"📥 Full File · All {total} Qs", "mq_pick:0", "success")],
                    [_btn("❌ Discard File", "mq_pickx", "danger"), _btn("⬅️ Back", "mq_pickb", None)]]
            return body, InlineKeyboardMarkup(rows)
        body += "<i>How many questions do you want to add? (taken in file order)</i>"
        opts = [n for n in (10, 20, 30, 50, 100) if n < total]
        btns = [_btn(f"{n} Qs", f"mq_pick:{n}", None) for n in opts]          # plain: just choices
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([_btn(f"📥 Full File · All {total} Qs", "mq_pick:0", "success")])
        rows.append([_btn("❌ Discard File", "mq_pickx", "danger"), _btn("✏️ Custom Number", "mq_pickc", None)])
        return body, InlineKeyboardMarkup(rows)

    @staticmethod
    def _mq_apply_pick(d: Dict, n: int) -> int:
        pending = list(d.get("pending") or [])
        n = len(pending) if n <= 0 else min(n, len(pending))
        added = 0
        for q in pending[:n]:
            if len(d["qs"]) >= MAX_Q_PER_SET:
                break
            key = q["question"].lower()
            if key in d["seen"]:
                continue
            d["seen"].add(key)
            d["qs"].append(q)
            added += 1
        d.pop("pending", None)
        d.pop("pending_src", None)
        d["stage"] = "collect"
        d["note"] = f"Added {added} of {len(pending)} questions from the file."
        return added

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
        for k in ("mq_renaming", "mq_attaching_file", "mq_edit_q", "mq_adding"):
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
            # calm: plain options; red only for Delete and Back
            [_btn("📝 Edit Questions", f"mq_ql:{sid}:1", None),
             _btn("➕ Add Questions", f"mq_add:{sid}", None)],
            [_btn("🏷 Rename", f"mq_rename:{sid}", None),
             _btn("⏱ Timer", f"mq_timer:{sid}", None)],
            [_btn("🔀 Shuffle: ON" if shuffle_on else "🔀 Shuffle: OFF", f"mq_shuffle:{sid}", None),
             _btn("📎 Attach File", f"mq_attach:{sid}", None)],
            *([[_btn("🔄 Re-read file (fix questions)", f"mq_reread:{sid}", None)]] if files_cnt else []),
            [_btn("🗑 Delete Quiz", f"mq_del:{sid}", "danger")],
            [_btn("⬅️ Back to Quiz", f"mq_open:{sid}", "danger"),
             _btn("🏠 Home", "nav_home", None)],
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
        rows.append([_btn("⬅️ Back", f"mq_edit:{sid}", "danger"),
                     _btn("➕ Add Questions", f"mq_add:{sid}", "success")])
        text = (
            "📝  <b>EDIT QUESTIONS</b>" + (f"  ·  Page {page}/{pages}" if pages > 1 else "") + "\n"
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
            [_btn("⬅️ Back", f"mq_edit:{sid}", "danger"),
             _btn("📋 All Questions", f"mq_ql:{sid}:{idx // 8 + 1}", None)],
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
        secs, neg, shuf = int(d.get("secs") or 20), float(d.get("neg") or 0), bool(d.get("shuffle", False))
        sid = await self._mq_db("create_quiz_set", user.id, title[:60], qs_list, secs)
        if not isinstance(sid, str) or not sid:
            sid = secrets.token_hex(4)
        extra = {"negative_rate": neg, "shuffle": shuf}
        if files_list:
            extra["source_files"] = files_list
        if self.db and hasattr(self.db, "update_quiz_set"):
            await self._mq_db("update_quiz_set", sid, user.id, extra)
        now_iso = datetime.datetime.utcnow().isoformat()
        doc = {
            "id": sid,
            "owner": user.id,
            "title": title[:60],
            "count": len(qs_list),
            "questions": qs_list,
            "secs": secs,
            "plays": 0,
            "shuffle": shuf,
            "negative_rate": neg,
            "source_files": files_list,
            "created_at": now_iso,
        }
        logger.info(f"[MYQUIZ] saved {sid} '{title}' ({len(qs_list)} Q, {len(files_list)} files) for {user.id}")
        card_text, kb = self._mq_card_view(doc, saved=True, bot_username=context.bot.username, is_owner=True,
                                           private=True)  # the create wizard runs only in DM
        await self._mq_show(context, d, card_text, kb)

    # ── callbacks ────────────────────────────────────────────────────────

    @staticmethod
    async def _mq_open_dm(query, context, param: str) -> None:
        """From a group button: open the bot's DM at /start <param> directly
        (answerCallbackQuery url) — nothing is posted in the group."""
        bot_user = getattr(context.bot, "username", "") or ""
        try:
            await query.answer(url=f"https://t.me/{bot_user}?start={param}")
        except Exception:
            await query.answer("Open my DM to continue 📚", show_alert=True)

    async def _mq_reread_files(self, context, qs: Dict) -> Tuple[List[Dict], List[Dict]]:
        """Download the quiz's own source files again and parse them with the current reader."""
        from src.bot.file_extract import extract
        out, problems, seen = [], [], set()
        for f in qs.get("source_files") or []:
            if not f.get("file_id"):
                continue
            try:
                tg_file = await context.bot.get_file(f["file_id"], read_timeout=60)
                data = bytes(await tg_file.download_as_bytearray(read_timeout=60))
                items, text, _note = await asyncio.to_thread(extract, f.get("file_name") or "file", data)
                got, probs = await asyncio.to_thread(parse_questions, text, items)
            except Exception as e:
                logger.warning(f"[MYQUIZ] re-read {f.get('file_name')} failed: {e}")
                continue
            problems += probs
            for q in got:
                k = q["question"].lower()
                if k not in seen:
                    seen.add(k)
                    out.append(q)
        return out, problems

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
            sid = arg
            prac = context.user_data.get("mq_prac")
            if not prac or (sid and prac.get("set_id") != sid):
                qs = await self._mq_db("get_quiz_set", sid)
                if isinstance(qs, dict) and qs.get("questions"):
                    prac = {
                        "set_id": sid,
                        "title": qs.get("title", "Practice"),
                        "questions": list(qs.get("questions") or []),
                        "idx": 0,
                        "correct": 0,
                        "wrong": 0,
                    }
                    context.user_data["mq_prac"] = prac
            if prac:
                await query.answer()
                await self._mq_practice_show_q(query, context, prac)
                return
            await query.answer("Quiz session expired. Reopen from My Quizzes.", show_alert=True)
            return

        if action == "mq_stop_solo":
            stopped = self._mq_stop_solo(user.id)
            if stopped:
                await query.answer("Quiz stopped ⏹")
            else:
                await query.answer("No active quiz to stop.")
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

        if action in ("mq_pick", "mq_pickc", "mq_pickb", "mq_pickx"):
            if not d or not d.get("pending"):
                await query.answer("This file selection has ended.", show_alert=True)
                return
            d["msg"] = query.message.message_id
            if action == "mq_pickc":
                d["stage"] = "pick_custom"
                await query.answer("Type a number in chat ✏️")
                await self._bt_edit(query, *self._mq_pick_view(d))
                return
            if action == "mq_pickb":
                d["stage"] = "pick"
                await query.answer()
                await self._bt_edit(query, *self._mq_pick_view(d))
                return
            if action == "mq_pickx":
                d.pop("pending", None)
                d.pop("pending_src", None)
                d["stage"] = "collect"
                d["note"] = "File questions discarded."
                await query.answer("Discarded.")
                await self._bt_edit(query, *self._mq_collect_view(d))
                return
            n = int(arg) if arg.isdigit() else 0
            added = self._mq_apply_pick(d, n)
            await query.answer(f"✅ Added {added} questions")
            await self._bt_edit(query, *self._mq_collect_view(d))
            return

        if action in ("mq_done", "mq_qsave", "mq_name", "mq_cancel", "mq_back", "mq_wt", "mq_wn", "mq_ws", "mq_wb"):
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
                d.setdefault("secs", 20)
                d.setdefault("neg", 0.0)
                d["msg"] = query.message.message_id
                await query.answer()
                await self._bt_edit(query, *self._mq_wizard_view(d, "name"))
                return
            d["msg"] = query.message.message_id
            if action == "mq_name":                      # suggested name → timer
                d["title"] = d.get("title") or d.get("default") or "My Quiz"
                d["stage"] = "wizard"
                await query.answer()
                await self._bt_edit(query, *self._mq_wizard_view(d, "timer"))
                return
            if action == "mq_wt":
                d["secs"] = max(5, min(int(arg) if arg.isdigit() else 20, 600))
                await query.answer()
                await self._bt_edit(query, *self._mq_wizard_view(d, "neg"))
                return
            if action == "mq_wn":
                try:
                    d["neg"] = max(0.0, min(float(arg), 1.0))
                except ValueError:
                    d["neg"] = 0.0
                await query.answer()
                await self._bt_edit(query, *self._mq_wizard_view(d, "shuf"))
                return
            if action == "mq_ws":
                d["shuffle"] = arg == "1"
                await query.answer("Saving quiz… ✅")
                await self._mq_save(context, user, d, d.get("title") or d.get("default") or "My Quiz")
                return
            if action == "mq_wb":                        # back one step
                prev = {"timer": "name", "neg": "timer", "shuf": "neg"}.get(arg, "name")
                d["stage"] = "name" if prev == "name" else "wizard"
                await query.answer()
                await self._bt_edit(query, *self._mq_wizard_view(d, prev))
                return
            await query.answer()
            return

        if action == "mq_list":
            self._mq_clear_modes(context)
            if query.message and query.message.chat and query.message.chat.type != "private":
                # group: open my DM straight away — no new message in the group chat
                await self._mq_open_dm(query, context, "mq")
                return
            await query.answer()
            await self.mq_open_list(update, context, query, int(arg) if arg.isdigit() else 1)
            return

        if action == "mq_new":
            self._mq_clear_modes(context)
            if query.message and query.message.chat and query.message.chat.type != "private":
                await self._mq_open_dm(query, context, "mq_new")
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
                              "mq_qdok", "mq_add", "mq_adddone", "mq_reread", "mq_rereadok"}
        if action in owner_only_actions and not is_owner:
            await query.answer("⛔ Only the creator of this quiz can edit or delete it.", show_alert=True)
            return
        x = query.data.split(":")       # x[1] = sid, x[2:] = extra args
        num = lambda i, dflt=0: int(x[i]) if len(x) > i and x[i].lstrip("-").isdigit() else dflt
        questions = list(qs.get("questions") or [])

        if action == "mq_open":
            self._mq_clear_modes(context)
            await query.answer()
            await self._bt_edit(query, *self._mq_card_view(qs, bot_username=context.bot.username, is_owner=is_owner,
                                                           private=_is_private(update)))

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

        elif action == "mq_reread":
            names = ", ".join(html.escape(str(f.get("file_name") or "file"))
                              for f in (qs.get("source_files") or []) if f.get("kind") != "photo")[:200]
            await query.answer()
            await self._bt_edit(
                query,
                f"🔄 <b>RE-READ FILE</b>\n<blockquote>📎 {names or 'attached file'}\n"
                f"❓ Questions now › {len(questions)}</blockquote>\n"
                f"<i>The file is read again with the improved reader and the questions are replaced. "
                f"Quiz ID, link, plays and group progress stay. Your manual question edits are replaced too.</i>",
                InlineKeyboardMarkup([[_btn("❌ Cancel", f"mq_edit:{sid}", "danger"),
                                       _btn("🔄 Re-read now", f"mq_rereadok:{sid}", "success")]]))

        elif action == "mq_rereadok":
            await query.answer("Reading the file… ⏳")
            new_qs, problems = await self._mq_reread_files(context, qs)
            if not new_qs:
                await self._bt_edit(query, "⚠️ <b>Couldn't read questions from the attached file.</b>\n"
                                           "<i>Nothing was changed.</i>",
                                    InlineKeyboardMarkup([[_btn("⬅️ Back", f"mq_edit:{sid}", "danger")]]))
                return
            old_n = len(questions)
            if 0 < old_n < 0.9 * len(new_qs):          # quiz was made from part of the file → keep that size
                new_qs = new_qs[:old_n]
            await self._mq_db("update_quiz_set", sid, user.id, {"questions": new_qs, "count": len(new_qs)})
            await self._bt_edit(
                query,
                f"✅ <b>File re-read!</b>\n<blockquote>❓ Questions › {old_n} → {len(new_qs)}\n"
                f"⚠️ Skipped › {len(problems)}</blockquote>",
                InlineKeyboardMarkup([[_btn("⬅️ Back", f"mq_edit:{sid}", "danger"),
                                       _btn("▶️ Open Quiz", f"mq_open:{sid}", "success")]]))

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

    def _mq_active_solo_map(self) -> Dict[int, SoloQuizSession]:
        if not hasattr(self, "_mq_active_solo"):
            self._mq_active_solo: Dict[int, SoloQuizSession] = {}
        return self._mq_active_solo

    def _mq_solo_polls_map(self) -> Dict[str, SoloQuizSession]:
        if not hasattr(self, "_mq_solo_polls"):
            self._mq_solo_polls: Dict[str, SoloQuizSession] = {}
        return self._mq_solo_polls

    def mq_on_poll_answer(self, poll_id: str, user, option_ids) -> bool:
        session = self._mq_solo_polls_map().get(poll_id)
        if not session or session.status != "running":
            return False
        return session.record_answer(poll_id, user.id, list(option_ids or []))

    def _mq_stop_solo(self, user_id: int) -> bool:
        session = self._mq_active_solo_map().get(user_id)
        if session and session.status == "running":
            session.status = "stopped"
            session.wake.set()
            return True
        return False

    async def _mq_practice_start(self, query, context, user, qs: Dict):
        questions = list(qs.get("questions") or [])
        if not questions:
            await query.answer("This quiz has no questions.", show_alert=True)
            return

        old = self._mq_active_solo_map().get(user.id)
        if old and old.status == "running":
            old.status = "stopped"
            old.wake.set()

        s = SoloQuizSession(user.id, qs)
        self._mq_active_solo_map()[user.id] = s
        await query.answer("Starting Telegram Quiz! 🚀")

        task = asyncio.create_task(self._mq_run_solo_poll_quiz(s, context))
        s.task = task

    async def _mq_run_solo_poll_quiz(self, s: SoloQuizSession, context: ContextTypes.DEFAULT_TYPE):
        user_id = s.user_id
        total = len(s.questions)
        try:
            stop_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏹ Stop Quiz", callback_data=f"mq_stop_solo:{s.set_id}", style="danger")]
            ])
            intro = (
                f"🚀 <b>Starting Telegram Quiz!</b>\n"
                f"<blockquote><b>📝 {html.escape(s.title)}\n"
                f"• Questions › {total} Qs\n"
                f"• Timer     › {s.secs} sec per question\n"
                f"• Format    › Native Telegram Quiz Poll</b></blockquote>\n"
                f"<i>Answer each question before the timer runs out!</i>"
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=intro,
                parse_mode=ParseMode.HTML,
                reply_markup=stop_kb
            )
            await asyncio.sleep(1.0)

            for i in range(total):
                if s.status != "running":
                    break

                q = s.questions[i]
                s.idx = i + 1
                s.wake.clear()

                opts = [str(o)[:100] for o in q.get("options") or []][:10]
                if len(opts) < 2:
                    continue
                correct = int(q.get("correct_answer", 0))
                if not 0 <= correct < len(opts):
                    correct = 0
                s.current_correct = correct

                q_text = str(q.get("question") or "").strip()
                cat = q.get("category") or ""
                cat_e = UI.cat_emoji(cat) if cat else "💡"
                head = f"[{i+1}/{total}] {cat_e} "
                if len(head) + len(q_text) > 295:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"❓ <b>Question {i+1}/{total} {cat_e}</b>\n\n{html.escape(q_text)}",
                        parse_mode=ParseMode.HTML,
                    )
                    poll_q = f"[{i+1}/{total}] {cat_e} Read question above ⬆️"
                else:
                    poll_q = head + q_text

                exp = str(q.get("explanation") or "").strip()
                kw = dict(
                    chat_id=user_id,
                    question=poll_q,
                    options=opts,
                    type=Poll.QUIZ,
                    correct_option_id=correct,
                    is_anonymous=False,
                    open_period=s.secs,
                )
                if exp:
                    kw["explanation"] = exp[:200]

                try:
                    poll_msg = await context.bot.send_poll(**kw)
                except Exception as e:
                    logger.warning(f"[MYQUIZ] Solo poll send failed: {e}")
                    await context.bot.send_message(chat_id=user_id, text=f"⚠️ Failed to send question {i+1}: {e}")
                    break

                pid = str(poll_msg.poll.id)
                s.poll_id = pid
                self._mq_solo_polls_map()[pid] = s

                answered = False
                try:
                    await asyncio.wait_for(s.wake.wait(), timeout=s.secs + 1.0)
                    answered = True
                except asyncio.TimeoutError:
                    pass

                self._mq_solo_polls_map().pop(pid, None)

                if s.status != "running":
                    break

                if answered:
                    await asyncio.sleep(2.0)
                else:
                    s.skipped += 1
                    await asyncio.sleep(1.0)

            if s.status == "stopped":
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⏹ <b>Quiz Stopped.</b> Completed {max(0, s.idx-1)} of {total} questions.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔁 Play Again", callback_data=f"mq_prac:{s.set_id}", style="success"),
                         InlineKeyboardButton("📚 My Quizzes", callback_data="mq_list", style="primary")]
                    ])
                )
            else:
                elapsed = int(time.monotonic() - s.start_time)
                mins, secs = divmod(elapsed, 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                acc = round(100 * s.correct / total) if total else 0

                await self._mq_db("inc_quiz_set_plays", s.set_id, s.correct, total)

                res_text = (
                    f"🎉 <b>QUIZ COMPLETED!</b>\n"
                    f"<blockquote><b>📝 {html.escape(s.title)}\n"
                    f"• Score    › {s.correct} / {total} ✅\n"
                    f"• Wrong    › {s.wrong} ❌\n"
                    f"• Skipped  › {s.skipped} ⏱\n"
                    f"• Accuracy › {acc}% 📊\n"
                    f"• Time     › {time_str} ⏱</b></blockquote>\n"
                    f"<i>Great session! Tap below to play again or return to quizzes.</i>"
                )
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Play Again", callback_data=f"mq_prac:{s.set_id}", style="success")],
                    [InlineKeyboardButton("⬅️ Back to Quiz", callback_data=f"mq_open:{s.set_id}", style="danger"),
                     InlineKeyboardButton("📚 My Quizzes", callback_data="mq_list", style="primary")],
                ])
                await context.bot.send_message(
                    chat_id=user_id,
                    text=res_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        except Exception as e:
            logger.error(f"[MYQUIZ] Solo quiz crashed: {e}", exc_info=True)
        finally:
            if self._mq_active_solo_map().get(user_id) is s:
                self._mq_active_solo_map().pop(user_id, None)

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
            label = f"{chr(65+i)}) {opt[:60]}"
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
        if not prac or prac.get("set_id") != set_id:
            qs = await self._mq_db("get_quiz_set", set_id)
            if isinstance(qs, dict) and qs.get("questions"):
                prac = {
                    "set_id": set_id,
                    "title": qs.get("title", "Practice"),
                    "questions": list(qs.get("questions") or []),
                    "idx": idx,
                    "correct": 0,
                    "wrong": 0,
                }
                context.user_data["mq_prac"] = prac
            else:
                await query.answer("Quiz session expired or not found.", show_alert=True)
                return

        if prac.get("idx") != idx:
            await query.answer("Already answered! Click Next Question ➡️", show_alert=True)
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

        opts_display = []
        for i, opt in enumerate(opts):
            letter = chr(65 + i)
            esc_opt = html.escape(opt)
            if i == correct and i == chosen:
                opts_display.append(f"<b>{letter}) {esc_opt}</b>  ✅ <i>(Your Correct Answer)</i>")
            elif i == correct:
                opts_display.append(f"<b>{letter}) {esc_opt}</b>  ✅ <i>(Correct Answer)</i>")
            elif i == chosen:
                opts_display.append(f"<b>{letter}) {esc_opt}</b>  ❌ <i>(Your Choice)</i>")
            else:
                opts_display.append(f"{letter}) {esc_opt}")

        q_text = html.escape(str(q.get("question") or ""))
        options_text = "\n".join(opts_display)
        head = f"▶️  <b>SOLO PRACTICE  ·  Q{idx+1} of {total}</b>\n<blockquote><b>📝 {html.escape(prac['title'])}</b></blockquote>\n"
        text = (
            f"{head}"
            f"❓ <b>{q_text}</b>\n\n"
            f"{options_text}\n\n"
            f"{exp_note}"
        )
        await query.answer("Correct! 🎉" if is_right else "Wrong! ❌")
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            try:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception as e:
                logger.error(f"[MYQUIZ] Failed to edit or send practice answer: {e}")

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
