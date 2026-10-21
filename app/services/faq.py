"""Answer common questions without AI: approved answers first, then knowledge docs.

Matching is word overlap with a small Hinglish synonym map, so "delivery kab tak aayega?"
finds a doc titled "Shipping & delivery". Brands improve it by adding approved answers.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_config import BotConfig
from app.models import Correction, KnowledgeDoc
from app.services.catalog import query_terms

# Each group: words customers use -> they all mean the same topic.
SYNONYMS: list[set[str]] = [
    {"shipping", "ship", "delivery", "deliver", "dispatch", "courier", "kab", "aayega", "ayega", "aaega", "pahunchega", "days", "din"},
    {"return", "returns", "exchange", "refund", "wapas", "vapas", "badalna", "badlna", "replace", "replacement"},
    {"cod", "cash"},
    {"offer", "offers", "discount", "coupon", "code", "sale", "off"},
    {"wash", "washing", "care", "dhona", "dhulai", "clean"},
    {"size", "sizes", "measurement", "dimension", "dimensions", "fit"},
    {"payment", "pay", "upi", "card", "online", "prepaid"},
    {"cancel", "cancellation", "cancelled"},
    {"store", "shop", "address", "location", "dukan", "showroom"},
    {"bulk", "wholesale", "corporate", "gifting"},
]
_TOPIC = {word: i for i, group in enumerate(SYNONYMS) for word in group}
_OFFER_TOPIC = _TOPIC["offer"]


@dataclass
class FaqAnswer:
    answer: str
    source: str  # "correction:<id>" | "knowledge:<id>" | "offers"


def _prep(text: str) -> str:
    # "cash on delivery" is about COD, not about delivery times.
    return re.sub(r"cash\s+on\s+delivery", "cod", text.lower())


def _topics(words: set[str]) -> set[int]:
    return {_TOPIC[w] for w in words if w in _TOPIC}


async def match_faq(session: AsyncSession, tenant_id: uuid.UUID, cfg: BotConfig, text: str) -> FaqAnswer | None:
    text = _prep(text)
    words = set(query_terms(text)) | {w for w in text.split() if w in _TOPIC}
    if not words:
        return None
    topics = _topics(words)

    # 1. Approved answers: most of the stored question's words appear in the message.
    best: tuple[float, Correction] | None = None
    for c in (await session.scalars(select(Correction).where(Correction.tenant_id == tenant_id))).all():
        q_words = set(query_terms(_prep(c.question)))
        if not q_words:
            continue
        overlap = len(q_words & words) + 0.5 * len(_topics(q_words) & topics - _topics(q_words & words))
        score = overlap / len(q_words)
        if score >= 0.6 and (best is None or score > best[0]):
            best = (score, c)
    if best:
        return FaqAnswer(best[1].answer, f"correction:{best[1].id}")

    # 2. Offers come from the bot config, not from documents.
    if _OFFER_TOPIC in topics and cfg.offers:
        lines = "\n".join(f"• *{o.code}*: {o.description}" for o in cfg.offers)
        return FaqAnswer(f"Abhi ke offers:\n{lines}", "offers")

    # 3. Knowledge docs: the title must match the topic or words; the body breaks ties.
    #    A message can ask two things ("delivery kab? COD hai?"), so up to 2 docs on different topics are combined.
    scored: list[tuple[int, KnowledgeDoc, set[int]]] = []
    for doc in (await session.scalars(select(KnowledgeDoc).where(KnowledgeDoc.tenant_id == tenant_id))).all():
        title = _prep(doc.title)
        title_words = set(query_terms(title)) | {w for w in title.replace("&", " ").split() if w in _TOPIC}
        matched_topics = _topics(title_words) & topics
        title_hit = len(title_words & words) + len(matched_topics)
        if title_hit == 0:
            continue
        body_hit = len(set(query_terms(doc.content[:3000])) & words)
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
