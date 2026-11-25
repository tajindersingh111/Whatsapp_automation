"""A qualifying sales flow for the menu bot: learn the job first, quote last.

An ad click usually opens with "price?". Answering that straight away turns the chat
into a rate comparison and leaves the team a lead with nothing in it. This instead walks
the customer down a funnel — who they are, what the factory does, which service they
want, which variant of it, their artwork, size, quantity, city — and only then quotes,
with a summary the team can act on cold.

The funnel narrows on purpose:

    name -> service -> variant -> photo -> design -> size -> qty -> city -> quote

`service` is where the customer is told what the factory actually does and picks a lane;
`variant` is the fork inside that lane, which is where the fabric-vs-hard-surface mistake
gets caught before anyone wastes time on it. Only UV DTF has a published rate card, so a
customer who picks anything else is told the team will price it rather than being shown
sticker rates for a t-shirt job.

Two things keep it from feeling like an interrogation: anything volunteered is absorbed,
so one message can fill several slots and skip those questions; and a question the reply
did not answer is dropped rather than asked again forever. A customer who keeps pushing
for the rate gets it after `price_after_asks` attempts, because stonewalling on WhatsApp
loses the lead.

All state lives in `Conversation.state["sf"]`, so the funnel survives between messages.
Everything here is pure (no DB, no I/O) so a whole conversation can be unit tested.
"""

import re

from app.bot_config import BotConfig

SLOTS = ("name", "service", "variant", "photo", "design", "size", "qty", "pages", "city", "phone", "payment")

# The lanes the factory sells. First match wins, and UV DTF goes first on purpose:
# "DTF" alone means garment printing, but "UV DTF" means stickers, and stickers are the
# product the ads run on. Checking fabric first sent every "uv dtf" enquiry down the
# garment lane and withheld the sticker rate card.
SERVICES: list[tuple[str, str, set[str]]] = [
    ("uvdtf", "UV DTF stickers",
     {"uvdtf", "sticker", "stickers", "uv", "transfer", "label", "labels", "stickering"}),
    ("fabric", "T-shirt / fabric printing",
     {"tshirt", "tshirts", "shirt", "kapda", "kapde", "kapra", "cloth", "clothes", "fabric",
      "cotton", "jersey", "hoodie", "banyan", "towel", "cap", "apron", "dtf"}),
    ("mug", "Mug printing",
     {"mug", "mugs", "cup", "cups", "sublimation"}),
    ("signage", "LED / neon / backlit signage",
     {"signage", "sign", "board", "led", "neon", "backlit", "glow", "channel", "acp", "flex",
      "banner", "hoarding"}),
    ("print", "Vinyl / wallpaper / canvas printing",
     {"vinyl", "wallpaper", "canvas", "oneway", "poster", "printing", "print"}),
    ("cnc", "CNC / laser cutting, wooden doors",
     {"cnc", "laser", "jali", "cutting", "door", "doors", "mdf", "engraving"}),
    ("events", "Exhibition stall / event branding",
     {"exhibition", "stall", "event", "events", "canopy", "standee", "awning", "booth"}),
]
_SERVICE_LABEL = {k: label for k, label, _ in SERVICES}

