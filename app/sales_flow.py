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

# The lanes the factory sells — the list from the handwritten sales script, in its
# order, with what it crossed out left out (non-lit board, a separate wooden-door line,
# corporate). Shown to the customer in this order.
SERVICES: list[tuple[str, str, set[str]]] = [
    ("display", "Digital Display Signage", {"digital", "display", "lcd", "screen"}),
    ("led", "LED Board / Neon Sign", {"led", "neon"}),
    ("glow", "Glow Sign Board", {"glow", "glowsign", "backlit", "signboard", "sign", "signage", "board", "acp", "channel"}),
    ("inshop", "Inshop Branding — Sunboard, Vinyl, One Way", {"inshop", "instore", "sunboard", "vinyl", "oneway", "window"}),
    ("uvprint", "UV Printing", {"flatbed"}),
    ("uvdtf", "UV DTF Sticker",
     {"uvdtf", "sticker", "stickers", "transfer", "label", "labels", "stickering",
      # The things it goes on — "bottle par logo chahiye" is a UV DTF enquiry.
      "mug", "mugs", "bottle", "bottles", "tumbler", "sipper", "jar", "jars"}),
    ("fabric", "DTF Printing — T-shirt & Fabric",
     {"tshirt", "tshirts", "shirt", "kapda", "kapde", "kapra", "cloth", "clothes", "fabric",
      "cotton", "jersey", "hoodie", "banyan", "towel", "cap", "apron", "dtf"}),
    ("cnc", "CNC / Laser Cutting — Wooden Doors", {"cnc", "laser", "jali", "cutting", "door", "doors", "mdf", "engraving"}),
    ("events", "Exhibition Stall / Event Branding", {"exhibition", "stall", "event", "events", "canopy", "standee", "booth", "expo", "backdrop"}),
    ("canvas", "Canvas Printing", {"canvas", "painting"}),
    ("flex", "Flex Printing", {"flex", "banner", "hoarding"}),
]
_SERVICE_LABEL = {k: label for k, label, _ in SERVICES}


def service_label(sf: dict) -> str | None:
    """What to call this enquiry in a message to the customer — the chosen service, or
    the item they named if they got that far, else None if they never said either."""
    key = sf.get("service")
    if not key:
        return None
    label = _SERVICE_LABEL.get(key, key)
    item = sf.get("item_said")
    return f"{label} ({item})" if item else label
_SERVICE_WORDS = {w for _k, _l, keys in SERVICES for w in keys} | {"uv"}
_SERVICE_KEYS = {k: keys for k, _l, keys in SERVICES}
# Matching order is not display order. UV DTF goes first on purpose: it is the product
# the ads run on, and "dtf sticker" must not land in the garment lane. Specific lanes
# before the catch-all signboard one, so "LED board" is LED, not glow sign.
_SERVICE_MATCH_ORDER = ("uvdtf", "fabric", "led", "display", "inshop", "uvprint", "cnc",
                        "events", "canvas", "flex", "glow")


def _service_of(words: set[str]) -> str | None:
    # "UV" alone is ambiguous between UV printing (direct, flatbed) and UV DTF stickers.
    if "uv" in words:
        if words & {"dtf", "sticker", "stickers", "label", "labels", "transfer"}:
            return "uvdtf"
        if words & {"print", "printing", "printer", "flatbed"}:
            return "uvprint"
        return "uvdtf"  # the main product; the variant question sorts out the rest
    for key in _SERVICE_MATCH_ORDER:
        if words & _SERVICE_KEYS[key]:
            return key
    return None

# The fork inside each lane. Catching this early is what stops a fabric job being
# quoted at sticker rates.
VARIANTS: dict[str, tuple[str, list[tuple[str, set[str]]]]] = {
    # UV DTF sticks by surface, not by product name: a glass jar and a ceramic mug are
    # the same job, and a cotton bag is a different process entirely. Asking by surface
    # is how the factory actually decides, and it catches the fabric mistake up front.
    # Item words stay in the keywords so "mug" still lands in the right bucket.
    # The script: "UV DTF sticker bahut trending hai aajkal — plain ya hard surface par
    # sabse achha hota hai. Aapko kispe paste karna hai?" with curved items (tumbler) and
    # cloth as the two things to tell apart. Box, diary, glass and the like are all plain
    # hard surfaces as far as printing goes, so their words live in that bucket.
    "uvdtf": (
        "UV DTF aajkal bahut trending mein hai \U0001f44d Finish kaafi premium hota hai, aur bottles, glasses, "
        "jars, packaging, gifts jaisi bahut saari smooth surfaces par professional look deta hai.\n\n"
        "Aapko kispe paste karna hai?", [
        ("plain / hard surface (glass, steel, acrylic, box, diary)",
         {"glass", "kaanch", "kanch", "ceramic", "cup", "cups", "steel", "metal", "acrylic",
          "aluminium", "mirror", "tile", "plate", "trophy", "laptop", "helmet", "mobile",
          "packaging", "box", "boxes", "carton", "kraft", "pouch", "packet", "packing", "chocolate",
          "wood", "wooden", "mdf", "ply", "leather", "diary", "notebook", "planner", "frame",
          "plain", "hard", "flat", "surface"}),
        ("curved item (bottle, tumbler, sipper, jar, mug)",
         {"bottle", "bottles", "sipper", "tumbler", "flask", "jar", "jars", "container", "pen",
          "dabba", "mug", "mugs", "curved", "gol"}),
        ("fabric", {"kapda", "kapde", "kapra", "cloth", "fabric", "tshirt", "cotton", "bag", "cap"}),
    ]),
    # The script, for cloth: "Aapko T-shirt par banwana hai? Usme aapko heat press se
    # paste karna hoga." The heat-press line goes in fabric_redirect.
    "fabric": ("Aapko T-shirt par banwana hai ya kisi aur fabric par?", [
        ("t-shirt", {"tshirt", "tshirts", "shirt", "tee"}),
        ("hoodie / sweatshirt", {"hoodie", "sweatshirt", "jacket"}),
        ("cap", {"cap", "caps", "hat"}),
        ("bag", {"bag", "bags", "tote", "cotton bag"}),
        ("apron / uniform", {"apron", "uniform", "coat"}),
    ]),
    "led": ("Kya chahiye?", [
        ("LED board", {"led", "board", "light"}),
        ("neon sign", {"neon"}),
    ]),
    "cnc": ("Kya banwana hai?", [
        ("wooden door", {"door", "doors", "darwaza"}),
        ("jali / partition", {"jali", "partition"}),
        ("name plate", {"nameplate"}),
        ("other wood / acrylic", {"wood", "acrylic", "mdf"}),
    ]),
}

