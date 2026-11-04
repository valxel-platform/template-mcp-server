from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
import uvicorn
from mcp.server.auth.provider import AccessToken

from mcp_server.app import create_app
from mcp_server.audit import Auditor, MemorySink
from mcp_server.settings import Settings
from mcp_server.store import Store

RESOURCE = "http://127.0.0.1/mcp"
TOKENS = {
    "reader": ("nurse-7", ["care:read"]),
    "writer": ("intake-agent", ["care:read", "care:write"]),
    "writer-2": ("other-agent", ["care:read", "care:write"]),
    "no-scopes": ("svc", []),
}


class FakeVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token not in TOKENS:
            return None
        sub, scopes = TOKENS[token]
        return AccessToken(token=token, client_id=sub, scopes=scopes, subject=sub, resource=RESOURCE)


class Live:
    def __init__(self, url: str, store: Store, audit: MemorySink) -> None:
        self.url, self.store, self.audit = url, store, audit

    def call(self, token: str | None, tool: str, args: dict[str, Any]) -> Any:
        return anyio.run(self._call, token, tool, args)

    def tools(self, token: str) -> Any:
        async def go(c: Any) -> Any:
            return (await c.list_tools()).tools

        return anyio.run(self._session, token, go)

    async def _call(self, token: str | None, tool: str, args: dict[str, Any]) -> Any:
        async def go(c: Any) -> Any:
            return await c.call_tool(tool, args)

        return await self._session(token, go)

    async def _session(self, token: str | None, fn: Any) -> Any:
        from mcp.client.client import Client
        from mcp.client.streamable_http import (  # type: ignore[attr-defined]
            create_mcp_http_client,
            streamable_http_client,
        )

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with Client(
            streamable_http_client(self.url, http_client=create_mcp_http_client(headers=headers))
        ) as c:
            return await fn(c)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live() -> Iterator[Live]:
    port = free_port()
    store = Store.seeded()
    settings = Settings(
        resource_url=RESOURCE, oidc_issuer="https://idp.test", rate_burst=5, rate_per_minute=60
    )
    audit = MemorySink()
    auditor = Auditor(audit, service="care", product="emr", environment="test", data_class="phi")
    app = create_app(settings, verifier=FakeVerifier(), store=store, auditor=auditor)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield Live(f"http://127.0.0.1:{port}/mcp", store, audit)
    server.should_exit = True
    thread.join(5)