# The fork inside each lane. Catching this early is what stops a fabric job being
# quoted at sticker rates.
VARIANTS: dict[str, tuple[str, list[tuple[str, set[str]]]]] = {
    # UV DTF sticks by surface, not by product name: a glass jar and a ceramic mug are
    # the same job, and a cotton bag is a different process entirely. Asking by surface
    # is how the factory actually decides, and it catches the fabric mistake up front.
    # Item words stay in the keywords so "mug" still lands in the right bucket.
    "uvdtf": ("UV DTF sticker aajkal bahut trending hai — plain ya hard surface par sabse achha baithta hai. Aapka item kaunsa hai?", [
        ("smooth hard surface (glass, ceramic, steel, acrylic)",
         {"glass", "kaanch", "kanch", "ceramic", "mug", "mugs", "cup", "cups", "steel", "metal",
          "acrylic", "aluminium", "mirror", "tile", "plate", "trophy", "laptop", "helmet", "mobile"}),
        ("curved item (bottle, sipper, jar, tumbler)",
         {"bottle", "bottles", "sipper", "tumbler", "flask", "jar", "jars", "container", "pen", "dabba"}),
        ("packaging (kraft box, carton, pouch)",
         {"packaging", "box", "boxes", "carton", "kraft", "pouch", "packet", "packing", "chocolate"}),
        ("wood ya leather (diary, wooden box, wallet)",
         {"wood", "wooden", "mdf", "ply", "leather", "diary", "notebook", "planner", "frame"}),
        ("cloth / fabric", {"kapda", "kapde", "kapra", "cloth", "fabric", "tshirt", "cotton", "bag", "cap"}),
    ]),
    "fabric": ("Kis garment par?", [
        ("t-shirt", {"tshirt", "tshirts", "shirt", "tee"}),
        ("hoodie / sweatshirt", {"hoodie", "sweatshirt", "jacket"}),
        ("cap", {"cap", "caps", "hat"}),
        ("bag", {"bag", "bags", "tote", "cotton bag"}),
        ("apron / uniform", {"apron", "uniform", "coat"}),
    ]),
    "mug": ("Kaunsa mug?", [
        ("ceramic white", {"ceramic", "white", "normal", "plain"}),
        ("magic mug", {"magic", "colour", "color", "heat"}),
        ("steel sipper", {"steel", "sipper", "bottle", "travel"}),
    ]),
    "signage": ("Kaunsa signage?", [
        ("LED board", {"led", "glow", "light"}),
        ("neon", {"neon"}),
        ("backlit", {"backlit", "lollipop"}),
        ("flex / banner", {"flex", "banner", "hoarding"}),
    ]),
    "print": ("Kis par print karwana hai?", [
        ("wallpaper", {"wallpaper", "wall"}),
        ("vinyl / glass", {"vinyl", "glass", "oneway", "window"}),
        ("canvas / poster", {"canvas", "poster", "frame"}),
        ("vehicle", {"car", "vehicle", "van", "canter"}),
    ]),
    "cnc": ("Kya banwana hai?", [
        ("wooden door", {"door", "doors", "darwaza"}),
        ("jali / partition", {"jali", "partition", "screen"}),
        ("name plate", {"nameplate", "plate", "name"}),
        ("other wood / acrylic", {"wood", "acrylic", "mdf"}),
    ]),
}

_PRICE_WORDS = {"price", "rate", "rates", "cost", "daam", "bhav", "quotation", "quote",
                "charge", "charges", "kitna", "kitne", "kitni", "paisa", "paise", "rupaye", "rupees",
                # Complaining about the price is a price message too. Missing these let the
                # FAQ answer "mehnga hai" with the sticker rate card — to a mug customer.
                "mehnga", "mahnga", "mehngi", "sasta", "sasti", "budget", "expensive"}
# The subset that is a complaint rather than a question.
_OBJECTION_WORDS = {"mehnga", "mahnga", "mehngi", "budget", "expensive", "zyada"}

# Said when they want it quick. Worth one line acknowledging it, not silently ignored
# while the funnel carries on as if nothing was said — that is what makes a bot obvious.
_URGENCY_WORDS = {"jaldi", "urgent", "urgently", "turant", "fatafat", "emergency", "asap"}

# A plain "thanks"/"ok"/"hi" once the deal is done, or after someone has backed off,
# still deserves a human reply. Falling through to "samajh nahi aaya" right after
# closing a sale is the single most robotic thing that could happen here.
_SOCIAL_WORDS = {
    "thanks", "thank", "thanku", "thankyou", "tnx", "dhanyavaad", "dhanyawad", "shukriya",
    "ok", "okay", "k", "theek", "thik", "acha", "accha", "great", "nice", "super", "cool",
    "badhiya", "perfect", "done", "hi", "hello", "hey", "namaste", "ji", "bye", "byee",
}

