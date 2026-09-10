"""Environment configuration. Gateway credentials fail closed (required); Phase 2's
news/Discord secrets are optional here since news providers and Discord posting are
each meant to degrade gracefully when unconfigured (skip that provider, refuse only
at the point a live post/fetch is actually attempted — see run.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """equity-scanner talks to SchwabGateway only; there is no direct-access mode."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gateway_url: str = Field(validation_alias="SCHWAB_GATEWAY_URL")
    gateway_api_key: SecretStr = Field(validation_alias="SCHWAB_GATEWAY_API_KEY")
    gateway_client_identity: Literal["equity-scanner"] = Field(
        default="equity-scanner", validation_alias="SCHWAB_GATEWAY_CLIENT_IDENTITY"
    )
    gateway_priority: Literal["background"] = Field(
        default="background", validation_alias="SCHWAB_GATEWAY_PRIORITY"
    )
    gateway_timeout_seconds: float = Field(
        default=5.0, gt=0, validation_alias="SCHWAB_GATEWAY_TIMEOUT_SECONDS"
    )
    gateway_max_attempts: int = Field(
        default=3, ge=1, le=6, validation_alias="SCHWAB_GATEWAY_MAX_ATTEMPTS"
    )
    gateway_retry_backoff_seconds: float = Field(
        default=0.25, ge=0, le=10, validation_alias="SCHWAB_GATEWAY_RETRY_BACKOFF_SECONDS"
    )
    sec_user_agent: str | None = Field(default=None, validation_alias="SEC_USER_AGENT")
    alpha_vantage_api_key: SecretStr | None = Field(
        default=None, validation_alias="ALPHA_VANTAGE_API_KEY"
    )
    discord_webhook_url: str | None = Field(
        default=None, validation_alias="EQUITY_SCANNER_DISCORD_WEBHOOK_URL"
    )

    @field_validator("gateway_url")
    @classmethod
    def gateway_url_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SCHWAB_GATEWAY_URL must not be blank")
        return value

    @field_validator("gateway_api_key")
    @classmethod
    def gateway_api_key_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("SCHWAB_GATEWAY_API_KEY must not be blank")
        return value
