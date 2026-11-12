"""Per-tenant bot brain and automation settings, stored in tenants.config (JSONB)."""

from typing import Literal

from pydantic import BaseModel, Field


class Offer(BaseModel):
    code: str
    description: str  # e.g. "10% off on first order above ₹999"


class CodConfirmationFlow(BaseModel):
    enabled: bool = True
    # Approved Meta utility template. Body params: {{1}} name, {{2}} order number, {{3}} total.
    # Buttons: quick reply 0 = confirm, quick reply 1 = cancel.
    template_name: str = "cod_order_confirmation"
    template_language: str = "en"
    reminder_after_hours: float = 4
    max_reminders: int = 1
    confirmed_reply: str = "Thank you! Your order {number} is confirmed and will be shipped soon."
    cancelled_reply: str = "Your order {number} has been cancelled. If this was a mistake, just message us here."


class ShippingUpdateFlow(BaseModel):
    enabled: bool = True
    # Body params: {{1}} name, {{2}} order number, {{3}} courier, {{4}} tracking link.
    template_name: str = "order_shipped"
    template_language: str = "en"


class MissedCallFlow(BaseModel):
    """When a customer calls and nobody picks up, send them a WhatsApp message."""

    enabled: bool = True
    text: str = "Hi {name}! Aapka call miss ho gaya 🙏 Bataiye kya chahiye, hum yahin reply kar denge."


class LeadFollowupFlow(BaseModel):
    enabled: bool = True
    after_hours: float = 3
    max_followups: int = 1


class LeadAlertConfig(BaseModel):
    """Tell the team on WhatsApp the moment a lead is worth a call.

    The alert is business-initiated, so it has to go out as an approved utility
    template — free text to a team number only works inside a 24h window and would
    silently fail the rest of the time. Body params are {{1}} customer name,
    {{2}} their number, {{3}} a one-line summary of what they asked for.
    """

    enabled: bool = False
    # Team numbers as E.164 digits without '+', e.g. ["919079444040"].
    to_numbers: list[str] = Field(default_factory=list)
    template_name: str = "new_hot_lead"
    template_language: str = "en"
    # Take the chat off the bot once it is quoted, so the team can close it without
    # the bot talking over them.
    handoff_on_quote: bool = True


class SalesFlowConfig(BaseModel):
    """Qualify first, quote last — the slot-filling flow in app/sales_flow.py.

    Off by default: a brand with a public price list wants the FAQ to answer "price?"
    straight away. Turn it on where the rate depends on the job (print, fabrication,
    anything quoted per size and quantity) so leads arrive with the detail to quote.
    """

    enabled: bool = False
    want_photo: bool = True      # ask for a photo of the item the sticker goes on
    want_design: bool = True     # ask for their artwork
    # How many times a customer may ask for the rate before the bot just gives it.
    # Holding out longer than this on WhatsApp loses the lead.
    price_after_asks: int = 2

    deflect_text: str = "Rate bilkul bata deti hoon ji 👍 Bas ek cheez pooch loon taaki aapko exact rate mile, andaaza nahi."
    summary_intro: str = "Perfect ji 🙏 Aapki detail note kar li:"
    price_text: str = (
        "Rate ye rahe 👇\n"
        "*A4 sheet* — 1 se 5 sheet Rs 200/sheet, 10+ sheet Rs 180/sheet\n"
        "*A3 sheet* — 1 se 5 sheet Rs 300/sheet, 5+ sheet Rs 280/sheet\n\n"
        "Aapke design ke size se ek sheet par kitne stickers aayenge, team layout bana kar exact bata degi — "
        "usi hisaab se final rate. Team abhi aapse is number par baat karegi 👍"
    )

    ask_item: str = "Bahut badhiya 👍 Pehle ye bataiye — sticker kis cheez par lagana hai? (mug, bottle, jar, packaging box, glass, ya kuch aur)"
    ask_photo: str = "Us item ki ek photo bhej dijiye 📷 Isse main bata paungi ki sticker kaisa baithega aur kitna size sahi rahega."
    ask_design: str = "Ab apna design ya logo bhej dijiye. Vector file (AI, CDR, PDF, EPS) sabse achhi, warna high-resolution PNG bhi chalega. Design nahi hai to bata dijiye — hamari team bana deti hai."
    ask_size: str = "Sticker ka approx size kitna chahiye? (jaise 2 inch ya 3x3 inch) — isi se pata chalta hai ek sheet par kitne aayenge."
    ask_qty: str = "Aur approx kitne piece chahiye? (ya kitni sheet)"
    ask_city: str = "Last cheez — aap kis city se hain? Courier aur timeline uske hisaab se batate hain."