# Someone stepping back. Pushing the next question at them is how a bot loses a lead
# that a person would have kept warm.
_BACKING_OFF = ("chhodo", "chodo", "rehne do", "rahne do", "nahi chahiye", "nhi chahiye",
                "baad mein", "abhi nahi", "sochta hoon", "sochti hoon", "soch kar", "dekhte hain")
_NO_DESIGN = {"nahi", "nhi", "no", "nahin", "banado", "banwana", "banwane", "banaoge", "aap", "tum"}
_NOT_A_NAME = {"hi", "hello", "hey", "namaste", "yes", "haan", "ok", "okay", "sticker", "price"}
# Every word that names something we sell or something it goes on. Anything containing
# one of these is an answer about the job, never a person's name.
_PRODUCT_WORDS = (
    {w for _k, _l, keys in SERVICES for w in keys}
    | {w for _prompt, opts in VARIANTS.values() for _label, keys in opts for w in keys}
    | {"par", "pe", "chahiye", "karwana", "karwani", "banwana", "print", "printing"}
)
_WORD_RE = re.compile("[0-9A-Za-zÀ-ɏऀ-ॿ]+")

# "A4" is the commonest answer a print customer gives, so it has to count as a size
# alongside "3 inch" and "3x3". Missing it makes the bot re-ask and shunts the whole
# funnel one step out of line.
_SIZE_RE = re.compile(
    r"\bA[2-6]\b"
    r"|(\d+(?:\.\d+)?)\s*(?:x|\*|by|×)\s*(\d+(?:\.\d+)?)\s*(inch|in|cm|mm|feet|ft)?"
    r"|(\d+(?:\.\d+)?)\s*(inch|inches|cm|mm|feet|ft)\b",
    re.I,
)
_QTY_RE = re.compile(r"(\d{1,6})\s*(piece|pieces|pcs|pc|nos|sticker|stickers|sheet|sheets|mug|mugs|bottle|bottles|box|boxes|tshirt|tshirts|unit|units)\b", re.I)
# "500", "500-600", "500 approx" — nobody repeats the unit they were just asked about.
# Only read as a quantity when the quantity was the question, so a pincode or a price
# mentioned elsewhere in the chat never lands in this slot.
_BARE_QTY_RE = re.compile(r"\b\d{1,6}(?:\s*(?:-|\u2013|to)\s*\d{1,6})?\b")

# Said in answer to "payment kaise karenge?" — and often volunteered much earlier.
_PAYMENT_LABEL = {
    "upi": "UPI", "gpay": "UPI", "googlepay": "UPI", "phonepe": "UPI", "paytm": "UPI",
    "bank": "bank transfer", "transfer": "bank transfer", "neft": "bank transfer",
    "rtgs": "bank transfer", "imps": "bank transfer",
    "cash": "cash", "cod": "cash on delivery", "card": "card", "cheque": "cheque",
}
_PAYMENT_WORDS = set(_PAYMENT_LABEL)

# A page/sheet count — only asked for UV DTF, which is priced per sheet rather than per
# piece. Kept apart from qty (how many stickers) and size (A4 or A3).
_PAGES_RE = re.compile(r"\b(\d{1,4})\s*(?:sheet|sheets|page|pages)?\b", re.I)

# A 10-digit Indian mobile, with or without a leading +91 / 91 / 0.
_PHONE_RE = re.compile(r"(?:\+?91[\s-]?|0)?\b([6-9]\d{9})\b")
_SAME_NUMBER_WORDS = {"yahi", "yehi", "isi", "same", "yahin"}


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def asks_price(text: str) -> bool:
    return bool(_words(text) & _PRICE_WORDS)


#  Address filler that rides along with almost every real message ("thanks bhai",
# "ok yaar") and carries no meaning of its own, so it should not stop a message from
# counting as social.
_FILLER_WORDS = {"bhai", "yaar", "sir", "mam", "boss", "g", "bro", "dost"}


def _is_social(text: str) -> bool:
    """True for a message that is nothing but a pleasantry — "thanks bhai", "ok yaar",
    "hi" — and has no slot-answer or question buried in it."""
    w = _words(text)
    return bool(w & _SOCIAL_WORDS) and not (w - _SOCIAL_WORDS - _FILLER_WORDS)


