// WA Agent Store API v1: the shapes your store returns to the WhatsApp agent.
// Money is in rupees (numbers, may have decimals). Phone numbers are digits with country code, e.g. "919876543210".

export type WaVariant = {
  id: string;
  title: string; // "King / Blue"
  price: number;
  in_stock: boolean;
  sku?: string | null;
};

export type WaProduct = {
  id: string;
  title: string;
  description?: string;
  price: number;
  compare_at_price?: number | null; // MRP, if higher than price
  currency?: string; // default "INR"
  url?: string | null; // absolute product page URL
  image_url?: string | null; // absolute URL, publicly reachable (WhatsApp downloads it)
  in_stock: boolean;
  stock?: number | null;
  category?: string | null;
  tags?: string[];
  variants?: WaVariant[]; // omit or [] for simple products
  active?: boolean; // false hides it from the bot
};

export type WaTracking = {
  courier?: string | null;
  awb?: string | null;
  url?: string | null;
  status?: string | null;
};

export type WaOrder = {
  id: string; // internal id
  number: string; // what the customer sees
  status: string; // your own status words are fine: "confirmed", "shipped", "cancelled"...
  payment_method: "cod" | "prepaid";
  total: number;
  currency?: string;
  customer: { name?: string | null; phone?: string | null };
  items: { title: string; quantity: number; price: number }[];
  tracking?: WaTracking | null;
  created_at?: string; // ISO date
};

export type WaOrderItemInput = { product_id: string; variant_id?: string | null; quantity: number };

export type WaCodOrderInput = {
  payment_method: "cod";
  source: "whatsapp";
  note?: string;
  customer: {
    name: string;
    phone: string;
    email?: string | null;
    address_line1: string;
    address_line2?: string;
    city: string;
    state: string;
    pincode: string;
  };
  items: WaOrderItemInput[];
};

/** Implement this once for your store (see examples/). Throw WaAgentError for expected failures. */
export interface WaStoreAdapter {
  listProducts(opts: { cursor: string | null; limit: number }): Promise<{ products: WaProduct[]; nextCursor: string | null }>;
  /** ref is whatever the customer typed: order number or id. Return null if not found. */
  getOrder(ref: string): Promise<WaOrder | null>;
  /** Most recent first, max 5. phone is digits with country code. */
  findOrdersByPhone(phone: string): Promise<WaOrder[]>;
  createCodOrder(input: WaCodOrderInput): Promise<WaOrder>;
  /** A URL that opens checkout (or the cart) with these items. */
  createCheckoutLink(items: WaOrderItemInput[]): Promise<string>;
  /** Customer tapped "Confirm" on the WhatsApp COD confirmation. */
  confirmCodOrder(orderId: string): Promise<WaOrder | void>;
  cancelOrder(orderId: string, reason: string): Promise<WaOrder | void>;
}

export class WaAgentError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export type WaEvent =
  | { type: "order.created" | "order.updated"; order: WaOrder }
  | { type: "product.updated"; product: WaProduct }
  | { type: "product.deleted"; product_id: string };
