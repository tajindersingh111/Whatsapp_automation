"""Push a qualified lead to the team's own WhatsApp the moment it is quoted.

Watching a dashboard is not realistic for a shop floor, so when the sales flow finishes
qualifying someone the team gets a message on the number they already live in. It goes
out as an approved utility template: the team has not written to the business number, so
there is no 24h service window to reply inside and free text would be dropped by Meta.

Team numbers become ordinary contacts (that is the only way to send to them), so they are
marked `campaigns_excluded` on creation — otherwise the shop's own staff would start
receiving the bulk campaigns meant for customers.
"""

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_config import BotConfig
from app.models import Contact, Tenant
from app.services.conversations import get_or_create_contact, get_or_create_conversation
from app.services.messaging import send_message, tenant_sender
from app.whatsapp.client import template_message

log = logging.getLogger(__name__)

# WhatsApp rejects newlines and tabs inside template body parameters.
_PARAM_BREAKS = re.compile(r"[\r\n\t]+")


def one_line(text: str, limit: int = 300) -> str:
    cleaned = _PARAM_BREAKS.sub(" · ", (text or "").strip()).replace("•", "").strip(" ·")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned[:limit] or "detail chat mein hai"


async def alert_team(session: AsyncSession, tenant: Tenant, cfg: BotConfig, contact: Contact, summary: str) -> int:
    """Message every team number about this lead. Returns how many alerts went out."""
    conf = cfg.lead_alert
    if not conf.enabled or not conf.to_numbers:
        return 0

    sender = await tenant_sender(session, tenant)
    name = (contact.name or "").strip() or "Naya customer"
    body = [name, contact.wa_id, one_line(summary)]
    sent = 0

    for raw in conf.to_numbers:
        wa_id = re.sub(r"\D", "", raw or "")
        if not wa_id or wa_id == contact.wa_id:
            continue  # never alert the customer about themselves
        try:
            team = await get_or_create_contact(session, tenant.id, wa_id, name="Team", source="lead_alert")
            # Staff numbers must never land in a customer campaign.
            if not team.campaigns_excluded:
                team.campaigns_excluded = True
                team.excluded_reason = "team alert number"
            conv = await get_or_create_conversation(session, tenant.id, team)
            message = template_message(conf.template_name, conf.template_language, body_params=body)
            await send_message(session, sender, conv, team, message, by="flow")
            sent += 1
        except Exception as exc:  # an alert must never break the customer's chat
            log.warning("lead alert to %s failed: %s", wa_id, exc)

    return sent
