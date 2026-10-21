import { createHmac, timingSafeEqual } from "node:crypto";

// signature = "sha256=" + hex(HMAC_SHA256(secret, `${timestamp}.${METHOD}.${pathWithQuery}.${body}`))
// Must match app/security.py in the agent.

export const TOLERANCE_SECONDS = 300;

export function sign(secret: string, method: string, pathWithQuery: string, body: string, timestamp = Math.floor(Date.now() / 1000)) {
  const ts = String(timestamp);
  const digest = createHmac("sha256", secret).update(`${ts}.${method.toUpperCase()}.${pathWithQuery}.${body}`).digest("hex");
  return { "X-WA-Agent-Timestamp": ts, "X-WA-Agent-Signature": `sha256=${digest}` };
}

export function verify(
  secret: string,
  method: string,
  pathWithQuery: string,
  body: string,
  timestamp: string | null,
  signature: string | null,
  now = Math.floor(Date.now() / 1000),
): boolean {
  if (!secret || !timestamp || !signature) return false;
  const ts = Number(timestamp);
  if (!Number.isInteger(ts) || Math.abs(now - ts) > TOLERANCE_SECONDS) return false;
  const expected = Buffer.from(sign(secret, method, pathWithQuery, body, ts)["X-WA-Agent-Signature"]);
  const given = Buffer.from(signature);
  return expected.length === given.length && timingSafeEqual(expected, given);
}