def load(state: dict) -> dict:
    sf = dict(state.get("sf") or {})
    sf.setdefault("price_asks", 0)
    return sf


def save(state: dict, sf: dict) -> None:
    state["sf"] = sf


def _match(words: set[str], table) -> tuple[str, str] | None:
    """Returns (bucket label, the word the customer actually used)."""
    for label, keys in table:
        hit = words & keys
        if hit:
            return label, sorted(hit)[0]
    return None


def _take(sf: dict, slot: str, text: str, loose: bool = False) -> None:
    """Read a free-text message into one slot, when it plausibly belongs there.

    ``loose`` is for the slot we actually just asked about, where almost anything the
    customer types is meant as the answer. Without it the same text is being offered
    speculatively to a slot nobody asked about, so the checks have to be stricter.
    """
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 60 or asks_price(cleaned):
        return
    words = _words(cleaned)

    if slot == "qty":
        m = _BARE_QTY_RE.search(cleaned)
        if m and not _SIZE_RE.search(cleaned):
            sf["qty"] = m.group(0).strip() + " piece"
        return

    if slot == "city":
        # A payment word, an item or a bare number is a late answer to another question,
        # not a city. "City: cash" on the team's alert is exactly the kind of thing that
        # makes the whole chat look automated.
        if words & (_PAYMENT_WORDS | _PRODUCT_WORDS | _NOT_A_NAME):
            return
        if re.fullmatch(r"[\d\s,.\u2013-]+", cleaned):
            return
        if not loose and (len(cleaned.split()) > 3
                          or not re.fullmatch(r"[A-Za-z\u0900-\u097f .,'-]+", cleaned)):
            return
        sf["city"] = cleaned
        return

    if slot == "payment" and loose:
        sf["payment"] = cleaned
        return

    if slot == "variant" and loose:
        sf["variant"] = cleaned


def absorb(sf: dict, text: str, kind: str = "text") -> None:
    """Fill whatever this message happens to answer, whichever question was asked."""
    words = _words(text)
    asked = sf.get("asked")

    if kind in ("image", "sticker"):
        # A picture before any artwork talk is the product; after that it is the design.
        sf["design" if sf.get("photo") else "photo"] = True
    elif kind in ("document", "video"):
        sf["design"] = True

    if not sf.get("service"):
        for key, _label, keys in SERVICES:
            if words & keys:
                sf["service"] = key
                break

    if sf.get("service") and not sf.get("variant"):
        table = VARIANTS.get(sf["service"])
        if table:
            found = _match(words, table[1])
            label = found[0] if found else None
            if label == "cloth / fabric":
                # Not a UV DTF job at all — move them to the lane that can actually do it
                # rather than quoting sticker rates for a garment.
                sf["service"] = "fabric"
                sf["switched_from_uvdtf"] = True
            elif found:
                sf["variant"] = label
                # Keep their own word too: "smooth hard surface" tells the team how it
                # will print, "mug" tells them what they are quoting.
                sf["item_said"] = found[1]

    if not sf.get("size"):
        m = _SIZE_RE.search(text or "")
        if m:
            sf["size"] = m.group(0).strip()

    if not sf.get("qty"):
        m = _QTY_RE.search(text or "")
        if m:
            sf["qty"] = m.group(0).strip()
        elif asked == "qty":
            bare = _BARE_QTY_RE.search(text or "")
            if bare and not _SIZE_RE.search(text or ""):
                sf["qty"] = bare.group(0).strip() + " piece"

    if not sf.get("pages") and asked == "pages":
        m = _PAGES_RE.search(text or "")
        if m:
            sf["pages"] = m.group(1).strip() + " sheet"

    # A phone number is read whenever it is said, same as payment — people drop it in
    # early ("order confirm karni hai, mera number 98293...") and re-asking it after
    # that is the kind of thing that makes a bot obvious.
    if not sf.get("phone"):
        m = _PHONE_RE.search(text or "")
        if m:
            sf["phone"] = m.group(1)
        elif asked == "phone" and words & _SAME_NUMBER_WORDS:
            sf["phone"] = "same (WhatsApp number)"

    # Taken whenever it is said, not only when asked. People answer the payment question
    # three messages early, and re-asking something they already told us is the single
    # most bot-like thing this flow can do.
    if not sf.get("payment"):
        hit = words & _PAYMENT_WORDS
        if hit:
            sf["payment"] = _PAYMENT_LABEL[sorted(hit)[0]]

    # "design nahi hai, aap bana do" is a real answer whenever it is said — people
    # volunteer it while we are still asking for the product photo, and ignoring it
    # there left the bot asking for artwork over and over.
    if not sf.get("design") and words & _NO_DESIGN and (asked == "design" or "design" in words or "logo" in words):
        sf["design"] = "team"

    # A name only counts when we actually asked for one, and even then it must not look
    # like an answer to some other question — "mug par" is an item, not a person, and
    # greeting someone as "Mug Par ji" is the kind of thing nobody forgives.
    if not sf.get("name") and asked == "name":
        cleaned = re.sub(r"\b(mera|naam|name|hai|is|ji|my)\b", " ", (text or "").lower()).strip(" .,-")
        cleaned = " ".join(cleaned.split())
        if cleaned and len(cleaned) <= 60:
            # "tajinder jaipur se" -> just "tajinder". People introduce themselves with
            # their city or a sentence around it; a first-name basis is how this chat is
            # meant to read, and it is what the human agent's own script says to do
            # ("first letter only"). The guards run on this one word, not the whole
            # reply, so trailing context never blocks a real name from landing.
            first = cleaned.split()[0]
            if (2 <= len(first) <= 20 and first.isalpha()
                    and first not in _NOT_A_NAME
                    and not (_words(first) & _PRODUCT_WORDS)
                    and not asks_price(first)):
                sf["name"] = first.title()


