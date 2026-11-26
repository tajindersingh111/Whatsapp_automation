"""The UV DTF sales funnel, driven message by message. Pure — no DB, no Graph API.

Most of these are bugs found by chatting with the bot as a customer in the Playground;
each test is the message that broke it.
"""

from app import sales_flow
from app.bot_config import BotConfig

CFG = BotConfig(sales_flow={"enabled": True})


def chat(*msgs: str) -> tuple[dict, list[str]]:
    sf = sales_flow.load({})
    replies = []
    for m in msgs:
        kind = "image" if m == "<image>" else "text"
        reply, _ = sales_flow.advance(sf, "" if kind == "image" else m, kind, CFG)
        replies.append(reply or "")
    return sf, replies


def test_name_is_first_word_only():
    sf, _ = chat("hi", "tajinder jaipur se")
    assert sf["name"] == "Tajinder"


def test_name_followed_by_a_comma():
    sf, _ = chat("hi", "Gurpreet, Ludhiana se")
    assert sf["name"] == "Gurpreet"


def test_first_message_asking_price_introduces_then_asks_name():
    _, replies = chat("Hi price kya hai uv dtf sticker ka")
    assert replies[0].startswith("Namaste") and "naam" in replies[0]
    assert "\n\n" not in replies[0]  # one line, not three stacked paragraphs


def test_company_intro_is_shown_even_when_service_came_first():
    _, replies = chat("UV DTF sticker ka rate kya hai?", "Gurpreet, Ludhiana se")
    r = replies[-1]
    assert "sabhi tarah ka signage aur printing" in r and "Exhibition Stall" in r and "Flex Printing" in r
    assert "Aapne UV DTF Sticker ke baare mein poocha tha" in r
    assert "kya service chahiye" not in r  # not asked again
    assert r.index("Hum ye sab banate hain") < r.index("Aapko kispe paste karna hai")


def test_company_intro_is_shown_once():
    _, replies = chat("hi", "Ravi", "uv dtf", "bottle")
    assert sum("Hum ye sab banate hain" in r for r in replies) == 1


def test_repeating_the_service_is_not_an_item():
    sf, replies = chat("hi", "Ravi", "uv dtf", "uv dtf")
    assert not sf.get("variant")
    assert replies[-1] != replies[-2]  # asked again in other words, not word for word


def test_shape_beats_material():
    sf, _ = chat("hi", "Ravi", "uv dtf", "steel ki bottle par lagana hai")
    assert sf["variant"].startswith("curved item") and sf["item_said"] == "bottle"


def test_uv_dtf_never_asks_for_a_product_photo():
    _, replies = chat("hi", "Ravi", "uv dtf", "bottle")
    assert "design ready" in replies[-1]


def test_photo_remark_is_not_design_refusal_or_backing_off():
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "photo abhi nahi hai")
    assert sf.get("design") != "team"
    assert not sf.get("paused")
    assert replies[-1].startswith("Koi baat nahi")


def test_has_design_is_recorded_and_file_upgrades_it():
    sf, _ = chat("hi", "Ravi", "uv dtf", "glass", "haan design ready hai")
    assert sf["design"] == "customer"
    sf, replies = chat("hi", "Ravi", "uv dtf", "glass", "haan design ready hai", "A4", "<image>")
    assert sf["design"] is True and replies[-1].startswith("Design mil gaya")


def test_wanting_a_logo_printed_is_not_having_the_design():
    sf, replies = chat("hi", "Gurpreet", "uv dtf", "mere gym ki water bottles pe logo lagwana hai, steel ki hain")
    assert not sf.get("design") and "design ready" in replies[-1]
    sf, replies = chat("hi", "Gurpreet", "uv dtf", "mere gym ki water bottles pe logo lagwana hai, steel ki hain",
                       "haan logo hai mere paas CDR mein")
    assert sf["design"] == "customer" and replies[-1].startswith(("Badhiya", "Achha", "Perfect"))


def test_refusal_is_never_an_address():
    sf, _ = chat("hi", "Ravi", "uv dtf", "jar", "nahi photo nahi")
    assert not sf.get("city")


def test_backing_off_matches_regardless_of_process_hash_seed():
    # The phrase check once joined a set of words, whose order changes per process.
    for _ in range(5):
        sf, replies = chat("hi", "Amit", "led board chahiye", "abhi nahi, sochta hoon")
        assert sf.get("paused") and "sample" in replies[-1]


def test_uv_dtf_size_retry_is_a4_or_a3_not_inches():
    _, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "pata nahi")
    assert "inch" not in replies[-1] and "A4" in replies[-1]


def test_low_sheet_count_offers_the_real_tradeoff_not_a_fake_discount():
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "A4", "200 piece", "3 sheet")
    assert sf["pages"] == "3 sheet"  # never force-bumped — their order, at the real rate
    assert "Rs 295/sheet" in replies[-1] and "Rs 180/sheet" in replies[-1]


