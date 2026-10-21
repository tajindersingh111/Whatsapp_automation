import { verify } from "./signature";
import { WaAgentError, type WaCodOrderInput, type WaOrderItemInput, type WaStoreAdapter } from "./types";

// Builds the GET/POST handlers for app/api/wa-agent/[...path]/route.ts.
// Every request must carry a valid signature from the agent (shared secret WA_AGENT_SECRET).

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

const bad = (message: string, status = 400) => json({ error: message }, status);

function segmentsAfterBase(url: URL, base: string): string[] {
  const rest = url.pathname.startsWith(base) ? url.pathname.slice(base.length) : url.pathname;
  return rest.split("/").filter(Boolean).map(decodeURIComponent);
}

function validItems(items: unknown): items is WaOrderItemInput[] {
  return (
    Array.isArray(items) &&
    items.length > 0 &&
    items.length <= 20 &&
    items.every((i) => i && typeof i.product_id === "string" && Number.isInteger(i.quantity) && i.quantity > 0 && i.quantity <= 20)
  );
}

export function createWaAgentHandler(adapter: WaStoreAdapter, opts: { basePath?: string; secret?: string } = {}) {
  const basePath = opts.basePath ?? "/api/wa-agent";

  async function handle(req: Request): Promise<Response> {
    const secret = opts.secret ?? process.env.WA_AGENT_SECRET ?? "";
    if (!secret) return bad("WA_AGENT_SECRET is not configured", 503);
    const url = new URL(req.url);
    const body = req.method === "GET" ? "" : await req.text();
    const ok = verify(
      secret,
      req.method,
      url.pathname + url.search,
      body,
      req.headers.get("x-wa-agent-timestamp"),
      req.headers.get("x-wa-agent-signature"),
    );
    if (!ok) return bad("invalid signature", 401);

    const seg = segmentsAfterBase(url, basePath);
    let data: any = {};
    try {
      data = body ? JSON.parse(body) : {};
    } catch {
      return bad("invalid JSON");
    }
    try {
      // GET /products?cursor=&limit=
      if (req.method === "GET" && seg.length === 1 && seg[0] === "products") {
        const limit = Math.min(Math.max(Number(url.searchParams.get("limit")) || 100, 1), 250);
        const page = await adapter.listProducts({ cursor: url.searchParams.get("cursor"), limit });
        return json({ products: page.products, next_cursor: page.nextCursor });
      }
      // GET /orders?phone=
      if (req.method === "GET" && seg.length === 1 && seg[0] === "orders") {
        const phone = (url.searchParams.get("phone") ?? "").replace(/\D/g, "");
        if (phone.length < 10) return bad("phone required");
        return json({ orders: (await adapter.findOrdersByPhone(phone)).slice(0, 5) });
      }
      // GET /orders/:ref
      if (req.method === "GET" && seg.length === 2 && seg[0] === "orders") {
        const order = await adapter.getOrder(seg[1]);
        return order ? json({ order }) : bad("order not found", 404);
      }
      // POST /orders  (Cash on Delivery order placed by the bot)
      if (req.method === "POST" && seg.length === 1 && seg[0] === "orders") {
        const input = data as WaCodOrderInput;
        const c = input.customer;
        if (input.payment_method !== "cod" || !c?.name || !c.phone || !c.address_line1 || !c.city || !c.state || !/^\d{6}$/.test(c.pincode ?? ""))
          return bad("invalid order input", 422);
        if (!validItems(input.items)) return bad("invalid items", 422);
        return json({ order: await adapter.createCodOrder(input) }, 201);
      }
      // POST /checkout-links
      if (req.method === "POST" && seg.length === 1 && seg[0] === "checkout-links") {
        if (!validItems(data.items)) return bad("invalid items", 422);
        return json({ url: await adapter.createCheckoutLink(data.items) });
      }
      // POST /orders/:id/confirm-cod  |  POST /orders/:id/cancel
      if (req.method === "POST" && seg.length === 3 && seg[0] === "orders") {
        if (seg[2] === "confirm-cod") return json({ order: (await adapter.confirmCodOrder(seg[1])) ?? null });
        if (seg[2] === "cancel") return json({ order: (await adapter.cancelOrder(seg[1], String(data.reason ?? ""))) ?? null });
      }
      return bad("not found", 404);
    } catch (err) {
      if (err instanceof WaAgentError) return bad(err.message, err.status);
      console.error("[wa-agent]", err);
      return bad("internal error", 500);
    }
  }

  return { GET: handle, POST: handle };
}