def _skip(slot: str, cfg: BotConfig, sf: dict | None = None) -> bool:
    f = cfg.sales_flow
    if slot == "photo" and not f.want_photo:
        return True
    if slot == "design" and not f.want_design:
        return True
    if slot == "name" and not f.want_name:
        return True
    # "No. of sheets" only means something for UV DTF, which is priced per sheet. Every
    # other lane is quoted by the team, so the question has no answer worth having there.
    if slot == "pages" and (sf or {}).get("service") != "uvdtf":
        return True
    return False


def next_slot(sf: dict, cfg: BotConfig) -> str | None:
    if sf.get("paused"):
        return None
    given_up = set(sf.get("skipped") or [])
    for slot in SLOTS:
        if slot in given_up or _skip(slot, cfg, sf):
            continue
        # Nothing to fork on until a service is chosen, and some lanes have no fork.
        if slot == "variant" and (not sf.get("service") or sf["service"] not in VARIANTS):
            continue
        if not sf.get(slot):
            return slot
    return None


def service_menu() -> str:
    return "\n".join(f"• {label}" for _k, label, _w in SERVICES)


# Repeating a question word for word is the loudest tell that nobody is really there.
# A second ask gets a shorter nudge instead, the way a person would follow up.
_RETRY: dict[str, str] = {
    "photo": "Us item ki ek photo mil jaye to size pakka bata dungi 📷",
    "design": "Design ya logo bhej dijiyega, phir layout bana ke dikha deti hoon.",
    "size": "Size ka andaaza bhi chalega — kitne inch ka chahiye?",
    "qty": "Approx kitne piece? Bas moti si ginti bata dijiye.",
    "pages": "Kitne sheet chahiye, bas andaza bata dijiye.",
    "city": "Delivery address bata dijiye, courier usi par bhej denge.",
    "phone": "Contact number bata dijiye, delivery confirm karne ke kaam aayega.",
    "name": "Naam bata dijiye na, baat karne mein aasani rahegi 🙂",
    "payment": "Payment ka mode bata dijiye — UPI, bank ya cash?",
}


