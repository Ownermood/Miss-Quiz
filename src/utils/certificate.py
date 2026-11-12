"""
Winner certificate (Pillow) for the Top 3 of a Group Quiz — Miss Quiz Bot.

Looks like a printed award: ivory paper, a guilloche (bank-note style) border with
corner rosettes, engraved serif title, the winner's name in script, a foil seal
(gold / silver / bronze by rank) with ribbon, and signature lines.

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

PAPER = (250, 245, 232)
PAPER_EDGE = (232, 221, 196)
NAVY = (27, 36, 68)
BODY = (62, 66, 84)
GOLD = (168, 126, 46)
GOLD_DARK = (128, 92, 28)

# rank → seal metal (light, mid, dark) and wording
THEMES = {
    1: {"metal": ((255, 236, 170), (212, 165, 60), (130, 92, 22)), "place": "First Place"},
    2: {"metal": ((246, 248, 252), (176, 184, 198), (98, 106, 122)), "place": "Second Place"},
    3: {"metal": ((250, 208, 170), (190, 122, 72), (112, 66, 30)), "place": "Third Place"},
}

_SMALLCAPS = str.maketrans("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡʏᴢ", "ABCDEFGHIJKLMNOPQRSTUVWYZ")
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


def _has_glyphs(font, text: str) -> bool:
    try:
        notdef = font.getmask("\uffff").tobytes()
        return all(c.isspace() or font.getmask(c).tobytes() != notdef for c in text)
    except Exception:
        return True


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
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
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


def _curve(d, pts, fill, width):
    d.line([(x * S, y * S) for x, y in pts], fill=fill, width=max(1, int(round(width * S))), joint="curve")


def _diamond(d, cx, cy, r, fill):
    d.polygon([(cx * S, (cy - r) * S), ((cx + r) * S, cy * S), (cx * S, (cy + r) * S), ((cx - r) * S, cy * S)], fill=fill)


def _rosette(d, cx, cy, r, petals, fill, width, layers=3, depth=.38):
    for j in range(layers):
        ph = j * math.tau / layers
        pts = []
        for i in range(361):
            a = i / 360 * math.tau
            rr = r * (1 - depth + depth * math.cos(petals * a + ph))
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        _curve(d, pts, fill, width)


def _guilloche_side(d, x0, y0, x1, y1, fill):
    """Interlaced waves along one straight border side."""
    length = math.hypot(x1 - x0, y1 - y0)
    n = max(1, round(length / 58))
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -uy, ux
    steps = int(length / 2)
    for amp, freq, phases, wdt in ((19, 1, 6, 1.0), (9, 2, 4, .8)):
        for k in range(phases):
            ph = k * math.tau / phases
            pts = []
            for i in range(steps + 1):
                t = i / steps
                off = amp * math.sin(t * n * freq * math.tau + ph)
                pts.append((x0 + ux * length * t + nx * off, y0 + uy * length * t + ny * off))
            _curve(d, pts, fill, wdt)


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
    """Foil seal: ribbon tails, starburst disc with a metal gradient, rank numeral."""
    light, mid, dark = th["metal"]
    R = 104
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # ribbon tails (behind the disc)
    for sx in (-1, 1):
        x0 = cx + sx * 22
        tail = [(x0 - 32, cy + 40), (x0 + 32, cy + 40), (x0 + 32 + sx * 24, cy + 150),
                (x0 + sx * 24, cy + 128), (x0 - 32 + sx * 24, cy + 150)]
        d.polygon([(x * S, y * S) for x, y in tail], fill=NAVY)
        for off in (-21, 21):
            _curve(d, [(x0 + off, cy + 40), (x0 + off + sx * 24, cy + 138)], mid, 1.5)

    # soft shadow under the disc
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse(_p(cx - R + 4, cy - R + 8, cx + R + 4, cy + R + 8), fill=(40, 30, 10, 110))
    layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8 * S)))

    # starburst disc with a metal gradient (light from the top-left)
    size = int(R * 2 * S)
    big = int(size * 1.5)
    grad = Image.radial_gradient("L").resize((big, big), Image.BICUBIC)
    o = int(size * .12)
    grad = grad.crop((o, o, o + size, o + size))
    metal = ImageOps.colorize(grad, light, dark, mid=mid, midpoint=120).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    pts = []
    for i in range(96):
        rr = R * S if i % 2 == 0 else (R - 8) * S
        a = i / 96 * math.tau
        pts.append((size / 2 + rr * math.cos(a), size / 2 + rr * math.sin(a)))
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    layer.paste(metal, (int((cx - R) * S), int((cy - R) * S)), mask)

    d = ImageDraw.Draw(layer)
    for rr, col, wd in ((88, dark, 2.2), (84.5, light, 1.2), (59, dark, 1.6), (56, light, 1.0)):
        d.ellipse(_p(cx - rr, cy - rr, cx + rr, cy + rr), outline=col, width=max(1, int(wd * S)))
    ring = f"{th['place'].upper()} • MISS QUIZ • GROUP QUIZ • "
    _arc_text(layer, cx, cy, 72, ring, _font("Cinzel", 13, 700), dark)

    # embossed numeral
    nf = _font("Cinzel", 72, 800)
    num = str(rank_pos if rank_pos in THEMES else 1)
    d = ImageDraw.Draw(layer)
    d.text(_p(cx - 1.6, cy + 23.4), num, font=nf, fill=light, anchor="ms")
    d.text(_p(cx + 1.6, cy + 26.6), num, font=nf, fill=dark, anchor="ms")
    d.text(_p(cx, cy + 25), num, font=nf, fill=tuple(int(m * .7 + k * .3) for m, k in zip(mid, dark)), anchor="ms")

    img.paste(layer, (0, 0), layer)


def _paper():
    """Paper + border + watermark. Same for every rank, so it is built once and reused."""
    if _paper_cache:
        return _paper_cache[0].copy()
    img = Image.new("RGB", (W * S, H * S), PAPER)
    # warm vignette towards the edges, then fine paper grain
    vg = Image.radial_gradient("L").resize(img.size, Image.BICUBIC).point(lambda v: int(max(0, v - 105) * .55))
    img.paste(Image.new("RGB", img.size, PAPER_EDGE), (0, 0), vg)
    grain = Image.effect_noise((W, H), 14).resize(img.size, Image.BICUBIC)
    img = Image.blend(img, Image.merge("RGB", (grain, grain, grain)), .045)

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # faint lace watermark behind the text
    _rosette(d, W / 2, 520, 360, 12, (*GOLD, 26), 1.0, layers=6, depth=.46)
    _rosette(d, W / 2, 520, 210, 8, (*GOLD, 22), 1.0, layers=4, depth=.5)

    # guilloche band
    m0, m1, mc = 30, 96, 63
    gold = (*GOLD, 205)
    _guilloche_side(d, mc, mc, W - mc, mc, gold)
    _guilloche_side(d, W - mc, H - mc, mc, H - mc, gold)
    _guilloche_side(d, mc, H - mc, mc, mc, gold)
    _guilloche_side(d, W - mc, mc, W - mc, H - mc, gold)
    d.rectangle(_p(m0, m0, W - m0, H - m0), outline=NAVY, width=3 * S)
    d.rectangle(_p(m0 + 7, m0 + 7, W - m0 - 7, H - m0 - 7), outline=(*GOLD, 255), width=int(1.2 * S))
    d.rectangle(_p(m1 - 6, m1 - 6, W - m1 + 6, H - m1 + 6), outline=(*GOLD, 255), width=int(1.2 * S))
    d.rectangle(_p(m1, m1, W - m1, H - m1), outline=NAVY, width=int(1.6 * S))

    # corner rosettes sit over the wave joins
    for cx, cy in ((mc, mc), (W - mc, mc), (mc, H - mc), (W - mc, H - mc)):
        d.ellipse(_p(cx - 43, cy - 43, cx + 43, cy + 43), fill=(*PAPER, 255), outline=NAVY, width=int(1.6 * S))
        _rosette(d, cx, cy, 37, 8, gold, 1.0, layers=4, depth=.42)
        d.ellipse(_p(cx - 5, cy - 5, cx + 5, cy + 5), fill=NAVY)
    img.paste(layer, (0, 0), layer)
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
    cx = W / 2

    img = _paper()
    d = ImageDraw.Draw(img)

    # ── heading
    _tracked(d, cx, 176, "MISS QUIZ", _font("Cinzel", 21, 600), GOLD_DARK, 9)
    d.line(_p(cx - 250, 169, cx - 122, 169), fill=GOLD, width=int(1.2 * S))
    d.line(_p(cx + 122, 169, cx + 250, 169), fill=GOLD, width=int(1.2 * S))
    _tracked(d, cx, 268, "CERTIFICATE", _font("Cinzel", 82, 700), NAVY, 13)
    _text(d, (cx, 330), "of Achievement", _font("PinyonScript", 56), GOLD_DARK)

    _text(d, (cx, 398), "This certificate is proudly presented to", _font("Cormorant-Italic", 29, 500), BODY)

    # ── the winner
    name = _clean_render_text(player_name, "Quiz Champion")
    nf, ntxt = _fit(name, lambda sz: _name_font(name, sz), 1080, range(128, 60, -6))
    _text(d, (cx, 518), ntxt, nf, NAVY)
    d.line(_p(cx - 330, 556, cx - 16, 556), fill=GOLD, width=int(1.4 * S))
    d.line(_p(cx + 16, 556, cx + 330, 556), fill=GOLD, width=int(1.4 * S))
    _diamond(d, cx, 556, 7, GOLD)

    # ── what for
    quiz = _clean_render_text(quiz_title, "Group Quiz")
    group = _clean_render_text(group_title, "")
    host = _clean_render_text(host_name, "")
    _text(d, (cx, 610), f"for securing {th['place']} in the group quiz", _font("Cormorant", 30, 500), BODY)
    qf = _font("NotoSansDevanagari", 28, 600) if _is_deva(quiz) else _font("Cormorant", 38, 700)
    y = 660
    for ln in _wrap(quiz, qf, 1040, 2):
        _text(d, (cx, y), ln, qf, NAVY)
        y += 44
    where = " · ".join(p for p in (f"held in {group}" if group else "", f"hosted by {host}" if host else "") if p)
    if where:
        wf = _font("NotoSansDevanagari", 20, 400) if _is_deva(where) else _font("Cormorant-Italic", 27, 500)
        _text(d, (cx - 0, y - 4), _wrap(where, wf, 1040, 1)[0], wf, BODY)

    # ── result: one quiet line each side of the seal, the way a printed award states it
    lf, vf = _font("Cinzel", 15, 600), _font("Cormorant", 34, 700)
    for x, lbl, val in ((cx - 300, "SCORE", f"{_fmt_num(score_pts)} / {total_q}"),
                        (cx + 300, "ACCURACY", f"{acc}%  ·  {_fmt_time(time_secs)}")):
        _text(d, (x, 850), val, vf, NAVY)
        _tracked(d, x, 878, lbl if lbl == "SCORE" else "ACCURACY  ·  TIME", lf, GOLD_DARK, 3)

    # ── signature (left) and date (right)
    ly = 982
    for sx0, sx1 in ((190, 520), (W - 520, W - 190)):
        d.line(_p(sx0, ly, sx1, ly), fill=NAVY, width=int(1.3 * S))
    _text(d, (355, ly - 12), "Miss Quiz", _font("PinyonScript", 56), NAVY)
    _tracked(d, 355, ly + 28, "QUIZ MASTER", _font("Cinzel", 14, 600), GOLD_DARK, 4)
    _text(d, (W - 355, ly - 14), datetime.now().strftime("%d %B %Y").lstrip("0"), _font("Cormorant", 32, 600), NAVY)
    _tracked(d, W - 355, ly + 28, "DATE", _font("Cinzel", 14, 600), GOLD_DARK, 4)

    # ── seal (bottom centre, over the border the way a stuck-on foil seal sits)
    _seal(img, cx, 900, rank_pos, th)

    # ── tiny imprint line inside the border band's inner edge
    tag = "@" + (bot_username or "MissQuizBot").lstrip("@")
    d = ImageDraw.Draw(img)
    _text(d, (W - 355, ly + 50), f"Issued by {tag} on Telegram", _font("Cormorant-Italic", 17, 500), BODY)

    out = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()
