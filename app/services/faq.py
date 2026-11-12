"""Answer common questions without AI: approved answers first, then knowledge docs.

Matching works on *topics*, not raw words. Every word that appears in a synonym group is
replaced by that group's id before comparing, so "kapde", "kapda" and "tshirt" all become
the same token and a stored answer keyed on "kapda" also answers "kapde par lagta hai?".
Words outside every group are compared as-is.

Two details matter for Hinglish chats:
  * the product-search stopword list drops "price", "rate" and "kitna" — exactly the words
    an FAQ needs — so any word that belongs to a synonym group is kept even if it is a
    stopword, on both the message and the stored question;
  * words are pulled with a regex, not str.split(), so "price?" still reads as "price".

Brands improve matching by adding short approved answers: a one or two word question like
"a4 price" is a key, not a sentence.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_config import BotConfig
from app.models import Correction, KnowledgeDoc
from app.services.catalog import query_terms

_WORD_RE = re.compile("[0-9A-Za-zÀ-ɏऀ-ॿ]+")

# Each group: words customers use -> they all mean the same topic. A word may appear in
# only one group; the first group that claims it wins.
SYNONYMS: list[set[str]] = [
    # --- generic commerce -------------------------------------------------
    {"shipping", "ship", "delivery", "deliver", "dispatch", "courier", "bhejoge", "bhejte", "bhijwa", "send"},
    {"kab", "kabtak", "days", "din", "dino", "time", "timeline", "jaldi", "urgent", "lagenge", "lagenga"},
    {"return", "returns", "exchange", "refund", "wapas", "vapas", "badalna", "badlna", "replace", "replacement"},
    {"cod", "cash"},
    {"offer", "offers", "discount", "coupon", "code", "sale", "off"},
    {"wash", "washing", "care", "dhona", "dhulai", "clean", "dishwasher"},
    {"size", "sizes", "measurement", "dimension", "dimensions", "fit", "inch", "inches", "cm", "mm"},
    {"payment", "pay", "upi", "card", "online", "prepaid", "advance", "paytm"},
    {"cancel", "cancellation", "cancelled"},
    {"store", "shop", "address", "location", "dukan", "showroom", "factory", "jaipur", "office", "kahan", "kaha"},
    {"bulk", "wholesale", "corporate", "gifting", "thok"},
    {"order", "orders", "booking", "book", "place", "confirm"},
    {"call", "phone", "number", "contact", "baat", "whatsapp"},
    {"gst", "bill", "invoice", "billing", "tax"},
    {"reseller", "dealer", "dealership", "distributor", "franchise"},

    # --- question words ---------------------------------------------------
    # "kitna" asks about an amount, which may be money OR time, so it is its own topic.
    {"kitna", "kitne", "kitni"},
    {"kis", "kaunsi", "kaunse", "konsa"},
    {"kaun", "bot", "robot", "insaan"},

    # --- money ------------------------------------------------------------
    {"price", "rate", "rates", "cost", "daam", "bhav", "quotation", "quote",
     "charge", "charges", "paisa", "paise", "rupaye", "rupees", "mehnga", "sasta"},
    {"a4"},
    {"a3"},
    {"sheet", "sheets"},
    {"minimum", "moq"},
    {"quantity", "qty", "piece", "pieces", "pcs", "nos"},

    # --- printing: what it goes on ---------------------------------------
    {"kapda", "kapde", "kapra", "kapdon", "cloth", "clothes", "fabric", "tshirt", "tshirts",
     "shirt", "cotton", "jersey", "towel", "hoodie", "banyan"},
    {"mug", "mugs", "cup", "cups"},
    {"bottle", "bottles", "jar", "jars", "sipper", "tumbler", "steel", "flask"},
    {"packaging", "box", "boxes", "carton", "dabba", "kraft", "pouch", "packet"},
    {"glass", "acrylic", "kaanch", "kanch", "wood", "wooden", "metal", "plastic", "leather"},
    {"surface", "item", "cheez", "cheezon", "chiz", "chize"},

    # --- printing: artwork, application, durability -----------------------
    {"sample", "samples", "demo", "trial"},
    {"file", "files", "format", "vector", "cdr", "eps", "png", "jpg", "jpeg", "svg", "resolution", "dpi"},
    {"design", "designs", "artwork", "logo", "banaoge", "banana", "banwana", "banwane", "banado"},
    {"lagana", "lagega", "lagta", "lagti", "lagegi", "lagate", "lagaye", "lagwana", "lagay",
     "chipkana", "chipkega", "chipakta", "apply", "application", "paste", "peel", "install", "rub"},
    {"heatpress", "press", "heat", "machine"},
    {"waterproof", "water", "paani", "pani", "geela"},
    {"scratch", "kharab", "nikal", "utar", "tikega", "chalega", "durable", "tikau"},
    {"dhoop", "sun", "sunlight", "outdoor"},
    {"shape", "cut", "cutting", "custom", "die"},

    # --- the rest of the catalogue of services ----------------------------
    {"services", "service", "signage", "led", "neon", "flex", "banner", "board", "vinyl",
     "wallpaper", "printing", "print", "cnc", "laser", "standee", "stall", "exhibition"},
]

_TOPIC: dict[str, int] = {}
for _i, _group in enumerate(SYNONYMS):
    for _word in _group:
        _TOPIC.setdefault(_word, _i)

_OFFER_TOPIC = _TOPIC["offer"]


@dataclass
class FaqAnswer:
    answer: str
    source: str  # "correction:<id>" | "knowledge:<id>" | "offers"


def _prep(text: str) -> str:
    # "cash on delivery" is about COD, not about delivery times.
    return re.sub(r"cash\s+on\s+delivery", "cod", text.lower())


def _words(text: str) -> set[str]:
    """Meaningful words, keeping topic words the product-search stopword list would drop."""
    return set(query_terms(text)) | {w for w in _WORD_RE.findall(text.lower()) if w in _TOPIC}


def _canon(words: set[str]) -> set[str]:
    """Collapse every known synonym to its topic id so spellings stop mattering."""
    return {f"@{_TOPIC[w]}" if w in _TOPIC else w for w in words}


def _topics(words: set[str]) -> set[int]:
    return {_TOPIC[w] for w in words if w in _TOPIC}


async def match_faq(session: AsyncSession, tenant_id: uuid.UUID, cfg: BotConfig, text: str) -> FaqAnswer | None:
    text = _prep(text)
    words = _words(text)
    if not words:
        return None
    canon = _canon(words)
    topics = _topics(words)

    # 1. Approved answers: most of the stored key's topics appear in the message.
    #    Ties go to the longer key, so "a4 price" beats a bare "price".
    best: tuple[float, int, Correction] | None = None
    stmt = select(Correction).where(Correction.tenant_id == tenant_id).order_by(Correction.id)
    for c in (await session.scalars(stmt)).all():
        key = _canon(_words(_prep(c.question)))
        if not key:
            continue
        score = len(key & canon) / len(key)
        if score >= 0.6 and (best is None or (score, len(key)) > (best[0], best[1])):
            best = (score, len(key), c)
    if best:
        return FaqAnswer(best[2].answer, f"correction:{best[2].id}")

    # 2. Offers come from the bot config, not from documents.
    if _OFFER_TOPIC in topics and cfg.offers:
        lines = "\n".join(f"• *{o.code}*: {o.description}" for o in cfg.offers)
        return FaqAnswer(f"Abhi ke offers:\n{lines}", "offers")

    # 3. Knowledge docs: the title must match the topic or words; the body breaks ties.
    #    A message can ask two things ("delivery kab? COD hai?"), so up to 2 docs on
    #    different topics are combined.
    scored: list[tuple[int, KnowledgeDoc, set[int]]] = []
    for doc in (await session.scalars(select(KnowledgeDoc).where(KnowledgeDoc.tenant_id == tenant_id))).all():
        title_words = _words(_prep(doc.title.replace("&", " ")))
        matched_topics = _topics(title_words) & topics
        title_hit = len(_canon(title_words) & canon)
        if title_hit == 0:
            continue
        body_hit = len(_canon(set(query_terms(doc.content[:3000]))) & canon)
        scored.append((title_hit * 3 + body_hit, doc, matched_topics))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    chosen = [scored[0]]
    covered = set(scored[0][2])
    for item in scored[1:]:
        if item[2] - covered:
            chosen.append(item)
            break
    limit = 900 if len(chosen) == 1 else 450
    parts = []
    for _, doc, _ in chosen:
        content = doc.content.strip()
        if len(content) > limit:
            content = content[:limit].rsplit(" ", 1)[0] + "…"
        parts.append(f"*{doc.title}*\n{content}")
    return FaqAnswer("\n\n".join(parts), ",".join(f"knowledge:{doc.id}" for _, doc, _ in chosen))
