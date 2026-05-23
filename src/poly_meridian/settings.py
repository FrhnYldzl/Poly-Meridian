"""Settings — pydantic-settings, env-driven, with YAML overlay. See §21."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))

    # --- Storage ---
    postgres_url: str = "postgresql+asyncpg://poly:polypass@db:5432/poly_meridian"
    redis_url: str = "redis://redis:6379/0"

    # --- Alerts ---
    slack_webhook_url: SecretStr = Field(default=SecretStr(""))
    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_chat_id: str = ""

    # --- Observability ---
    prometheus_port: int = 8000

    @property
    def is_live(self) -> bool:
        return self.mode in (Mode.LIVE_CONSERVATIVE, Mode.LIVE_NORMAL)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
