// Example adapter for a Prisma store shaped like shatkon-web (prices in paise, images as a JSON
// array, variants, shipments). Copy to lib/wa-agent/adapter.ts and adjust to your schema.
//
// Store-specific decisions made here (review them):
// - Shatkon is prepaid-only, so createCodOrder refuses. Set ALLOW_COD = true only if you ship COD,
//   and also turn on "COD orders in chat" in the agent dashboard.
// - The cart lives in the browser, so a checkout link opens the product page (single item) or /cart.

import type { Prisma } from "@prisma/client";
import { db } from "@/lib/db";
import { createOrder } from "@/lib/orders";
import { priceCart } from "@/lib/pricing";
import { SITE } from "@/lib/seo";
import { WaAgentError, type WaOrder, type WaProduct, type WaStoreAdapter } from "@/lib/wa-agent/types";

const ALLOW_COD = false;
const rupees = (paise: number | null | undefined) => (paise ?? 0) / 100;
const absolute = (src: string | null | undefined) => (!src ? null : src.startsWith("http") ? src : `${SITE()}${src.startsWith("/") ? "" : "/"}${src}`);
const digits = (phone: string | null | undefined) => {
  const d = (phone ?? "").replace(/\D/g, "");
  return d.length === 10 ? `91${d}` : d.replace(/^0/, "91");
};

const productInclude = { variants: true, category: { select: { name: true } } } as const;
type ProductRow = Prisma.ProductGetPayload<{ include: typeof productInclude }>;

function loadProducts(where: object, take: number, cursor?: string) {
  return db.product.findMany({
    where,
    take,
    ...(cursor ? { skip: 1, cursor: { id: cursor } } : {}),
    orderBy: { id: "asc" },
    include: productInclude,
  });
}

function toWaProduct(p: ProductRow): WaProduct {
  const images = Array.isArray(p.images) ? (p.images as string[]) : [];
  return {
    id: p.id,
    title: p.name,
    description: [p.tagline, p.description].filter(Boolean).join(". ").slice(0, 1500),
    price: rupees(p.price),
    compare_at_price: p.comparePrice && p.comparePrice > p.price ? rupees(p.comparePrice) : null,
    url: `${SITE()}/products/${p.slug}`,
    image_url: absolute(images[0]),
    in_stock: p.preorder || p.stock > 0,
    stock: p.stock,
    category: p.category?.name ?? null,
    tags: [...p.styles, ...p.moods, ...p.occasions],
    variants: p.variants.map((v) => ({ id: v.id, title: v.label, price: rupees(v.price ?? p.price), in_stock: p.preorder || v.stock > 0, sku: v.sku })),
    active: p.active,
  };
}

const orderInclude = { items: true, shipments: { orderBy: { createdAt: "desc" } } } as const;
type OrderRow = Prisma.OrderGetPayload<{ include: typeof orderInclude }>;
const COD_SOURCE = "whatsapp-cod";

function toWaOrder(o: OrderRow): WaOrder {
  const ship = o.shipments.find((s) => s.awb || s.trackUrl);
  const address = (o.address ?? {}) as { name?: string };
  return {
    id: o.id,
    number: o.number,
    status: o.status.toLowerCase(),
    // Only orders the bot placed as COD are COD; website orders here are always prepaid.
    payment_method: o.source === COD_SOURCE ? "cod" : "prepaid",
    total: rupees(o.total),
    customer: { name: address.name ?? null, phone: digits(o.phone) },
    items: o.items.map((i) => ({ title: i.variant ? `${i.name} - ${i.variant}` : i.name, quantity: i.qty, price: rupees(i.unitPrice) })),
    tracking: ship ? { courier: ship.courier, awb: ship.awb, url: ship.trackUrl, status: ship.status } : null,
    created_at: o.createdAt.toISOString(),
  };
}

export const adapter: WaStoreAdapter = {
  async listProducts({ cursor, limit }) {
    const rows = await loadProducts({ active: true }, limit, cursor ?? undefined);
    return { products: rows.map(toWaProduct), nextCursor: rows.length === limit ? rows[rows.length - 1].id : null };
  },

  async getOrder(ref) {
    const o = await db.order.findFirst({ where: { OR: [{ number: ref }, { id: ref }] }, include: orderInclude });
    return o ? toWaOrder(o) : null;
  },

  async findOrdersByPhone(phone) {
    const last10 = phone.slice(-10);
    const rows = await db.order.findMany({ where: { phone: { endsWith: last10 } }, include: orderInclude, orderBy: { createdAt: "desc" }, take: 5 });
    return rows.map(toWaOrder);
  },

  async createCodOrder(input) {
    if (!ALLOW_COD) throw new WaAgentError(422, "This store does not offer Cash on Delivery. Send a checkout link instead.");
    const c = input.customer;
    const address = { name: c.name, phone: c.phone, line1: c.address_line1, line2: c.address_line2 ?? "", city: c.city, state: c.state, pincode: c.pincode, country: "IN" };
    const priced = await priceCart({
      lines: input.items.map((i) => ({ productId: i.product_id, variantId: i.variant_id ?? null, qty: i.quantity })),
      address: { country: "IN", state: c.state, pincode: c.pincode },
    });
    if (priced.issues.length) throw new WaAgentError(422, priced.issues.join(" "));
    if (!priced.serviceable) throw new WaAgentError(422, "We don't deliver to this pincode yet.");
    try {
      const order = await createOrder({ priced, userId: null, contact: { name: c.name, phone: c.phone, email: c.email ?? null }, address, notes: input.note, source: COD_SOURCE });
      const confirmed = await db.order.update({ where: { id: order.id }, data: { status: "CONFIRMED" }, include: orderInclude });
      return toWaOrder(confirmed);
    } catch (err) {
      throw new WaAgentError(422, err instanceof Error ? err.message : "Could not place the order");
    }
  },

  async createCheckoutLink(items) {
    if (items.length === 1) {
      const p = await db.product.findUnique({ where: { id: items[0].product_id }, select: { slug: true } });
      if (!p) throw new WaAgentError(422, "Unknown product");
      return `${SITE()}/products/${p.slug}`;
    }
    return `${SITE()}/cart`;
  },

  async confirmCodOrder(orderId) {
    const current = await db.order.findUnique({ where: { id: orderId }, select: { internalNote: true } });
    if (!current) throw new WaAgentError(404, "Order not found");
    const note = [current.internalNote, "COD confirmed by customer on WhatsApp"].filter(Boolean).join("\n");
    const o = await db.order.update({ where: { id: orderId }, data: { internalNote: note }, include: orderInclude });
    return toWaOrder(o);
  },

  async cancelOrder(orderId) {
    const { cancelOrder } = await import("@/lib/orders");
    await cancelOrder(orderId, "customer");
    const o = await db.order.findUniqueOrThrow({ where: { id: orderId }, include: orderInclude });
    return toWaOrder(o);
  },
};
