from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres@localhost:5436/wa_agent"

    # Platform operator token for /admin APIs and the dashboard (managed-service mode).
    admin_token: str = "change-me"
    # Fernet key used to encrypt tenant secrets (WhatsApp tokens, store API keys).
    encryption_key: str = ""

    # Public base URL of this service (used when telling stores where to send webhooks).
    public_base_url: str = "http://localhost:8000"

    # Meta WhatsApp Cloud API (one Meta app serves every client WABA).
    meta_app_secret: str = ""
    meta_verify_token: str = "change-me-verify"
    meta_graph_version: str = "v23.0"

    # Claude
    anthropic_api_key: str = ""
    default_model: str = "claude-opus-5-5"
    default_effort: str = "low"

    # Worker
    run_worker_in_process: bool = True
    worker_poll_seconds: float = 1.0
    inbound_debounce_seconds: float = 3.0

    log_level: str = "INFO"
    # "production" refuses to start with unsafe settings (see problems()).
    environment: str = "development"

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        # Railway/Heroku style URLs (postgres://, postgresql://) -> SQLAlchemy asyncpg URL.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    def problems(self) -> list[str]:
        """Settings that must be fixed before the server is exposed to the internet."""
        issues = []
        if self.admin_token in ("", "change-me") or len(self.admin_token) < 20:
            issues.append("ADMIN_TOKEN must be a random value of 20+ characters")
        if not self.encryption_key:
            issues.append("ENCRYPTION_KEY is not set")
        if not self.meta_app_secret:
            issues.append("META_APP_SECRET is not set (WhatsApp webhooks will be rejected)")
        if self.meta_verify_token in ("", "change-me-verify"):
            issues.append("META_VERIFY_TOKEN is not set")
        return issues


@lru_cache
def get_settings() -> Settings:
    return Settings()
