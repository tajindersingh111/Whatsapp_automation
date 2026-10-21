// Copy to: app/api/wa-agent/[...path]/route.ts  (or src/app/... if your project uses src/)
import { createWaAgentHandler } from "@/lib/wa-agent/handler";
import { adapter } from "@/lib/wa-agent/adapter";

export const runtime = "nodejs"; // uses node:crypto
export const dynamic = "force-dynamic";

export const { GET, POST } = createWaAgentHandler(adapter);
