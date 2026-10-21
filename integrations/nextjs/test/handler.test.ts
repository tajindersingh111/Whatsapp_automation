// Run: npx tsx --test integrations/nextjs/test/handler.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { createWaAgentHandler } from "../lib/wa-agent/handler";
import { sign, verify } from "../lib/wa-agent/signature";
import { WaAgentError, type WaOrder, type WaStoreAdapter } from "../lib/wa-agent/types";

const SECRET = "test-secret";
const BASE = "https://store.example.com";

test("signature matches the Python agent (shared vector)", () => {
  const h = sign(SECRET, "GET", "/api/wa-agent/products?limit=100", "", 1700000000);
  assert.equal(h["X-WA-Agent-Signature"], "sha256=db47e7cbb604b7cc29701deb31829065140b3c0a1840a8217b6aca4a27c19be3");
  assert.ok(verify(SECRET, "GET", "/api/wa-agent/products?limit=100", "", "1700000000", h["X-WA-Agent-Signature"], 1700000100));
  assert.ok(!verify(SECRET, "GET", "/api/wa-agent/products?limit=100", "", "1700000000", h["X-WA-Agent-Signature"], 1700001000)); // expired
  assert.ok(!verify("other", "GET", "/api/wa-agent/products?limit=100", "", "1700000000", h["X-WA-Agent-Signature"], 1700000100));
});

const order: WaOrder = {
  id: "o1", number: "SK-1", status: "confirmed", payment_method: "cod", total: 999,
  customer: { name: "Priya", phone: "919876543210" }, items: [{ title: "Sheet", quantity: 1, price: 999 }],
};

const calls: string[] = [];
const adapter: WaStoreAdapter = {
  async listProducts({ cursor, limit }) {
    calls.push(`list:${cursor}:${limit}`);
    return { products: [{ id: "p1", title: "Sheet", price: 999, in_stock: true }], nextCursor: null };
  },
  async getOrder(ref) { return ref === "SK-1" ? order : null; },
  async findOrdersByPhone(phone) { calls.push(`phone:${phone}`); return [order]; },
  async createCodOrder(input) {
    if (input.items[0].product_id === "oos") throw new WaAgentError(422, "out of stock");
    return { ...order, id: "o2", number: "SK-2" };
  },
  async createCheckoutLink(items) { return `${BASE}/cart?add=${items.map((i) => i.product_id).join(",")}`; },
  async confirmCodOrder(id) { calls.push(`confirm:${id}`); return order; },
  async cancelOrder(id, reason) { calls.push(`cancel:${id}:${reason}`); },
};

const { GET, POST } = createWaAgentHandler(adapter, { secret: SECRET });

function signed(method: "GET" | "POST", pathWithQuery: string, body?: unknown): Request {
  const raw = body === undefined ? "" : JSON.stringify(body);
  return new Request(`${BASE}${pathWithQuery}`, {
    method,
    headers: { "Content-Type": "application/json", ...sign(SECRET, method, pathWithQuery, raw) },
    body: method === "POST" ? raw : undefined,
  });
}

test("rejects unsigned and tampered requests", async () => {
  assert.equal((await GET(new Request(`${BASE}/api/wa-agent/products`))).status, 401);
  const req = signed("POST", "/api/wa-agent/orders/o1/cancel", { reason: "x" });
  const tampered = new Request(req.url, { method: "POST", headers: req.headers, body: JSON.stringify({ reason: "y" }) });
  assert.equal((await POST(tampered)).status, 401);
});

test("routes every endpoint of the contract", async () => {
  const list = await (await GET(signed("GET", "/api/wa-agent/products?limit=50&cursor=abc"))).json();
  assert.deepEqual(list, { products: [{ id: "p1", title: "Sheet", price: 999, in_stock: true }], next_cursor: null });
  assert.ok(calls.includes("list:abc:50"));

  assert.equal((await (await GET(signed("GET", "/api/wa-agent/orders/SK-1"))).json()).order.number, "SK-1");
  assert.equal((await GET(signed("GET", "/api/wa-agent/orders/nope"))).status, 404);
  assert.equal((await (await GET(signed("GET", "/api/wa-agent/orders?phone=919876543210"))).json()).orders.length, 1);

  const cod = {
    payment_method: "cod", source: "whatsapp",
    customer: { name: "Priya", phone: "919876543210", address_line1: "12 MI Road", city: "Jaipur", state: "RJ", pincode: "302001" },
    items: [{ product_id: "p1", quantity: 1 }],
  };
  const created = await POST(signed("POST", "/api/wa-agent/orders", cod));
  assert.equal(created.status, 201);
  assert.equal((await created.json()).order.number, "SK-2");
  const oos = await POST(signed("POST", "/api/wa-agent/orders", { ...cod, items: [{ product_id: "oos", quantity: 1 }] }));
  assert.equal(oos.status, 422);
  assert.deepEqual(await oos.json(), { error: "out of stock" });
  const badPin = await POST(signed("POST", "/api/wa-agent/orders", { ...cod, customer: { ...cod.customer, pincode: "123" } }));
  assert.equal(badPin.status, 422);

  const link = await (await POST(signed("POST", "/api/wa-agent/checkout-links", { items: [{ product_id: "p1", quantity: 2 }] }))).json();
  assert.equal(link.url, `${BASE}/cart?add=p1`);

  assert.equal((await POST(signed("POST", "/api/wa-agent/orders/o1/confirm-cod", {}))).status, 200);
  assert.equal((await POST(signed("POST", "/api/wa-agent/orders/o1/cancel", { reason: "changed mind" }))).status, 200);
  assert.ok(calls.includes("confirm:o1") && calls.includes("cancel:o1:changed mind"));
});