_PRICE_WORDS = {"price", "rate", "rates", "cost", "daam", "bhav", "quotation", "quote",
                "charge", "charges", "kitna", "kitne", "kitni", "paisa", "paise", "rupaye", "rupees",
                # Complaining about the price is a price message too. Missing these let the
                # FAQ answer "mehnga hai" with the sticker rate card — to a mug customer.
                "mehnga", "mahnga", "mehngi", "sasta", "sasti", "budget", "expensive",
                # "33 rupay ek logo?" — how it is actually typed.
                "rupay", "rupye", "rupya", "rs"}
# The subset that is a complaint rather than a question.
_OBJECTION_WORDS = {"mehnga", "mahnga", "mehngi", "budget", "expensive", "zyada", "jyada"}

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
# A greeting means "I'm back", not "thanks" — a completed order is not the end of the
# relationship, and a plain "hi" days later almost always means another one is wanted.
_GREETING_WORDS = {"hi", "hii", "hiii", "hy", "hello", "helo", "hey", "namaste", "namaskar"}

# Someone stepping back. Pushing the next question at them is how a bot loses a lead
# that a person would have kept warm.
_BACKING_OFF = ("chhodo", "chodo", "rehne do", "rahne do", "nahi chahiye", "nhi chahiye",
                "baad mein", "abhi nahi", "sochta hoon", "sochti hoon", "soch kar", "dekhte hain")
_NO_DESIGN = {"nahi", "nhi", "no", "nahin", "banado", "banwana", "banwane", "banaoge", "aap", "tum"}
# "photo abhi nahi hai" is about the product photo, not the artwork. Reading its "nahi"
# as "design nahi hai" told a customer who had a logo that the team would make one.
_PHOTO_WORDS = {"photo", "pic", "picture", "image", "tasveer", "foto"}
# "design hai mere paas", "logo ready hai" — they have artwork and will send it. Only
# words that mean *having* the file: "logo lagwana hai" also has "logo" and "hai" but
# means they want a logo printed, and reading it as "has a design" skipped the design
# question for a customer who had not answered it yet.
_HAS_DESIGN_WORDS = {"paas", "ready", "bhej", "bhejta", "bhejti", "bhejunga", "bhejungi",
                     "cdr", "png", "pdf", "file", "banaya", "bana"}
_NEGATIONS = {"nahi", "nhi", "nahin", "no", "nai"}
# Never an address: a refusal, a remark about the photo or design, a bare yes/no. Found
# live — "nahi photo nahi" was saved as the delivery address, the address question was
# then skipped, and the real address that came two messages later was thrown away.
_NOT_AN_ADDRESS = (_NEGATIONS | _PHOTO_WORDS | {"design", "logo", "file", "abhi", "baad", "haan", "ha",
                    "yes", "ok", "okay", "theek", "thik", "pata", "malum", "hai", "h"})
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
# UV DTF: the sheet the rate is per, and the size of one design on it.
_SHEET_RE = re.compile(r"\bA\s?[34]\b", re.I)
_SHEET_IN = {"A4": (8.27, 11.69), "A3": (11.69, 16.54)}
_DIM_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:x|\*|by|×)\s*(\d+(?:\.\d+)?)\s*(inch|inches|in|cm|mm|")?'
    r'|(\d+(?:\.\d+)?)\s*(inch|inches|cm|mm|")',
    re.I,
)
_TO_INCH = {"inch": 1.0, "inches": 1.0, "in": 1.0, '"': 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4}
# Breathing room for cutting: a margin round the sheet and a gap between pieces. Only
# used as a fallback when the size does not match a yield the factory has actually run —
# see _KNOWN_YIELD below, which is real counts, not a geometry guess.
_SHEET_MARGIN_IN = 0.4
_PIECE_GAP_IN = 0.2

