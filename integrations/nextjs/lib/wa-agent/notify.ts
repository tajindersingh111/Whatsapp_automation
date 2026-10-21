import { randomUUID } from "node:crypto";
import { sign } from "./signature";
import type { WaEvent, WaOrder, WaProduct } from "./types";

// Tell the WhatsApp agent that something changed in the store.
// Call these from your order/product code. They never throw: a WhatsApp hiccup must not break checkout.
//
// In a Route Handler or Server Action, prefer running them after the response is sent:
//   import { after } from "next/server";      // Next.js 15+
//   after(() => notifyOrderCreated(order));
// On older Next.js versions, just `await notifyOrderCreated(order)` (it times out after 5s).

export async function notifyAgent(event: WaEvent): Promise<boolean> {
  const endpoint = process.env.WA_AGENT_WEBHOOK_URL; // e.g. https://agent.example.com/webhooks/store/<tenant-id>
  const secret = process.env.WA_AGENT_SECRET;
  if (!endpoint || !secret) return false;
  const body = JSON.stringify({ event_id: randomUUID(), ...event });
  const url = new URL(endpoint);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...sign(secret, "POST", url.pathname + url.search, body) },
      body,
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) console.error("[wa-agent] webhook rejected", res.status, await res.text().catch(() => ""));
    return res.ok;
  } catch (err) {
    console.error("[wa-agent] webhook failed", err);
    return false;
  }
}

/** New order on the website. COD orders get a WhatsApp confirmation message automatically. */
export const notifyOrderCreated = (order: WaOrder) => notifyAgent({ type: "order.created", order });

/** Status/tracking changed. Include tracking (courier, awb, url) once shipped to send the shipping update. */
export const notifyOrderUpdated = (order: WaOrder) => notifyAgent({ type: "order.updated", order });

export const notifyProductUpdated = (product: WaProduct) => notifyAgent({ type: "product.updated", product });

export const notifyProductDeleted = (productId: string) => notifyAgent({ type: "product.deleted", product_id: productId });