class MenuConfig(BaseModel):
    """Texts for the menu bot (no AI). {name}, {brand}, {hours}, {product} are filled in."""

    welcome_text: str = "Namaste {name}! 🙏 {brand} mein aapka swagat hai.\nAap kya karna chahenge? Neeche se chuniye ya product ka naam likhiye."
    products_label: str = "Products dekhein"
    track_label: str = "Order track karein"
    human_label: str = "Team se baat"
    categories_text: str = "Kaunsi category dekhna chahenge?"
    products_text: str = "Ye products dekhiye 👇 Kisi par tap karke details dekhein."
    search_results_text: str = "Aapki search ke liye ye mile 👇"
    product_buttons_text: str = "Order karne ke liye 'Order now' dabaiye."
    buy_text: str = "Neeche button dabakar order poora kijiye 🛒"
    variant_text: str = "Kaunsa option chahiye?"
    no_orders_text: str = "Is number par koi order nahi mila. Apna order number bhejiye (jaise 1023)."
    handoff_text: str = "Thik hai! Hamari team jaldi aapse baat karegi ({hours}). 🙏"
    not_understood: str = "Maaf kijiye, ye samajh nahi aaya 🙏 Neeche se chuniye ya product ka naam likhiye (jaise 'blue bedsheet')."
    followup_text: str = "Hi {name}! Aapne {product} dekha tha. Order karna ho to neeche button dabaiye, ya koi sawaal ho to poochiye 😊"


class BotConfig(BaseModel):
    enabled: bool = True
    # menu: buttons + FAQ + catalog search, no AI cost.  ai: Claude sales agent.
    reply_mode: Literal["menu", "ai"] = "menu"
    menu: MenuConfig = Field(default_factory=MenuConfig)
    sales_flow: SalesFlowConfig = Field(default_factory=SalesFlowConfig)
    lead_alert: LeadAlertConfig = Field(default_factory=LeadAlertConfig)
    brand_name: str = "Our Store"
    assistant_name: str = "Riya"
    store_url: str | None = None
    # Free-text description of the brand, audience, USP. Goes into the system prompt.
    about: str = ""
    tone: str = "Warm, helpful and concise, like a trusted shop assistant. Use 1-2 emojis at most."
    language: str = "Reply in the customer's language: Hindi, Hinglish or English."
    # Brand-specific dos and don'ts, one per line in the dashboard.
    rules: list[str] = Field(default_factory=list)
    offers: list[Offer] = Field(default_factory=list)
    allow_cod_orders_in_chat: bool = True
    # Phrases that should always go to a human.
    handoff_triggers: list[str] = Field(default_factory=lambda: ["complaint", "refund", "damaged", "legal"])
    support_hours: str = "10am-7pm, Monday to Saturday"

    model: str | None = None  # falls back to settings.default_model
    effort: str | None = None  # falls back to settings.default_effort

    cod_confirmation: CodConfirmationFlow = Field(default_factory=CodConfirmationFlow)
    shipping_update: ShippingUpdateFlow = Field(default_factory=ShippingUpdateFlow)
    lead_followup: LeadFollowupFlow = Field(default_factory=LeadFollowupFlow)
    missed_call: MissedCallFlow = Field(default_factory=MissedCallFlow)


def load_config(raw: dict | None) -> BotConfig:
    return BotConfig.model_validate(raw or {})