# Actual pieces per sheet, from the factory's own production — not calculated, measured.
# Keyed by (sheet, sorted(width, height)) so "2x3" and "3x2" hit the same entry.
_KNOWN_YIELD: dict[tuple[str, float, float], int] = {
    ("A3", 1.5, 1.5): 54, ("A4", 1.5, 1.5): 30,
    ("A3", 2.0, 2.0): 35, ("A4", 2.0, 2.0): 15,
    ("A3", 2.5, 2.5): 24, ("A4", 2.5, 2.5): 12,
    ("A3", 2.0, 3.0): 25, ("A4", 2.0, 3.0): 9,
}


def _dims_inches(text: str | None) -> tuple[float, float] | None:
    m = _DIM_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        unit = (m.group(3) or "inch").lower()
        return float(m.group(1)) * _TO_INCH[unit], float(m.group(2)) * _TO_INCH[unit]
    k = _TO_INCH[m.group(5).lower()]
    side = float(m.group(4)) * k
    return side, side  # "3 inch ka logo" — about that big each way


def _dims_label(text: str | None) -> str | None:
    m = _DIM_RE.search(text or "")
    return m.group(0).strip() if m else None


def per_sheet(sf: dict) -> int | None:
    """How many of their design fit on the chosen sheet. A size the factory has actually
    run (_KNOWN_YIELD) wins over the geometric guess below it, every time."""
    sheet_key = (sf.get("size") or "").upper()
    dims = sf.get("sticker_in")
    if not dims:
        return None
    w, h = sorted(round(d, 2) for d in dims)
    known = _KNOWN_YIELD.get((sheet_key, w, h))
    if known is not None:
        return known
    sheet = _SHEET_IN.get(sheet_key)
    if not sheet:
        return None
    W, H = sheet[0] - _SHEET_MARGIN_IN, sheet[1] - _SHEET_MARGIN_IN
    fit = lambda a, b: int(W // (a + _PIECE_GAP_IN)) * int(H // (b + _PIECE_GAP_IN))
    return max(fit(w, h), fit(h, w))


_QTY_RE = re.compile(r"(\d{1,6})\s*(piece|pieces|pcs|pc|nos|sticker|stickers|sheet|sheets|mug|mugs|bottle|bottles|box|boxes|tshirt|tshirts|unit|units|meter|meters|metre|metres|mtr|mtrs)\b", re.I)
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
_MEDIA_KINDS = ("image", "sticker", "document", "video")

# "5 sheet mein kitne piece aayenge?" — a question, not an answer to the pages slot.
# Loose on word order ("kitne piece 5 sheet mein aayenge" asks the same thing) and on
# what sits between the number and the question word.
_SHEET_QTY_WORD_RE = re.compile(r"\bkitn[ei]\b", re.I)
_SHEET_QTY_PIECE_RE = re.compile(r"\b(piece|pieces|sticker|stickers)\b", re.I)
_SHEET_QTY_NUM_RE = re.compile(r"\b(\d{1,3})\s*sheets?\b", re.I)


def _asks_sheet_yield(text: str) -> int | None:
    if not (_SHEET_QTY_WORD_RE.search(text or "") and _SHEET_QTY_PIECE_RE.search(text or "")):
        return None
    m = _SHEET_QTY_NUM_RE.search(text or "")
    return int(m.group(1)) if m else None


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def asks_price(text: str) -> bool:
    return bool(_words(text) & _PRICE_WORDS)


#  Address filler that rides along with almost every real message ("thanks bhai",
# "ok yaar") and carries no meaning of its own, so it should not stop a message from
# counting as social.
_FILLER_WORDS = {"bhai", "yaar", "sir", "mam", "boss", "g", "bro", "dost",
                 # "theek hai thanks", "bahut shukriya", "ho gaya" — glue, not content.
                 "hai", "h", "ho", "gaya", "gya", "bahut", "bhut", "aapka", "apka", "aap", "sab", "ab"}


def _is_social(text: str) -> bool:
    """True for a message that is nothing but a pleasantry — "thanks bhai", "ok yaar",
    "hi" — and has no slot-answer or question buried in it."""
    w = _words(text)
    return bool(w & _SOCIAL_WORDS) and not (w - _SOCIAL_WORDS - _FILLER_WORDS)


def _is_greeting(text: str) -> bool:
    """True for a plain "hi"/"hello"/"namaste" — nothing else in the message."""
    w = _words(text)
    return bool(w & _GREETING_WORDS) and not (w - _GREETING_WORDS - _FILLER_WORDS)


def load(state: dict) -> dict:
    sf = dict(state.get("sf") or {})
    sf.setdefault("price_asks", 0)
    return sf


def save(state: dict, sf: dict) -> None:
    state["sf"] = sf


# "steel ki bottle", "glass jar", "wooden box": the material comes first and the object
# second, and it is the object that decides how a UV DTF print goes on. So when a reply
# names both, the shape buckets win over the material one. Matching order only — the
# options are still shown to the customer in the order the table lists them.
_MATCH_ORDER = {
    "uvdtf": ("curved item", "fabric", "plain / hard surface"),
}


def _match(words: set[str], table, service: str | None = None) -> tuple[str, str] | None:
    """Returns (bucket label, the word the customer actually used)."""
    order = _MATCH_ORDER.get(service or "")
    if order:
        table = sorted(table, key=lambda row: next((i for i, p in enumerate(order) if row[0].startswith(p)), len(order)))
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
    # A full "naam, address, mobile" reply is long; everything else this short.
    limit = 300 if (slot == "city" and loose) else 60
    if not cleaned or len(cleaned) > limit or asks_price(cleaned):
        return
    words = _words(cleaned)

    if slot == "city" and loose:
        # Asked for name + address + mobile together, the way the sales script does it.
        # The number is already read into its own slot, so it is cut out of the address.
        # A real address is several words with a house number or a comma in it — and may
        # well contain "Stickers" or "LED" in the shop's name, so product words alone are
        # no reason to refuse it here.
        address = _PHONE_RE.sub("", cleaned).strip(" ,.-")
        aw = _words(address)
        if (len(aw) >= 3 and (re.search(r"\d", address) or "," in address)
                and not (aw & (_NEGATIONS | _PHOTO_WORDS))):
            sf["city"] = address
            return
        cleaned, words = address, aw
        if not cleaned:
            return
        # The full-address reading already had its generous chance and did not pan out
        # ("2*2" is not an address with a house number or a comma just because it is
        # short) — the plain city check below must apply its own rules in full, not the
        # loose ones meant for "give the whole free-text reply the benefit of the doubt".
        loose = False

    if slot == "qty":
        m = _BARE_QTY_RE.search(cleaned)
        if m and not _SIZE_RE.search(cleaned):
            # DTF (fabric) is sold by the meter; nothing else here is.
            unit = "meter" if sf.get("service") == "fabric" else "piece"
            sf["qty"] = m.group(0).strip() + " " + unit
        return

    if slot == "city":
        # A payment word, an item or a bare number is a late answer to another question,
        # not a city. "City: cash" on the team's alert is exactly the kind of thing that
        # makes the whole chat look automated.
        if words & (_PAYMENT_WORDS | _PRODUCT_WORDS | _NOT_A_NAME | _NOT_AN_ADDRESS):
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
        # Something outside the list ("phone cover") is a fine answer. Repeating the
        # service ("uv dtf") or saying no is not — taking it made the team's alert read
        # "Item: uv dtf" and skipped the question that would have found the real item.
        if words & (_SERVICE_WORDS | _NEGATIONS | _PHOTO_WORDS) or _is_social(cleaned):
            return
        sf["variant"] = cleaned


def absorb(sf: dict, text: str, kind: str = "text") -> None:
    """Fill whatever this message happens to answer, whichever question was asked."""
    words = _words(text)
    asked = sf.get("asked")

    if kind in ("image", "sticker"):
        # A picture before any artwork talk is the product; after that it is the design.
        # UV DTF never asks for a product photo, and a picture sent while we are asking
        # for artwork is the artwork — either way it is the design.
        product_photo_open = (not sf.get("photo") and sf.get("service") != "uvdtf" and asked != "design"
                              and "photo" not in (sf.get("skipped") or []))
        sf["photo" if product_photo_open else "design"] = True
    elif kind in ("document", "video"):
        sf["design"] = True

    if not sf.get("service"):
        found_service = _service_of(words)
        if found_service:
            sf["service"] = found_service

    if sf.get("service") and not sf.get("variant"):
        table = VARIANTS.get(sf["service"])
        if table:
            found = _match(words, table[1], sf["service"])
            label = found[0] if found else None
            if label == "fabric":
                # Not a UV DTF job at all — move them to the lane that can actually do it
                # rather than quoting sticker rates for a garment.
                sf["service"] = "fabric"
                sf["switched_from_uvdtf"] = True
            elif found:
                sf["variant"] = label
                # Keep their own word too: "smooth hard surface" tells the team how it
                # will print, "mug" tells them what they are quoting.
                sf["item_said"] = found[1]

    if sf.get("service") == "uvdtf":
        # UV DTF has two sizes that matter and they are different things: the sheet it
        # prints on (A4/A3 — what the rate is per) and the size of their design. "logo 3
        # inch ka hai" used to be dropped, or worse, taken as the sheet size.
        if not sf.get("size"):
            m = _SHEET_RE.search(text or "")
            if m:
                # "a 4" — people space it out; the slot always stores the clean "A4".
                sf["size"] = m.group(0).upper().replace(" ", "")
        if not sf.get("sticker_in"):
            dims = _dims_inches(text)
            if dims:
                sf["sticker_in"] = list(dims)
                sf["sticker_size"] = _dims_label(text)
    elif not sf.get("size"):
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
                unit = "meter" if sf.get("service") == "fabric" else "piece"
                sf["qty"] = bare.group(0).strip() + " " + unit

    if not sf.get("pages") and asked == "pages":
        m = _PAGES_RE.search(text or "")
        if m:
            sf["pages"] = m.group(1).strip() + " sheet"

    # Once we know their design size, the sheet and how many pieces, the number of
    # sheets is arithmetic — ours to do, not a question for a customer who has no idea
    # how many 3-inch logos fit on an A4.
    if not sf.get("pages") and sf.get("service") == "uvdtf":
        per = per_sheet(sf)
        qty = re.match(r"\d+", sf.get("qty") or "")
        if per and qty:
            sf["pages"] = f"{-(-int(qty.group()) // per)} sheet (approx)"
            sf["pages_auto"] = True

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
    talks_design = bool(words & {"design", "logo", "artwork"})
    about_photo_only = bool(words & _PHOTO_WORDS) and not talks_design
    if (not sf.get("design") and not about_photo_only and words & _NO_DESIGN
            and (asked == "design" or talks_design)):
        sf["design"] = "team"
    # "design hai mere paas", "haan logo ready hai" — they have artwork and will send it.
    # Recorded so the funnel moves on instead of asking again; the file itself replaces
    # this the moment it arrives.
    elif (not sf.get("design") and not (words & _NEGATIONS)
          and kind not in ("image", "sticker", "document", "video")
          and ((talks_design and words & _HAS_DESIGN_WORDS)
               or (asked == "design" and words & {"haan", "ha", "yes", "ready", "h"}))):
        sf["design"] = "customer"

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
            # By word, not by whitespace: "Gurpreet, Ludhiana se" split on spaces gives
            # "gurpreet," — the comma failed isalpha() and the name was never taken.
            tokens = _WORD_RE.findall(cleaned)
            first = tokens[0] if tokens else ""
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
    # The UV DTF script goes item -> "design ready hai? bhej do" -> size. A photo of the
    # bottle it goes on was never part of it, and in the live playground two customers
    # out of two refused it straight away — friction with nothing to show for it.
    if slot == "photo" and (sf or {}).get("service") == "uvdtf":
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
    "design": "Design ho to bhej dijiye (PDF ya PNG) — nahi to hamare catalogue se select kar lijiye 👍",
    "size": "Size ka andaaza bhi chalega — kitne inch ka chahiye?",
    "variant": "Aapko kispe paste karna hai — jaise bottle, glass, box ya diary? Bas naam likh dijiye.",
    # UV DTF only prints on two sheet sizes; asking it "kitne inch" was asking a question
    # with no right answer.
    "size_uvdtf": "Bas ye bata dijiye — A4 sheet chahiye ya A3?",
    "qty": "Approx kitne piece? Bas moti si ginti bata dijiye.",
    "pages": "Kitne sheet chahiye, bas andaza bata dijiye.",
    "city": "Order confirm karne ke liye bas poora naam, address aur mobile number bhej dijiye 👍",
    "phone": "Contact number bata dijiye, delivery confirm karne ke kaam aayega.",
    "name": "Naam bata dijiye na, baat karne mein aasani rahegi 🙂",
    "payment": "Advance payment UPI se karenge ya bank transfer se?",
}


