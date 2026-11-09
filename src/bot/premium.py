"""
premium.py — premium (custom) emoji + styled buttons.

Telegram shows custom emoji from a bot only if the bot OWNER has Telegram
Premium (or the bot bought a Fragment username). So this is opt-in:
PREMIUM_EMOJI=1 → <tg-emoji> / button icons; otherwise the normal emoji.
IDs come from the packs "Peach and Goma" (@TgEmojis) and "UI Animation"
(@technoprint_official).
"""

import os
from typing import Optional

from telegram import InlineKeyboardButton

def premium_on() -> bool:
    """Read at call time so .env / Render settings always apply."""
    return os.environ.get("PREMIUM_EMOJI", "0").strip().lower() in ("1", "true", "yes", "on")

EMOJI = {
    # Peach and Goma (cute reactions)
    "hi":       ("👋", "5424611538151437896"),
    "love":     ("💕", "5424895302345695654"),
    "party":    ("🥳", "5424968454228680999"),
    "cool":     ("😎", "5424663180838182778"),
    "cry":      ("😭", "5424908668283919719"),
    "think":    ("🤨", "5424613565375978984"),
    "wow":      ("🤩", "5424639292230082961"),
    "clap":     ("👏", "5424679359979986283"),
    "flower":   ("🌸", "5424768308752686263"),
    "ok":       ("👌", "5424750497523311050"),
    "fight":    ("👊", "5425027561568608124"),
    "question": ("❓", "5424700761802023186"),
    # UI Animation (icons)
    "check":    ("✅", "5444987348334965906"),
    "cross":    ("❌", "5445092669522996408"),
    "warn":     ("⚠️", "5447381715293074599"),
    "gear":     ("⚙️", "5444869180899752137"),
    "stats":    ("📊", "5445146408153806223"),
    "bell":     ("🔔", "5445333299360723379"),
    "plus":     ("➕", "5447224884562263112"),
    "trash":    ("🗑", "5445005936953424165"),
    "medal":    ("🏅", "5444931419270839381"),
    "star":     ("⭐️", "5444939047132755535"),
    "info":     ("ℹ️", "5247236071795754971"),
    "diamond":  ("💎", "5260681660189408650"),
    "key":      ("🔑", "5445373775132522312"),
    "folder":   ("📂", "5447282724886839705"),
    "play":     ("▶️", "5444883062234053429"),
    "back":     ("⬅️", "5447506720316225765"),
    "clock":    ("⏰", "5445350406215465190"),
    "signal":   ("📡", "5447448489149625830"),
    "users":    ("🫂", "5445372439397691719"),
    "edit":     ("📝", "5444889156792646660"),
    "bolt":     ("⚡️", "5445388803223091254"),
    "door":     ("🚪", "5447434637880098257"),
    "search":   ("🔎", "5445023138297447592"),
    "book":     ("📖", "5445203741672243391"),
    "cap":      ("🎓", "5444939047132755535"),
}


def e(name: str) -> str:
    """Emoji for HTML messages: premium <tg-emoji> if enabled, else the plain one."""
    item = EMOJI.get(name)
    if not item:
        return ""
    fallback, cid = item
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>' if premium_on() else fallback


def btn(text: str, emoji: Optional[str] = None, style: Optional[str] = None,
        **kw) -> InlineKeyboardButton:
    """Inline button with optional premium icon and colour.
    style: "danger" (red), "success" (green), "primary" (blue).
    Without premium, the plain emoji is put in front of the text instead."""
    icon = None
    if emoji and emoji in EMOJI:
        if premium_on():
            icon = EMOJI[emoji][1]
        else:
            text = f"{EMOJI[emoji][0]} {text}"
    return InlineKeyboardButton(text, style=style, icon_custom_emoji_id=icon, **kw)


