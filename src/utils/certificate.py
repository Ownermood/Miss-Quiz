"""
Winner certificate (Pillow) for the Top 3 of a Group Quiz — Miss Quiz Bot.

Design: a night-ink "answer sheet". A big OMR bubble holds the rank in a metal
ring (gold / silver / bronze), the winner's name is set large in Fraunces, and an
OMR strip at the bottom is filled as far as the player's accuracy.

Fonts are bundled in assets/fonts (SIL OFL). If they are missing it falls back to
system DejaVu so it still renders on any VPS. Never raises on odd names:
fancy Unicode letters (𝐁𝐨𝐥𝐝, 𝙼𝚘𝚗𝚘, ꜱᴍᴀʟʟ ᴄᴀᴘꜱ) are folded to plain letters,
Hindi names use Noto Sans Devanagari, emoji-only names fall back to a symbol font.
"""

import io
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_SYS_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"]
_SYS_REG = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/arial.ttf"]

S = 2                      # supersample factor (drawn at 2x, downscaled → smooth circles)
W, H = 1600, 1000          # output size

INK = (16, 22, 74)
TEXT = (244, 245, 251)
MUTED = (166, 172, 214)
OMR = (231, 128, 154)

THEMES = {
    1: {"metal": (240, 189, 79), "glow": (240, 189, 79), "place": "First place", "verb": "first", "sfx": "st"},
    2: {"metal": (206, 214, 232), "glow": (150, 170, 230), "place": "Second place", "verb": "second", "sfx": "nd"},
    3: {"metal": (222, 145, 92), "glow": (222, 120, 80), "place": "Third place", "verb": "third", "sfx": "rd"},
}

_SMALLCAPS = str.maketrans("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡʏᴢ", "ABCDEFGHIJKLMNOPQRSTUVWYZ")
_font_cache: dict = {}


# ── fonts ────────────────────────────────────────────────────────────────────

def _sys(paths, size):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _font(name: str, size: int, **axes):
    """Bundled variable font at a size with axis values (e.g. Weight=700)."""
    key = (name, size, tuple(sorted(axes.items())))
    if key in _font_cache:
        return _font_cache[key]
    f = None
    try:
        f = ImageFont.truetype(str(_FONT_DIR / f"{name}.ttf"), size * S)
        if axes:
            vals = []
            for a in f.get_variation_axes():
                nm = a["name"].decode() if isinstance(a["name"], bytes) else str(a["name"])
                v = axes.get(nm.replace(" ", ""), a.get("default", a["minimum"]))
                vals.append(max(a["minimum"], min(a["maximum"], v)))
            f.set_variation_by_axes(vals)
    except Exception:
        f = _sys(_SYS_BOLD if axes.get("Weight", 400) >= 600 else _SYS_REG, size * S)
    _font_cache[key] = f
    return f


def _symbol_font(size):
    return _sys(_SYS_BOLD, size * S)


def _has_glyph(font, ch: str) -> bool:
    try:
        notdef = font.getmask("\uffff").tobytes()
        return ch.isspace() or font.getmask(ch).tobytes() != notdef
    except Exception:
        return True


# ── text cleaning ────────────────────────────────────────────────────────────

def _clean_render_text(text, fallback: str = "") -> str:
    """Fold fancy Unicode letters to plain ones and drop emoji / control chars."""
    t = unicodedata.normalize("NFKC", str(text or "")).translate(_SMALLCAPS)
    out = []
    for ch in t:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cp > 0xFFFF or 0xFE00 <= cp <= 0xFE0F or cat in ("Cc", "Cf", "Co", "Cs", "Cn"):
            continue
        if 0x2600 <= cp <= 0x27BF and not (0x2660 <= cp <= 0x2667):   # misc emoji-ish symbols (keep ♠♡♢♣)
            continue
        out.append(ch)
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    return cleaned or fallback


def _is_deva(s: str) -> bool:
    return any(0x0900 <= ord(c) <= 0x097F for c in s)