def question(slot: str, sf: dict, cfg: BotConfig) -> str:
    f = cfg.sales_flow
    if (sf.get("tries") or {}).get(slot) and slot in _RETRY:
        return _RETRY[slot]
    if slot == "service":
        return f.ask_service.replace("{services}", service_menu())
    if slot == "variant":
        table = VARIANTS.get(sf.get("service") or "")
        if table:
            prompt, options = table
            opts = "\n".join("\u2022 " + label for label, _ in options)
            return f.ask_variant.replace("{prompt}", prompt).replace("{options}", opts)
    # UV DTF is priced by page size (A4/A3), not by inches \u2014 the generic size question
    # would ask for something nobody there can answer.
    if slot == "size" and sf.get("service") == "uvdtf":
        text = f.ask_size_uvdtf
    else:
        text = getattr(f, f"ask_{slot}")
    first = (sf.get("name") or "").split(" ")[0]
    return text.replace("{name}", first) if first else text.replace("{name} ", "").replace("{name}", "")


# Answering a question with nothing but the next question is what makes a bot feel like
# a bot. Saying back what the customer just gave us costs one line and reads like someone
# is actually listening.
_ACK: dict[str, tuple[str, ...]] = {
    "name": ("Shukriya {v} ji 🙏", "Namaste {v} ji 🙏", "{v} ji, swagat hai 🙏"),
    "service": ("Badhiya choice! {v} humara sabse demand wala kaam hai 👍", "{v} — bilkul, roz ka kaam hai, best quality milegi.", "Haan ji, {v} mein humari khaasiyat hai."),
    "variant": ("{v} par? Zabardast, us par result bahut achha aata hai 👍", "{v} — perfect, bilkul sahi choose kiya.", "{v}, bilkul ho jaayega aur achha dikhega."),
    "photo": ("Photo mil gayi, dekh li 👍", "Mil gayi photo.", "Haan, dikh gayi 👍"),
    "design": ("Design mil gaya, shukriya 🙏", "Mil gaya design, dekh liya 👍", "Aa gaya, dekh liya maine 👍"),
    "design_team": ("Koi baat nahi, hamari team bana degi 👍", "Theek hai, design hum bana denge.", "Arre koi dikkat nahi, bana denge hum."),
    "size": ("{v} — note kar liya.", "Theek hai, {v}.", "{v}, likh liya."),
    "qty": ("{v} — samajh gayi.", "{v}, theek hai.", "Achha, {v}."),
    "city": ("{v} se hain, achha 👍", "Theek hai, {v}.", "{v} — note kiya."),
    "payment": ("Theek hai ji 👍", "Samajh gayi.", "Note kar liya 👍"),
}


def ack(slot: str, value, seed: int) -> str:
    key = "design_team" if slot == "design" and value == "team" else slot
    options = _ACK.get(key)
    if not options:
        return ""
    line = options[seed % len(options)]
    if "{v}" in line:
        said = _SERVICE_LABEL.get(value, value) if slot == "service" else value
        if not isinstance(said, str) or not said.strip():
            return ""
        said = said.strip()
        line = line.replace("{v}", said[0].upper() + said[1:])
    return line


def _short_item(sf: dict) -> str:
    bucket = re.sub(r"\s*\([^)]*\)", "", sf.get("variant") or "").strip()
    return sf.get("item_said") or bucket


def _item_line(sf: dict) -> str | None:
    """"mug (smooth hard surface)" — their word, then how it prints."""
    bucket = sf.get("variant")
    if not bucket:
        return None
    # The bucket labels carry examples for the customer; the team does not need them
    # repeated inside a bracket inside a bracket.
    short = re.sub(r"\s*\([^)]*\)", "", bucket).strip()
    said = sf.get("item_said")
    if said and said.lower() not in short.lower():
        return f"{said} ({short})"
    return short


def summary(sf: dict) -> str:
    rows = [
        ("Naam", sf.get("name")),
        ("Service", _SERVICE_LABEL.get(sf.get("service") or "", sf.get("service"))),
        ("Item", _item_line(sf)),
        ("Size", sf.get("size")),
        ("Quantity", sf.get("qty")),
        ("Pages", sf.get("pages")),
        ("Address", sf.get("city")),
        ("Contact", sf.get("phone")),
        ("Payment", sf.get("payment")),
        ("Design", {"team": "team banayegi", True: "mil gaya"}.get(sf.get("design"))),
    ]
    return "\n".join(f"• {k}: {v}" for k, v in rows if v)


