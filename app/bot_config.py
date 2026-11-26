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

    deflect_text: str = "Pricing design ke size aur quantity par depend karti hai 👍 Aap size ya approximate requirement aur quantity bata dijiye, main aapko best available pricing share kar deti hoon."
    summary_intro: str = "Perfect 🙏 Ye sab note kar liya maine:"
    price_text: str = (
        "Rate ye rahe 👇 (GST included)\n"
        "*A4 sheet* — 5+ sheet Rs 180/sheet, 5 se kam par Rs 295/sheet\n"
        "*A3 sheet* — 5+ sheet Rs 350/sheet, 5 se kam par Rs 400/sheet\n"
        "10+ sheet lene par rate aur bhi kam ho jaata hai.\n"
        "Minimum order 5 sheet ka hai — usse kam bhi ban jaata hai, thoda zyada rate par.\n\n"
        "Aapke design ke size se ek sheet par kitne stickers aayenge, team layout bana kar exact bata degi — "
        "usi hisaab se final rate.\n\n"
        "Payment 100% advance hota hai, shipping charges extra lagte hain."
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
    # Per sheet, GST included: [below the minimum order, at/above it]. A smaller order
    # still gets made, just not at the volume rate. Keep in step with price_text.
    sheet_rate: dict[str, list[int]] = Field(default_factory=lambda: {"A4": [295, 180], "A3": [400, 350]})
    # {size} their design, {sheet} A4/A3, {n} pieces per sheet, {per_piece} Rs, {rate} Rs/sheet.
    estimate_text: str = "Aapka {size} ka design ek {sheet} sheet par lagbhag {n} piece aayega — yaani approx Rs {per_piece} per piece (Rs {rate}/sheet ke hisaab se, GST included) 👍"
    estimate_too_big: str = "{size} ka design {sheet} sheet se bada pad raha hai — team aapko sahi sheet/layout suggest karegi."
    # "Minimum order — 5 sheet." Below it, a smaller order is still offered, at the
    # higher rate, with the lower one named as the reason to round up.
    min_sheets: int = 5
    # {count} their sheets, {sheet} A4/A3, {below} and {at_min} the two rates, {min} 5.
    below_min_text: str = "Theek hai, {count} {sheet} sheet bhi ban jayegi, lekin price thoda zyada hoga — Rs {below}/sheet. {min} sheet banwa lijiye to Rs {at_min}/sheet ka rate milega, kaafi kam padega 👍"
    # Said once past 10 sheets — a real further discount, with no fixed number to quote.
    above_ten_text: str = "10+ sheet lene par rate aur bhi kam ho jaata hai — team exact rate confirm kar degi 👍"
    # Sheets worked out from quantity and design size, instead of asking.
    pages_auto_text: str = "{qty} piece ke liye lagbhag {pages} {sheet} sheet lagengi 👍"

    # "5 sheet mein kitne piece aayenge?" asked before we know which sheet they mean.
    ask_size_for_yield: str = "Aapko sheet konsi chahiye — A4 ya A3? Uske according aapko bata dungi 👍"
    # Sheet known, design size still isn't — can't multiply without it.
    ask_design_size_for_yield: str = "Design ka size bata dijiye (jaise 2 inch ya 2x2 inch), tabhi exact piece count bata paungi 👍"
    # {count} sheets asked about, {sheet} A4/A3, {pieces} the multiplied answer.
    sheet_yield_text: str = "{count} {sheet} sheet mein lagbhag {pieces} piece aa jayenge 👍"
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
        "Hamare yaha sabhi tarah ka signage aur printing ka kaam hota hai — sirf bulk order \U0001f44d\n\n"
        "Hum ye sab banate hain:\n{services}\n\n"
        "Aapko inme se kya service chahiye?"
    )
    # The same company intro, shown when the customer named the service before we could
    # ask (most ad clicks: "UV DTF ka rate kya hai?"). The sales script always opens with
    # it, and skipping it meant they never heard what else HS does.
    company_intro: str = (
        "Hamare yaha sabhi tarah ka signage aur printing ka kaam hota hai — sirf bulk order \U0001f44d\n\n"
        "Hum ye sab banate hain:\n{services}"
    )
    # Follows company_intro, so the service they already asked for is not asked again.
    service_known: str = "Aapne {service} ke baare mein poocha tha — ye hamara sabse demand wala kaam hai \U0001f44d"
    # {prompt} and {options} come from the chosen service's fork.
    ask_variant: str = "{prompt}\n{options}"
    # The script: "Simer -> Name: Aapka naam jaan sakti hoon?"
    ask_name: str = "Namaste 🙏 Main Simer, HS Company se. Aapka naam jaan sakti hoon?"
    # First message already asks the rate (most ad clicks do). Promise it, then the name.
    ask_name_price: str = "Namaste 🙏 Main Simer, HS Company se. Rate zaroor bataungi — pehle aapka naam jaan sakti hoon?"
    # When nothing matched. The menu bot's version says "neeche se chuniye ... jaise
    # 'blue bedsheet'" — buttons that are gone, for a shop that does not sell bedsheets.
    not_understood: str = "Ye main theek se samajh nahi paayi 🙏 Thoda aur bata dijiye — ya team se baat karni ho to 'team se baat' likh dijiye."
    # "photo/design abhi nahi hai" while we asked for the design — fine either way.
    no_problem_design: str = "Koi baat nahi 👍 Hum ready designs bhi dikha sakte hain — hamare catalogue se select kar lijiye, main screenshots bhejwa deti hoon."
    # A photo/file arriving after the quote or a soft close.
    media_received: str = "Mil gaya 👍 Team ko forward kar diya hai, wo isi ke hisaab se aage kaam karegi."
    ask_item: str = "Bahut badhiya 👍 Pehle ye bata dijiye — print lagana kis cheez par hai?"
    ask_photo: str = "Us cheez ki ek photo bhej dijiye na 📷 Dekh kar main sahi size bata paungi, warna andaaza hi rahega."
    # The script: "Kya aapke paas design ready hai? -> Design bhej do (PDF/PNG format
    # only). Nahi hai -> yahan se catalogue mein select karna hai, SS bhejwa denge."
    ask_design: str = "Theek hai 👍 Kya aapke paas design ready hai? Ho to yahin bhej dijiye (PDF ya PNG format mein). Nahi hai to hamare catalogue se select kar sakte hain — main screenshots bhejwa deti hoon."
    # DTF (fabric) also takes a vector file, since garment printing is often cut from one.
    ask_design_fabric: str = "Kya aapka khud ka design hai? Ho to yahin bhej dijiye — PDF, PNG, CDR teeno file accept hain. Nahi hai to hamare catalogue se select kar sakte hain — main screenshots bhejwa deti hoon."
    # Generic lanes (signage, fabric, mug) — examples that fit a board and a t-shirt alike.
    ask_size: str = "{name} ji, kitne size ka chahiye? Jaise 4 inch, 12x18 inch ya 3x2 feet — size se hi rate banta hai."
    # UV DTF only prints in two page sizes, so the question is a choice, not an open one.
    ask_size_uvdtf: str = "Bahut achhe! Aapko kis size mein print karwana hai? Hamare yaha A4 aur A3 size mein print hote hain."
    ask_qty: str = "Kitne piece chahiye? Quantity likh dijiye — bulk order mein rate aur kam ho jaata hai 👍"
    # DTF (fabric) prints are priced by the meter, not the piece.
    ask_qty_fabric: str = "DTF printing meter ke according hoti hai — aapko kitne meter print karwana hai?"
    # Only asked for UV DTF — the rate card is per sheet, not per piece.
    ask_pages: str = "Kitni sheet print karwani hain? Minimum order 5 sheet ka hai — 10+ sheet par rate aur kam ho jaata hai."
    # The script asks for all three in one go to confirm the order.
    ask_city: str = "Order confirm karne ke liye apna poora naam, full address (city ke saath) aur mobile number bhej dijiye 👍"
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
        "Ek baat bata doon ji — UV DTF sticker fabric par paste nahi hota, wo plain ya hard surface ke liye hai. "
        "Fabric ke liye hamare paas alag DTF printing machine hai, jo sirf fabric par print karne ke liye hai 👍"
    )
    # Said when the customer calls it expensive, which is an objection, not a question.
    objection_text: str = (
        "Samajh sakti hoon ji 🙏 Ek baat bata doon — rate quantity ke saath neeche aata hai, "
        "aur ek sheet par aapke design ke size ke hisaab se kai piece nikal jaate hain, "
        "to per piece kaafi sasta pad jaata hai."
    )
    # Prepaid only — offering cash contradicted the 100% advance rule.
    ask_payment: str = "Payment 100% advance hota hai — UPI se karenge ya bank transfer se? Team usi hisaab se detail bhej degi."

    # Everything except UV DTF is priced by the team, so the bot never invents a number
    # for a job that has no published rate.
    quote_by_team: str = (
        "Is kaam ka rate size, material aur quantity par banta hai, isliye main andaaza nahi dungi — "
        "hamari team aapko exact quote bhejegi."
    )
    # DTF (fabric) already told us the meter and design — no need to also say "main
    # andaaza nahi dungi, team bata degi" after just asking for exactly that.
    fabric_quote_text: str = "Rate meter aur design ke hisaab se tay hota hai — team aapko final rate confirm karegi."

    # Sent once, a few hours after a customer goes quiet mid-funnel (lead_followup).
    # Plain text, no buttons — the funnel never shows any, and this is still that chat.
    followup_with_service: str = "Hi {name} 🙏 Aap {service} ke liye enquiry kar rahe the. Agar aap abhi bhi banwana chahte hain to apna design ya requirement share kar dijiye — hum aapko size aur pricing samajhne mein help kar denge."
    followup_no_service: str = "Hi {name} 🙏 Kuch poochna tha kya? Bata dijiye, main yahin hoon."


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
