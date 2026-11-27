"""Serves the MCP server over Streamable HTTP at /mcp, with /healthz for the platform; /metrics is on
the management port (management.py)."""

from __future__ import annotations

from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import cohorts, errors, events, flags, http_metrics, management, telemetry, usage
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

    # The usage metrics (usage.py) and the product events counter (events.py) are scraped on the
    # management port (management.py, main); the gateway routes only /mcp to the outside.
    events.register(usage.registry)

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

    # Requests per flag, on and off, for the SDLC tool's gradual releases (logged once a minute).
    cohorts.start_reporting()
    # Product events the feature goals count (events.record), summed once a minute for the SDLC tool.
    events.start_reporting()
    # Which app is calling (User-Agent), and the client each session's initialize names (usage.py).
    return OpenTelemetryMiddleware(
        # Requests, errors and latency per route, on the management port (http_metrics.py).
        errors.TraceResponseMiddleware(
            cohorts.CohortMiddleware(
                usage.UsageMiddleware(http_metrics.HttpMetrics(app, registry=usage.registry))
            )
        ),
        excluded_urls="healthz,metrics",
    )


def main() -> None:
    import uvicorn

    settings = Settings()
    telemetry.setup_logging(settings.name)
    # log_config=None: uvicorn's own and access logs go through the JSON logging set up above,
    # like everything else (trace ids included), instead of uvicorn's plain-text format.
    app = create_app(settings)
    # /metrics on the management port (MANAGEMENT_PORT, 8081), apart from the API, as in the Java
    # templates: the mesh lets the data plane's metrics collector reach that port only.
    management.serve(usage.metrics_body, host=settings.host)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