# ════════════════════════════════════════════════════════════════════════════
#  Automatic premium emoji for EVERY outgoing message and button
# ════════════════════════════════════════════════════════════════════════════
# All ids from the two packs (emoji without U+FE0F → custom_emoji_id).
PACK = {
    # Peach and Goma
    "💆‍♀": "5422537202091434052", "🤧": "5424609261818748707", "🚶‍♀": "5425033342594589713",
    "😭": "5424908668283919719", "🧹": "5425036812928164275", "🛀": "5422546728328896810",
    "💃": "5425040167297621674", "👏": "5424679359979986283", "😘": "5424662811470994801",
    "🥺": "5422885115917247336", "😉": "5424897256555816653", "🤗": "5424829516331622320",
    "☺": "5424599469293313135", "🥰": "5422868133616557832", "🤳": "5424897698937447043",
    "🍑": "5424933961346326845", "🤨": "5424613565375978984", "😡": "5424705567870427646",
    "🍿": "5424673243946557836", "🫠": "5425054856085774122", "🤯": "5422681839410094322",
    "🥳": "5424968454228680999", "😵": "5424681086556839770", "😎": "5424663180838182778",
    "🎧": "5424729550967807644", "😼": "5424625784557936624", "❓": "5424700761802023186",
    "😨": "5424769661667386290", "☕": "5425142344569594119", "👯‍♀": "5425009896368120119",
    "😅": "5425018838490030164", "❤": "5425060164665351935", "🫥": "5424723336150131847",
    "😢": "5425070270723401711", "😏": "5422405801861980799", "🤩": "5424639292230082961",
    "🛌": "5424673944026226164", "🕺": "5424637359494798783", "👮‍♀": "5422464797532759490",
    "🤤": "5424655351112802557", "📦": "5422725295889196694", "🌬": "5424612560353640271",
    "💕": "5424895302345695654", "👋": "5424611538151437896", "💞": "5425147386861199071",
    "🐟": "5424593885835829968", "👸": "5424709970211905165", "💋": "5424709356031581626",
    "💻": "5424994438780821530", "🍲": "5424752078071275816", "🍜": "5424965233003208253",
    "🍔": "5424645799105535217", "💅": "5424716983893500055", "🍝": "5422617200152288810",
    "🙃": "5424614548923489515", "🌸": "5424768308752686263", "👌": "5424750497523311050",
    "👊": "5425027561568608124",
    # UI Animation
    "🔎": "5445023138297447592", "🔲": "5445385947069838800", "📲": "5445059250382469069",
    "🌐": "5447602197439218445", "⬅": "5447506720316225765", "➡": "5445350109862720603",
    "📂": "5447282724886839705", "🚪": "5447434637880098257", "⬇": "5444961234933806330",
    "📁": "5447389837076231920", "✔": "5447242579827523388", "✅": "5444987348334965906",
    "☑": "5445210909972655435", "➕": "5447224884562263112", "🏅": "5444931419270839381",
    "➖": "5447155993286832278", "▶": "5444883062234053429", "💬": "5445140257760639304",
    "🚫": "5447647474984449520", "⚡": "5445388803223091254", "👍": "5447153029759397673",
    "👎": "5447128406711891434", "💔": "5445053099989303899", "⚠": "5447381715293074599",
    "🖼": "5447588260270341594", "📝": "5444889156792646660", "🔠": "5445137435967126342",
    "↔": "5447554042265891906", "↕": "5445088739627924307", "↪": "5445365692004071819",
    "🗑": "5445005936953424165", "🔽": "5445358884480916784", "🔼": "5445307250384075361",
    "🪙": "5447629406057034342", "🔛": "5445279663809130789", "📴": "5445349654596189741",
    "👩‍💻": "5445224894386172410", "🫂": "5445372439397691719", "❌": "5445092669522996408",
    "💰": "5444860552310457690", "💲": "5447579253723918909", "👛": "5444903695256941915",
    "✍": "5445128296276718145", "👨‍🎨": "5447226864542190055", "🔍": "5444989577422993015",
    "⭐": "5444939047132755535", "◀": "5447651546613449378", "↩": "5445362436418859744",
    "⬆": "5444988177263654261", "®": "5445373981290952548", "⚙": "5444869180899752137",
    "📹": "5445344161333015312", "🖥": "5447607759421863856", "📋": "5445260044398524944",
    "📑": "5445203741672243391", "📔": "5444854925903295394", "🎙": "5447321534211323909",
    "🔊": "5445030469806618301", "🛒": "5447319442562251569", "🛍": "5447387942995655277",
    "💳": "5447453226498552490", "⚫": "5447429226221303478", "⏰": "5445350406215465190",
    "📡": "5447448489149625830", "📟": "5447604619800770047", "💼": "5444924349754667822",
    "🔑": "5445373775132522312", "☀": "5447425115937602108", "🔔": "5445333299360723379",
    "📄": "5447421246172069841", "📊": "5445146408153806223", "📬": "5445163772706582819",
    "💦": "5444979432710242887", "✈": "5447213867971144371", "🔖": "5447460764166158313",
    "🗄": "5447257461889202335", "🗃": "5447210891558814377", "🗓": "5444933979071348347",
    "🎨": "5447642819239903347", "🔗": "5447479640547428304", "👨‍🦰": "5447311106030726740",
    "📍": "5447187153274567373", "ℹ": "5247236071795754971", "💎": "5260681660189408650",
}

