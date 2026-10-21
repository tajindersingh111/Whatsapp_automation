"""Menu bot (no AI): buttons, lists, FAQ, catalog search, tracking, handoff, follow-up."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.models import Contact, Conversation, Correction, Job, KnowledgeDoc, Tenant
from tests.conftest import ADMIN, CUSTOMER, drain, meta_payload, post_meta


@pytest.fixture
async def menu_tenant(tenant, db, api) -> Tenant:
    t = await db.get(Tenant, tenant.id)
    t.config = {**t.config, "reply_mode": "menu", "support_hours": "10am-7pm"}
    db.add(KnowledgeDoc(tenant_id=t.id, title="Shipping & delivery", content="Orders ship in 1-2 days. Delivery takes 3-5 days in metros."))
    db.add(KnowledgeDoc(tenant_id=t.id, title="Cash on Delivery", content="COD is available on orders up to ₹5,000."))
    db.add(KnowledgeDoc(tenant_id=t.id, title="Returns", content="7-day easy returns on unused items."))
    db.add(Correction(tenant_id=t.id, question="Do you do bulk orders for weddings?", answer="Haan ji, 20+ pieces par special rate milta hai."))
    await db.commit()
    r = await api.post(f"/admin/tenants/{t.id}/store/sync", headers=ADMIN)
    assert r.status_code == 200
    await drain()
    return t


_n = 0


async def say(api, text=None, button=None, phone=CUSTOMER):
    global _n
    _n += 1
    await post_meta(api, meta_payload(text, wamid=f"wamid.menu.{_n}", button=button, wa_id=phone))
    await drain()


def last(graph) -> dict:
    return graph.sent[-1]


def button_ids(m: dict) -> list[str]:
    return [b["reply"]["id"] for b in m["interactive"]["action"]["buttons"]]


def row_ids(m: dict) -> list[str]:
    return [r["id"] for s in m["interactive"]["action"]["sections"] for r in s["rows"]]


async def test_browse_to_checkout_without_ai(api, menu_tenant, db, graph, fake_llm):
    await say(api, "Hi")
    welcome = last(graph)
    assert welcome["type"] == "interactive" and "Jaipur Linen Co" in welcome["interactive"]["body"]["text"]
    assert button_ids(welcome) == ["menu:products", "menu:track", "menu:human"]

    # Template quick replies and interactive taps both arrive as button payloads.
    await say(api, button=("menu:products", "Products dekhein"))
    cats = last(graph)
    assert cats["interactive"]["type"] == "list"
    assert set(row_ids(cats)) == {"cat:Bedsheets", "cat:Cushion Covers", "cat:Quilts"}

    await say(api, button=("cat:Bedsheets", "Bedsheets"))
    listing = last(graph)
    assert row_ids(listing) == ["prod:p2", "prod:p1"]  # in stock, cheapest first
    rows = listing["interactive"]["action"]["sections"][0]["rows"]
    assert all(len(r["title"]) <= 24 for r in rows) and rows[1]["description"] == "₹1,899 (MRP ₹2,499)"

    await say(api, button=("prod:p1", "Indigo"))
    card = last(graph)
    assert card["interactive"]["header"] == {"type": "image", "image": {"link": "https://picsum.photos/seed/indigo-bedsheet/800/800"}}
    assert button_ids(card)[0] == "buy:p1"

    await say(api, button=("buy:p1", "Order now"))
    cta = last(graph)
    assert cta["interactive"]["type"] == "cta_url"
    assert cta["interactive"]["action"]["parameters"]["url"] == "https://shop.example.com/cart?items=p1:1"

    contact = await db.scalar(select(Contact).where(Contact.wa_id == CUSTOMER))
    assert contact.lead_stage == "checkout_sent"
    assert fake_llm.calls == []  # not a single AI call


async def test_variant_picker_and_out_of_stock(api, menu_tenant, graph):
    await say(api, button=("buy:p3", "Order now"))
    picker = last(graph)
    assert row_ids(picker) == ["buyv:p3:p3-red"]  # the blue option is out of stock
    await say(api, button=("buyv:p3:p3-red", "Red & Orange"))
    assert last(graph)["interactive"]["action"]["parameters"]["url"].endswith("items=p3-red:1")

    await say(api, button=("prod:p4", "Razai"))
    card = last(graph)
    assert "out of stock" in card["interactive"]["body"]["text"] and "buy:p4" not in button_ids(card)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("delivery kab tak aayegi?", "Shipping & delivery"),
        ("COD available hai?", "Cash on Delivery"),
        ("cash on delivery milega", "Cash on Delivery"),
        ("return kaise kare", "Returns"),
        ("koi offer ya discount code hai?", "FIRST10"),
        ("delivery kitne din mein hogi? COD hai?", "*Shipping & delivery*"),
        ("delivery kitne din mein hogi? COD hai?", "*Cash on Delivery*"),
        ("wedding ke liye bulk orders lete ho?", "20+ pieces"),
    ],
)
async def test_faq_answers(api, menu_tenant, graph, question, expected):
    await say(api, "hello")
    await say(api, question)
    assert expected in last(graph)["interactive"]["body"]["text"]


async def test_free_text_search_and_unknown(api, menu_tenant, graph):
    await say(api, "hello")
    await say(api, "mujhe blue bedsheet chahiye")
    assert row_ids(last(graph))[0] == "prod:p1"
    await say(api, "asdfgh qwerty")
    assert "samajh nahi aaya" in last(graph)["interactive"]["body"]["text"]


async def test_first_message_from_ad_gets_menu(api, menu_tenant, graph):
    await say(api, "I want to know more about this")
    assert button_ids(last(graph)) == ["menu:products", "menu:track", "menu:human"]


async def test_tracking_by_phone_then_by_number(api, menu_tenant, graph, mock_store):
    await say(api, button=("menu:track", "Order track karein"))
    assert "koi order nahi mila" in last(graph)["interactive"]["body"]["text"]

    mock_store.state.orders["ord_1050"] = {
        "id": "ord_1050", "number": "1050", "status": "shipped", "payment_method": "cod", "total": 1899,
        "customer": {"name": "Priya", "phone": CUSTOMER}, "items": [{"title": "Indigo", "quantity": 1, "price": 1899}],
        "tracking": {"courier": "Delhivery", "awb": "D99", "url": "https://t.example/D99"}, "created_at": "2026-10-11T10:00:00Z",
    }
    mock_store.state.orders["ord_2"] = {
        "id": "ord_2", "number": "2002", "status": "shipped", "payment_method": "cod", "total": 1,
        "customer": {"name": "Other", "phone": "919999999999"}, "items": [], "created_at": "2026-10-11T10:00:00Z",
    }
    await say(api, "2002")  # we asked for an order number; this one is not theirs
    assert "kisi aur number" in last(graph)["interactive"]["body"]["text"]
    await say(api, "mera order kab aayega")
    status = last(graph)["interactive"]["body"]["text"]
    assert "*Order 1050*" in status and "AWB D99" in status and "https://t.example/D99" in status


async def test_handoff_and_followup(api, menu_tenant, db, graph):
    await say(api, "hi", phone="919800000001")
    await say(api, button=("prod:p2", "Floral"), phone="919800000001")
    job = await db.scalar(select(Job).where(Job.kind == "lead_followup").order_by(Job.id.desc()))
    await db.execute(update(Job).where(Job.id == job.id).values(run_at=datetime.now(timezone.utc)))
    await db.commit()
    await drain()
    follow = last(graph)
    assert "Sanganeri Floral" in follow["interactive"]["body"]["text"] and button_ids(follow)[0] == "buy:p2"

    await say(api, "product toota hua aaya hai")
    assert "team jaldi" in last(graph)["text"]["body"] and "10am-7pm" in last(graph)["text"]["body"]
    conv = await db.scalar(select(Conversation).join(Contact).where(Contact.wa_id == CUSTOMER))
    assert conv.mode == "human"


async def test_playground_menu_mode_with_buttons(api, menu_tenant):
    r = await api.post(f"/admin/tenants/{menu_tenant.id}/playground", json={"message": "hi", "session": "m1"}, headers=ADMIN)
    body = r.json()
    assert body["reply_mode"] == "menu"
    assert body["replies"][0]["payload"]["interactive"]["action"]["buttons"][0]["reply"]["id"] == "menu:products"
    r = await api.post(f"/admin/tenants/{menu_tenant.id}/playground", json={"button": {"id": "menu:products", "title": "Products"}, "session": "m1"}, headers=ADMIN)
    assert r.json()["replies"][0]["payload"]["interactive"]["type"] == "list"
