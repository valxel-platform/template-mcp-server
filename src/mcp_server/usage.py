"""Who calls which tool: the clients this server serves, how often, how fast, and how it went.

Every tool call (security.guarded) is recorded three ways, none of them naming a person:

- one log line on the `mcp.calls` logger, JSON like every other log line (trace ids included):
      {"message": "tools/call create_task", "mcp.tool": "create_task", "mcp.client.id": "care-desk",
       "mcp.client.app": "claude-code", "mcp.client.version": "2.1.4", "event.outcome": "success",
       "event.duration_ms": 41.7, ...}
- attributes on the request's trace span: gen_ai.tool.name, mcp.method.name, mcp.client.id,
  mcp.client.app, so a slow or failed call can be followed into the services it called;
- Prometheus metrics on /metrics:
      mcp_tool_calls_total{tool, client, app, outcome}
      mcp_tool_call_duration_seconds{tool, client, app}   (a histogram)

Two names say which client called:

- `client` is the OAuth client the caller's token was issued to (azp or client_id): which
  registered application, e.g. care-desk or claude-ai. Never the person (`sub`); who did what to
  which records is the audit trail's job (audit.py), which is kept and read under stricter rules.
- `app` is the calling software as it names itself: the clientInfo every request carries in the
  stateless protocol (`_meta["io.modelcontextprotocol/clientInfo"]`, 2026-07-28 on), or that an
  older client's `initialize` sends; failing both, the first product in its User-Agent
  (claude-code/2.1.4 -> claude-code). Its version goes in the log line, not the metrics.

Each `server/discover` or `initialize` is also logged on `mcp.clients` with the client's name,
version and protocol version: which clients, at which versions, are connecting.

Label values are bounded: at most 100 OAuth clients and 50 apps are named; later ones count as
"other", so a client registering itself afresh for every user can't grow the metrics without end.
Request bodies are read only for the client's name and version; arguments and results are never
recorded here.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

calls_log = logging.getLogger("mcp.calls")
clients_log = logging.getLogger("mcp.clients")
MAX_CLIENTS = 100
MAX_APPS = 50
MAX_BODY = 64 * 1024  # read ahead at most this much of a request for the client's name
CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
PROTOCOL = "io.modelcontextprotocol/protocolVersion"
_SAFE = re.compile(r"[^a-z0-9._-]+")

registry = CollectorRegistry()
CALLS = Counter(
    "mcp_tool_calls",
    "MCP tool calls, by tool, client and outcome",
    ["tool", "client", "app", "outcome"],
    registry=registry,
)
DURATION = Histogram(
    "mcp_tool_call_duration_seconds",
    "How long MCP tool calls take",
    ["tool", "client", "app"],
    registry=registry,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

_app: ContextVar[str] = ContextVar("mcp_client_app", default="unknown")
_version: ContextVar[str] = ContextVar("mcp_client_version", default="")


class _Bounded:
    """The first `limit` names seen keep their name; after that, "other"."""

    def __init__(self, limit: int) -> None:
        self.limit, self.seen, self.lock = limit, set[str](), threading.Lock()

    def __call__(self, name: str) -> str:
        with self.lock:
            if name in self.seen:
                return name
            if len(self.seen) < self.limit:
                self.seen.add(name)
                return name
        return "other"

    def reset(self) -> None:
        with self.lock:
            self.seen.clear()


_clients, _apps = _Bounded(MAX_CLIENTS), _Bounded(MAX_APPS)


def clean(name: str, limit: int = 40) -> str:
    return _SAFE.sub("-", (name or "").strip().lower()).strip("-.")[:limit] or "unknown"


def app_from_user_agent(user_agent: str) -> str:
    """The calling software: the first product in its User-Agent (claude-code/2.1.4 -> claude-code)."""
    first = (user_agent or "").strip().split(" ", 1)[0]
    return clean(first.split("/", 1)[0])


def client_info(body: bytes) -> tuple[str, str, str, str]:
    """(method, client name, client version, protocol version) from a JSON-RPC request body; the
    arguments are never looked at. Empty strings when the body doesn't say."""
    try:
        msg = json.loads(body)
    except ValueError:
        return "", "", "", ""
    if not isinstance(msg, dict):
        return "", "", "", ""
    raw = msg.get("params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    raw_meta = params.get("_meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_info = meta.get(CLIENT_INFO) or params.get("clientInfo")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    protocol = meta.get(PROTOCOL) or params.get("protocolVersion") or ""
    return (
        str(msg.get("method") or ""),
        str(info.get("name") or ""),
        str(info.get("version") or ""),
        str(protocol),
    )


def record_call(tool: str, client_id: str, outcome: str, seconds: float) -> None:
    """One tool call: the log line, the span's attributes, the metrics. Never raises."""
    try:
        client = _clients(clean(client_id, 80)) if client_id else "none"
        app = _apps(_app.get())
        CALLS.labels(tool, client, app, outcome).inc()
        DURATION.labels(tool, client, app).observe(seconds)
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attributes(
                {
                    "gen_ai.tool.name": tool,
                    "mcp.method.name": "tools/call",
                    "mcp.client.id": client,
                    "mcp.client.app": app,
                }
            )
        calls_log.info(
            "tools/call %s",
            tool,
            extra={
                "fields": {
                    "mcp.tool": tool,
                    "mcp.client.id": client,
                    "mcp.client.app": app,
                    "mcp.client.version": _version.get(),
                    "event.outcome": outcome,
                    "event.duration_ms": round(seconds * 1000, 1),
                }
            },
        )
    except Exception:
        logging.getLogger(__name__).debug("usage not recorded", exc_info=True)


def metrics_body() -> tuple[bytes, str]:
    return generate_latest(registry), CONTENT_TYPE_LATEST


def _connected(method: str, name: str, version: str, protocol: str) -> None:
    clients_log.info(
        "%s %s",
        method,
        clean(name),
        extra={
            "fields": {
                "mcp.client.name": clean(name, 80),
                "mcp.client.version": clean(version, 40),
                "mcp.protocol.version": clean(protocol, 20),
                "mcp.client.app": _app.get(),
            }
        },
    )


class UsageMiddleware:
    """Notes which app is calling (clientInfo, else User-Agent) for the request, and logs clients
    connecting (server/discover, initialize)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        token, vtoken = _app.set(app_from_user_agent(headers.get("user-agent", ""))), _version.set("")
        try:
            if scope.get("method") != "POST" or int(headers.get("content-length") or 0) > MAX_BODY:
                await self.app(scope, receive, send)
                return
            # Read ahead at most MAX_BODY (a chunked body has no length); anything beyond still
            # flows to the app after the chunks read here.
            body, more, chunks = b"", True, []
            while more and len(body) <= MAX_BODY:
                message = await receive()
                chunks.append(message)
                body += message.get("body", b"")
                more = bool(message.get("more_body", False)) and message.get("type") == "http.request"
            if not more:
                method, name, version, protocol = client_info(body)
                if name:
                    _app.set(clean(name))
                    _version.set(clean(version, 40))
                if method in ("server/discover", "initialize"):
                    _connected(method, name, version, protocol)
            replay = iter(chunks)

            async def receive_again() -> Any:
                try:
                    return next(replay)
                except StopIteration:
                    return await receive()

            await self.app(scope, receive_again, send)
        finally:
            _app.reset(token)
            _version.reset(vtoken)


def reset() -> None:
    """Tests: forget the names seen."""
    _clients.reset()
    _apps.reset()
