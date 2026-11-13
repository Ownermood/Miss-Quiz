"""
Winner certificate (Pillow) for the Top 3 of a Group Quiz — Miss Quiz Bot.

Dark award style: charcoal sheet, a crimson curved panel with layered dark bands,
gold-foil title, the winner's name in gold script, a foil seal on the panel
(gold / silver / bronze by rank) with ribbon, and date / signature lines.

Fonts are bundled in assets/fonts (SIL OFL). If they are missing it falls back to
system DejaVu so it still renders on any VPS. Never raises on odd names:
fancy Unicode letters (𝐁𝐨𝐥𝐝, 𝙼𝚘𝚗𝚘, ꜱᴍᴀʟʟ ᴄᴀᴘꜱ) are folded to plain letters,
Hindi names use Noto Sans Devanagari, emoji-only names fall back to "Quiz Champion".
"""

import io
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_SYS_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
             "C:/Windows/Fonts/georgiab.ttf"]
_SYS_REG = ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
            "C:/Windows/Fonts/georgia.ttf"]

S = 2                      # supersample factor (drawn at 2x, downscaled → smooth curves)
W, H = 1600, 1132          # output size (A4 landscape ratio)

BG_IN = (58, 58, 62)          # charcoal, lighter at the centre
BG_OUT = (18, 18, 20)
RED_TOP = (206, 20, 30)
RED_BOT = (92, 4, 10)
WHITE = (236, 236, 240)
GREY = (160, 160, 168)
LINE = (92, 92, 100)
GOLD_STOPS = ((255, 238, 160), (226, 182, 82), (168, 118, 34), (230, 190, 96))  # top → bottom foil

# rank → seal metal (light, mid, dark) and wording
THEMES = {
    1: {"metal": ((255, 236, 170), (212, 165, 60), (130, 92, 22)), "place": "First Place"},
    2: {"metal": ((246, 248, 252), (176, 184, 198), (98, 106, 122)), "place": "Second Place"},
    3: {"metal": ((250, 208, 170), (190, 122, 72), (112, 66, 30)), "place": "Third Place"},
}

_SMALLCAPS = str.maketrans("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡʏᴢ", "ABCDEFGHIJKLMNOPQRSTUVWYZ")
# Look-alike letters people use for "fancy" names (Ꭰαяк Ꭵмρυℓѕє → Dark impulse).
# Applied only when EVERY non-Latin letter in the name is in this table, so a real
# Russian or Greek name is left as it is.
_LOOKALIKE = {
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "γ": "y", "ω": "w", "η": "n", "σ": "o", "ς": "c", "ϲ": "c", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z",
    "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Χ": "X", "Υ": "Y",
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "я": "r", "ѕ": "s", "і": "i", "ј": "j", "є": "e", "ԁ": "d", "ɢ": "G", "ʀ": "R",
    "ᴋ": "K", "п": "n", "и": "u", "ь": "b", "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "Ү": "Y", "Ѕ": "S", "І": "I", "Ј": "J", "Я": "R",
    # Cherokee
    "Ꭰ": "D", "Ꭱ": "R", "Ꭲ": "T", "Ꭵ": "i", "Ꭹ": "y", "Ꭺ": "A", "Ꭻ": "J", "Ꭼ": "E", "Ꮃ": "W", "Ꮇ": "M",
    "Ꮋ": "H", "Ꮐ": "G", "Ꮓ": "Z", "Ꮤ": "W", "Ꮪ": "S", "Ꮯ": "C", "Ꮮ": "L", "Ꮶ": "K", "Ꮲ": "P", "Ᏼ": "B",
    "Ꮩ": "V", "Ꮒ": "h", "Ꭷ": "o",
    # Latin variants
    "ɑ": "a", "ɐ": "a", "ɛ": "e", "ɨ": "i", "ɴ": "N", "ʏ": "Y", "ʟ": "L", "ɾ": "r", "ʋ": "u", "ɱ": "m",
}


def _fold_lookalikes(t: str) -> str:
    odd = [c for c in t if c.isalpha() and not ("a" <= c.lower() <= "z") and not (0x0900 <= ord(c) <= 0x097F)]
    if odd and all(c in _LOOKALIKE for c in odd):
        return "".join(_LOOKALIKE.get(c, c) for c in t)
    return t
