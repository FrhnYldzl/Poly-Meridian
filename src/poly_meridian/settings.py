"""Settings — pydantic-settings, env-driven, with YAML overlay. See §21."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from poly_meridian.domain import Mode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Operation ---
    mode: Mode = Mode.PAPER
    log_level: str = "INFO"
    config_dir: Path = Path("config")

    # --- Polymarket ---
    polymarket_private_key: SecretStr = Field(default=SecretStr(""))
    polymarket_api_key: SecretStr = Field(default=SecretStr(""))
    polymarket_api_secret: SecretStr = Field(default=SecretStr(""))
    polymarket_passphrase: SecretStr = Field(default=SecretStr(""))
    polymarket_chain_id: int = 137
    polymarket_clob_host: str = "https://clob.polymarket.com"
    polymarket_gamma_host: str = "https://gamma-api.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # --- On-chain ---
    alchemy_api_key: SecretStr = Field(default=SecretStr(""))
    polygon_rpc_url: str = ""

    # --- Sources ---
    gdelt_api_key: SecretStr = Field(default=SecretStr(""))
    newsapi_key: SecretStr = Field(default=SecretStr(""))
    x_bearer_token: SecretStr = Field(default=SecretStr(""))

    # --- LLM ---
    # Multiple aliases let operators name vars per-project on shared accounts
    # (e.g. ANTHROPIC_API_KEY_POLLY_AGENT to avoid collisions across services).
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENAI_API_KEY", "OPENAI_API_KEY_POLLY_AGENT"),
    )
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_POLLY_AGENT"
        ),
    )
    gemini_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY_POLLY_AGENT"
        ),
    )
    embedding_model: str = "text-embedding-3-small"   # OpenAI, 1536-d
    sentiment_model: str = "claude-haiku-4-5-20251001"
    sentiment_batch_size: int = 10
    sentiment_window_sec: int = 1800

    # --- Storage ---
    postgres_url: str = "postgresql+asyncpg://poly:polypass@db:5432/poly_meridian"
    redis_url: str = "redis://redis:6379/0"

    # --- Alerts ---
    slack_webhook_url: SecretStr = Field(default=SecretStr(""))
    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_chat_id: str = ""

    # --- Observability / API port ---
    # `prometheus_port` is the FastAPI port (legacy name kept for back-compat).
    # On Railway, the platform sets $PORT — read that with precedence, then
    # fall back to the legacy PROMETHEUS_PORT for local docker-compose.
    prometheus_port: int = Field(
        default=8000,
        validation_alias=AliasChoices("PORT", "PROMETHEUS_PORT"),
    )

    # --- Bankroll ---
    # Paper mode bankroll. Default $100K matches MASTER_SPEC; operator can
    # override to mirror the real deployable capital so paper-track is a
    # realistic dry-run (e.g. PAPER_STARTING_NAV=250 to match a $250 live
    # pilot). Kelly fractions + risk caps are pct-of-bankroll so they scale
    # naturally; what changes at small bankroll is absolute trade size,
    # min-order rejections, and fee impact per trade.
    paper_starting_nav: float = Field(
        default=100_000.0,
        validation_alias=AliasChoices("PAPER_STARTING_NAV", "PAPER_BANKROLL"),
    )

    @property
    def is_live(self) -> bool:
        return self.mode in (Mode.LIVE_CONSERVATIVE, Mode.LIVE_NORMAL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
