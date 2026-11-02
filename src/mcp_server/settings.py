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
    # Hostnames clients may use (DNS rebinding protection); empty allows any, for local use.
    allowed_hosts: list[str] = []
