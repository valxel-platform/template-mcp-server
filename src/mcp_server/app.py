"""Serves the MCP server over Streamable HTTP at /mcp, with /healthz for the platform."""

from __future__ import annotations

from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import errors, flags, telemetry
from .security import JwtVerifier
from .server import build_server
from .settings import Settings


def create_app(
    settings: Settings | None = None, verifier: Any = None, store: Any = None, auditor: Any = None
) -> Any:
    settings = settings or Settings()
    telemetry.setup(settings.name)
    errors.start_errors()
    flags.configure(settings)
    verifier = verifier or JwtVerifier(
        issuer=settings.oidc_issuer, jwks_url=settings.oidc_jwks_url, audience=settings.resource_url
    )
    server = build_server(settings, verifier=verifier, store=store, auditor=auditor)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts), allowed_hosts=settings.allowed_hosts
    )
    # Stateless: any replica can serve any request, so the server scales horizontally.
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security, host=settings.host
    )
    # Incoming requests join the caller's trace (W3C traceparent); /healthz is not traced. Each
    # response names its trace (traceresponse), set inside the span.
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    return OpenTelemetryMiddleware(errors.TraceResponseMiddleware(app), excluded_urls="healthz")


def main() -> None:
    import uvicorn

    settings = Settings()
    telemetry.setup_logging(settings.name)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
