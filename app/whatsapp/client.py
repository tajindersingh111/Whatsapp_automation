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


# Errors that mean OUR setup is wrong (template, parameters, image link, account), not that the customer's
# number is bad. Sending the same message to the next customer would fail the same way, so campaigns pause.
CONFIG_ERROR_CODES = {
    100, 131008, 131009, 131037, 131042, 131052, 131053,
    132000, 132001, 132005, 132007, 132012, 132015, 132016, 132068, 132069,
    10, 190, 200, 368, 80008,
}
_CONFIG_WORDS = re.compile(r"format mismatch|header|template|parameter|media|access token|permission", re.I)
_CODE_IN_TEXT = re.compile(r"\(code (\d+)\)")


def is_config_error(text: str | None) -> bool:
    """True when a stored send error points at the template/setup rather than the customer's number."""
    if not text:
        return False
    m = _CODE_IN_TEXT.search(text)
    if m and int(m.group(1)) in CONFIG_ERROR_CODES:
        return True
    return bool(_CONFIG_WORDS.search(text))


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
            code = err.get("code")
            raise WhatsAppError(f"Meta API {resp.status_code}{f' (code {code})' if code else ''}: {detail}", code=code, status=resp.status_code)
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
    header_media_id: str | None = None,
) -> dict:
    components: list[dict] = []
    if header_media_id:
        components.append({"type": "header", "parameters": [{"type": "image", "image": {"id": header_media_id}}]})
    elif header_image_url:
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
    """Message templates of a WhatsApp Business Account, simplified for the dashboard.

    Returns every status (not just APPROVED) with a `status` field, so a template still in
    review shows up and the user isn't confused about where it went.
    """
    url = f"https://graph.facebook.com/{graph_version}/{waba_id}/message_templates"
    params = {"fields": "name,language,status,category,components", "limit": 200}
    resp = await http.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    data = _json(resp)
    if resp.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise WhatsAppError(f"Meta API {resp.status_code}: {err.get('message') or resp.text[:200]}", code=err.get("code"), status=resp.status_code)
    out = []
    for t in data.get("data", []):
        body = next((c.get("text", "") for c in t.get("components", []) if c.get("type") == "BODY"), "")
        header = next((c for c in t.get("components", []) if c.get("type") == "HEADER"), {})
        buttons = next((c.get("buttons", []) for c in t.get("components", []) if c.get("type") == "BUTTONS"), [])
        out.append({
            "name": t["name"],
            "language": t.get("language", "en"),
            "category": t.get("category"),
            "status": t.get("status"),  # APPROVED | PENDING | REJECTED | PAUSED | ...
            "approved": t.get("status") == "APPROVED",
            "body": body,
            "param_count": len(set(re.findall(r"\{\{(\d+)\}\}", body))),
            "header": header.get("format"),  # TEXT | IMAGE | VIDEO | DOCUMENT | None
            "header_example": ((header.get("example") or {}).get("header_handle") or [None])[0],  # sample image given at creation
            "buttons": [b.get("text") for b in buttons],
        })
    # Approved first, then by name.
    return sorted(out, key=lambda t: (not t["approved"], t["name"], t["language"]))


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