_font_cache: dict = {}
_paper_cache: list = []
# lining figures (Cormorant's default old-style 1 reads like an I); needs libraqm
_FEAT = ["lnum"] if features.check("raqm") else None


def _len(font, s):
    try:
        return font.getlength(s, features=_FEAT)
    except Exception:
        return font.getlength(s)


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


def _font(name: str, size: int, weight: int = 0):
    """Bundled font at a size; weight sets the variable-font Weight axis."""
    key = (name, size, weight)
    if key in _font_cache:
        return _font_cache[key]
    try:
        f = ImageFont.truetype(str(_FONT_DIR / f"{name}.ttf"), size * S)
        if weight:
            vals = []
            for a in f.get_variation_axes():
                nm = a["name"].decode() if isinstance(a["name"], bytes) else str(a["name"])
                v = weight if nm == "Weight" else a.get("default", a["minimum"])
                vals.append(max(a["minimum"], min(a["maximum"], v)))
            f.set_variation_by_axes(vals)
    except Exception:
        f = _sys(_SYS_BOLD if weight >= 600 else _SYS_REG, size * S)
    _font_cache[key] = f
    return f


def _glyph_print(font, ch: str) -> bytes:
    """Pixels of one character as this font draws it (used to spot the 'missing glyph' box)."""
    l, t, r, b = font.getbbox(ch)
    im = Image.new("L", (max(1, r - l) + 2, max(1, b - t) + 2))
    ImageDraw.Draw(im).text((1 - l, 1 - t), ch, font=font, fill=255)
    return bytes(str(im.size), "ascii") + im.tobytes()


_notdef_cache: dict = {}


def _has_glyphs(font, text: str) -> bool:
    """False if any character would come out as the font's empty box (tofu)."""
    try:
        key = id(font)
        if key not in _notdef_cache:
            _notdef_cache[key] = {_glyph_print(font, "\ue000"), _glyph_print(font, "\U0010fffd")}
        notdef = _notdef_cache[key]
        return all(c.isspace() or _glyph_print(font, c) not in notdef for c in text)
    except Exception:
        return False


# ── text helpers ─────────────────────────────────────────────────────────────

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
    cleaned = re.sub(r"\s+", " ", _fold_lookalikes("".join(out))).strip()
    return cleaned or fallback


def _is_deva(s: str) -> bool:
    return any(0x0900 <= ord(c) <= 0x097F for c in s)


def _name_font(name: str, size: int):
    """Script for normal Latin names; serif for ALL CAPS; Devanagari / symbol fallbacks."""
    if _is_deva(name):
        return _font("NotoSansDevanagari", int(size * .62), 600)
    letters = [c for c in name if c.isalpha()]
    script = _font("PinyonScript", size)
    if letters and not all(c.isupper() for c in letters) and _has_glyphs(script, name):
        return script
    serif = _font("Cormorant", int(size * .74), 700)
    if any(c.isalnum() for c in name) and _has_glyphs(serif, name):
        return serif
    return _sys(_SYS_BOLD, int(size * .6) * S)


def _fit(text: str, make_font, max_w: int, sizes):
    """Largest size that fits max_w; else cut with an ellipsis at the smallest."""
    f = None
    for size in sizes:
        f = make_font(size)
        if _len(f, text) <= max_w * S:
            return f, text
    while text and _len(f, text + "…") > max_w * S:
        text = text[:-1]
    return f, text.rstrip() + "…"


def _wrap(text: str, font, max_w: int, max_lines: int = 2):
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if _len(font, trial) <= max_w * S or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] += "…"
    for i, ln in enumerate(lines):                      # cut anything still wider than the line
        if _len(font, ln) > max_w * S:
            ln = ln.rstrip("…")
            while ln and _len(font, ln + "…") > max_w * S:
                ln = ln[:-1]
            lines[i] = ln.rstrip() + "…"
    return lines or [""]


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

def _p(*v):
    return [int(round(a * S)) for a in v]


def _text(d, xy, s, font, fill, anchor="ms"):
    d.text(_p(*xy), s, font=font, fill=fill, anchor=anchor, features=_FEAT)


