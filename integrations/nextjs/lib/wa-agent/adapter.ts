// Your store <-> WhatsApp agent mapping. Fill in each method using your own database/helpers.
// See examples/shatkon-adapter.ts for a complete Prisma example.
import { WaAgentError, type WaStoreAdapter } from "./types";

export const adapter: WaStoreAdapter = {
  async listProducts({ cursor, limit }) {
    // Return active products, `limit` at a time. nextCursor = whatever lets you fetch the next page (or null).
    throw new WaAgentError(501, "listProducts not implemented");
  },
  async getOrder(ref) {
    return null;
  },
  async findOrdersByPhone(phone) {
    return [];
  },
  async createCodOrder(input) {
    throw new WaAgentError(422, "Cash on Delivery orders are not enabled for this store");
  },
  async createCheckoutLink(items) {
    throw new WaAgentError(501, "createCheckoutLink not implemented");
  },
  async confirmCodOrder(orderId) {},
  async cancelOrder(orderId, reason) {},
};