def question(slot: str, sf: dict, cfg: BotConfig) -> str:
    f = cfg.sales_flow
    if (sf.get("tries") or {}).get(slot) and slot in _RETRY:
        if slot == "size" and sf.get("service") == "uvdtf":
            return _RETRY["size_uvdtf"]
        if slot == "variant" and sf.get("service") != "uvdtf":
            table = VARIANTS.get(sf.get("service") or "")
            if table:
                return "Inme se kaunsa? Bas naam likh dijiye \U0001f447\n" + "\n".join("• " + label for label, _ in table[1])
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
    elif slot in ("design", "qty") and sf.get("service") == "fabric":
        # DTF (fabric) takes a vector file too, and is priced by the meter, not the
        # piece — the generic questions ask for things that don't apply here.
        text = getattr(f, f"ask_{slot}_fabric")
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
    # The script: no design -> "yahan se catalogue mein select karna hai, SS bhejwa denge".
    "design_team": ("Koi baat nahi 👍 Hamare catalogue se design select kar lijiye — main screenshots bhejwa deti hoon.",
                    "Theek hai 👍 Catalogue se design chun lijiye, screenshots main bhejwa deti hoon.",
                    "Koi dikkat nahi 👍 Hamare catalogue ke designs ke screenshots bhejwa deti hoon, wahan se select kar lijiye."),
    # The script: "Perfect! Aap apna logo/design yahan share kar dijiye, hum aapke design
    # ke according stickers prepare kar denge." The next question is the size itself, so
    # the ack does not also promise to ask it.
    "design_customer": ("Perfect! Jab ho sake yahin bhej dijiyega 👍 Aapke design ke according hi prepare karenge.",
                        "Badhiya, design ready hai to kaam fast ho jayega — yahin bhej dijiye.",
                        "Perfect, bas yahin bhej dijiyega jab free ho 👍 Aapke design ke hisaab se hi prepare karenge."),
    "size": ("{v} — note kar liya.", "Theek hai, {v}.", "{v}, likh liya."),
    "qty": ("{v} — samajh gayi.", "{v}, theek hai.", "Achha, {v}."),
    # It is a full delivery address now, not a city — "Shop 12, MI Road se hain" read
    # like the bot had not understood what it was given.
    "city": ("Address note kar liya 👍", "Theek hai, isi address par bhejenge.", "Achha, address likh liya."),
    "phone": ("Number note kar liya 👍", "Theek hai, isi number par contact karenge.", "Achha, number likh liya."),
    "payment": ("Theek hai ji 👍", "Samajh gayi.", "Note kar liya 👍"),
}