def test_objection_that_triggers_the_rate_is_answered_first():
    _, replies = chat("hi", "Ravi", "uv dtf", "jar", "design nahi hai", "rate kitna hai", "bahut mehnga hai")
    assert replies[-1].startswith("Samajh sakti hoon") and "Rate ye rahe" in replies[-1]


def test_design_size_gives_per_piece_price_and_sheet_count():
    sf, replies = chat("hi", "Ravi", "uv dtf", "jar", "design nahi hai", "A4", "2x2 inch")
    # The sheet count is not known yet, so this uses the headline (at-minimum) rate.
    assert "15 piece" in replies[-1] and "Rs 12 per piece" in replies[-1]
    assert sf["size"] == "A4"  # the design size never overwrites the sheet
    sf, replies = chat("hi", "Ravi", "uv dtf", "jar", "design nahi hai", "A4", "2x2 inch", "40 piece")
    # 40 pieces need 3 sheets — under the 5-sheet minimum, so it stays their own number,
    # offered at the real (higher) rate rather than silently bumped to 5.
    assert sf["pages"] == "3 sheet (approx)"
    assert "lagbhag 3 A4 sheet" in replies[-1]
    assert "Rs 295/sheet" in replies[-1] and "Rs 180/sheet" in replies[-1]


def test_estimate_answers_the_question_before_the_rate_card():
    _, replies = chat("hi", "Gurpreet", "uv dtf", "bottle", "design nahi hai", "rate?",
                      "A4 lunga. ek sheet mein kitne logo aayenge? logo 3 inch ka hai")
    assert replies[-1].index("6 piece") < replies[-1].index("Rate ye rahe")


GURPREET = ("Hello, UV DTF sticker ka rate kya hai?", "Gurpreet, Ludhiana se",
            "mere gym ki water bottles pe logo lagwana hai, steel ki hain", "haan logo hai mere paas CDR mein",
            "A4 lunga. ek sheet mein kitne logo aayenge? logo 3 inch ka hai")


def test_objection_after_the_quote_is_answered_with_the_real_rate():
    sf, replies = chat(*GURPREET, "33 rupay ek logo? thoda zyada hai bhai. 300 piece chahiye")
    r = replies[-1]
    assert r.startswith("Samajh sakti hoon")
    assert "50 A4 sheet" in r and "Rs 180/sheet" in r and "Rs 30 per piece" in r
    assert "10+ sheet" in r  # a real further discount, worth mentioning
    assert "Rate ye rahe" not in r  # the rate card is not sent a second time


def test_zyada_without_money_context_is_not_a_price_complaint():
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "zyada time lagega kya")
    assert not replies[-1].startswith("Samajh sakti hoon")


def test_file_and_thanks_after_close_get_a_human_reply():
    msgs = ("hi", "Ravi", "uv dtf", "box", "design nahi hai", "A3", "100", "6 sheet",
            "Jaipur, Malviya Nagar", "yahi", "UPI")
    _, replies = chat(*msgs, "<image>")
    assert replies[-1].startswith("Mil gaya")
    _, replies = chat(*msgs, "thanks bhai")
    assert replies[-1].startswith("Bilkul ji")
    _, replies = chat(*msgs, "theek hai thanks")
    assert replies[-1].startswith("Bilkul ji")


def test_services_list_is_the_sales_script():
    _, replies = chat("hi", "Ravi")
    r = replies[-1]
    for line in ("Digital Display Signage", "LED Board / Neon Sign", "Glow Sign Board", "Inshop Branding",
                 "UV Printing", "UV DTF Sticker", "DTF Printing", "CNC / Laser Cutting", "Exhibition Stall",
                 "Canvas Printing", "Flex Printing"):
        assert line in r, line
    assert "Mug printing" not in r and "Corporate" not in r and "Non-lit" not in r
    assert r.rstrip().endswith("Aapko inme se kya service chahiye?")


def test_uv_alone_or_with_printing_picks_the_right_lane():
    assert chat("hi", "Ravi", "uv printing chahiye")[0]["service"] == "uvprint"
    assert chat("hi", "Ravi", "uv dtf chahiye")[0]["service"] == "uvdtf"
    assert chat("hi", "Ravi", "dtf tshirt print")[0]["service"] == "fabric"
    assert chat("hi", "Ravi", "LED board chahiye")[0]["service"] == "led"


def test_no_design_means_catalogue_not_team_makes_it():
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai")
    assert "catalogue" in replies[-1] and "screenshots" in replies[-1]


def test_name_address_and_mobile_in_one_message():
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "A4", "200 piece", "10 sheet",
                       "Ravi Kumar, Sharma Stickers, 14 Model Town Ludhiana, 9876512345")
    assert sf["phone"] == "9876512345"
    assert "Model Town" in sf["city"] and "9876512345" not in sf["city"]
    assert "UPI" in replies[-1] and "cash" not in replies[-1]  # prepaid only