# Emojis the bot uses that are not in the packs → closest pack emoji.
SUBST = {
    "♻": "↪", "👇": "⬇", "👆": "⬆", "☝": "⬆", "👉": "➡", "📎": "📂", "🖇": "📂", "🧷": "📂",
    "🗳": "☑", "🗒": "📝",
    "🎓": "⭐", "📚": "📑", "📖": "📑", "📜": "📄", "📰": "📄", "📃": "📄", "🗂": "📂",
    "⚔": "👊", "💪": "👊", "🤝": "🤗", "🙏": "🤗", "😊": "☺", "🙂": "☺", "😀": "☺",
    "😃": "☺", "😄": "☺", "😁": "☺", "🥲": "😢", "😔": "😢", "😞": "😢", "😳": "😨",
    "🎯": "📍", "📌": "📍", "🏁": "📍", "🔥": "⚡", "🏓": "⚡", "💥": "⚡", "✨": "⭐",
    "🌟": "⭐", "💫": "⭐", "💡": "☀", "🌞": "☀", "👤": "👨‍🦰", "👥": "🫂", "👑": "👸",
    "🔱": "👸", "🏆": "🏅",   # 🥇🥈🥉 stay native: one premium medal for all three hid the ranking
    "🎖": "🏅", "🎉": "🥳",
    "🎊": "🥳", "🎁": "📦", "📭": "📬", "📨": "📬", "📩": "📬", "📧": "📬", "⏳": "⏰",
    "⌛": "⏰", "⏱": "⏰", "🕐": "⏰", "⏲": "⏰", "📅": "🗓", "📆": "🗓", "📢": "📡",
    "📣": "📡", "🔄": "↪", "🔁": "↪", "🔃": "↪", "✏": "✍", "🖊": "✍", "🖋": "✍",
    "🔒": "🔑", "🔐": "🔑", "🔓": "🔑", "🛡": "🔑", "⛔": "🚫", "🚷": "🚫", "📈": "📊",
    "📉": "📊", "🔬": "🔎", "🧪": "🔎", "🎭": "🎨", "🖌": "🎨", "🎮": "🕺", "🎲": "🍿",
    "🎬": "📹", "🎥": "📹", "📷": "🖼", "📸": "🖼", "🏠": "🚪", "🏡": "🚪", "🛠": "⚙",
    "🔧": "⚙", "🔨": "⚙", "📥": "⬇", "📤": "⬆", "🤖": "💻", "📱": "📲", "🌍": "🌐",
    "🌎": "🌐", "🌏": "🌐", "🗺": "🌐", "🏛": "💼", "🏢": "💼", "🔴": "❤", "💖": "💕",
    "💗": "💕", "💓": "💞", "💘": "💞", "💝": "💕", "🩷": "💕", "🌷": "🌸", "🌺": "🌸",
    "🌹": "🌸", "🌱": "🌸", "☘": "🌸", "🍁": "🌸", "🌼": "🌸", "⏸": "🔲", "⏹": "🔲",
    "💾": "🗄", "🔘": "🔛", "💠": "💎", "🆔": "🔖", "🏷": "🔖", "🔙": "⬅", "🔜": "➡",
    "🚀": "✈", "🧠": "🤯", "💯": "🏅", "❗": "⚠", "‼": "⚠", "❕": "⚠", "🆕": "⭐",
    "📶": "📡", "🌀": "↪", "🧾": "📋", "🗒": "📋", "🔢": "🔠", "🔤": "🔠", "💭": "💬",
    "🗨": "💬", "📞": "📲", "☎": "📲", "✖": "❌", "❎": "❌", "🟢": "✅", "🔵": "ℹ",
    "🌙": "⏰", "🧑‍💻": "👩‍💻", "👨‍💻": "👩‍💻", "🙋": "👋", "🤚": "👋", "✋": "👋",
    "🍀": "🌸", "🎈": "🥳", "🎶": "🎧", "🎵": "🎧", "⏭": "➡", "⚖": "💼",
    "📛": "🔖", "🔷": "💎", "🕒": "⏰", "🟡": "⭐",
}

