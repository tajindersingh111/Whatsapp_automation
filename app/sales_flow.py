"""A qualifying sales flow for the menu bot: learn the job first, quote last.

An ad click usually opens with "price?". Answering that straight away turns the chat
into a rate comparison, so this walks the customer through a short slot-filling
conversation first — what the sticker goes on, a photo of that item, their artwork,
size, quantity, city — and only then shows the rate card together with a summary the
team can act on.

Two things keep it from feeling like an interrogation:
  * anything the customer volunteers is absorbed, so a message like "500 mugs ke liye
    3 inch sticker chahiye" fills three slots at once and skips those questions;
  * a customer who keeps pushing for the rate gets it. Stonewalling loses the lead, so
    after `price_after_asks` attempts the flow quotes and carries on collecting.

All state lives in `Conversation.state["sf"]`, so the flow survives between messages.
Everything here is pure (no DB, no I/O) so a whole conversation can be unit tested.
"""

import re

from app.bot_config import BotConfig

SLOTS = ("item", "photo", "design", "size", "qty", "city")

# What the sticker goes on. First match wins, so check fabric before anything else:
# it is the one answer that has to redirect the customer to a different process.
_ITEMS: list[tuple[str, set[str]]] = [
    ("kapde", {"kapda", "kapde", "kapra", "cloth", "clothes", "fabric", "tshirt", "tshirts",
               "shirt", "cotton", "jersey", "towel", "hoodie", "banyan", "bag"}),
    ("mug", {"mug", "mugs", "cup", "cups"}),
    ("bottle", {"bottle", "bottles", "sipper", "tumbler", "flask", "steel"}),
    ("jar", {"jar", "jars", "dabba", "container"}),
    ("packaging box", {"packaging", "box", "boxes", "carton", "kraft", "pouch", "packet", "packing"}),
    ("glass", {"glass", "kaanch", "kanch", "mirror"}),
    ("acrylic", {"acrylic"}),
    ("wood", {"wood", "wooden", "ply", "mdf"}),
    ("metal", {"metal", "steel plate", "aluminium"}),
    ("gift item", {"gift", "gifting", "hamper", "trophy", "memento"}),
    ("diary", {"diary", "notebook", "planner"}),
    ("laptop", {"laptop", "macbook"}),
    ("helmet", {"helmet"}),
    ("mobile cover", {"mobile", "cover", "phone case"}),
]

_PRICE_WORDS = {"price", "rate", "rates", "cost", "daam", "bhav", "quotation", "quote",
                "charge", "charges", "kitna", "kitne", "kitni", "paisa", "paise", "rupaye", "rupees"}
_NO_DESIGN = {"nahi", "nhi", "no", "nahin", "banado", "banwana", "banwane", "banaoge", "aap", "tum"}
_WORD_RE = re.compile("[0-9A-Za-zÀ-ɏऀ-ॿ]+")

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|\*|by|×)\s*(\d+(?:\.\d+)?)\s*(inch|in|cm|mm)?|(\d+(?:\.\d+)?)\s*(inch|inches|cm|mm)\b", re.I)
_QTY_RE = re.compile(r"(\d{1,6})\s*(piece|pieces|pcs|pc|nos|sticker|stickers|sheet|sheets|mug|mugs|bottle|bottles|box|boxes|unit|units)\b", re.I)


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


def absorb(sf: dict, text: str, kind: str = "text") -> None:
    """Fill whatever this message happens to answer, whichever question was asked."""
    words = _words(text)

    if kind in ("image", "sticker"):
        # A picture before any artwork talk is the product; after that it is the design.
        sf["design" if sf.get("photo") else "photo"] = True
    elif kind in ("document", "video"):
        sf["design"] = True

    if not sf.get("item"):
        for label, keys in _ITEMS:
            if words & keys:
                sf["item"] = label
                break

    if not sf.get("size"):
        m = _SIZE_RE.search(text or "")
        if m:
            sf["size"] = m.group(0).strip()

    if not sf.get("qty"):
        m = _QTY_RE.search(text or "")
        if m:
            sf["qty"] = m.group(0).strip()

    # "design nahi hai" / "aap bana do" is a real answer to the artwork question.
    if not sf.get("design") and sf.get("asked") == "design" and words & _NO_DESIGN:
        sf["design"] = "team"


def _skip(slot: str, cfg: BotConfig) -> bool:
    f = cfg.sales_flow
    return (slot == "photo" and not f.want_photo) or (slot == "design" and not f.want_design)


def next_slot(sf: dict, cfg: BotConfig) -> str | None:
    for slot in SLOTS:
        if not _skip(slot, cfg) and not sf.get(slot):
            return slot
    return None


def question(slot: str, cfg: BotConfig) -> str:
    return getattr(cfg.sales_flow, f"ask_{slot}")


def summary(sf: dict) -> str:
    rows = [
        ("Item", sf.get("item")),
        ("Size", sf.get("size")),
        ("Quantity", sf.get("qty")),
        ("City", sf.get("city")),
        ("Design", {"team": "team banayegi", True: "mil gaya"}.get(sf.get("design"))),
    ]
    return "\n".join(f"• {k}: {v}" for k, v in rows if v)


def quote(sf: dict, cfg: BotConfig) -> str:
    """The rate card plus whatever we know, so the team can pick it up cold."""
    parts = [cfg.sales_flow.price_text.strip()]
    s = summary(sf)
    if s:
        parts.insert(0, cfg.sales_flow.summary_intro.strip() + "\n" + s)
    return "\n\n".join(parts)


def advance(sf: dict, text: str, kind: str, cfg: BotConfig) -> tuple[str | None, bool]:
    """Take one customer message. Returns (reply, quoted). None means 'not my turn'."""
    f = cfg.sales_flow
    if not f.enabled:
        return None, False

    absorb(sf, text, kind)

    # Already quoted and nothing left to ask: the flow is finished, let the normal
    # FAQ / fallback path own the conversation again.
    if sf.get("quoted") and next_slot(sf, cfg) is None:
        return None, False

    # A free-text answer to the question we actually asked (city, or an item we
    # could not recognise) — take it at face value rather than dropping it.
    asked = sf.get("asked")
    if asked in ("city", "item") and not sf.get(asked):
        cleaned = (text or "").strip()
        if cleaned and len(cleaned) <= 60 and not asks_price(cleaned):
            sf[asked] = cleaned

    pushing = asks_price(text)
    if pushing:
        sf["price_asks"] = int(sf.get("price_asks", 0)) + 1

    slot = next_slot(sf, cfg)

    if slot is None or (pushing and sf["price_asks"] >= f.price_after_asks):
        sf["quoted"] = True
        sf["asked"] = None
        reply = quote(sf, cfg)
        if slot is not None:
            reply += "\n\n" + question(slot, cfg)
            sf["asked"] = slot
        return reply, True

    sf["asked"] = slot
    lead = f.deflect_text.strip() + "\n\n" if pushing else ""
    if sf.get("quoted"):
        lead = ""
    return lead + question(slot, cfg), False
