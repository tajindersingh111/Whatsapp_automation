# WhatsApp Sales Agent

A multi-tenant WhatsApp AI sales agent for Indian D2C brands, built on FastAPI, PostgreSQL, the official Meta WhatsApp Cloud API and Claude. One deployment serves many brands. Each brand gets its own catalog, its own trained "bot brain", its own inbox and its own analytics.

What it does for each brand:

| Feature | How it works |
|---|---|
| **Menu bot (no AI, default)** | Buttons and lists on WhatsApp: browse categories, product cards with photo, *Order now* (checkout link), FAQ answers from the brand's knowledge and approved answers, keyword product search ("blue bedsheet"), order tracking and "talk to team". Costs nothing per message. |
| **AI sales chat (optional)** | Answers customers in Hindi, Hinglish or English. It searches the live catalog, sends product photos, handles doubts, and then shares a checkout link or places a **COD order in chat**. |
| **Lead capture** | Every chat becomes a lead with a stage (browsing → interested → checkout_sent → ordered/lost) and a note. Click-to-WhatsApp ad referrals are recorded as the lead source. |
| **COD confirmation** | When a COD order is placed on the website, the customer gets a template with Confirm/Cancel buttons. A tap updates the order in the store. There is a reminder, and orders with no reply are marked `no_response`. |
| **Shipping updates** | When the store adds tracking, the customer gets an "order shipped" template, sent once per AWB. |
| **Order tracking** | "Mera order kahan hai?" is answered from the store. The bot only shows orders that belong to that customer's number. |
| **Follow-ups** | A lead who goes quiet gets one polite AI follow-up inside the 24h window. |
| **Bulk messages** | Upload a CSV of opted-in customers, pick an approved template, fill its variables from CSV columns, send yourself a test, then send to everyone. Duplicates, invalid numbers and customers who replied STOP are removed. Sending is throttled, pauses on Meta rate limits without failing anyone, and tracks sent → delivered → read. Replies land in the inbox and the bot answers them. |
| **Audience console** | Every customer's campaign history as WhatsApp ticks: single tick (never delivered), delivered, seen, replied, failed. One click excludes dead numbers from future campaigns, new campaigns auto-skip numbers that never got delivered, and the list exports to CSV. |
| **Human handoff** | Complaints, refunds and "talk to a person" pause the bot for that chat. The team replies from the inbox. |
| **Per-brand training** | Each brand has its own personality, rules, offers, knowledge docs and approved answers ("Teach a better answer" in the inbox). There is no fine-tuning: the brand brain goes into a cached system prompt. |
| **Billing data** | Claude token usage is logged per brand, so AI cost can be billed to the client with a margin. |

Supported stores: **custom stores via the Next.js kit** (`integrations/nextjs`), **Shopify** and **WooCommerce**.

## Reply modes

Each brand picks a mode in **Bot training → Reply mode**:

- **Menu bot** (default for new brands). No AI calls. Answers come from buttons, lists, the catalog, knowledge docs, approved answers and offers. FAQ matching uses word overlap plus a Hinglish synonym map (`app/services/faq.py`). When a customer asks something it misses, add it as an approved answer and it is answered from the next message.
- **AI sales bot**. Free conversation with Claude, using the same catalog and training. Needs `ANTHROPIC_API_KEY`, and each message is billed. Choose the model per brand in Advanced: `claude-haiku-5-5` costs roughly ₹1 per conversation, `claude-opus-5-5` roughly ₹40.

COD confirmation, shipping updates and the order-status lookup never use AI in either mode.

## Architecture

```
Meta webhook ─┐                       ┌─> Claude (tools: search_products, place_cod_order, track_order, ...)
Store webhook ┼─> FastAPI ─> jobs ─> worker ─┤
Dashboard ────┘    (verify, enqueue)  (Postgres)  └─> WhatsApp Cloud API / store API
```

- **Webhooks only verify and enqueue**, then return 200 right away. Meta and stores retry slow webhooks.
- **Job queue** is a Postgres table (`SELECT … FOR UPDATE SKIP LOCKED`), so there's no Redis. Jobs retry with backoff. `PermanentJobError` stops retries.
- **Idempotency**: Meta message ids are unique in `messages`, store events are recorded in `webhook_events`, and jobs have dedupe keys.
- **Debounce**: if a customer sends 3 messages in a row, they get one reply. Each message schedules a reply 3s later, and only the newest one runs. A per-conversation advisory lock prevents double replies.
- **24h rule**: free-form messages are blocked outside the customer-service window. Only templates go out then.
- **Multi-tenant from day one**: every table has `tenant_id`, and client secrets are Fernet-encrypted.

Code map:

| Path | What |
|---|---|
| `app/pipeline.py` | All job handlers: inbound message → agent reply, COD flow, shipping, follow-ups, store webhooks, catalog sync |
| `app/agent/` | Claude loop (`runner.py`), tools (`tools.py`), system prompt (`prompt.py`), SDK wrapper (`llm.py`) |
| `app/connectors/` | `custom.py` (Next.js kit protocol), `shopify.py`, `woocommerce.py` |
| `app/whatsapp/` | Cloud API sender + message builders, webhook parser |
| `app/api/` | `/webhooks/*` and the `/admin/*` API used by the dashboard |
| `app/static/dashboard.html` | Operator console: setup, bot training, playground, inbox, leads, analytics |
| `integrations/nextjs/` | Drop-in kit for Next.js stores |
| `scripts/` | Mock store, local demo, customer simulator |