# New Emojis pack (@NewEmojis / SoLo_HaMiD) — first id per emoji
PACK_NEW = {
    "🏴‍☠": "5116309444090661129", "✅": "5084979757905347540", "💜": "5084974483685507801",
    "👻": "5082478549340783285", "💙": "5082628525303792441", "🐾": "5084923566848213749",
    "⚡": "5085022089103016925", "🦋": "5084613633418199991", "🪫": "5084813564145828384",
    "♥": "5100657930429006538", "🕷": "5098265504796115765", "🐐": "5098094273039959279",
    "🥸": "5100702284556272564", "📍": "5107195471948940313", "✨": "5104960787579929462",
    "🚫": "5116151848855667552", "⏫": "5116395218882528029", "⏬": "5116204921766544244",
    "🫀": "5116296632203216188", "🫡": "5116245732545790791", "💪": "5116296400274981694",
    "🚨": "5116508099213001597", "👤": "5116582462276764538", "🎸": "5116416491855545424",
    "🕊": "5116445341150872576", "🌙": "5116574228824458340", "🎮": "5116175844837950263",
    "💤": "5116590266232341370", "🛸": "5116380246626534280", "❤": "5118454879039259395",
    "🙏": "5116159438062879454", "👾": "5116512467194741904", "🔇": "5116466579764151159",
    "‼": "5116275208906343429", "🔥": "5116414868357907335", "⭐": "5116163917713769254",
    "🔘": "5116444615301399317", "💬": "5116468787377341336", "🆓": "5116382939571028928",
    "💰": "5116648080787112958", "❓": "5116240346656801621", "🧠": "5116599934203724812",
    "😐": "5116268964023894989", "😇": "5116553153419936517", "🖕": "5116156972751651938",
    "👺": "5116206523789345674", "🥷": "5116116063188157350", "🐍": "5116298753917060171",
    "🦇": "5118357331742032622", "💼": "5118686540985271080", "🎥": "5118744200921219799",
    "🐈": "5123237479742178762", "📶": "5121007227779416740", "💦": "5123293121043497777",
    "⚪": "5123344136665039833", "💡": "5123359615727174427", "⏰": "5123230779593196220",
    "💉": "5120959729736090635", "⭕": "5122933683820430249", "👎": "5121063440311386962",
    "👁": "5122983123188974322", "💀": "5125286720308249351", "💣": "5134472688986756318",
    "☢": "5134516583552517044", "⏹": "5134537521518085000", "🐋": "5134201302888219205",
    "👣": "5134445209785992072", "💋": "5134145167665660725", "🪳": "5134420956105671865",
    "💱": "5134438483867206614", "🕸": "5134457377428341766", "🫱": "5134653266591744867",
    "🐜": "5134594180726653827", "🪱": "5134654202894615343", "🌿": "5134384281379931657",
    "💫": "5134202243486057363", "🫗": "5134352404132660484", "🧪": "5134183530313548836",
    "🍷": "5134186747244053395", "💊": "5134469678214677470", "🪐": "5134452506935427991",
    "💔": "5134435481685066618", "🌳": "5134648812710658661", "0⃣": "5140999334174655345",
    "1⃣": "5141109049114232089", "2⃣": "5140871649091912628", "3⃣": "5141399818400170896",
    "4⃣": "5138822752123225428", "5⃣": "5141062672057369534", "6⃣": "5139005588881015916",
    "7⃣": "5140999557512954818", "8⃣": "5141013683660391172", "9⃣": "5141137309999039199",
    "🐆": "5172843385643336752", "🌱": "5172453033245672405", "🌟": "5172632227871196306",
    "☀": "5175089039358821263", "❄": "5172640658892002083", "🥃": "5172739563398890485",
    "🕺": "5174924430442234980", "📞": "5172893417717367746", "🍹": "5172739056592749710",
    "🦎": "5172819844927587194", "🎵": "5172447776205702031", "🩵": "4902715076873553054",
    "✈": "4904934320835134291", "😌": "4902563765175714716", "⚙": "4904936030232117798",
    "✝": "4902349923049014048", "🐉": "4904742417401381633", "🔫": "4904870274282816510",
    "🔪": "4902427455798641776", "🐺": "4902693778130732452", "🥺": "4904683803982693140",
    "☹": "4904468402782864209", "😟": "4902467987405014368", "🐎": "4904521759161582943",
    "🎼": "4904898741326054363", "🔝": "4904832912362309275", "🌈": "4904819211416633963",
    "🗣": "4904973211763999824", "🫶": "4902326682980975467", "🦄": "4904532277536490620",
}

