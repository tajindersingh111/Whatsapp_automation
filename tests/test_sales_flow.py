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


def test_first_message_asking_price_introduces_then_asks_name():
    _, replies = chat("Hi price kya hai uv dtf sticker ka")
    assert replies[0].startswith("Namaste") and "naam" in replies[0]
    assert "\n\n" not in replies[0]  # one line, not three stacked paragraphs


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


def test_low_sheet_count_gets_the_volume_break():
    _, replies = chat("hi", "Ravi", "uv dtf", "bottle", "design nahi hai", "A4", "200 piece", "3 sheet")
    assert "10+ sheet" in replies[-1]


def test_objection_that_triggers_the_rate_is_answered_first():
    _, replies = chat("hi", "Ravi", "uv dtf", "jar", "design nahi hai", "rate kitna hai", "bahut mehnga hai")
    assert replies[-1].startswith("Samajh sakti hoon") and "Rate ye rahe" in replies[-1]


def test_file_and_thanks_after_close_get_a_human_reply():
    msgs = ("hi", "Ravi", "uv dtf", "box", "design nahi hai", "A3", "100", "6 sheet",
            "Jaipur, Malviya Nagar", "yahi", "UPI")
    _, replies = chat(*msgs, "<image>")
    assert replies[-1].startswith("Mil gaya")
    _, replies = chat(*msgs, "thanks bhai")
    assert replies[-1].startswith("Bilkul ji")