_STALE_TAIL_RE = re.compile(
    r"[,.;]?\s*(?:aur\s+)?(?:wo\s+|team\s+)?(?:abhi\s+)?aapse\s+is\s+number\s+par\s+"
    r"baat\s+karegi\s*\U0001f44d?\s*$",
    re.I,
)


def quote(sf: dict, cfg: BotConfig, final: bool = True) -> str:
    """What we know, then a price only where there is a published one to give."""
    f = cfg.sales_flow
    parts = []
    s = summary(sf)
    if s:
        parts.append(f.summary_intro.strip() + "\n" + s)
    # Only UV DTF has a rate card. Showing sheet rates for a t-shirt or a signage job
    # would be a number we cannot stand behind.
    body = f.price_text.strip() if sf.get("service") == "uvdtf" else f.quote_by_team.strip()
    # A tenant config saved before the closing line was split out still carries that
    # line inside the rate text, and PUT /config freezes every default into the stored
    # JSON. Dropping the duplicate beats saying it twice in one message.
    trimmed = _STALE_TAIL_RE.sub("", body).rstrip(" ,;\u2014-")
    if trimmed and trimmed[-1].isalnum():
        trimmed += "."
    parts.append(trimmed or body)
    parts.append(f.delivery_note.strip())
    # Saying "team abhi call karegi" and then asking another question in the same breath
    # is the contradiction that gives a bot away. When the rate came out early because
    # they pushed for it, the close waits until the questions are actually done.
    parts.append((f.quote_tail if final else f.quote_more).strip())
    return "\n\n".join(parts)