def _tracked(d, cx, y, s, font, fill, track):
    """Centered text with letter-spacing (engraved-title look)."""
    widths = [_len(font, c) / S for c in s]
    x = cx - (sum(widths) + track * (len(s) - 1)) / 2
    for c, w in zip(s, widths):
        d.text(_p(x, y), c, font=font, fill=fill, anchor="ls", features=_FEAT)
        x += w + track


def _tracked_l(d, x, y, s, font, fill, track):
    """Left-aligned text with letter-spacing."""
    for c in s:
        d.text(_p(x, y), c, font=font, fill=fill, anchor="ls", features=_FEAT)
        x += _len(font, c) / S + track


def _curve(d, pts, fill, width):
    d.line([(x * S, y * S) for x, y in pts], fill=fill, width=max(1, int(round(width * S))), joint="curve")


def _vgrad(w, h, stops):
    """Vertical multi-stop gradient image (RGB)."""
    col = Image.new("RGB", (1, len(stops)))
    for i, c in enumerate(stops):
        col.putpixel((0, i), c)
    return col.resize((max(1, w), max(1, h)), Image.BICUBIC)


def _gold_text(img, xy, s, font, anchor="ls", track=0.0):
    """Text filled with a gold-foil gradient, plus a faint dark drop shadow."""
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    if track:
        x, y = xy
        if anchor[0] == "m":
            x -= (sum(_len(font, c) / S for c in s) + track * (len(s) - 1)) / 2
        for c in s:
            md.text(_p(x, y), c, font=font, fill=255, anchor="l" + anchor[1], features=_FEAT)
            x += _len(font, c) / S + track
    else:
        md.text(_p(*xy), s, font=font, fill=255, anchor=anchor, features=_FEAT)
    box = mask.getbbox()
    if not box:
        return
    x0, y0, x1, y1 = box
    pad = 8 * S
    sb = (max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad))
    shadow = mask.crop(sb).point(lambda v: int(v * .55)).filter(ImageFilter.GaussianBlur(2 * S))
    img.paste((0, 0, 0), (sb[0] + int(1.5 * S), sb[1] + int(2.5 * S)), shadow)
    grad = _vgrad(x1 - x0, y1 - y0, GOLD_STOPS)
    img.paste(grad, (x0, y0), mask.crop(box))


def _arc_text(layer, cx, cy, r, text, font, fill):
    """Text set around a full circle, reading clockwise from the top."""
    step = 360 / len(text)
    box = int(font.size * 1.6)
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        ang = -90 + i * step
        tile = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((box / 2, box / 2), ch, font=font, fill=fill, anchor="mm")
        tile = tile.rotate(-(ang + 90), resample=Image.BICUBIC)
        px = cx * S + r * S * math.cos(math.radians(ang)) - box / 2
        py = cy * S + r * S * math.sin(math.radians(ang)) - box / 2
        layer.alpha_composite(tile, (int(px), int(py)))


