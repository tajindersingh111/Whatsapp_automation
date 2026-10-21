"""Meta WhatsApp Cloud API sender.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""

import logging
import re
import uuid
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

# Meta error codes where retrying will not help.
PERMANENT_ERROR_CODES = {
    100,  # invalid parameter
    131008,  # required parameter missing
    131009,  # parameter value invalid
    131026,  # message undeliverable (not on WhatsApp, old app version...)
    131047,  # re-engagement: more than 24h since the customer's last message
    131051,  # unsupported message type
    132000,  # template param count mismatch
    132001,  # template does not exist / not approved in this language
    132005,  # template hydrated text too long
    132007,  # template format character policy violated
    132012,  # template parameter format mismatch
    133010,  # phone number not registered
}


# Throughput / pair-rate / spam-rate limits: retry later, never mark the customer as failed.
RATE_LIMIT_CODES = {4, 80007, 130429, 131048, 131056}


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def permanent(self) -> bool:
        if self.code in RATE_LIMIT_CODES:
            return False
        if self.code in PERMANENT_ERROR_CODES:
            return True
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


class Sender(Protocol):
    simulated: bool

    async def send(self, to: str, message: dict) -> str: ...

    async def mark_read(self, wa_message_id: str) -> None: ...


class CloudApiSender:
    simulated = False

    def __init__(self, phone_number_id: str, access_token: str, graph_version: str, http: httpx.AsyncClient):
        self.base = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.http = http

    async def send(self, to: str, message: dict) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message}
        resp = await self.http.post(self.base, json=body, headers=self.headers, timeout=20)
        data = _json(resp)
        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            detail = err.get("error_data", {}).get("details") or err.get("message") or resp.text[:300]
            raise WhatsAppError(f"Meta API {resp.status_code}: {detail}", code=err.get("code"), status=resp.status_code)
        return data["messages"][0]["id"]

    async def mark_read(self, wa_message_id: str) -> None:
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id}
        try:
            await self.http.post(self.base, json=body, headers=self.headers, timeout=10)
        except httpx.HTTPError:
            log.warning("mark_read failed for %s", wa_message_id)


class SimulatedSender:
    """Used for the playground and for tenants without a WhatsApp number yet."""

    simulated = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, to: str, message: dict) -> str:
        self.sent.append((to, message))
        return f"sim.{uuid.uuid4().hex}"

    async def mark_read(self, wa_message_id: str) -> None:
        return None


def _json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


# --- message builders ---


def text_message(body: str, preview_url: bool = True) -> dict:
    return {"type": "text", "text": {"body": body[:4096], "preview_url": preview_url}}


def image_message(link: str, caption: str | None = None) -> dict:
    image: dict = {"link": link}
    if caption:
        image["caption"] = caption[:1024]
    return {"type": "image", "image": image}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def buttons_message(body: str, buttons: list[tuple[str, str]], header: str | None = None, image_url: str | None = None) -> dict:
    """Up to 3 reply buttons; titles are cut to Meta's 20 character limit. Optional text or image header."""
    interactive: dict = {
        "type": "button",
        "body": {"text": body[:1024]},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}} for bid, title in buttons[:3]
            ]
        },
    }
    if image_url:
        interactive["header"] = {"type": "image", "image": {"link": image_url}}
    elif header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    return {"type": "interactive", "interactive": interactive}


def list_message(body: str, button: str, sections: list[tuple[str, list[tuple[str, str, str]]]]) -> dict:
    """Interactive list: sections of (title, [(row_id, row_title, row_description)]), max 10 rows in total."""
    out, budget = [], 10
    for title, rows in sections:
        rows = rows[:budget]
        if not rows:
            continue
        budget -= len(rows)
        out.append({
            "title": _clip(title, 24),
            "rows": [{"id": rid[:200], "title": _clip(rt, 24), **({"description": _clip(rd, 72)} if rd else {})} for rid, rt, rd in rows],
        })
    return {
        "type": "interactive",
        "interactive": {"type": "list", "body": {"text": body[:4096]}, "action": {"button": _clip(button, 20), "sections": out}},
    }


def cta_url_message(body: str, button_text: str, url: str) -> dict:
    return {
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body[:1024]},
            "action": {"name": "cta_url", "parameters": {"display_text": button_text[:20], "url": url}},
        },
    }


def template_message(
    name: str,
    language: str,
    body_params: list[str] | None = None,
    quick_reply_payloads: list[str] | None = None,
    header_image_url: str | None = None,
) -> dict:
    components: list[dict] = []
    if header_image_url:
        components.append({"type": "header", "parameters": [{"type": "image", "image": {"link": header_image_url}}]})
    if body_params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in body_params]}
        )
    for index, payload in enumerate(quick_reply_payloads or []):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": payload}],
            }
        )
    template: dict = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components
    return {"type": "template", "template": template}


