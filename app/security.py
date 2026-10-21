import base64
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Header, HTTPException

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set. Generate one with: python -m app.cli gen-key")
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Could not decrypt secret; ENCRYPTION_KEY changed?") from exc


def encrypt_json(data: dict) -> str:
    return encrypt(json.dumps(data, sort_keys=True))


def decrypt_json(token: str) -> dict:
    return json.loads(decrypt(token))


def generate_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# --- Meta: X-Hub-Signature-256: sha256=<hex hmac of raw body with app secret> ---


def verify_meta_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# --- Shopify / WooCommerce: base64 HMAC-SHA256 of raw body ---


def verify_base64_hmac(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header.strip())


# --- Custom store protocol (Next.js kit), both directions ---
# signature = hex HMAC-SHA256(secret, f"{timestamp}.{METHOD}.{path_with_query}.{body}")

SIGNATURE_TOLERANCE_SECONDS = 300


def sign_request(secret: str, method: str, path_with_query: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    message = f"{ts}.{method.upper()}.{path_with_query}.".encode() + body
    sig = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return {"X-WA-Agent-Timestamp": ts, "X-WA-Agent-Signature": f"sha256={sig}"}


def verify_request_signature(
    secret: str, method: str, path_with_query: str, body: bytes, timestamp: str | None, signature: str | None
) -> bool:
    if not timestamp or not signature or not secret:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = sign_request(secret, method, path_with_query, body, ts)["X-WA-Agent-Signature"]
    return hmac.compare_digest(expected, signature)


# --- Admin auth ---


def require_admin(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().admin_token
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), token):
        raise HTTPException(status_code=403, detail="Invalid token")
