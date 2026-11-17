"""Menu bot: WhatsApp buttons + lists, FAQ answers and catalog search. Zero AI cost.

Button/list ids (payloads):
    menu:main | menu:products | menu:track | menu:human
    cat:<category>          products in a category
    prod:<product_id>       product card with "Order now"
    buy:<product_id>        checkout link (or variant picker)
    buyv:<product_id>:<variant_id>
"""

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import sales_flow
from app.bot_config import BotConfig
from app.connectors.base import OrderItemInput, StoreConnector, StoreError
from app.models import Contact, Conversation, Message, Product, Tenant
from app.services import catalog
from app.services.conversations import set_lead_stage
from app.services import lead_alert
from app.services.faq import match_faq
from app.services.messaging import send_message
from app.services.orders import lookup_customer_orders, order_view
from app.whatsapp.client import Sender, buttons_message, cta_url_message, list_message, text_message

log = logging.getLogger(__name__)

GREETINGS = {
    "hi", "hii", "hiii", "hey", "hello", "helo", "hlo", "hy", "namaste", "namaskar", "menu", "start", "help",
    "good morning", "good evening", "gm", "ram ram", "radhe radhe", "jai shree krishna", "sat sri akal", "salam",
}
HUMAN_WORDS = {"human", "agent", "person", "insaan", "team", "call", "complaint", "shikayat", "refund", "damaged",
               "damage", "toota", "tuta", "kharab", "broken", "wrong", "galat", "manager", "owner", "baat"}
TRACK_WORDS = {"track", "tracking", "status", "kahan", "kaha", "kidhar", "where", "awb", "dispatch", "shipped", "pahuncha"}
WHEN_WORDS = {"kab", "aayega", "ayega", "aaega", "late", "delay", "abhi", "tak"}
_ORDER_NO = re.compile(r"#?\b([A-Za-z]{0,4}-?\d{3,12}(?:-\d+)*)\b")


@dataclass
class MenuResult:
    events: list[dict] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"[^\w\sऀ-ॿ]", " ", text.lower()).strip()


def _words(text: str) -> set[str]:
    return set(_norm(text).split())


def _price(p: Product) -> str:
    price = f"₹{p.price:,.0f}"
    if p.compare_at_price and p.compare_at_price > p.price:
        price += f" (MRP ₹{p.compare_at_price:,.0f})"
    return price


