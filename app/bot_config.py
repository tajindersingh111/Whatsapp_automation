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