def _fit_name(name: str, max_w: int):
    """Pick font for the name (Latin serif / Devanagari / symbol) and shrink to fit."""
    for size in range(124, 54, -4):
        if _is_deva(name):
            f = _font("NotoSansDevanagari", int(size * .82), Weight=700)
        else:
            f = _font("Fraunces", size, Weight=650, OpticalSize=144, Softness=60, Wonky=1)
        txt = "".join(c for c in name if _has_glyph(f, c)).strip()
        if not any(c.isalnum() for c in txt):
            f, txt = _symbol_font(int(size * .8)), name
        if f.getlength(txt) <= max_w * S:
            return f, txt
    while txt and f.getlength(txt + "…") > max_w * S:
        txt = txt[:-1]
    return f, txt.rstrip() + "…"


def _wrap(text: str, font, max_w: int, max_lines: int = 2):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w * S or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and font.getlength(last + "…") > max_w * S:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _fmt_time(secs: float) -> str:
    secs = max(0, int(round(float(secs or 0))))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    if m:
        return f"{m}:{s:02d}"
    return f"{s}s"


def _fmt_num(x: float) -> str:
    x = round(float(x), 2)
    return str(int(x)) if x == int(x) else f"{x:.2f}".rstrip("0").rstrip(".")


# ── drawing helpers (logical coords, scaled by S) ────────────────────────────

def _c(*v):
    return [int(round(a * S)) for a in v]


def _circle(d, cx, cy, r, **kw):
    if "width" in kw:
        kw["width"] = max(1, int(round(kw["width"] * S)))
    d.ellipse(_c(cx - r, cy - r, cx + r, cy + r), **kw)


def _text(d, xy, s, font, fill, anchor="la"):
    d.text(_c(*xy), s, font=font, fill=fill, anchor=anchor)