class MenuBot:
    def __init__(self, session: AsyncSession, tenant: Tenant, cfg: BotConfig, contact: Contact, conv: Conversation,
                 sender: Sender, connector: StoreConnector | None):
        self.session, self.tenant, self.cfg = session, tenant, cfg
        self.contact, self.conv, self.sender, self.connector = contact, conv, sender, connector
        self.m = cfg.menu
        self.state = dict(conv.state or {})
        self.result = MenuResult()

    # --- sending helpers ---

    async def send(self, message: dict) -> None:
        await send_message(self.session, self.sender, self.conv, self.contact, message, by="bot")

    def fmt(self, text: str, **extra: str) -> str:
        first = (self.contact.name or "").split(" ")[0] or "ji"
        values = {"name": first, "brand": self.cfg.brand_name, "hours": self.cfg.support_hours, "product": "products", **extra}
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return text

    def main_buttons(self) -> list[tuple[str, str]]:
        return [("menu:products", self.m.products_label), ("menu:track", self.m.track_label), ("menu:human", self.m.human_label)]

    # --- entry point ---

    async def handle(self, msg: Message) -> MenuResult:
        payload = (msg.payload or {}).get("button_payload") or ""
        text = msg.body.strip()
        if payload:
            await self.on_payload(payload)
        elif self.state.get("awaiting") == "order_number" and _ORDER_NO.search(text) and not _words(text) & GREETINGS:
            self.state.pop("awaiting", None)
            await self.track(_ORDER_NO.search(text).group(1))
        else:
            self.state.pop("awaiting", None)
            await self.on_text(text, msg.kind)
        self.conv.state = self.state
        return self.result

    async def on_text(self, text: str, kind: str = "text") -> None:
        norm, words = _norm(text), _words(text)
        greeting = norm in GREETINGS or (words and words <= GREETINGS | {"ji", "sir", "mam", "bhai"})
        # With the funnel on, "hi" is the start of the sale, not a request for the menu.
        # Sending the button menu first made the bot ask for a name one turn late, which
        # then landed the customer's real answer in the wrong slot.
        if greeting and not self.cfg.sales_flow.enabled:
            return await self.welcome()
        triggers = {t.lower() for t in self.cfg.handoff_triggers}
        if words & HUMAN_WORDS or any(t in norm for t in triggers):
            return await self.handoff(f"Customer asked: {text[:200]}")
        if words & TRACK_WORDS or ("order" in words and (_ORDER_NO.search(text) or words & WHEN_WORDS)):
            number = _ORDER_NO.search(text)
            return await self.track(number.group(1) if number else None)

        if self.cfg.sales_flow.enabled:
            if await self.sales_step(text, kind):
                return

        faq = await match_faq(self.session, self.tenant.id, self.cfg, text)
        if faq:
            self.result.events.append({"type": "faq", "source": faq.source})
            self.state["misses"] = 0
            if len(faq.answer) <= 1000:
                return await self.send(buttons_message(faq.answer, [("menu:products", self.m.products_label), ("menu:human", self.m.human_label)]))
            return await self.send(text_message(faq.answer))

        products = await catalog.search_products(self.session, self.tenant.id, text, limit=10)
        if products:
            self.state["misses"] = 0
            set_lead_stage(self.contact, "browsing", f"searched: {text[:120]}")
            return await self.product_list(products, self.m.search_results_text)

        self.state["misses"] = int(self.state.get("misses", 0)) + 1
        if not self.state.get("greeted"):
            return await self.welcome()
        await self.send(buttons_message(self.fmt(self.m.not_understood), self.main_buttons()))

    async def sales_step(self, text: str, kind: str) -> bool:
        """Qualify before quoting. True when the flow handled this message.

        A factual question mid-flow still gets its answer — the pending question is
        appended to it, so the customer is never stonewalled to keep the flow moving.
        """
        sf = sales_flow.load(self.state)
        pending = sf.get("asked")
        reply, quoted = sales_flow.advance(sf, text, kind, self.cfg)
        # A word can be both an answer and an FAQ hit: a customer typing their city
        # matched the "address" answer, so the bot recited its own address back at
        # someone who had just given theirs. If the message answered the question we
        # asked, it is an answer, not a question.
        answered_pending = bool(pending and sf.get(pending))
        faq = None
        if not answered_pending and not sales_flow.asks_price(text):
            faq = await match_faq(self.session, self.tenant.id, self.cfg, text)
        sales_flow.save(self.state, sf)
        if reply is None and faq is None:
            return False

        parts = []
        if faq:
            self.result.events.append({"type": "faq", "source": faq.source})
            parts.append(faq.answer)
        # Several stored answers ask a question of their own, and not always as the last
        # line — "...rehne wala hai kya?" sits mid-answer with a sign-off after it. Any
        # question mark at all means this answer already asked something, so the funnel
        # holds its own question back rather than asking two things at once.
        if reply and not (faq and "?" in faq.answer):
            parts.append(reply)
        body = "\n\n".join(parts)

        self.state["misses"] = 0
        summary = sales_flow.summary(sf)
        if quoted or sf.get("quoted"):
            set_lead_stage(self.contact, "interested", summary.replace("\n", "; ")[:240] or None)
        else:
            set_lead_stage(self.contact, "browsing", f"qualifying: {sf.get('asked') or ''}")
        self.result.events.append({"type": "sales_flow", "step": sf.get("asked"), "quoted": bool(sf.get("quoted"))})

        # A lead counts as hot once it is quoted AND every question is answered. A
        # customer who bullied the rate out early is not there yet, so the bot keeps
        # qualifying and this fires on the turn that completes the picture.
        # Someone stepping back is a lead worth a human, not a lost one — the team can
        # offer a sample or a bulk rate the bot is not allowed to promise.
        if sf.pop("needs_team", None):
            sf["alerted"] = True
            sales_flow.save(self.state, sf)
            self.conv.mode = "human"
            self.conv.handoff_reason = f"Backed off — {summary.replace(chr(10), '; ')}"[:500]
            set_lead_stage(self.contact, "interested", summary.replace("\n", "; ")[:240] or None)
            sent = await lead_alert.alert_team(
                self.session, self.tenant, self.cfg, self.contact, f"PEECHE HAT RAHA HAI · {summary}"
            )
            self.result.events.append({"type": "lead_alert", "sent": sent, "on": "backed_off"})

        complete = sf.get("quoted") and sales_flow.next_slot(sf, self.cfg) is None
        if complete and not sf.get("alerted"):
            sf["alerted"] = True
            sales_flow.save(self.state, sf)
            if self.cfg.lead_alert.handoff_on_quote:
                self.conv.mode = "human"
                self.conv.handoff_reason = f"Quoted lead — {lead_alert.one_line(summary)}"
            sent = await lead_alert.alert_team(self.session, self.tenant, self.cfg, self.contact, summary)
            self.result.events.append({"type": "lead_alert", "sent": sent})

        if len(body) <= 1000:
            await self.send(buttons_message(body, [("menu:human", self.m.human_label)]))
        else:
            await self.send(text_message(body))
        return True

    async def on_payload(self, payload: str) -> None:
        kind, _, rest = payload.partition(":")
        if payload == "menu:products":
            return await self.categories()
        if payload == "menu:track":
            return await self.track(None)
        if payload == "menu:human":
            return await self.handoff("Customer tapped 'talk to team'")
        if kind == "cat":
            return await self.category(rest)
        if kind == "prod":
            return await self.product_card(rest)
        if kind == "buy":
            return await self.buy(rest, None)
        if kind == "buyv":
            pid, _, vid = rest.partition(":")
            return await self.buy(pid, vid or None)
        await self.welcome()

    # --- screens ---

    async def welcome(self) -> None:
        self.state["greeted"] = True
        await self.send(buttons_message(self.fmt(self.m.welcome_text), self.main_buttons()))

    async def categories(self) -> None:
        rows = (await self.session.execute(
            select(Product.category, func.count())
            .where(Product.tenant_id == self.tenant.id, Product.active.is_(True), Product.category.is_not(None))
            .group_by(Product.category)
            .order_by(func.count().desc())
            .limit(10)
        )).all()
        if len(rows) <= 1:
            products = await catalog.search_products(self.session, self.tenant.id, "", limit=10)
            if not products:
                return await self.send(buttons_message(self.fmt(self.m.not_understood), self.main_buttons()))
            return await self.product_list(products, self.m.products_text)
        set_lead_stage(self.contact, "browsing")
        await self.send(list_message(
            self.fmt(self.m.categories_text),
            self.m.products_label,
            [("Categories", [(f"cat:{name}"[:200], name, f"{count} products") for name, count in rows])],
        ))

    async def category(self, name: str) -> None:
        products = (await self.session.scalars(
            select(Product)
            .where(Product.tenant_id == self.tenant.id, Product.active.is_(True), Product.category == name)
            .order_by(Product.in_stock.desc(), Product.price)
            .limit(10)
        )).all()
        if not products:
            return await self.categories()
        self.state["category"] = name
        await self.product_list(list(products), self.m.products_text)

    async def product_list(self, products: list[Product], body: str) -> None:
        rows = [(f"prod:{p.external_id}", p.title, _price(p) if p.in_stock else f"{_price(p)} · out of stock") for p in products[:10]]
        await self.send(list_message(self.fmt(body), self.m.products_label, [("Products", rows)]))

    async def product_card(self, product_id: str) -> None:
        p = await catalog.get_product(self.session, self.tenant.id, product_id)
        if p is None or not p.active:
            return await self.categories()
        self.state["last_product"] = p.external_id
        set_lead_stage(self.contact, "interested", f"viewed {p.title[:100]}")
        lines = [f"*{p.title}*", _price(p)]
        if p.variants:
            options = ", ".join(v["title"] for v in p.variants if v.get("in_stock", True))
            if options:
                lines.append(f"Options: {options}")
        if p.description:
            desc = p.description.strip()
            lines.append(desc[:400].rsplit(" ", 1)[0] + "…" if len(desc) > 400 else desc)
        if not p.in_stock:
            lines.append("_Abhi out of stock hai._")
            buttons = [("menu:products", self.m.products_label), ("menu:human", self.m.human_label)]
        else:
            lines.append(self.fmt(self.m.product_buttons_text))
            buttons = [(f"buy:{p.external_id}", "Order now"), ("menu:products", self.m.products_label), ("menu:human", self.m.human_label)]
        await self.send(buttons_message("\n".join(lines), buttons, image_url=p.image_url))

    async def buy(self, product_id: str, variant_id: str | None) -> None:
        p = await catalog.get_product(self.session, self.tenant.id, product_id)
        if p is None or not p.active:
            return await self.categories()
        if p.variants and not variant_id:
            rows = [(f"buyv:{p.external_id}:{v['id']}", v["title"], f"₹{float(v['price']):,.0f}") for v in p.variants if v.get("in_stock", True)]
            if not rows:
                return await self.send(buttons_message("Ye abhi out of stock hai.", [("menu:products", self.m.products_label), ("menu:human", self.m.human_label)]))
            return await self.send(list_message(self.fmt(self.m.variant_text), "Options", [(p.title, rows)]))
        url = None
        if self.connector is not None:
            try:
                url = await self.connector.create_checkout_link([OrderItemInput(product_id=p.external_id, variant_id=variant_id, quantity=1)])
            except StoreError as exc:
                log.warning("checkout link failed for %s: %s", p.external_id, exc)
        url = url or p.url
        if not url:
            return await self.handoff(f"Wants to buy {p.title} but no checkout link is available")
        set_lead_stage(self.contact, "checkout_sent", f"checkout link for {p.title[:100]}")
        self.result.events.append({"type": "checkout_link", "url": url})
        await self.send(cta_url_message(f"*{p.title}*\n{_price(p)}\n\n{self.fmt(self.m.buy_text)}", "Order now", url))

    async def track(self, order_number: str | None) -> None:
        orders, note = await lookup_customer_orders(self.session, self.tenant.id, self.connector, self.contact, order_number)
        if not orders:
            self.state["awaiting"] = "order_number"
            if note and "different phone" in note:
                text = "Ye order kisi aur number se place hua hai. Usi number se message kijiye, ya team se baat kijiye."
            elif order_number:
                text = f"Order {order_number} nahi mila. Number check karke dobara bhejiye."
            else:
                text = self.m.no_orders_text
            return await self.send(buttons_message(text, [("menu:human", self.m.human_label), ("menu:main", "Menu")]))
        parts = []
        for o in orders[:3]:
            v = order_view(o)
            lines = [f"*Order {v['order_number']}*", f"Status: {v['status']}", f"Total: {v['total']} ({v['payment']})"]
            tracking = v.get("tracking") or {}
            if tracking.get("courier") or tracking.get("awb"):
                lines.append(f"Courier: {tracking.get('courier') or ''} {('AWB ' + tracking['awb']) if tracking.get('awb') else ''}".strip())
            if tracking.get("url"):
                lines.append(f"Track: {tracking['url']}")
            parts.append("\n".join(lines))
        await self.send(buttons_message("\n\n".join(parts)[:1000], [("menu:main", "Menu"), ("menu:human", self.m.human_label)]))

    async def handoff(self, reason: str) -> None:
        already = self.conv.mode == "human"
        self.conv.mode = "human"
        self.conv.handoff_reason = reason[:500]
        self.result.events.append({"type": "handoff", "reason": reason})
        await self.send(text_message(self.fmt(self.m.handoff_text)))
        # Someone asking for a person is the most urgent thing that happens here, so the
        # team hears about it on their own phone. Only on the turn the chat flips over,
        # not again on every later message in the same handed-over chat.
        if not already and self.cfg.lead_alert.alert_on_handoff:
            sf = sales_flow.load(self.state)
            detail = sales_flow.summary(sf).replace("\n", "; ") or reason
            sent = await lead_alert.alert_team(
                self.session, self.tenant, self.cfg, self.contact, f"TEAM SE BAAT · {detail}"
            )
            self.result.events.append({"type": "lead_alert", "sent": sent, "on": "handoff"})


async def run_menu_bot(session: AsyncSession, *, tenant: Tenant, cfg: BotConfig, contact: Contact, conv: Conversation,
                       sender: Sender, connector: StoreConnector | None, message: Message) -> MenuResult:
    return await MenuBot(session, tenant, cfg, contact, conv, sender, connector).handle(message)


def menu_followup(cfg: BotConfig, contact: Contact, conv: Conversation, product: Product | None) -> dict:
    first = (contact.name or "").split(" ")[0] or "ji"
    text = cfg.menu.followup_text
    try:
        text = text.format(name=first, product=product.title if product else "hamare products", brand=cfg.brand_name, hours=cfg.support_hours)
    except (KeyError, IndexError, ValueError):
        pass
    if product is not None and product.in_stock:
        return buttons_message(text, [(f"buy:{product.external_id}", "Order now"), ("menu:products", cfg.menu.products_label)])
    return buttons_message(text, [("menu:products", cfg.menu.products_label), ("menu:human", cfg.menu.human_label)])
