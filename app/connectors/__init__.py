import httpx

from app.connectors.base import StoreConnector, StoreError
from app.connectors.custom import CustomStoreConnector
from app.connectors.shopify import ShopifyConnector
from app.connectors.woocommerce import WooCommerceConnector
from app.models import StoreConnection
from app.security import decrypt_json

STORE_KINDS = ("custom", "shopify", "woocommerce")

# Required credential keys per store kind (validated when a store is connected).
REQUIRED_CREDENTIALS = {
    "custom": ("secret",),
    "shopify": ("access_token", "api_secret"),
    "woocommerce": ("consumer_key", "consumer_secret", "webhook_secret"),
}

_http: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(follow_redirects=True)
    return _http


async def close_http_client() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
    _http = None


def build_connector(conn: StoreConnection, http: httpx.AsyncClient | None = None) -> StoreConnector:
    creds = decrypt_json(conn.credentials_enc)
    http = http or http_client()
    if conn.kind == "custom":
        return CustomStoreConnector(conn.base_url, creds["secret"], http)
    if conn.kind == "shopify":
        return ShopifyConnector(conn.base_url, creds["access_token"], creds["api_secret"], http, creds.get("storefront_url"))
    if conn.kind == "woocommerce":
        return WooCommerceConnector(conn.base_url, creds["consumer_key"], creds["consumer_secret"], creds["webhook_secret"], http)
    raise StoreError(f"Unknown store kind {conn.kind}", permanent=True)


__all__ = ["StoreConnector", "StoreError", "build_connector", "http_client", "close_http_client", "STORE_KINDS", "REQUIRED_CREDENTIALS"]
