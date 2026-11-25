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
    # Also alert when a customer asks for a person, complains or is handed over for any
    # other reason. That is more urgent than a quoted lead, not less.
    alert_on_handoff: bool = True


class SalesFlowConfig(BaseModel):
    """Qualify first, quote last — the slot-filling flow in app/sales_flow.py.

    Off by default: a brand with a public price list wants the FAQ to answer "price?"
    straight away. Turn it on where the rate depends on the job (print, fabrication,
    anything quoted per size and quantity) so leads arrive with the detail to quote.
    """

    enabled: bool = False
    want_name: bool = True       # open by asking who we are talking to
    want_photo: bool = True      # ask for a photo of the item the print goes on
    want_design: bool = True     # ask for their artwork
    # How many times a customer may ask for the rate before the bot just gives it.
    # Holding out longer than this on WhatsApp loses the lead.
    price_after_asks: int = 2

    deflect_text: str = "Haan rate to abhi batati hoon 👍 Bas ek cheez jaan loon pehle, taaki aapko exact number milay — andaaze ka kya fayda."
    summary_intro: str = "Perfect 🙏 Ye sab note kar liya maine:"
    price_text: str = (
        "Rate ye rahe 👇\n"
        "*A4 sheet* — 1 se 5 sheet Rs 200/sheet, 10+ sheet Rs 180/sheet\n"
        "*A3 sheet* — 1 se 5 sheet Rs 300/sheet, 5+ sheet Rs 280/sheet\n\n"
        "Aapke design ke size se ek sheet par kitne stickers aayenge, team layout bana kar exact bata degi — "
        "usi hisaab se final rate.\n\n"
        "Payment 100% advance hota hai, +18% GST aur shipping charges extra lagte hain."
    )
    # Said after the summary, right before the closing line — dispatch speed and
    # delivery reach, the same on every lane, so it is not tied to a published rate.
    delivery_note: str = (
        "Payment ke baad dispatch same day ya next day mein ho jaata hai, courier se delivery "
        "approx 2-3 din lagti hai 🚚 Pan India delivery hoti hai, shipping charges extra lagenge — "
        "jitni jaldi order confirm karenge utni jaldi dispatch hoga 👍"
    )
    # The closing line, kept apart from the rate so it is only said when there is
    # genuinely nothing left to ask.
    quote_tail: str = "Order details hamari production/design team ko bhej di gayi hai 👍 Team bahut jaldi aapko is number par contact karke order finalize karegi."
    # Said right after a low sheet count, before the customer has to ask for a better
    # rate — the volume break is already on the card, not a discount invented on the spot.
    bulk_upsell: str = "Ek baat bata doon — 10+ sheet A4 lene par rate Rs 180/sheet ho jaata hai (abhi Rs 200/sheet hai), kaafi bachat ho jaati hai 👍 Chahen to quantity thodi badha sakte hain."
    bulk_upsell_a3: str = "Ek baat bata doon — 5+ sheet A3 lene par rate Rs 280/sheet ho jaata hai (abhi Rs 300/sheet hai) 👍 Chahen to quantity thodi badha sakte hain."
    # A bare "thanks"/"ok"/"hi" once the deal is done, or after a soft close. Falling
    # through to "samajh nahi aaya" right after closing a sale is what makes a bot
    # obvious; this keeps the door open instead.
    social_reply: str = "Bilkul ji 🙏 Koi aur sawaal ho ya kuch aur chahiye ho to bata dijiyega, main yahin hoon."
    # Said once, when the customer signals they want this quick.
    urgency_ack: str = "Bilkul samajh sakti hoon, jaldi chahiye aapko — main priority mein le leti hoon, bas ye detail jaldi se complete kar lein 👍"
    # Said when the last question gets answered after the rate already went out.
    closing_intro: str = "Bas ji, pura detail note ho gaya 🙏"
    # Said instead when the rate came out early because the customer pushed for it.
    quote_more: str = "Bas ek-do cheez reh gayi — wo bata dijiye, phir team exact layout aur final rate ke saath aapse baat karegi 👍"

    # Shown when the customer has not picked a lane yet. {services} is filled from the
    # catalogue in app/sales_flow.py, so the menu never drifts from what is actually sold.
    ask_service: str = (
        "Hamare yaha sabhi tarah ka signage aur printing ka kaam hota hai, bulk order mein \U0001f44d\n\n"
        "Ye sab banate hain:\n{services}\n\n"
        "Aapko inme se kya chahiye?"
    )
    # {prompt} and {options} come from the chosen service's fork.
    ask_variant: str = "{prompt}\n{options}"
    ask_name: str = "Namaste 🙏 Main Simran, HS Company se. Aapka naam jaan lun?"
    # First message already asks the rate (most ad clicks do). Promise it, then the name.
    ask_name_price: str = "Namaste 🙏 Main Simran, HS Company se. Rate zaroor bataungi — bas pehle aapka naam jaan lun?"
    # "photo/design abhi nahi hai" while we asked for the design — fine either way.
    no_problem_design: str = "Koi baat nahi 👍 Jab ho sake bhej dijiyega, warna hamari team bana degi."
    # A photo/file arriving after the quote or a soft close.
    media_received: str = "Mil gaya 👍 Team ko forward kar diya hai, wo isi ke hisaab se aage kaam karegi."
    ask_item: str = "Bahut badhiya 👍 Pehle ye bata dijiye — print lagana kis cheez par hai?"
    ask_photo: str = "Us cheez ki ek photo bhej dijiye na 📷 Dekh kar main sahi size bata paungi, warna andaaza hi rahega."
    # The sales script's own question ("kya aapke paas design ready hai? design bhej do")
    # and its own format rule (CDR/PNG only).
    ask_design: str = "Kya aapke paas design ready hai? Ho to yahin bhej dijiye — CDR ya PNG format mein. Nahi hai to koi baat nahi, hamari team bana degi 👍"
    # Generic lanes (signage, fabric, mug) — examples that fit a board and a t-shirt alike.
    ask_size: str = "{name} ji, kitne size ka chahiye? Jaise 4 inch, 12x18 inch ya 3x2 feet — size se hi rate banta hai."
    # UV DTF only prints in two page sizes, so the question is a choice, not an open one.
    ask_size_uvdtf: str = "Page size A4 chahiye ya A3? Hamare yaha inhi do size mein print hota hai, price bhi isi ke hisaab se hota hai."
    ask_qty: str = "Aur kitne piece ka soch rahe hain? Approx bhi chalega."
    # Only asked for UV DTF — the rate card is per sheet, not per piece.
    ask_pages: str = "Kitne sheet (page) print karwane hain? Jaise 5 sheet, 10 sheet."
    ask_city: str = "Delivery ka full address bata dijiye (house/shop no, area, city) — courier isi par bhejenge."
    ask_phone: str = "Aur ek contact number bata dijiye delivery ke liye — isi WhatsApp number par bhejna ho to 'yahi' likh dijiye."
    # Asked, not stated — the terms are the team's to set, the bot only learns the
    # customer's preference so the team is not starting from zero on the call.
    # Not a goodbye. One honest reason to stay, then a human who can actually offer a
    # sample or a better rate.
    soft_close: str = (
        "Koi baat nahi ji \U0001f64f Ek cheez bata doon \u2014 quantity badhne par rate kaafi neeche "
        "aa jaata hai, aur sample dekhne ka koi charge nahi hota.\n\n"
        "Main aapka number team ko de rahi hoon. Wo aapko sample aur apna best rate dono "
        "bhej denge \u2014 dekh kar aaram se decide kar lijiyega \U0001f44d"
    )
    # UV DTF does not bond to fabric. Saying so plainly, and moving them to the lane
    # that can do it, keeps the lead instead of quoting the wrong process.
    fabric_redirect: str = (
        "Ek baat bata doon ji — UV DTF sticker cloth par nahi chipakta, wo plain ya hard surface ke liye hai. "
        "Cloth/T-shirt ke liye hamari alag DTF machine hai, usmein design heat press se paste hota hai aur "
        "wash mein bhi nahi nikalta. Usi hisaab se aage badhte hain 👍"
    )
    # Said when the customer calls it expensive, which is an objection, not a question.
    objection_text: str = (
        "Samajh sakti hoon ji 🙏 Ek baat bata doon — rate quantity ke saath neeche aata hai, "
        "aur ek sheet par aapke design ke size ke hisaab se kai piece nikal jaate hain, "
        "to per piece kaafi sasta pad jaata hai."
    )
    ask_payment: str = "Aur payment aap kaise karna pasand karenge — UPI, bank transfer, ya cash? Team isi hisaab se detail bhej degi."

    # Everything except UV DTF is priced by the team, so the bot never invents a number
    # for a job that has no published rate.
    quote_by_team: str = (
        "Is kaam ka rate size, material aur quantity par banta hai, isliye main andaaza nahi dungi — "
        "hamari team aapko exact quote bhejegi."
    )


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
    handoff_text: str = "Bilkul, abhi bulati hoon unhe 🙏 Aapka message team tak pahuncha diya hai — wo jaldi aapse baat karenge ({hours})."
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