def _seal(img, cx, cy, rank_pos, th):
    """Foil seal: metal ribbon tails, starburst rim, dark centre with the rank numeral."""
    light, mid, dark = th["metal"]
    R = 118
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # ribbon tails (behind the disc), shaded metal
    for sx in (-1, 1):
        x0 = cx + sx * 30
        tail = [(x0 - 34, cy + 50), (x0 + 34, cy + 50), (x0 + 34 + sx * 40, cy + 230),
                (x0 + sx * 40, cy + 200), (x0 - 34 + sx * 40, cy + 230)]
        d.polygon([(x * S, y * S) for x, y in tail], fill=mid if sx < 0 else dark)
        _curve(d, [(x0 - 34 + 6, cy + 54), (x0 - 34 + 6 + sx * 40, cy + 222)], light, 1.4)

    # shadow under the disc
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse(_p(cx - R + 4, cy - R + 10, cx + R + 4, cy + R + 10), fill=(0, 0, 0, 150))
    layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(10 * S)))

    # starburst rim with a metal gradient (light from the top-left)
    size = int(R * 2 * S)
    big = int(size * 1.5)
    o = int(size * .12)
    grad = Image.radial_gradient("L").resize((big, big), Image.BICUBIC).crop((o, o, o + size, o + size))
    metal = ImageOps.colorize(grad, light, dark, mid=mid, midpoint=120).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    pts = []
    for i in range(100):
        rr = R * S if i % 2 == 0 else (R - 9) * S
        a = i / 100 * math.tau
        pts.append((size / 2 + rr * math.cos(a), size / 2 + rr * math.sin(a)))
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    layer.paste(metal, (int((cx - R) * S), int((cy - R) * S)), mask)

    d = ImageDraw.Draw(layer)
    d.ellipse(_p(cx - 98, cy - 98, cx + 98, cy + 98), outline=dark, width=int(2.2 * S))
    d.ellipse(_p(cx - 94, cy - 94, cx + 94, cy + 94), outline=light, width=int(1.2 * S))
    # dark centre
    d.ellipse(_p(cx - 66, cy - 66, cx + 66, cy + 66), fill=(26, 26, 28), outline=light, width=int(2 * S))
    d.ellipse(_p(cx - 59, cy - 59, cx + 59, cy + 59), outline=mid, width=int(1 * S))
    ring = f"{th['place'].upper()} • MISS QUIZ • "
    _arc_text(layer, cx, cy, 80, ring, _font("Cinzel", 14, 800), dark)

    # three small stars under the numeral
    for k in (-1, 0, 1):
        sx, sy, r = cx + k * 14, cy + 40, 4.2
        d.polygon([((sx + r * (1 if j % 2 == 0 else .42) * math.cos(math.radians(-90 + j * 36))) * S,
                    (sy + r * (1 if j % 2 == 0 else .42) * math.sin(math.radians(-90 + j * 36))) * S) for j in range(10)],
                  fill=mid)
    nf = _font("Cinzel", 64, 800)
    num = str(rank_pos if rank_pos in THEMES else 1)
    d.text(_p(cx, cy + 22), num, font=nf, fill=light, anchor="ms")
    d.text(_p(cx, cy - 34), "AWARD", font=_font("Cinzel", 11, 700), fill=mid, anchor="ms")

    img.paste(layer, (0, 0), layer)


def _band(cx_base, bulge, yc, half):
    """Points of a panel whose right edge bulges into a rounded point at yc."""
    pts = [(-10, -10)]
    for i in range(0, 201):
        y = -10 + (H + 20) * i / 200
        t = min(1.0, abs(y - yc) / half)
        x = cx_base + bulge * (1 - t ** 1.7) ** .62
        pts.append((x, y))
    pts.append((-10, H + 10))
    return [(x * S, y * S) for x, y in pts]