def test_below_minimum_sheets_stays_what_they_asked_for():
    """A below-minimum order is offered at the real (higher) rate, not silently bumped —
    the business will make 2 sheets, just not at the 5-sheet price."""
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "A4", "100 piece", "2 sheet")
    assert sf["pages"] == "2 sheet"
    assert "2 A4 sheet bhi ban jayegi" in replies[-1]
    assert "Rs 295/sheet" in replies[-1] and "Rs 180/sheet" in replies[-1]


def test_rate_for_below_and_at_minimum():
    assert sales_flow.rate_for({"service": "uvdtf", "size": "A4"}, CFG, pages=2) == 295
    assert sales_flow.rate_for({"service": "uvdtf", "size": "A4"}, CFG, pages=5) == 180
    assert sales_flow.rate_for({"service": "uvdtf", "size": "A3"}, CFG, pages=4) == 400
    assert sales_flow.rate_for({"service": "uvdtf", "size": "A3"}, CFG, pages=5) == 350


def test_price_card_has_no_bare_gst_line_since_rates_include_it():
    _, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "rate kitna hai", "rate kitna hai")
    r = replies[-1]
    assert "GST included" in r
    assert "+18% GST" not in r


def test_followup_names_the_service_not_a_catalog_product():
    """A customer who goes quiet mid-funnel gets reminded about what they actually
    asked for, as plain text — not "{product} dekha tha" (there is no catalog product)
    and never with buttons, which nothing else in this mode shows."""
    from types import SimpleNamespace

    from app.menu_bot import menu_followup

    contact = SimpleNamespace(name="Ravi Kumar")
    sf, _ = chat("hi", "Ravi Kumar", "uv dtf", "bottle")
    conv = SimpleNamespace(state={"sf": sf})
    msg = menu_followup(CFG, contact, conv, None)
    assert msg["type"] == "text"
    assert "UV DTF Sticker" in msg["text"]["body"] and "bottle" in msg["text"]["body"]

    conv_fresh = SimpleNamespace(state={"sf": sales_flow.load({})})
    msg2 = menu_followup(CFG, contact, conv_fresh, None)
    assert msg2["type"] == "text" and "{service}" not in msg2["text"]["body"]


def test_real_production_yields_override_the_geometric_guess():
    """These eight sizes are the factory's own measured counts, not a layout guess —
    they must win over per_sheet()'s geometry fallback."""
    known = [
        ("A3", "1.5x1.5 inch", 54), ("A4", "1.5x1.5 inch", 30),
        ("A3", "2x2 inch", 35), ("A4", "2x2 inch", 15),
        ("A3", "2.5x2.5 inch", 24), ("A4", "2.5x2.5 inch", 12),
        ("A3", "3x2 inch", 25), ("A4", "3x2 inch", 9),
    ]
    for sheet, size_text, expected in known:
        sf = {"service": "uvdtf", "size": sheet, "sticker_in": list(sales_flow._dims_inches(size_text))}
        assert sales_flow.per_sheet(sf) == expected, (sheet, size_text)
    # Orientation and the rate card's own "2"x2"" notation hit the same entry.
    assert sales_flow.per_sheet({"service": "uvdtf", "size": "A3", "sticker_in": [2.0, 3.0]}) == 25
    assert sales_flow.per_sheet({"service": "uvdtf", "size": "A4",
                                 "sticker_in": list(sales_flow._dims_inches('2"x2"'))}) == 15


def test_every_per_piece_line_says_approx():
    sf, replies = chat("hi", "Ravi", "uv dtf", "box", "design nahi hai", "A3", "2.5x2.5 inch")
    assert "24 piece" in replies[-1]
    assert "lagbhag" in replies[-1] and "approx" in replies[-1]


def test_spaced_a4_sets_size_not_qty():
    """Found in a real chat: "a 4" (with a space) failed the A4 regex, fell through to
    the out-of-order slot search, and got misread as a quantity of 4 pieces."""
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "a 4")
    assert sf["size"] == "A4" and sf.get("qty") is None
    assert "note kar liya" in replies[-1] or "A4" in replies[-1]


def test_dimension_like_reply_is_never_read_as_the_address():
    """Found live: "2*2" (a design size) sent while the bot was waiting for the
    name+address+mobile message landed as the city, because the strict character-class
    check only ran "if not loose" — and the loose flag was never cleared after the
    full-address reading had already failed."""
    sf, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "A4", "4 piece",
                       "2 sheet", "2*2")
    assert sf.get("city") is None
    assert sf["sticker_in"] == [2.0, 2.0]
    assert "15 piece" in replies[-1]  # the estimate still fires
    assert "address" in replies[-1].lower()  # and the address question is asked again