def ack(slot: str, value, seed: int) -> str:
    key = {"team": "design_team", "customer": "design_customer"}.get(value, slot) if slot == "design" else slot
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
    label = sf.get("variant") or ""
    # A plain label ("t-shirt", "cap") is already what they said, and better spelled —
    # "t-shirt" splits into "t" and "shirt", and reading back "Shirt" sounds off. Their
    # own word only beats a long descriptive bucket like "curved item (bottle, ...)".
    if "(" not in label:
        return label
    bucket = re.sub(r"\s*\([^)]*\)", "", label).strip()
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
        ("Design size", sf.get("sticker_size")),
        ("Quantity", sf.get("qty")),
        ("Pages", sf.get("pages")),
        ("Address", sf.get("city")),
        ("Contact", sf.get("phone")),
        ("Payment", sf.get("payment")),
        ("Design", {"team": "catalogue se select karenge (SS bhejne hain)", "customer": "customer ke paas hai, bhejenge", True: "mil gaya"}.get(sf.get("design"))),
    ]
    return "\n".join(f"• {k}: {v}" for k, v in rows if v)


_STALE_TAIL_RE = re.compile(
    r"[,.;]?\s*(?:aur\s+)?(?:wo\s+|team\s+)?(?:abhi\s+)?aapse\s+is\s+number\s+par\s+"
    r"baat\s+karegi\s*\U0001f44d?\s*$",
    re.I,
)