def _background():
    """Charcoal sheet + layered crimson panel. Same for every rank: built once, reused."""
    if _paper_cache:
        return _paper_cache[0].copy()
    vg = Image.radial_gradient("L").resize((W * S, H * S), Image.BICUBIC)
    vg = vg.point(lambda v: min(255, int(v * 1.25)))
    img = ImageOps.colorize(vg, BG_IN, BG_OUT)
    img = Image.blend(img, Image.effect_noise((W, H), 10).convert("RGB").resize(img.size), .02)

    yc, half = H * .44, H * .62
    for base, bulge, col in ((300, 300, (44, 44, 48)), (230, 300, (30, 30, 33)), (130, 300, None)):
        poly = _band(base, bulge, yc, half)
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).polygon([(x + 10 * S, y + 6 * S) for x, y in poly], fill=170)
        img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(18 * S)))
        m = Image.new("L", img.size, 0)
        ImageDraw.Draw(m).polygon(poly, fill=255)
        if col is None:
            fill = _vgrad(W * S, H * S, (RED_TOP, (160, 10, 20), RED_BOT))
        else:
            fill = _vgrad(W * S, H * S, (tuple(c + 10 for c in col), col, tuple(max(0, c - 8) for c in col)))
        img.paste(fill, (0, 0), m)
        # thin highlight along the curved edge
        edge = poly[1:-1]
        ImageDraw.Draw(img).line(edge, fill=(255, 255, 255) if col is None else (70, 70, 76),
                                 width=int((1.4 if col is None else 1) * S))
    img = img.convert("RGB")
    _paper_cache.append(img)
    return img.copy()


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
    """Render a 1600x1132 PNG certificate for rank 1, 2 or 3."""
    th = THEMES.get(rank_pos, THEMES[1])
    total_q = max(1, int(total_q))
    correct = max(0, min(int(correct), total_q))
    acc = round(100 * correct / total_q)

    img = _background()
    d = ImageDraw.Draw(img)
    x0, x1 = 700, 1500                 # text column
    mw = x1 - x0

    # ── heading
    _gold_text(img, (x0, 300), "CERTIFICATE", _font("Cinzel", 96, 800), "ls", track=3)
    _gold_text(img, (x0 + 2, 358), "OF ACHIEVEMENT", _font("Cinzel", 36, 700), "ls", track=9)
    _tracked_l(d, x0 + 2, 470, "PROUDLY PRESENTED TO", _font("Cinzel", 21, 700), WHITE, 6)

    # ── the winner (gold script)
    name = _clean_render_text(player_name, "Quiz Champion")
    nf, ntxt = _fit(name, lambda sz: _name_font(name, sz), mw, range(128, 60, -6))
    _gold_text(img, (x0 - 4, 590), ntxt, nf, "ls")
    d = ImageDraw.Draw(img)
    d.line(_p(x0, 622, x1, 622), fill=LINE, width=int(1.6 * S))

    # ── what for
    quiz = _clean_render_text(quiz_title, "Group Quiz")
    group = _clean_render_text(group_title, "")
    host = _clean_render_text(host_name, "")
    if host.lower() in ("group", "channel", "groupanonymousbot") or host == group:
        host = ""                                   # anonymous admin: Telegram calls the sender "Group"
    _tracked_l(d, x0, 668, f"FOR SECURING {th['place'].upper()} IN", _font("Cinzel", 17, 700), WHITE, 3)
    qf = _font("NotoSansDevanagari", 26, 600) if _is_deva(quiz) else _font("Cormorant", 36, 700)
    y = 714
    for ln in _wrap(quiz, qf, mw, 2):
        _text(d, (x0, y), ln, qf, WHITE, "ls")
        y += 42
    where = " · ".join(p for p in (f"Held in {group}" if group else "", f"hosted by {host}" if host else "") if p)
    if where:
        wf = _font("NotoSansDevanagari", 19, 400) if _is_deva(where) else _font("Cormorant-Italic", 26, 500)
        _text(d, (x0, y - 2), _wrap(where, wf, mw, 1)[0], wf, GREY, "ls")

    # ── results
    vf, lf = _font("Cormorant", 40, 700), _font("Cinzel", 13, 700)
    sy = max(y + 70, 870)
    cols = [("SCORE", f"{_fmt_num(score_pts)} / {total_q}"), ("ACCURACY", f"{acc}%"), ("TIME", _fmt_time(time_secs))]
    cw = mw / 3
    for i, (lbl, val) in enumerate(cols):
        x = x0 + i * cw
        _gold_text(img, (x, sy), val, vf, "ls")
        d = ImageDraw.Draw(img)
        _tracked_l(d, x + 1, sy + 30, lbl, lf, GREY, 3)
        if i:
            d.line(_p(x - 26, sy - 34, x - 26, sy + 30), fill=LINE, width=int(1.2 * S))

    # ── date and signature
    ly = 1030
    d.line(_p(x0, ly, x0 + 300, ly), fill=GREY, width=int(1.2 * S))
    d.line(_p(x1 - 300, ly, x1, ly), fill=GREY, width=int(1.2 * S))
    _text(d, (x0 + 150, ly - 14), datetime.now().strftime("%d %B %Y").lstrip("0"), _font("Cormorant", 30, 600), WHITE)
    _gold_text(img, (x1 - 150, ly - 10), "Miss Quiz", _font("PinyonScript", 56), "ms")
    d = ImageDraw.Draw(img)
    _tracked(d, x0 + 150, ly + 30, "DATE", _font("Cinzel", 13, 700), GREY, 4)
    _tracked(d, x1 - 150, ly + 30, "SIGNATURE", _font("Cinzel", 13, 700), GREY, 4)

    # ── seal on the red panel
    _seal(img, 245, 430, rank_pos, th)

    # ── imprint
    tag = "@" + (bot_username or "MissQuizBot").lstrip("@")
    _text(ImageDraw.Draw(img), (x1, H - 34), f"Issued by {tag} on Telegram", _font("Cormorant-Italic", 20, 500), GREY, "rs")

    out = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()