# Exact matches first. Where an emoji is in several packs, the UI/Peach one
# (PACK) wins so the bot's existing look doesn't change.
EXACT = dict(PACK_NEW)
EXACT.update(PACK)
# Closest-look substitutes only for emojis no pack has exactly.
EMOJI_ID = dict(EXACT)
EMOJI_ID.update({k: EXACT[v] for k, v in SUBST.items() if k not in EXACT and v in EXACT})

import html as _html
import re as _re

_EMOJI_RE = _re.compile(
    "(" + "|".join("".join(_re.escape(c) + "\ufe0f?" for c in k)
                   for k in sorted(EMOJI_ID, key=len, reverse=True)) + ")")
_TAG_RE = _re.compile(r"(<[^>]+>)")
_SKIP_TAGS = ("tg-emoji", "code", "pre")


def _emoji_id(raw: str):
    return EMOJI_ID.get(raw.replace("️", ""))


def premiumize_html(text: str) -> str:
    """Wrap every known emoji in an HTML message with <tg-emoji>, except
    inside tags, <code>, <pre> and existing <tg-emoji>."""
    if not text:
        return text
    out, skip = [], 0
    for part in _TAG_RE.split(text):
        if part.startswith("<") and part.endswith(">"):
            name = part[1:].split()[0].rstrip(">").lower() if len(part) > 2 else ""
            if name.lstrip("/") in _SKIP_TAGS:
                skip += -1 if name.startswith("/") else 1
                skip = max(skip, 0)
            out.append(part)
        elif skip:
            out.append(part)
        else:
            out.append(_EMOJI_RE.sub(
                lambda m: f'<tg-emoji emoji-id="{_emoji_id(m.group(0))}">{m.group(0)}</tg-emoji>', part))
    return "".join(out)


def premiumize_markup(markup):
    """Turn a leading emoji in each inline button into its premium icon."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows, changed = [], False
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            m = _EMOJI_RE.match(b.text or "")
            if m and not b.icon_custom_emoji_id:
                d = b.to_dict()
                d["text"] = (b.text[m.end():].strip()) or b.text
                d["icon_custom_emoji_id"] = _emoji_id(m.group(0))
                b = InlineKeyboardButton.de_json(d, None)
                changed = True
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if changed else markup


# Only these methods take HTML text/caption (answerCallbackQuery, polls etc. don't)
_HTML_ENDPOINTS = {
    "sendMessage", "editMessageText", "sendPhoto", "sendVideo", "sendDocument",
    "sendAnimation", "sendAudio", "sendVoice", "editMessageCaption", "copyMessage",
}


def premiumize_request(data: dict, endpoint: str = "sendMessage") -> dict:
    """Applied to every Bot API request (see PremiumExtBot)."""
    if not premium_on() or not isinstance(data, dict):
        return data
    if endpoint == "sendPoll":
        # question and explanation accept custom emoji via their own parse modes
        for key, mode_key, ent_key in (("question", "question_parse_mode", "question_entities"),
                                       ("explanation", "explanation_parse_mode", "explanation_entities")):
            val = data.get(key)
            if isinstance(val, str) and _EMOJI_RE.search(val) \
                    and not data.get(mode_key) and not data.get(ent_key):
                data[key] = premiumize_html(_html.escape(val, quote=False))
                data[mode_key] = "HTML"
    if endpoint not in _HTML_ENDPOINTS:
        if "reply_markup" in data:
            data["reply_markup"] = premiumize_markup(data["reply_markup"])
        return data
    for key in ("text", "caption"):
        val = data.get(key)
        if not isinstance(val, str) or not _EMOJI_RE.search(val):
            continue
        mode = str(data.get("parse_mode") or "").upper()
        if mode == "HTML":
            data[key] = premiumize_html(val)
        elif not mode and not data.get("entities") and not data.get("caption_entities"):
            data[key] = premiumize_html(_html.escape(val, quote=False))
            data["parse_mode"] = "HTML"
    if "reply_markup" in data:
        data["reply_markup"] = premiumize_markup(data["reply_markup"])
    return data


def bold_on() -> bool:
    return os.environ.get("BOLD_TEXT", "1").strip().lower() not in ("0", "false", "no", "off")


_GAP_RE = _re.compile(r"\n[ \t]*\n([ \t]*\n)+")


def tidy_html(text: str) -> str:
    """Bold everything and remove extra blank lines.
    - 3+ line breaks → one empty line; no blank lines at start/end
    - whole message wrapped in <b> (inner <b> tags dropped). Skipped when the
      message has <code>/<pre>, which Telegram doesn't allow inside bold."""
    if not text:
        return text
    text = _GAP_RE.sub("\n\n", text).strip("\n")
    text = _re.sub(r"\s*\n(</blockquote>)", r"\1", text)      # no blank last line inside a quote
    if not bold_on():
        return text
    text = _re.sub(r"</?b>", "", text)
    if "<code" not in text and "<pre" not in text:
        return "<b>" + text + "</b>"
    # Telegram can't bold <code>/<pre>: bold every text run outside them
    # (each run gets its own <b>, so tags always stay properly nested).
    out, skip = [], 0
    for part in _TAG_RE.split(text):
        if part.startswith("<") and part.endswith(">"):
            name = part[1:].split()[0].rstrip(">").lower() if len(part) > 2 else ""
            if name.lstrip("/") in ("code", "pre", "tg-emoji"):
                skip = max(0, skip + (-1 if name.startswith("/") else 1))
            out.append(part)
        elif skip or not part.strip():
            out.append(part)
        else:
            out.append(f"<b>{part}</b>")
    return "".join(out)