def rate_for(sf: dict, cfg: BotConfig, pages: int | None = None) -> int | None:
    """The per-sheet rate (GST included) that actually applies: the lower, volume rate
    at or above the minimum order, the higher one below it — a smaller order still gets
    made, just not at the rate a 5-sheet order gets."""
    f = cfg.sales_flow
    sheet = (sf.get("size") or "").upper()
    tier = f.sheet_rate.get(sheet)
    if not tier:
        return None
    if pages is None:
        n = re.match(r"\d+", sf.get("pages") or "")
        pages = int(n.group()) if n else None
    below, at_min = tier
    if pages is None:
        return at_min  # headline number before we know the count
    return at_min if pages >= f.min_sheets else below


def estimate_line(sf: dict, cfg: BotConfig) -> str | None:
    """'Aapka 3 inch logo A4 par ~6 aayega — approx Rs 33/piece.' The per-piece number is
    what a customer actually compares, and "Rs 200 per sheet" on its own hides it."""
    f = cfg.sales_flow
    if sf.get("service") != "uvdtf":
        return None
    sheet = (sf.get("size") or "").upper()
    per = per_sheet(sf)
    if per is None:
        return None
    label = sf.get("sticker_size") or "design"
    if per == 0:
        return f.estimate_too_big.replace("{size}", label).replace("{sheet}", sheet).strip()
    rate = rate_for(sf, cfg)
    if not rate:
        return None
    return (f.estimate_text.replace("{size}", label).replace("{sheet}", sheet)
            .replace("{n}", str(per)).replace("{per_piece}", str(round(rate / per)))
            .replace("{rate}", str(rate)).strip())


def below_min_line(sf: dict, cfg: BotConfig) -> str | None:
    """'2 sheet bhi ban jayegi, thoda zyada rate par — 5 sheet par kam padega.' Offered,
    not forced: the order stays at what they asked for unless they choose to raise it."""
    f = cfg.sales_flow
    if sf.get("service") != "uvdtf":
        return None
    sheet = (sf.get("size") or "").upper()
    n = re.match(r"\d+", sf.get("pages") or "")
    tier = f.sheet_rate.get(sheet)
    if not n or not tier or int(n.group()) >= f.min_sheets:
        return None
    below, at_min = tier
    return (f.below_min_text.replace("{count}", n.group()).replace("{sheet}", sheet)
            .replace("{below}", str(below)).replace("{min}", str(f.min_sheets))
            .replace("{at_min}", str(at_min)).strip())


def bulk_line(sf: dict, cfg: BotConfig) -> str | None:
    """'10+ sheet lene par rate aur kam ho jaata hai' — said once they are past 10, since
    there is no fixed number for it; the team settles the exact rate."""
    f = cfg.sales_flow
    if sf.get("service") != "uvdtf":
        return None
    n = re.match(r"\d+", sf.get("pages") or "")
    if not n or int(n.group()) <= 10:
        return None
    return f.above_ten_text.strip()


def quote_body(sf: dict, cfg: BotConfig) -> str:
    """The rate itself: the published card for UV DTF, the meter-based line for DTF
    (fabric), and a plain "team will quote you" for every other lane, which has no
    published rate to show."""
    f = cfg.sales_flow
    if sf.get("service") == "uvdtf":
        return f.price_text.strip()
    if sf.get("service") == "fabric":
        return f.fabric_quote_text.strip()
    return f.quote_by_team.strip()