def describe(message: dict) -> str:
    """Human-readable text for the inbox / conversation history."""
    kind = message.get("type")
    if kind == "text":
        return message["text"]["body"]
    if kind == "image":
        return message["image"].get("caption", "") or "[image]"
    if kind == "interactive":
        inter = message["interactive"]
        text = inter.get("body", {}).get("text", "")
        if inter["type"] == "button":
            titles = " | ".join(b["reply"]["title"] for b in inter["action"]["buttons"])
            return f"{text}\n[buttons: {titles}]"
        if inter["type"] == "list":
            rows = " | ".join(r["title"] for sec in inter["action"]["sections"] for r in sec["rows"])
            return f"{text}\n[list: {rows}]"
        if inter["type"] == "cta_url":
            params = inter["action"]["parameters"]
            return f"{text}\n[{params['display_text']}: {params['url']}]"
        return text
    if kind == "template":
        tpl = message["template"]
        params = [
            p.get("text", "")
            for c in tpl.get("components", [])
            if c["type"] == "body"
            for p in c["parameters"]
        ]
        return f"[template {tpl['name']}] " + ", ".join(params)
    return f"[{kind}]"


async def list_templates(http: httpx.AsyncClient, graph_version: str, waba_id: str, access_token: str) -> list[dict]:
    """Approved message templates of a WhatsApp Business Account, simplified for the dashboard."""
    url = f"https://graph.facebook.com/{graph_version}/{waba_id}/message_templates"
    params = {"fields": "name,language,status,category,components", "limit": 200}
    resp = await http.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    data = _json(resp)
    if resp.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise WhatsAppError(f"Meta API {resp.status_code}: {err.get('message') or resp.text[:200]}", code=err.get("code"), status=resp.status_code)
    out = []
    for t in data.get("data", []):
        if t.get("status") != "APPROVED":
            continue
        body = next((c.get("text", "") for c in t.get("components", []) if c.get("type") == "BODY"), "")
        header = next((c for c in t.get("components", []) if c.get("type") == "HEADER"), {})
        buttons = next((c.get("buttons", []) for c in t.get("components", []) if c.get("type") == "BUTTONS"), [])
        out.append({
            "name": t["name"],
            "language": t.get("language", "en"),
            "category": t.get("category"),
            "body": body,
            "param_count": len(set(re.findall(r"\{\{(\d+)\}\}", body))),
            "header": header.get("format"),  # TEXT | IMAGE | VIDEO | DOCUMENT | None
            "buttons": [b.get("text") for b in buttons],
        })
    return sorted(out, key=lambda t: (t["name"], t["language"]))


async def check_connection(http: httpx.AsyncClient, graph_version: str, phone_number_id: str, waba_id: str | None, access_token: str) -> dict:
    """Validate a client's number + token with Meta and subscribe our app to their WhatsApp account.

    Subscribing is what makes Meta deliver that account's customer messages to our webhook.
    """
    base = f"https://graph.facebook.com/{graph_version}"
    headers = {"Authorization": f"Bearer {access_token}"}
    result: dict = {"ok": False, "phone": None, "subscribed": False, "problems": []}

    resp = await http.get(f"{base}/{phone_number_id}", params={"fields": "display_phone_number,verified_name,quality_rating,name_status,platform_type"}, headers=headers, timeout=20)
    if resp.status_code == 400 and "nonexisting field" in resp.text.lower():
        resp = await http.get(f"{base}/{phone_number_id}", params={"fields": "display_phone_number,verified_name,quality_rating"}, headers=headers, timeout=20)
    data = _json(resp)
    if resp.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        if err.get("code") == 190 or resp.status_code == 401:
            result["problems"].append("Access token is invalid or expired. Generate a permanent system-user token.")
        else:
            result["problems"].append(f"Meta could not find this phone number id: {err.get('message') or resp.text[:200]}")
        return result
    result["phone"] = {k: data.get(k) for k in ("display_phone_number", "verified_name", "quality_rating", "name_status", "platform_type")}
    if data.get("platform_type") and data["platform_type"] != "CLOUD_API":
        result["problems"].append("This number is not registered on the Cloud API yet. Finish 'Production setup' in Meta (verify OTP + set PIN).")

    if not waba_id:
        result["problems"].append("Add the WhatsApp Business Account (WABA) ID so customer messages and templates work.")
    else:
        sub = await http.post(f"{base}/{waba_id}/subscribed_apps", headers=headers, timeout=20)
        sub_data = _json(sub)
        if sub.status_code < 400 and sub_data.get("success"):
            result["subscribed"] = True
        else:
            err = sub_data.get("error", {}) if isinstance(sub_data, dict) else {}
            result["problems"].append(f"Could not subscribe the app to this WhatsApp account: {err.get('message') or sub.text[:200]}")
    result["ok"] = not result["problems"]
    return result
