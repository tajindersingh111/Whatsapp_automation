# Next.js store kit

Connects any Next.js (App Router) store to the WhatsApp agent. Your store exposes a small signed API that the agent calls, and it notifies the agent when orders or products change. The kit has no dependencies beyond Node's `crypto`.

## Install (about 15 minutes)

1. Copy files into your store (use `src/` paths if your project has `src/`):

   | From this folder | To your store |
   |---|---|
   | `lib/wa-agent/types.ts`, `signature.ts`, `handler.ts`, `notify.ts` | `lib/wa-agent/` |
   | `lib/wa-agent/adapter.ts` (template) or `examples/shatkon-adapter.ts` | `lib/wa-agent/adapter.ts` |
   | `app/api/wa-agent/[...path]/route.ts` | `app/api/wa-agent/[...path]/route.ts` |

2. **Write the adapter**: map your products and orders to the `WaProduct` / `WaOrder` shapes in `types.ts`. Money is in **rupees** (convert if you store paise). Image and product URLs must be absolute. `examples/shatkon-adapter.ts` is a complete Prisma example.

3. **Connect in the agent dashboard**: Setup → Store → *Custom / Next.js*, URL `https://yourstore.com/api/wa-agent`. Copy the shared secret and webhook URL it shows into your store's env:

   ```
   WA_AGENT_SECRET=<shared secret from the dashboard>
   WA_AGENT_WEBHOOK_URL=<store webhook URL from the dashboard>
   ```

   Redeploy, then click **Sync products now**. The product count on the Setup page should match your catalog.

4. **Send events** from your own code:

   ```ts
   import { notifyOrderCreated, notifyOrderUpdated, notifyProductUpdated } from "@/lib/wa-agent/notify";
   import { after } from "next/server"; // Next.js 15+; on 14, just await the call

   // after creating an order (website checkout):
   after(() => notifyOrderCreated(toWaOrder(order)));      // COD orders get the WhatsApp confirmation
   // after adding tracking / changing status:
   after(() => notifyOrderUpdated(toWaOrder(order)));      // sends the "shipped" template once per AWB
   // after editing a product in admin:
   after(() => notifyProductUpdated(toWaProduct(product)));
   ```

   The notify helpers never throw and time out after 5s, so they can't break checkout.

## The API your store serves

Every request is signed: `X-WA-Agent-Signature: sha256=HMAC(secret, "{timestamp}.{METHOD}.{path?query}.{body}")`. Requests older than 5 minutes are rejected. `handler.ts` checks all of this for you.

| Method & path | Adapter method | Returns |
|---|---|---|
| `GET /products?cursor=&limit=` | `listProducts` | `{ products, next_cursor }` |
| `GET /orders/:ref` | `getOrder` | `{ order }` or 404 |
| `GET /orders?phone=91…` | `findOrdersByPhone` | `{ orders }` |
| `POST /orders` (COD) | `createCodOrder` | `{ order }` (201) |
| `POST /checkout-links` | `createCheckoutLink` | `{ url }` |
| `POST /orders/:id/confirm-cod` | `confirmCodOrder` | `{ order }` |
| `POST /orders/:id/cancel` | `cancelOrder` | `{ order }` |

Throw `new WaAgentError(422, "message")` for expected problems (out of stock, unserviceable pincode). The bot reads that message and explains it to the customer.

## Gotchas

- `trailingSlash: true` in `next.config` redirects `/api/wa-agent/products` and breaks the signature. Exclude this route from the redirect, or use the slash-less URL.
- If your app uses `basePath`, include it in the URL you connect, and pass the same prefix as `createWaAgentHandler(adapter, { basePath: "/shop/api/wa-agent" })`.
- The route needs the Node runtime (`export const runtime = "nodejs"`), not Edge.
- **Shatkon-style stores (prepaid only):** keep `ALLOW_COD = false` in the adapter and turn off "Allow COD orders in chat" in Bot training. The bot will then send product or cart links instead of placing COD orders.

## Test

```bash
npx tsx --test integrations/nextjs/test/handler.test.ts
```

This checks the signature against the Python agent (shared test vector) and exercises every route with a fake adapter.