## Run locally

Requires Python 3.12+ and PostgreSQL 18 (already installed on this machine).

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
```

**Database.** This project uses its own Postgres cluster in `./.pgdata` on port 5436 with local trust auth. It does not start on its own after a reboot:

```bash
"/c/Program Files/PostgreSQL/18/bin/pg_ctl.exe" -D .pgdata -o "-p 5436" -l .pgdata/server.log start
```

First time only:

```bash
"/c/Program Files/PostgreSQL/18/bin/initdb.exe" -D .pgdata -U postgres -A trust -E UTF8 --locale=C
# start it (command above), then:
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -h localhost -p 5436 -U postgres -c "create database wa_agent;" -c "create database wa_agent_test;"
```

**Config + schema**: copy `.env.example` to `.env`, fill `ADMIN_TOKEN` and `ENCRYPTION_KEY`, then:

```bash
.venv/Scripts/alembic upgrade head
```

**Demo** (agent on :8000 plus a mock store on :8100, with a seeded demo brand and sandboxed WhatsApp):

```bash
.venv/Scripts/python scripts/run_demo.py
```

Open http://127.0.0.1:8000/dashboard and sign in with `ADMIN_TOKEN`. Then:

```bash
# a customer messages the brand
.venv/Scripts/python -m scripts.simulate_customer "2000 ke under blue bedsheet hai?" --phone 919812345678 --name Asha
# a COD order is placed on the website -> COD confirmation template appears in the inbox
.venv/Scripts/python -m scripts.simulate_customer --website-cod-order --phone 919811122233 --name Rahul
# the customer taps "Confirm order" on that template
.venv/Scripts/python -m scripts.simulate_customer --tap confirm --phone 919811122233
```

The demo brand uses the menu bot, so no API key is needed. In AI mode, replies need `ANTHROPIC_API_KEY` in `.env`; without it, chats are handed to the team with the reason "AI unavailable" and nothing is retried. The **Playground** tab chats with the bot directly. It uses the real catalog and brain, simulates orders, and never sends WhatsApp messages.

**Tests**:

```bash
.venv/Scripts/python -m pytest
```

They use the `wa_agent_test` database, a fake Claude, a fake Graph API and the mock store. Nothing leaves the machine.

## Onboarding a client (managed service)

1. **Dashboard → + New brand.**
2. **WhatsApp.** In the client's Meta Business Manager, add their number to a WhatsApp Business Account. Give your Meta app access (as a partner, or later through Embedded Signup as a Tech Provider). Create a **system user token** with `whatsapp_business_messaging` and `whatsapp_business_management`. Paste the phone number id and token in Setup. Subscribe your Meta app's webhook (`/webhooks/meta`, verify token = `META_VERIFY_TOKEN`) to the `messages` field.
3. **Templates.** Create these in the client's WhatsApp Manager (category **Utility**), and wait for approval:
   - `cod_order_confirmation`, body: `Hi {{1}}, thanks for your order {{2}} of {{3}} (Cash on Delivery). Please confirm so we can ship it.` Quick reply buttons: **Confirm order**, **Cancel order**, in that order.
   - `order_shipped`, body: `Hi {{1}}, your order {{2}} has been shipped with {{3}}. Track it here: {{4}}`

   Template names and languages can be changed per brand in Bot training → Advanced.
4. **Store**: Setup → Store.
   - **Next.js / custom**: install the kit (see `integrations/nextjs/README.md`). Connect with the store's `/api/wa-agent` URL. Copy the generated shared secret and the webhook URL into the store's env.
   - **Shopify**: create a custom app (scopes `read_products`, `read_orders`, `write_orders`). Paste the Admin API token and API secret. Add webhooks `orders/create`, `orders/updated`, `products/create`, `products/update` and `products/delete` pointing to the store webhook URL shown in Setup.
   - **WooCommerce**: create a Read/Write REST key. Add webhooks `order.created`, `order.updated`, `product.created`, `product.updated` and `product.deleted` with one secret.
5. **Bot training.** Fill in the brand info, rules, offers and knowledge (shipping, returns, COD, care, size guide). Test in the Playground until the answers are right.
6. **Go live**, then review the inbox daily for the first week. Use **Teach a better answer** on any weak reply.

## Deploy

Any host that runs a Python web process and Postgres works (Railway, Render, a ₹800–1500/month VPS). It needs HTTPS for Meta webhooks.

- Web: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Migrations: `alembic upgrade head` on each deploy
- With more traffic, run the worker separately: `python -m app.worker`, and set `RUN_WORKER_IN_PROCESS=false` on the web process.

## Notes and limits

- The Shopify connector is written against Shopify's GraphQL Admin API (2025-07). Its tests use mocked responses, and it has not been run against a live store. Place one test COD order on a Shopify development store before the first Shopify client.
- WooCommerce checkout links support one product. For multi-item orders the bot uses COD in chat or product links.
- Meta charges per template message (marketing is costlier than utility). Customer-service replies inside 24h are free. Check Meta's current India rates before quoting clients.
- AI model: `DEFAULT_MODEL` (default `claude-opus-5-5`) can be set per brand in Bot training → Advanced. Effort defaults to `low`, which suits chat. A smaller model such as `claude-haiku-5-5` lowers cost but may hurt quality, so measure on real chats before switching.
