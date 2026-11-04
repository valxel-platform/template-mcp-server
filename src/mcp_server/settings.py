"""Configuration from the environment. Auth has no defaults: a server without an issuer refuses to
start rather than serve product data to anyone."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_")

    name: str = "care"
    host: str = "0.0.0.0"  # noqa: S104  (inside the container)
    port: int = 8080
    # This server's public URL (the token audience / RFC 8707 resource).
    resource_url: str = "http://localhost:8080/mcp"
    oidc_issuer: str = ""
    oidc_jwks_url: str = ""
    read_scope: str = "care:read"
    write_scope: str = "care:write"
    # Per caller: calls per minute, and burst.
    rate_per_minute: int = 120
    rate_burst: int = 30
    # Feature flags: the product's Flipt over OFREP. Unset, every flag reads as its default (off).
    flags_url: str = ""
    flags_environment: str = ""  # Flipt environment, e.g. production (header x-flipt-environment)
    flags_namespace: str = ""  # the product's Flipt namespace, e.g. emr
    flags_token: str = ""  # a Flipt client token, if the product's Flipt requires authentication
    # Hostnames clients may use (DNS rebinding protection); empty allows any, for local use.
    allowed_hosts: list[str] = []
