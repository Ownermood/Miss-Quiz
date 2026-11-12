"""
Royal Certificate of Excellence Generator (Pillow / PIL) — Miss Quiz Bot.
Generates high-resolution (1280x820) achievement certificates for Top 3 Group Quiz Winners.
Uses built-in DejaVu/Liberation Linux fonts with graceful fallback so it works on any VPS.
"""

import io
import re
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont


_FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
]

_FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
]

_FONT_PATHS_SERIF = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
]

_FONT_PATHS_ITALIC = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "C:/Windows/Fonts/georgiai.ttf",
    "C:/Windows/Fonts/ariali.ttf",
]


def _load_font(paths, size: int):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _clean_render_text(text: str, fallback: str = "Champion") -> str:
    """Keep printable Latin/Devanagari/basic punctuation and strip astral emojis
    that standard TrueType fonts render as empty tofu boxes."""
    if not text:
        return fallback
    # Strip surrogate/astral emojis and variation selectors
    cleaned = "".join(
        ch for ch in str(text)
        if (0x20 <= ord(ch) <= 0x024F) or (0x0900 <= ord(ch) <= 0x097F) or ch in "’‘“”–—•·"
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else fallback


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
    """Render a 1280x820 high-resolution PNG certificate for rank 1, 2, or 3."""
    W, H = 1280, 820
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # Rank theme colors (1st: Royal Gold, 2nd: Platinum Silver, 3rd: Amber Bronze)
    themes = {
        1: {
            "label": "First Place",
            "badge": "#1 CHAMPION",
            "accent": (217, 155, 22),      # Gold
            "accent_light": (254, 243, 199),
            "hdr_left": (30, 27, 75),      # Deep royal indigo
            "hdr_right": (67, 56, 202),    # Electric indigo
        },
        2: {
            "label": "Second Place",
            "badge": "#2 RUNNER-UP",
            "accent": (100, 116, 139),     # Platinum slate
            "accent_light": (241, 245, 249),
            "hdr_left": (15, 23, 42),
            "hdr_right": (51, 65, 85),
        },
        3: {
            "label": "Third Place",
            "badge": "#3 MEDALIST",
            "accent": (180, 83, 9),        # Bronze amber
            "accent_light": (254, 243, 199),
            "hdr_left": (41, 37, 36),
            "hdr_right": (120, 53, 15),
        },
    }
    th = themes.get(rank_pos, themes[1])
    acc_color = th["accent"]

    # 1. Top Gradient Banner (height: 150px)
    BANNER_H = 150
    r1, g1, b1 = th["hdr_left"]
    r2, g2, b2 = th["hdr_right"]
    for x in range(W):
        t = x / max(1, W - 1)
        col = (int(r1 + (r2 - r1) * t), int(g1 + (g2 - g1) * t), int(b1 + (b2 - b1) * t))
        draw.line([(x, 0), (x, BANNER_H)], fill=col)

    # Gold/Rank accent bar under banner
    draw.rectangle([0, BANNER_H, W, BANNER_H + 6], fill=acc_color)

    # 2. Subtle Watermark Seal in center of white body
    cx, cy = W // 2, 460
    for rad in (170, 156, 140):
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=(241, 245, 249), width=3)

    # 3. Ornamental Outer Border on Certificate Body
    draw.rectangle([28, BANNER_H + 24, W - 28, H - 24], outline=(226, 232, 240), width=2)
    draw.rectangle([36, BANNER_H + 32, W - 36, H - 32], outline=acc_color, width=2)

    # Fonts
    f_brand = _load_font(_FONT_PATHS_BOLD, 34)
    f_brand_sub = _load_font(_FONT_PATHS_REGULAR, 15)
    f_badge = _load_font(_FONT_PATHS_BOLD, 20)
    f_cert_title = _load_font(_FONT_PATHS_BOLD, 28)
    f_awarded = _load_font(_FONT_PATHS_REGULAR, 20)
    f_winner = _load_font(_FONT_PATHS_SERIF, 54)
    f_desc_bold = _load_font(_FONT_PATHS_BOLD, 22)
    f_meta = _load_font(_FONT_PATHS_REGULAR, 18)
    f_box_lbl = _load_font(_FONT_PATHS_BOLD, 15)
    f_box_val = _load_font(_FONT_PATHS_BOLD, 34)
    f_sig = _load_font(_FONT_PATHS_ITALIC, 28)
    f_foot = _load_font(_FONT_PATHS_REGULAR, 16)
    f_foot_bold = _load_font(_FONT_PATHS_BOLD, 16)

    # 4. Header Content (Left: Miss Quiz Logo + Brand, Right: Rank Pill)
    # Left logo badge
    draw.rounded_rectangle([54, 34, 134, 114], radius=20, fill=(255, 255, 255))
    draw.text((94, 74), "MQ", font=_load_font(_FONT_PATHS_BOLD, 36), fill=th["hdr_left"], anchor="mm")

    draw.text((154, 55), "MISS QUIZ", font=f_brand, fill=(255, 255, 255), anchor="lm")
    draw.text((154, 94), "OFFICIAL TOURNAMENT CERTIFICATE", font=f_brand_sub, fill=(203, 213, 225), anchor="lm")

    # Right rank pill in header
    draw.rounded_rectangle([W - 290, 42, W - 56, 108], radius=16, fill=acc_color)
    draw.text((W - 173, 75), th["badge"], font=f_badge, fill=(255, 255, 255), anchor="mm")

    # 5. Certificate Title
    draw.text((cx, 215), "C E R T I F I C A T E   O F   A C H I E V E M E N T", font=f_cert_title, fill=(30, 41, 59), anchor="mm")
    draw.line([(cx - 190, 242), (cx + 190, 242)], fill=acc_color, width=3)

    draw.text((cx, 278), "This certificate is proudly awarded to", font=f_awarded, fill=(100, 116, 139), anchor="mm")

    # 6. Winner Name
    clean_winner = _clean_render_text(player_name, "Quiz Champion")[:32]
    draw.text((cx, 342), clean_winner, font=f_winner, fill=(15, 23, 42), anchor="mm")

    # 7. Quiz & Group Details
    clean_quiz = _clean_render_text(quiz_title, "Live Group Quiz")[:48]
    clean_group = _clean_render_text(group_title, "Telegram Group")[:38]
    clean_host = _clean_render_text(host_name, "Admin")[:28]

    desc_line = f"For achieving {th['label']} in  “{clean_quiz}”"
    draw.text((cx, 410), desc_line, font=f_desc_bold, fill=(51, 65, 85), anchor="mm")

    sub_line = f"Hosted by {clean_host}  •  Group: {clean_group}"
    draw.text((cx, 446), sub_line, font=f_meta, fill=(100, 116, 139), anchor="mm")

    # 8. Four Premium Stat Cards (SCORE | ACCURACY | TIME | RANK)
    total_q = max(1, int(total_q))
    accuracy = round(100 * correct / total_q)
    cards = [
        ("SCORE", f"{correct}/{total_q}", (15, 23, 42)),
        ("ACCURACY", f"{accuracy}%", (22, 163, 74)),
        ("SPEED TIME", f"{round(float(time_secs), 1)}s", (37, 99, 235)),
        ("RANK", th["label"], acc_color),
    ]

    box_w, box_h = 250, 136
    gap = 28
    total_row_w = 4 * box_w + 3 * gap
    start_x = (W - total_row_w) // 2
    box_y = 492

    for i, (lbl, val, val_col) in enumerate(cards):
        bx = start_x + i * (box_w + gap)
        # Card background & border
        draw.rounded_rectangle([bx, box_y, bx + box_w, box_y + box_h], radius=16, fill=(248, 250, 252), outline=(226, 232, 240), width=2)
        # Bottom accent bar on each stat card
        draw.rounded_rectangle([bx + 36, box_y + box_h - 6, bx + box_w - 36, box_y + box_h], radius=3, fill=val_col)
        draw.text((bx + box_w // 2, box_y + 38), lbl, font=f_box_lbl, fill=(100, 116, 139), anchor="mm")
        draw.text((bx + box_w // 2, box_y + 86), val, font=f_box_val, fill=val_col, anchor="mm")

    # 9. Footer (Signature, Date, Bot handle)
    foot_y = 725
    draw.text((145, foot_y - 12), "Miss Quiz Official", font=f_sig, fill=(51, 65, 85), anchor="mm")
    draw.line([(65, foot_y + 12), (225, foot_y + 12)], fill=(203, 213, 225), width=2)
    draw.text((145, foot_y + 30), "AUTHORIZED SIGNATURE", font=_load_font(_FONT_PATHS_BOLD, 12), fill=(148, 163, 184), anchor="mm")

    date_str = datetime.now().strftime("%b %d, %Y")
    draw.text((cx, foot_y + 10), f"Issued on {date_str}", font=f_foot, fill=(100, 116, 139), anchor="mm")

    bot_tag = f"@{bot_username.lstrip('@')}" if bot_username else "@MissQuizBot"
    draw.text((W - 70, foot_y + 10), f"Powered by {bot_tag}", font=f_foot_bold, fill=(51, 65, 85), anchor="rm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
