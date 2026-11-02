"""Serves the MCP server over Streamable HTTP at /mcp, with /healthz for the platform."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .security import JwtVerifier
from .server import build_server
from .settings import Settings


def create_app(settings: Settings | None = None, verifier: Any = None, store: Any = None) -> Any:
    settings = settings or Settings()
    verifier = verifier or JwtVerifier(
        issuer=settings.oidc_issuer, jwks_url=settings.oidc_jwks_url, audience=settings.resource_url
    )
    server = build_server(settings, verifier=verifier, store=store)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts), allowed_hosts=settings.allowed_hosts
    )
    # Stateless: any replica can serve any request, so the server scales horizontally.
    return server.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security, host=settings.host
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
