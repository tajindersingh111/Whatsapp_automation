"""The agent loop: history -> Claude -> tools -> ... -> WhatsApp reply."""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm import LLMUnavailable, get_llm
from app.agent.prompt import build_system, knowledge_fits_inline, turn_context
from app.agent.tools import ToolContext, execute_tool, tool_definitions
from app.bot_config import BotConfig
from app.config import get_settings
from app.jobs import enqueue
from app.connectors.base import StoreConnector
from app.models import AIUsage, Contact, Conversation, Correction, KnowledgeDoc, Tenant
from app.services.conversations import recent_messages, to_llm_history
from app.services.messaging import send_message
from app.whatsapp.client import Sender, WhatsAppError, text_message

log = logging.getLogger(__name__)

MAX_ITERATIONS = 8
MAX_TOKENS = 8000
FALLBACK_REPLY = "Sorry, I couldn't process that just now. Our team will get back to you shortly."


@dataclass
class AgentResult:
    reply: str | None
    events: list[dict] = field(default_factory=list)
    handed_off: bool = False


async def _brain(session: AsyncSession, tenant: Tenant) -> tuple[list[KnowledgeDoc], list[Correction]]:
    knowledge = list((await session.scalars(select(KnowledgeDoc).where(KnowledgeDoc.tenant_id == tenant.id).order_by(KnowledgeDoc.id))).all())
    corrections = list(
        (await session.scalars(select(Correction).where(Correction.tenant_id == tenant.id).order_by(Correction.id).limit(200))).all()
    )
    return knowledge, corrections


async def _record_usage(session: AsyncSession, tenant: Tenant, conv: Conversation, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    session.add(
        AIUsage(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            model=str(getattr(response, "model", "")),
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        )
    )


def _texts(response) -> str:
    return "\n\n".join(b.text.strip() for b in response.content if getattr(b, "type", None) == "text" and b.text.strip())


async def run_agent(
    session: AsyncSession,
    *,
    tenant: Tenant,
    cfg: BotConfig,
    contact: Contact,
    conv: Conversation,
    sender: Sender,
    connector: StoreConnector | None,
    dry_run: bool = False,
) -> AgentResult:
    """Answer the customer's latest message(s) and send the reply on WhatsApp."""
    settings = get_settings()
    knowledge, corrections = await _brain(session, tenant)
    inline = knowledge_fits_inline(knowledge)
    history, preamble = to_llm_history(await recent_messages(session, conv.id, limit=40))
    if not history or history[-1]["role"] != "user":
        return AgentResult(reply=None)

    ctx = ToolContext(
        session=session, tenant=tenant, cfg=cfg, contact=contact, conv=conv, sender=sender,
        connector=connector, dry_run=dry_run, knowledge_inline=inline,
    )
    system = [*build_system(cfg, knowledge, corrections), turn_context(contact, preamble)]
    tools = tool_definitions(inline)
    model = cfg.model or settings.default_model
    effort = cfg.effort or settings.default_effort
    messages: list = list(history)
    reply = ""

    for _ in range(MAX_ITERATIONS):
        try:
            response = await get_llm().create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": effort},
            )
        except LLMUnavailable as exc:
            # Park the chat with the team instead of retrying something that cannot succeed.
            log.error("AI unavailable for tenant %s: %s", tenant.id, exc)
            conv.mode = "human"
            conv.handoff_reason = f"AI unavailable: {exc}"[:500]
            ctx.events.append({"type": "error", "message": str(exc)})
            return AgentResult(reply=None, events=ctx.events, handed_off=True)
        await _record_usage(session, tenant, conv, response)

        if response.stop_reason == "refusal":
            log.warning("model refused in conversation %s", conv.id)
            conv.mode = "human"
            conv.handoff_reason = "Assistant could not answer this message"
            reply = FALLBACK_REPLY
            ctx.events.append({"type": "handoff", "reason": "refusal"})
            break

        text = _texts(response)
        if text:
            reply = text
        if response.stop_reason != "tool_use":
            break

        # Keep the full assistant content (thinking + tool_use blocks) unchanged for the next call.
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            output, is_error = await execute_tool(ctx, block.name, block.input)
            log.info("tool %s -> %s%s", block.name, "ERROR " if is_error else "", output[:200])
            ctx.events.append({"type": "tool", "name": block.name, "input": json.loads(json.dumps(block.input, default=str)), "error": is_error})
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})
        reply = ""  # only send what the model says after seeing the tool results
    else:
        log.warning("agent hit max iterations in conversation %s", conv.id)

    if not reply:
        if any(e["type"] == "image_sent" for e in ctx.events):
            return AgentResult(reply=None, events=ctx.events, handed_off=conv.mode == "human")
        reply = FALLBACK_REPLY
    try:
        await send_message(session, sender, conv, contact, text_message(reply), by="bot")
    except WhatsAppError as exc:
        # Temporary Meta error: retry just the delivery, never the whole turn (tools may have placed orders).
        log.warning("reply delivery failed for conversation %s, will retry: %s", conv.id, exc)
        await enqueue(session, "deliver_text", {"conversation_id": conv.id, "text": reply, "by": "bot"}, tenant_id=tenant.id, delay_seconds=30)
    return AgentResult(reply=reply, events=ctx.events, handed_off=conv.mode == "human")


FOLLOWUP_INSTRUCTIONS = """\
You write one WhatsApp follow-up message for a customer who stopped replying during a shopping chat.
Rules: 1 to 3 short lines, friendly, no pressure, no fake urgency, no new discounts unless listed as an offer.
Refer to what they were looking at and make it easy to continue (e.g. ask one simple question or offer help ordering).
Write in the same language the customer used. Output only the message text."""


async def compose_followup(session: AsyncSession, *, tenant: Tenant, cfg: BotConfig, contact: Contact, conv: Conversation) -> str | None:
    settings = get_settings()
    history, _ = to_llm_history(await recent_messages(session, conv.id, limit=20))
    if not history:
        return None
    transcript = "\n".join(f"{'Customer' if t['role'] == 'user' else 'Store'}: {t['content']}" for t in history)
    knowledge, corrections = await _brain(session, tenant)
    try:
        response = await _followup_call(cfg, settings, knowledge, corrections, transcript)
    except LLMUnavailable as exc:
        log.error("follow-up skipped, AI unavailable: %s", exc)
        return None
    await _record_usage(session, tenant, conv, response)
    if response.stop_reason == "refusal":
        return None
    return _texts(response) or None


async def _followup_call(cfg: BotConfig, settings, knowledge, corrections, transcript: str):
    return await get_llm().create(
        model=cfg.model or settings.default_model,
        max_tokens=2000,
        system=[*build_system(cfg, knowledge, corrections), {"type": "text", "text": FOLLOWUP_INSTRUCTIONS}],
        messages=[{"role": "user", "content": f"<chat_transcript>\n{transcript}\n</chat_transcript>\nWrite the follow-up message."}],
        output_config={"effort": "low"},
    )