def style_request(data: dict, endpoint: str) -> dict:
    """Always on (premium or not): bold + tidy HTML text, red Back buttons, no link preview."""
    if not isinstance(data, dict):
        return data
    if endpoint in ("sendMessage", "editMessageText"):
        if "link_preview_options" not in data and "disable_web_page_preview" not in data:
            data["link_preview_options"] = {"is_disabled": True}
    if "reply_markup" in data:
        data["reply_markup"] = style_markup(data["reply_markup"])
    if endpoint in _HTML_ENDPOINTS:
        for key in ("text", "caption"):
            val = data.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            mode = str(data.get("parse_mode") or "").upper()
            if mode == "HTML":
                data[key] = tidy_html(val)
            elif not mode and not data.get("entities") and not data.get("caption_entities"):
                data[key] = tidy_html(_html.escape(val, quote=False))
                data["parse_mode"] = "HTML"
    return data


_BACK_RE = _re.compile(r"\bback\b", _re.IGNORECASE)


def style_markup(markup):
    """Always on: every 'Back' button without a style becomes red (danger)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows, changed = [], False
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if not b.style and _BACK_RE.search(b.text or ""):
                d = b.to_dict(); d["style"] = "danger"
                b = InlineKeyboardButton.de_json(d, None); changed = True
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if changed else markup


def make_premium_bot_class():
    from telegram.ext import ExtBot

    class PremiumExtBot(ExtBot):
        async def _do_post(self, endpoint, data, *args, **kwargs):
            from telegram.error import BadRequest
            original = dict(data) if isinstance(data, dict) else data
            try:
                data = style_request(data, endpoint)
                return await super()._do_post(endpoint, premiumize_request(data, endpoint), *args, **kwargs)
            except BadRequest as e:
                # Premium emoji / bold rejected (e.g. Premium expired, odd markup):
                # never lose the message — resend it exactly as the code wrote it.
                if not any(w in str(e).lower() for w in ("emoji", "entit", "document_invalid", "can't parse")):
                    raise
                import logging
                log = logging.getLogger(__name__)
                log.warning(f"{endpoint} rejected ({e}) — resending as written")
                try:
                    return await super()._do_post(endpoint, original, *args, **kwargs)
                except BadRequest as e2:
                    if "parse" not in str(e2).lower() or not isinstance(original, dict):
                        raise
                    # The code's own HTML is broken (e.g. unescaped '<' in user
                    # text). Last resort: same words, no formatting — never lose it.
                    log.error(f"{endpoint}: broken HTML ({e2}) — sent without formatting")
                    plain = dict(original)
                    for key, mode in (("text", "parse_mode"), ("caption", "parse_mode")):
                        if isinstance(plain.get(key), str) and plain.get(mode):
                            plain[key] = _html.unescape(_re.sub(r"<[^>]+>", "", plain[key]))
                    plain.pop("parse_mode", None)
                    return await super()._do_post(endpoint, plain, *args, **kwargs)

    return PremiumExtBot


def make_rate_limiter():
    """Keeps every send under Telegram's limits (~30 msg/s overall, 20/min per
    group) and automatically waits + retries on 429 'Flood control'."""
    from telegram.ext import AIORateLimiter
    return AIORateLimiter(max_retries=3)