async def discover_numbers(http: httpx.AsyncClient, graph_version: str, access_token: str) -> list[dict]:
    """Every WhatsApp number this token can use, so nobody has to hunt for IDs in Meta's UI.

    debug_token lists the WhatsApp Business Accounts granted to the token; each account lists its numbers.
    """
    base = f"https://graph.facebook.com/{graph_version}"
    resp = await http.get(f"{base}/debug_token", params={"input_token": access_token, "access_token": access_token}, timeout=20)
    data = _json(resp).get("data", {})
    if resp.status_code >= 400 or not data.get("is_valid"):
        message = (data.get("error") or {}).get("message") or _json(resp).get("error", {}).get("message") or "Token is not valid"
        raise WhatsAppError(f"Meta rejected the token: {message}", code=190, status=resp.status_code)
    waba_ids: list[str] = []
    for scope in data.get("granular_scopes", []):
        if scope.get("scope") in ("whatsapp_business_management", "whatsapp_business_messaging"):
            waba_ids += [t for t in scope.get("target_ids", []) if t not in waba_ids]
    numbers = []
    for waba_id in waba_ids:
        r = await http.get(
            f"{base}/{waba_id}/phone_numbers",
            params={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        for n in _json(r).get("data", []):
            numbers.append({
                "waba_id": waba_id,
                "phone_number_id": n.get("id"),
                "display_phone_number": n.get("display_phone_number"),
                "verified_name": n.get("verified_name"),
                "quality_rating": n.get("quality_rating"),
                # Meta's own test numbers show up as +1 555...; real business numbers do not.
                "is_test_number": (n.get("display_phone_number") or "").replace(" ", "").startswith("+1555"),
            })
    return numbers


MAX_IMAGE_BYTES = 5 * 1024 * 1024


async def check_image_url(http: httpx.AsyncClient, url: str) -> str | None:
    """Why Meta could not use this link as a template header image, or None if it looks fine.

    Meta downloads the link itself: it must be a public https URL straight to a JPEG/PNG (<= 5 MB),
    not a Google Drive / Instagram / web page link.
    """
    if not url.lower().startswith("https://"):
        return "Image link must start with https://"
    try:
        async with http.stream("GET", url, timeout=15, headers={"User-Agent": "WhatsApp/2"}) as resp:
            if resp.status_code >= 400:
                return f"The image link returned an error ({resp.status_code}). It must be a public direct link to the image."
            kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            size = int(resp.headers.get("content-length") or 0)
    except httpx.HTTPError as exc:
        return f"Could not open the image link ({type(exc).__name__}). Meta will not be able to either."
    if kind not in ("image/jpeg", "image/jpg", "image/png"):
        return f"The link is not a JPEG/PNG image (it returned '{kind or 'unknown'}'). Use a direct link to a .jpg or .png file, not a Drive/Instagram/web page link."
    if size > MAX_IMAGE_BYTES:
        return "The image is larger than 5 MB. Use a smaller image."
    return None


def sniff_image(data: bytes) -> str | None:
    """MIME type from the file's first bytes (JPEG/PNG only: what WhatsApp template headers accept)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


async def upload_media(http: httpx.AsyncClient, graph_version: str, phone_number_id: str, access_token: str, data: bytes, mime: str, filename: str) -> str:
    """Upload an image to Meta's media servers for this number and return its media id."""
    resp = await http.post(
        f"https://graph.facebook.com/{graph_version}/{phone_number_id}/media",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (filename, data, mime)},
        timeout=60,
    )
    body = _json(resp)
    if resp.status_code >= 400 or not body.get("id"):
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise WhatsAppError(f"Meta media upload failed ({resp.status_code}): {err.get('message') or resp.text[:200]}", code=err.get("code"), status=resp.status_code)
    return str(body["id"])


async def download_media(http: httpx.AsyncClient, graph_version: str, media_id: str, access_token: str) -> tuple[bytes, str]:
    """Fetch a photo/file a customer sent us. Returns (bytes, mime type).

    Two calls, both needing the token: the first resolves the media id to a download
    URL (Meta's media ids are not themselves fetchable), the second needs the same
    Authorization header again — the URL alone is not public. Meta's own URLs expire in
    minutes, so this is done fresh on every view rather than cached.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    meta = _json(await http.get(f"https://graph.facebook.com/{graph_version}/{media_id}", headers=headers, timeout=20))
    url = meta.get("url")
    if not url:
        err = meta.get("error", {}) if isinstance(meta, dict) else {}
        raise WhatsAppError(f"Meta media lookup failed: {err.get('message') or 'no url returned'}", code=err.get("code"))
    resp = await http.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise WhatsAppError(f"Meta media download failed ({resp.status_code})", status=resp.status_code)
    return resp.content, meta.get("mime_type") or "application/octet-stream"


_ANY_CODE = re.compile(r"^\s*(\d{4,6}):|\(code (\d+)\)")

# Meta decided not to deliver this marketing message to this customer (not engaged / too much marketing).
# Not a bad number and not charged: the customer can receive it later, e.g. after messaging the business.
ENGAGEMENT_CODE = 131049

_EXPLANATIONS: dict[int, tuple[str, str]] = {
    131049: (
        "Held back by Meta: this customer hasn't engaged with you",
        "Meta decides this per customer to protect them from marketing, so it is not a bug and you are not charged. "
        "Ask the customer to message your number first (even just 'Hi'), then send again: marketing templates reach customers who "
        "have chatted with you recently far more reliably. Send fewer marketing messages per customer.",
    ),
    131026: (
        "Number can't receive WhatsApp messages",
        "The number is not on WhatsApp, is switched off for a long time, or is invalid. Safe to exclude from future campaigns.",
    ),
    131048: ("Your account hit Meta's spam limit", "Too many customers reported or ignored your messages. Pause marketing for a day and improve the message."),
    131056: ("Sending to this customer too fast", "Too many messages to the same customer in a short time. Wait and send later."),
    130472: ("Customer is part of a Meta experiment", "Meta skipped this customer. Nothing is wrong with your account; try again later."),
    131053: ("Image could not be used", "Meta could not download or read the header image. Resume the campaign with the template's own image or a new upload."),
    132001: ("Template not found", "The template name or language does not exist in this WhatsApp account."),
    132012: ("Template variables don't match", "The values sent don't match the template (a missing variable or a missing header image)."),
}


def error_code_of(text: str | None) -> int | None:
    """Meta error code from a stored error (webhook form '131049: ...' or send form '... (code 131049): ...')."""
    m = _ANY_CODE.search(text or "")
    return int(m.group(1) or m.group(2)) if m else None


def is_engagement_block(text: str | None) -> bool:
    return error_code_of(text) == ENGAGEMENT_CODE


def explain_error(text: str | None) -> dict | None:
    """Plain-language reason and next step for a stored send error."""
    code = error_code_of(text)
    if code in _EXPLANATIONS:
        title, action = _EXPLANATIONS[code]
        return {"code": code, "title": title, "action": action, "customer_side": code in (ENGAGEMENT_CODE, 130472)}
    return None


async def get_calling_settings(http: httpx.AsyncClient, graph_version: str, phone_number_id: str, access_token: str) -> dict:
    """Current voice-calling settings of a business number."""
    resp = await http.get(
        f"https://graph.facebook.com/{graph_version}/{phone_number_id}/settings",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    data = _json(resp)
    if resp.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise WhatsAppError(f"Meta API {resp.status_code}: {err.get('message') or resp.text[:200]}", code=err.get("code"), status=resp.status_code)
    return data.get("calling") or {}


async def set_calling_enabled(http: httpx.AsyncClient, graph_version: str, phone_number_id: str, access_token: str, enabled: bool) -> dict:
    """Turn the call button on or off for a business number. Meta needs a 2,000/day messaging limit to enable it."""
    resp = await http.post(
        f"https://graph.facebook.com/{graph_version}/{phone_number_id}/settings",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"calling": {"status": "ENABLED" if enabled else "DISABLED", "call_icon_visibility": "DEFAULT"}},
        timeout=20,
    )
    data = _json(resp)
    if resp.status_code >= 400 or not data.get("success", True):
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise WhatsAppError(f"Meta API {resp.status_code}: {err.get('message') or resp.text[:300]}", code=err.get("code"), status=resp.status_code)
    return data
