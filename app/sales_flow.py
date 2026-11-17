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

SLOTS = ("name", "service", "variant", "photo", "design", "size", "qty", "city")

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
    ("gifting", "Corporate gifting / name plates",
     {"gift", "gifting", "hamper", "trophy", "memento", "nameplate", "plate", "diary"}),
    ("events", "Exhibition stall / event branding",
     {"exhibition", "stall", "event", "events", "canopy", "standee", "awning", "booth"}),
]
_SERVICE_LABEL = {k: label for k, label, _ in SERVICES}

# The fork inside each lane. Catching this early is what stops a fabric job being
# quoted at sticker rates.
VARIANTS: dict[str, tuple[str, list[tuple[str, set[str]]]]] = {
    "uvdtf": ("Kis cheez par lagana hai?", [
        ("mug", {"mug", "mugs", "cup", "cups"}),
        ("bottle", {"bottle", "bottles", "sipper", "tumbler", "flask", "steel"}),
        ("jar", {"jar", "jars", "dabba", "container"}),
        ("packaging box", {"packaging", "box", "boxes", "carton", "kraft", "pouch", "packing"}),
        ("glass", {"glass", "kaanch", "kanch", "mirror"}),
        ("acrylic / wood / metal", {"acrylic", "wood", "wooden", "mdf", "metal", "aluminium"}),
        ("gift item", {"gift", "hamper", "trophy", "memento"}),
        ("laptop / helmet / diary", {"laptop", "helmet", "diary", "notebook", "mobile", "cover"}),
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
        ("non-lit / ACP board", {"acp", "nonlit", "plain", "board"}),
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
                "charge", "charges", "kitna", "kitne", "kitni", "paisa", "paise", "rupaye", "rupees"}
_NO_DESIGN = {"nahi", "nhi", "no", "nahin", "banado", "banwana", "banwane", "banaoge", "aap", "tum"}
_NOT_A_NAME = {"hi", "hello", "hey", "namaste", "yes", "haan", "ok", "okay", "sticker", "price"}
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


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def asks_price(text: str) -> bool:
    return bool(_words(text) & _PRICE_WORDS)


def load(state: dict) -> dict:
    sf = dict(state.get("sf") or {})
    sf.setdefault("price_asks", 0)
    return sf


def save(state: dict, sf: dict) -> None:
    state["sf"] = sf


def _match(words: set[str], table) -> str | None:
    for label, keys in table:
        if words & keys:
            return label
    return None


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
            if found:
                sf["variant"] = found

    if not sf.get("size"):
        m = _SIZE_RE.search(text or "")
        if m:
            sf["size"] = m.group(0).strip()

    if not sf.get("qty"):
        m = _QTY_RE.search(text or "")
        if m:
            sf["qty"] = m.group(0).strip()

    # "design nahi hai, aap bana do" is a real answer to the artwork question.
    if not sf.get("design") and asked == "design" and words & _NO_DESIGN:
        sf["design"] = "team"

    # A name only counts when we actually asked for one — otherwise "sticker chahiye"
    # would be filed as somebody's name.
    if not sf.get("name") and asked == "name":
        cleaned = re.sub(r"\b(mera|naam|name|hai|is|ji|my)\b", " ", (text or "").lower()).strip(" .,-")
        cleaned = " ".join(cleaned.split())
        if cleaned and len(cleaned) <= 40 and not (_words(cleaned) & _NOT_A_NAME) and not asks_price(cleaned):
            sf["name"] = cleaned.title()


def _skip(slot: str, cfg: BotConfig) -> bool:
    f = cfg.sales_flow
    if slot == "photo" and not f.want_photo:
        return True
    if slot == "design" and not f.want_design:
        return True
    if slot == "name" and not f.want_name:
        return True
    return False


def next_slot(sf: dict, cfg: BotConfig) -> str | None:
    given_up = set(sf.get("skipped") or [])
    for slot in SLOTS:
        if slot in given_up or _skip(slot, cfg):
            continue
        # Nothing to fork on until a service is chosen, and some lanes have no fork.
        if slot == "variant" and (not sf.get("service") or sf["service"] not in VARIANTS):
            continue
        if not sf.get(slot):
            return slot
    return None


def service_menu() -> str:
    return "\n".join(f"• {label}" for _k, label, _w in SERVICES)


def question(slot: str, sf: dict, cfg: BotConfig) -> str:
    f = cfg.sales_flow
    if slot == "service":
        return f.ask_service.replace("{services}", service_menu())
    if slot == "variant":
        table = VARIANTS.get(sf.get("service") or "")
        if table:
            prompt, options = table
            opts = " / ".join(label for label, _ in options)
            return f.ask_variant.replace("{prompt}", prompt).replace("{options}", opts)
    text = getattr(f, f"ask_{slot}")
    first = (sf.get("name") or "").split(" ")[0]
    return text.replace("{name}", first) if first else text.replace("{name} ", "").replace("{name}", "")


# Answering a question with nothing but the next question is what makes a bot feel like
# a bot. Saying back what the customer just gave us costs one line and reads like someone
# is actually listening.
_ACK: dict[str, tuple[str, ...]] = {
    "name": ("Shukriya {v} ji 🙏", "Namaste {v} ji 🙏"),
    "service": ("{v} — bilkul, ye hamara roz ka kaam hai 👍", "{v}, theek hai."),
    "variant": ("{v} par? Haan, us par achha aata hai 👍", "{v} — note kar liya."),
    "photo": ("Photo mil gayi, dekh li 👍", "Mil gayi photo."),
    "design": ("Design mil gaya, shukriya 🙏", "Mil gaya design, dekh liya 👍"),
    "design_team": ("Koi baat nahi, hamari team bana degi 👍", "Theek hai, design hum bana denge."),
    "size": ("{v} — note kar liya.", "Theek hai, {v}."),
    "qty": ("{v} — samajh gayi.", "{v}, theek hai."),
    "city": ("{v} se hain, achha 👍", "Theek hai, {v}."),
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


def summary(sf: dict) -> str:
    rows = [
        ("Naam", sf.get("name")),
        ("Service", _SERVICE_LABEL.get(sf.get("service") or "", sf.get("service"))),
        ("Item", sf.get("variant")),
        ("Size", sf.get("size")),
        ("Quantity", sf.get("qty")),
        ("City", sf.get("city")),
        ("Design", {"team": "team banayegi", True: "mil gaya"}.get(sf.get("design"))),
    ]
    return "\n".join(f"• {k}: {v}" for k, v in rows if v)


def quote(sf: dict, cfg: BotConfig) -> str:
    """What we know, then a price only where there is a published one to give."""
    f = cfg.sales_flow
    parts = []
    s = summary(sf)
    if s:
        parts.append(f.summary_intro.strip() + "\n" + s)
    # Only UV DTF has a rate card. Showing sheet rates for a t-shirt or a signage job
    # would be a number we cannot stand behind.
    parts.append(f.price_text.strip() if sf.get("service") == "uvdtf" else f.quote_by_team.strip())
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
        return None, False

    # A free-text answer to the question we actually asked — take it at face value
    # rather than dropping it.
    if asked in ("city", "variant") and not sf.get(asked):
        cleaned = (text or "").strip()
        if cleaned and len(cleaned) <= 60 and not asks_price(cleaned):
            sf[asked] = cleaned

    pushing = asks_price(text)
    if pushing:
        sf["price_asks"] = int(sf.get("price_asks", 0)) + 1

    # The customer answered something else. Asking the same question again until they
    # comply loses the lead, so give the question up and carry on. A message that filled
    # some other slot is not a refusal though — sending the product photo while we asked
    # for artwork is progress, and must not cost them the artwork question.
    filled_any = any(sf.get(s) and not before.get(s) for s in SLOTS)
    if asked and not sf.get(asked) and not pushing and not filled_any:
        tries = dict(sf.get("tries") or {})
        tries[asked] = tries.get(asked, 0) + 1
        sf["tries"] = tries
        if tries[asked] >= (1 if asked in ("photo", "design") else 2):
            sf["skipped"] = list(sf.get("skipped") or []) + [asked]

    slot = next_slot(sf, cfg)

    if slot is None or (pushing and sf["price_asks"] >= f.price_after_asks):
        sf["quoted"] = True
        sf["asked"] = None
        reply = quote(sf, cfg)
        if slot is not None:
            reply += "\n\n" + question(slot, sf, cfg)
            sf["asked"] = slot
        return reply, True

    sf["asked"] = slot
    lines = []
    for s in SLOTS:
        if sf.get(s) and not before.get(s):
            said = ack(s, sf.get(s), len(text or ""))
            if said:
                lines.append(said)
            break
    if pushing and not sf.get("quoted"):
        lines.append(f.deflect_text.strip())
    lines.append(question(slot, sf, cfg))
    return "\n\n".join(lines), False