def quote(sf: dict, cfg: BotConfig, final: bool = True) -> str:
    """What we know, then a price only where there is a published one to give."""
    f = cfg.sales_flow
    parts = []
    s = summary(sf)
    if s:
        parts.append(f.summary_intro.strip() + "\n" + s)
    # Their question first ("ek sheet mein kitne aayenge?"), the rate card after — it was
    # buried as the fifth paragraph of the reply that was supposed to answer it.
    est = estimate_line(sf, cfg)
    if est:
        parts.append(est)
    # Only UV DTF has a rate card. Showing sheet rates for a t-shirt or a signage job
    # would be a number we cannot stand behind.
    body = quote_body(sf, cfg)
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
    had_estimate = per_sheet(sf) is not None
    had_design_size = bool(sf.get("sticker_in"))
    absorb(sf, text, kind)
    pages_auto = sf.pop("pages_auto", False)

    # "5 sheet mein kitne piece aayenge?" is a direct question, not an answer to the
    # pages slot — answering it needs the sheet (A4/A3) and the design size, in that
    # order; ask for whichever is missing instead of guessing, and remember the count
    # across the turn it takes to get the sheet size.
    yield_n = sf.pop("pending_sheet_qty", None) or (
        _asks_sheet_yield(text) if sf.get("service") == "uvdtf" and not sf.get("quoted") else None)
    if yield_n:
        if not sf.get("size"):
            sf["pending_sheet_qty"] = yield_n
            sf["asked"] = "size"
            return f.ask_size_for_yield.strip(), False
        per = per_sheet(sf)
        if per:
            if not sf.get("pages"):
                sf["pages"] = f"{yield_n} sheet"
            return (f.sheet_yield_text.replace("{count}", str(yield_n)).replace("{sheet}", sf["size"])
                    .replace("{pieces}", str(per * yield_n))).strip(), False
        sf["pending_sheet_qty"] = yield_n  # sheet known, design size still is not
        return f.ask_design_size_for_yield.strip(), False

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
        # A fresh greeting means they are back for another order, not asking about the
        # last one — "hi" is also a social word, and without this check it got stuck
        # answering every future "hi" with "koi aur sawaal ho?" forever.
        if _is_greeting(text):
            sf.clear()
            sf["price_asks"] = 0
            return advance(sf, text, kind, cfg)
        # The deal is done and this message answered nothing new — but a bare "thanks"
        # or "ok" still gets a human reply instead of silence.
        if _is_social(text):
            return f.social_reply.strip(), False
        # The design they promised arriving after the quote is the most normal thing in
        # the world. Without this it fell through to "samajh nahi aaya".
        if kind in _MEDIA_KINDS:
            return f.media_received.strip(), False
        return None, False

    # Once someone has stepped back, stay stepped back. next_slot() returns None while
    # paused, and without this that reads as "nothing left to ask" and fires a quote at
    # the customer who just said not now.
    if sf.get("paused"):
        if _is_greeting(text):
            sf.clear()
            sf["price_asks"] = 0
            return advance(sf, text, kind, cfg)
        if _is_social(text):
            return f.social_reply.strip(), False
        if kind in _MEDIA_KINDS:
            return f.media_received.strip(), False
        return None, False

    # A free-text answer to the question we actually asked — take it at face value
    # rather than dropping it.
    if asked in ("city", "variant", "payment") and not sf.get(asked) and not sf.get("switched_from_uvdtf"):
        _take(sf, asked, text, loose=True)

    # A customer who says "rehne do" gets a soft close, not another question. The funnel
    # stops asking; they can still ask whatever they like and the team can pick it up.
    # In the order they were typed. This used to join _words(), which is a set — its
    # order changes with every process start, so "abhi nahi" matched on one server run
    # and not the next. Found live: the same "abhi nahi, sochta hoon" got a soft close
    # once and was ignored after a restart.
    low = " " + " ".join(_WORD_RE.findall((text or "").lower())) + " "
    # "photo abhi nahi hai" is about the photo, not about the order — reading it as
    # "abhi nahi (chahiye)" handed a willing customer a goodbye.
    about_a_file = bool(_words(text) & (_PHOTO_WORDS | {"design", "logo", "file", "artwork"}))
    if not sf.get("quoted") and not about_a_file and any(f" {p} " in low for p in _BACKING_OFF):
        sf["paused"] = True
        sf["asked"] = None
        # Backing off is the moment a person would save the sale, not the moment to
        # stop selling. The bot gives one honest reason to stay and hands the chat to
        # the team, who can offer a sample or a bulk rate the bot is not allowed to.
        sf["needs_team"] = True
        return f.soft_close.strip(), False

    # "zyada" is only a price complaint when it is about money — a rupee word, a number,
    # or after they have already seen the rate. "thoda zyada hai bhai" right after a quote
    # was ignored outright.
    objection = bool(_words(text) & _OBJECTION_WORDS) and (
        asks_price(text) or bool(sf.get("quoted")) or bool(re.search(r"\d", text or "")))
    pushing = asks_price(text) or objection
    if pushing:
        sf["price_asks"] = int(sf.get("price_asks", 0)) + 1

    # The customer answered something else. Asking the same question again until they
    # comply loses the lead, so give the question up and carry on. A message that filled
    # some other slot is not a refusal though — sending the product photo while we asked
    # for artwork is progress, and must not cost them the artwork question.
    filled_any = (any(sf.get(s) and not before.get(s) for s in SLOTS)
                  # The promised design file arriving is progress too, not a non-answer.
                  or (before.get("design") == "customer" and sf.get("design") is True)
                  # So is their design size, even though it is not a question we ask.
                  or (not had_design_size and bool(sf.get("sticker_in"))))
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

    # Once the rate card has gone out it does not go out again on every later mention of
    # price — that is a wall of text repeated, not an answer.
    if slot is None or (pushing and sf["price_asks"] >= f.price_after_asks and not sf.get("quoted")):
        sf["quoted"] = True
        sf["asked"] = None
        reply = quote(sf, cfg, final=slot is None)
        # "bahut mehnga hai" that tips us into giving the rate is still a complaint;
        # answering it with a bare rate card skipped the one line a person would say.
        if objection:
            reply = f.objection_text.strip() + "\n\n" + reply
        if slot is not None:
            reply += "\n\n" + question(slot, sf, cfg)
            sf["asked"] = slot
        return reply, True

    sf["asked"] = slot
    lines = []
    if sf.pop("switched_from_uvdtf", None):
        lines.append(f.fabric_redirect.strip())
    if slot == "name":
        # First contact: say who we are and ask their name, in one breath. Acking the
        # service and saying "rate abhi batati hoon" first stacked three paragraphs
        # before even a namaste — the reply read like a bot emptying its pockets.
        lines.append((f.ask_name_price if pushing else question("name", sf, cfg)).strip())
        return "\n\n".join(lines), False
    # A complaint gets heard before anything else, and replaces the "samajh gayi" ack —
    # "300 piece, samajh gayi. Samajh sakti hoon ji..." said the same thing twice.
    if objection:
        lines.append(f.objection_text.strip())
    for s in SLOTS:
        if objection:
            break
        upgraded = s == "design" and before.get(s) == "customer" and sf.get(s) is True
        if (sf.get(s) and not before.get(s)) or upgraded:
            # "Mug par? Haan, us par achha aata hai" — not "Smooth hard surface (glass,
            # ceramic, steel, acrylic) par?". Read their own word back to them.
            value = _short_item(sf) if s == "variant" else sf.get(s)
            said = ack(s, value, len(text or ""))
            if said:
                lines.append(said)
            break
    # Every chat hears what HS does, once. Asking the service shows it as part of the
    # question; when they named the service before we could ask, show it here and say
    # back what they asked for instead of asking it again.
    if slot == "service":
        sf["intro_shown"] = True
    elif not sf.get("intro_shown") and sf.get("service") and sf.get("name"):
        sf["intro_shown"] = True
        lines.append(f.company_intro.replace("{services}", service_menu()).strip())
        if not (sf.get("service") and not before.get("service")):  # acked above if new
            lines.append(f.service_known.replace(
                "{service}", _SERVICE_LABEL.get(sf["service"], sf["service"])).strip())
    # The moment we can work out how many fit on a sheet, say it, with the per-piece
    # price — the question every customer is really asking.
    if not had_estimate and per_sheet(sf) is not None:
        est = estimate_line(sf, cfg)
        if est:
            lines.append(est)
    if pages_auto:
        qty_n = re.match(r"\d+", sf.get("qty") or "")
        lines.append(f.pages_auto_text.replace("{qty}", qty_n.group() if qty_n else "")
                     .replace("{pages}", re.match(r"\d+", sf["pages"]).group())
                     .replace("{sheet}", (sf.get("size") or "").upper()).strip())
    # A real sales manager explains the rate before being asked. Below the minimum, that
    # is the honest tradeoff ("2 sheet bhi ban jayegi, thoda zyada rate par") rather than
    # a made-up discount; past 10 sheets, it is a real one worth mentioning.
    if sf.get("pages") and not before.get("pages") and sf.get("service") == "uvdtf":
        line = below_min_line(sf, cfg) or bulk_line(sf, cfg)
        if line:
            lines.append(line)
    # A plain "no" to something ("photo abhi nahi hai", "nahi photo nahi") that filled
    # nothing used to get silence and the next question — a person says "koi baat nahi"
    # first. Said about the design, it also covers both things they might have meant.
    if (asked and not filled_any and not pushing and not lines
            and _words(text) & _NEGATIONS and not sf.get(asked)):
        lines.append(f.no_problem_design.strip() if asked == "design" else "Koi baat nahi 👍")
    # Said once, not on every message — a customer who keeps saying "jaldi" does not
    # want to hear the same reassurance back each time.
    if not sf.get("quoted") and not sf.get("urgency_acked") and (_words(text) & _URGENCY_WORDS):
        sf["urgency_acked"] = True
        lines.append(f.urgency_ack.strip())
    if objection:
        # The answer to "zyada hai" is the concrete rate they qualify for, in rupees per
        # piece, plus the further discount past 10 sheets when that also applies —
        # unless a line above already said it.
        for extra in (estimate_line(sf, cfg), bulk_line(sf, cfg)):
            if extra and extra not in lines:
                lines.append(extra)
    elif pushing:
        if sf.get("quoted"):
            # Asked again after the rate already went out — answer again for real, every
            # time. "Maine upar bhej diya hai, check kar lijiye" is not an answer.
            again = estimate_line(sf, cfg)
            if not again:
                again = quote_body(sf, cfg)
            lines.append(again)
        else:
            lines.append(f.deflect_text.strip())
    lines.append(question(slot, sf, cfg))
    return "\n\n".join(lines), False
