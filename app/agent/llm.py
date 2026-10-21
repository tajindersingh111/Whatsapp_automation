"""Thin wrapper around the Anthropic SDK so the agent loop can be tested with a fake."""

import logging
from typing import Any, Protocol

import anthropic

from app.config import get_settings

log = logging.getLogger(__name__)

# Models that accept server-side refusal fallbacks in their "default" form.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5"}


class LLMUnavailable(Exception):
    """Retrying will not help (missing/invalid key, bad model name, invalid request)."""


class LLM(Protocol):
    async def create(self, **params: Any) -> Any: ...


class AnthropicLLM:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None):
        settings = get_settings()
        self.client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None, timeout=60.0, max_retries=2)

    async def create(self, **params: Any) -> Any:
        if params.get("model") in _FALLBACK_MODELS:
            params.setdefault("betas", ["server-side-fallback-2026-07-01"])
            params.setdefault("fallbacks", "default")
        try:
            return await self.client.beta.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable("Claude API key is invalid") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMUnavailable(f"Claude API permission denied: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailable(f"Unknown Claude model {params.get('model')!r}") from exc
        except anthropic.BadRequestError as exc:
            log.error("Claude rejected the request: %s (request id %s)", exc.message, exc.request_id)
            raise LLMUnavailable(f"Claude rejected the request: {exc.message}") from exc
        except TypeError as exc:
            # Raised by the SDK before sending when no credentials are configured.
            if "authentication" in str(exc).lower():
                raise LLMUnavailable("ANTHROPIC_API_KEY is not set") from exc
            raise


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = AnthropicLLM()
    return _llm


def set_llm(llm: LLM | None) -> None:
    """Tests inject a scripted fake here."""
    global _llm
    _llm = llm