def generate_certificate_image(
    player_name: str,
    quiz_title: str,
    group_title: str,
    host_name: str,
    rank_pos: int,            # 1, 2, or 3
    correct: int,
    total_q: int,
    score_pts: float,
    time_secs: float,
    bot_username: str = "MissQuizBot",
) -> bytes:
    """Render a 1600x1000 PNG certificate for rank 1, 2 or 3."""
    th = THEMES.get(rank_pos, THEMES[1])
    metal = th["metal"]
    total_q = max(1, int(total_q))
    correct = max(0, min(int(correct), total_q))
    acc = correct / total_q

    img = Image.new("RGB", (W * S, H * S), INK)

    # glow behind the big bubble + faint answer-sheet grid
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    _circle(gd, 400, 500, 330, fill=th["glow"] + (70,))
    glow = glow.filter(ImageFilter.GaussianBlur(140 * S))
    img.paste(glow, (0, 0), glow)

    grid = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gr = ImageDraw.Draw(grid)
    for gy in range(22, H, 44):
        for gx in range(22 + (22 if (gy // 44) % 2 else 0), W, 44):
            _circle(gr, gx, gy, 9, outline=(255, 255, 255, 13), width=1.4)
    img.paste(grid, (0, 0), grid)
    d = ImageDraw.Draw(img)

    # ── top bar: brand + label
    _circle(d, 76, 76, 15, fill=(58, 79, 224))
    _circle(d, 76, 76, 19, outline=OMR, width=2.5)
    _text(d, (106, 76), "Miss Quiz", _font("Lexend", 30, Weight=600), TEXT, "lm")
    _text(d, (W - 64, 76), "Group Quiz · Certificate of achievement", _font("Lexend", 21, Weight=400), MUTED, "rm")

    # ── left: the rank bubble
    bx, by, br = 400, 510, 240
    for k in range(48):                          # ring of tiny answer bubbles around it
        a = k / 48 * math.tau - math.pi / 2
        px, py = bx + math.cos(a) * (br + 46), by + math.sin(a) * (br + 46)
        filled = k < round(acc * 48)
        _circle(d, px, py, 6.5, fill=metal if filled else None, outline=metal if filled else OMR, width=1.6)
    _circle(d, bx, by, br, fill=tuple(int(c * .16 + i * .84) for c, i in zip(metal, INK)))
    _circle(d, bx, by, br, outline=metal, width=12)
    num_f = _font("Fraunces", 330, Weight=800, OpticalSize=144, Softness=100, Wonky=1)
    sfx_f = _font("Fraunces-Italic", 64, Weight=500, OpticalSize=72, Softness=100)
    num = str(rank_pos if rank_pos in THEMES else 1)
    nw = num_f.getlength(num) / S
    _text(d, (bx - 18, by + 8), num, num_f, metal, "mm")
    _text(d, (bx - 18 + nw / 2 + 4, by - 92), th["sfx"], sfx_f, metal, "ls")
    _text(d, (bx, by + br + 104), th["place"], _font("Lexend", 30, Weight=500), metal, "mm")

    # ── right: who, what, where
    rx, rmax = 770, 770
    _text(d, (rx, 236), "Awarded to", _font("Lexend", 26, Weight=300), MUTED, "ls")
    name = _clean_render_text(player_name, "Quiz Champion")
    nf, ntxt = _fit_name(name, rmax)
    _text(d, (rx - 4, 360), ntxt, nf, TEXT, "ls")

    quiz = _clean_render_text(quiz_title, "Live Group Quiz")
    group = _clean_render_text(group_title, "")
    host = _clean_render_text(host_name, "")
    _text(d, (rx, 428), f"for finishing {th['verb']} in", _font("Lexend", 25, Weight=300), MUTED, "ls")
    qf = _font("Lexend", 34, Weight=600)
    y = 476
    for ln in _wrap(quiz, qf, rmax, 2):
        _text(d, (rx, y), ln, qf, TEXT, "ls")
        y += 46
    where = " · ".join(p for p in (group, f"hosted by {host}" if host else "") if p)
    if where:
        wf = _font("Lexend", 23, Weight=400)
        _text(d, (rx, y + 4), _wrap(where, wf, rmax, 1)[0], wf, MUTED, "ls")

    # ── stats
    sy = 640
    d.line(_c(rx, sy, rx + rmax, sy), fill=(60, 66, 120), width=2 * S)
    stats = [
        ("Score", _fmt_num(score_pts), f"/{total_q}"),
        ("Correct", str(correct), ""),
        ("Accuracy", f"{round(acc * 100)}", "%"),
        ("Time", _fmt_time(time_secs), ""),
    ]
    vf, sf, lf = _font("Lexend", 50, Weight=600), _font("Lexend", 24, Weight=400), _font("Lexend", 21, Weight=300)
    colw = rmax / 4
    for i, (lbl, val, suf) in enumerate(stats):
        x = rx + i * colw
        _text(d, (x, sy + 78), val, vf, metal if i == 0 else TEXT, "ls")
        if suf:
            _text(d, (x + vf.getlength(val) / S + 4, sy + 78), suf, sf, MUTED, "ls")
        _text(d, (x, sy + 114), lbl, lf, MUTED, "ls")

    # ── OMR strip: filled as far as the accuracy
    n, gap = 30, rmax / 30
    filled = round(acc * n)
    for k in range(n):
        cx = rx + gap / 2 + k * gap
        if k < filled:
            _circle(d, cx, 846, 9.5, fill=metal)
        else:
            _circle(d, cx, 846, 9.5, outline=OMR, width=1.6)

    # ── footer
    ff = _font("Lexend", 20, Weight=400)
    _text(d, (rx, 924), datetime.now().strftime("Issued %d %b %Y"), ff, MUTED, "ls")
    tag = "@" + (bot_username or "MissQuizBot").lstrip("@")
    _text(d, (rx + rmax, 924), tag, _font("Lexend", 20, Weight=500), TEXT, "rs")

    out = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