def advance(sf: dict, text: str, kind: str, cfg: BotConfig) -> tuple[str | None, bool]:
    """Take one customer message. Returns (reply, quoted). None means 'not my turn'."""
    f = cfg.sales_flow
    if not f.enabled:
        return None, False

    before = {s: sf.get(s) for s in SLOTS}
    asked = sf.get("asked")
    absorb(sf, text, kind)

    # Already quoted and nothing left to ask: the funnel is finished, let the normal
    # FAQ / fallback path own the conversation again.
    if sf.get("quoted") and next_slot(sf, cfg) is None:
        # The rate went out early because they pushed for it, so the questions carried
        # on afterwards and the last one has only now been answered. Close it off: the
        # team's alert needs the full picture and the customer needs to hear that a
        # person is coming. Silence here reads as the bot having lost interest.
        if not sf.get("closed"):
            sf["closed"] = True
            sf["asked"] = None
            if any(sf.get(s_) and not before.get(s_) for s_ in SLOTS):
                return (f.closing_intro.strip() + "\n" + summary(sf)
                        + "\n\n" + f.quote_tail.strip()), True
        # The deal is done and this message answered nothing new — but a bare "thanks"
        # or "ok" still gets a human reply instead of silence.
        if _is_social(text):
            return f.social_reply.strip(), False
        return None, False

    # Once someone has stepped back, stay stepped back. next_slot() returns None while
    # paused, and without this that reads as "nothing left to ask" and fires a quote at
    # the customer who just said not now.
    if sf.get("paused"):
        if _is_social(text):
            return f.social_reply.strip(), False
        return None, False

    # A free-text answer to the question we actually asked — take it at face value
    # rather than dropping it.
    if asked in ("city", "variant", "payment") and not sf.get(asked) and not sf.get("switched_from_uvdtf"):
        _take(sf, asked, text, loose=True)

    # A customer who says "rehne do" gets a soft close, not another question. The funnel
    # stops asking; they can still ask whatever they like and the team can pick it up.
    low = " " + " ".join(_words(text)) + " "
    if not sf.get("quoted") and any(p in low.replace("  ", " ") for p in _BACKING_OFF):
        sf["paused"] = True
        sf["asked"] = None
        # Backing off is the moment a person would save the sale, not the moment to
        # stop selling. The bot gives one honest reason to stay and hands the chat to
        # the team, who can offer a sample or a bulk rate the bot is not allowed to.
        sf["needs_team"] = True
        return f.soft_close.strip(), False

    pushing = asks_price(text)
    if pushing:
        sf["price_asks"] = int(sf.get("price_asks", 0)) + 1

    # The customer answered something else. Asking the same question again until they
    # comply loses the lead, so give the question up and carry on. A message that filled
    # some other slot is not a refusal though — sending the product photo while we asked
    # for artwork is progress, and must not cost them the artwork question.
    filled_any = any(sf.get(s) and not before.get(s) for s in SLOTS)
    if asked and not sf.get(asked) and not pushing:
        tries = dict(sf.get("tries") or {})
        tries[asked] = tries.get(asked, 0) + 1
        sf["tries"] = tries
        limit = 1 if asked in ("photo", "design") else 2
        # A message that answered something else is not a refusal, but it cannot buy
        # the question unlimited retries either — otherwise a chatty customer can keep
        # the funnel stuck on one slot forever. One extra nudge, then move on: asking a
        # third time is what makes a bot feel like a bot.
        if tries[asked] >= (limit + 1 if filled_any else limit):
            sf["skipped"] = list(sf.get("skipped") or []) + [asked]

    slot = next_slot(sf, cfg)

    # Nothing in this message answered the question we asked — but people reply out of
    # order all the time ("Delhi" while we are still asking for artwork, "500" while we
    # are asking for a design). Offer it to the other open slots before giving up on it;
    # re-asking something the customer already told us is what gives a bot away.
    if slot and asked and not filled_any and not pushing:
        for cand in SLOTS:
            if cand == asked or sf.get(cand) or _skip(cand, cfg, sf):
                continue
            _take(sf, cand, text)
            if sf.get(cand):
                filled_any = True
                break
        if filled_any:
            slot = next_slot(sf, cfg) or slot

    if slot is None or (pushing and sf["price_asks"] >= f.price_after_asks):
        sf["quoted"] = True
        sf["asked"] = None
        reply = quote(sf, cfg, final=slot is None)
        if slot is not None:
            reply += "\n\n" + question(slot, sf, cfg)
            sf["asked"] = slot
        return reply, True

    sf["asked"] = slot
    lines = []
    if sf.pop("switched_from_uvdtf", None):
        lines.append(f.fabric_redirect.strip())
    for s in SLOTS:
        if sf.get(s) and not before.get(s):
            # "Mug par? Haan, us par achha aata hai" — not "Smooth hard surface (glass,
            # ceramic, steel, acrylic) par?". Read their own word back to them.
            value = _short_item(sf) if s == "variant" else sf.get(s)
            said = ack(s, value, len(text or ""))
            if said:
                lines.append(said)
            break
    # A real sales manager tells a customer about a better rate before they have to ask
    # for one. The volume breaks are on the published card, so suggesting them here is
    # not a made-up discount — it is the one they would get anyway, said a step early.
    if sf.get("pages") and not before.get("pages") and sf.get("service") == "uvdtf":
        n = re.match(r"\d+", sf["pages"])
        if n:
            is_a3 = (sf.get("size") or "").strip().upper() == "A3"
            if int(n.group()) < (5 if is_a3 else 10):
                lines.append((f.bulk_upsell_a3 if is_a3 else f.bulk_upsell).strip())
    # Said once, not on every message — a customer who keeps saying "jaldi" does not
    # want to hear the same reassurance back each time.
    if not sf.get("quoted") and not sf.get("urgency_acked") and (_words(text) & _URGENCY_WORDS):
        sf["urgency_acked"] = True
        lines.append(f.urgency_ack.strip())
    if pushing and not sf.get("quoted"):
        objection = bool(_words(text) & _OBJECTION_WORDS)
        lines.append((f.objection_text if objection else f.deflect_text).strip())
    lines.append(question(slot, sf, cfg))
    return "\n\n".join(lines), False
